import asyncio
import tempfile
import unittest
from pathlib import Path

from app.agent.loop import AgentLoop
from app.agent.service import AgentService, normalize_reply
from app.memory.store import MemoryStore
from app.tools.builtin import build_registry
from app.utils.llm import LLMResponse, ToolCall


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages_seen = []

    async def complete(self, messages, **kwargs):
        self.messages_seen.append([dict(m) for m in messages])
        return self.responses.pop(0) if self.responses else None


class StubPersona:
    def __init__(self, learn=False):
        self.learn = learn
        self.analyzed = []

    def get_system_prompt(self):
        return "你是小鱼，回答简洁。"

    def should_analyze(self, text):
        return self.learn

    async def analyze_for_rules(self, user_text, reply):
        self.analyzed.append((user_text, reply))


def tool_call(name, args, cid="c1"):
    return LLMResponse(content=None, tool_calls=[ToolCall(id=cid, name=name, arguments=args)])


def answer(text):
    return LLMResponse(content=text)


class AgentServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")
        self.registry = build_registry(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def make_service(self, responses, persona=None):
        llm = ScriptedLLM(responses)
        loop = AgentLoop(self.registry, llm=llm, max_steps=4)
        service = AgentService(loop, self.store, persona=persona or StubPersona())
        return service, llm

    def test_remember_flow(self):
        service, _ = self.make_service([
            tool_call("remember", '{"text": "用户喜欢猫"}'),
            answer("记住了"),
        ])
        reply = asyncio.run(service.handle(
            "记住我喜欢猫", chat_id="user:10001", scope_type="user", scope_id="10001",
        ))
        self.assertEqual(reply, "记住了")
        memories = self.store.recall("user", "10001", "猫")
        self.assertEqual(memories[0]["text"], "用户喜欢猫")
        # 对话落库
        history = self.store.recent_messages("user:10001")
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])

    def test_recall_is_injected_into_system_prompt(self):
        self.store.remember("user", "10001", "用户喜欢猫")
        service, llm = self.make_service([
            tool_call("recall", '{"query": "喜欢"}'),
            answer("你喜欢猫"),
        ])
        asyncio.run(service.handle(
            "我喜欢什么", chat_id="user:10001", scope_type="user", scope_id="10001",
        ))
        system = llm.messages_seen[0][0]
        self.assertEqual(system["role"], "system")
        self.assertIn("用户喜欢猫", system["content"])

    def test_history_is_reused(self):
        service, llm = self.make_service([
            answer("第一句"),
            answer("第二句"),
        ])
        asyncio.run(service.handle("一", chat_id="user:1", scope_type="user", scope_id="1"))
        asyncio.run(service.handle("二", chat_id="user:1", scope_type="user", scope_id="1"))
        second_messages = llm.messages_seen[1]
        contents = [m["content"] for m in second_messages if m["role"] != "system"]
        self.assertIn("一", contents)
        self.assertIn("第一句", contents)

    def test_memory_is_isolated_between_scopes(self):
        self.store.remember("user", "A", "A 的偏好")
        service, _ = self.make_service([tool_call("recall", "{}"), answer("不知道")])
        asyncio.run(service.handle("说说偏好", chat_id="user:B", scope_type="user", scope_id="B"))
        system = service.loop.llm.messages_seen[0][0]["content"]
        self.assertNotIn("A 的偏好", system)

    def test_help_does_not_call_llm(self):
        service, llm = self.make_service([])
        reply = asyncio.run(service.handle(
            "帮助", chat_id="user:1", scope_type="user", scope_id="1",
        ))
        self.assertIn("功能列表", reply)
        self.assertEqual(llm.messages_seen, [])

    def test_at_targets_are_added_to_prompt(self):
        service, llm = self.make_service([answer("打个招呼")])
        asyncio.run(service.handle(
            "和@奶鱼 打个招呼", chat_id="group:1", scope_type="group", scope_id="1",
            at_targets=[{"qq": "20002", "name": "奶鱼"}],
        ))
        system = llm.messages_seen[0][0]["content"]
        self.assertIn("[[at:20002]]", system)
        self.assertIn("奶鱼", system)

    def test_auto_participation_can_skip(self):
        service, _ = self.make_service([answer("[SKIP]")])
        reply = asyncio.run(service.handle(
            "群里随便聊聊", chat_id="group:1", scope_type="group", scope_id="1",
            auto_participate=True,
        ))
        self.assertEqual(reply, "")
        self.assertEqual(self.store.recent_messages("group:1"), [])

    def test_auto_participation_reply_is_saved(self):
        service, _ = self.make_service([answer("这句我接")])
        reply = asyncio.run(service.handle(
            "群里随便聊聊", chat_id="group:1", scope_type="group", scope_id="1",
            auto_participate=True,
        ))
        self.assertEqual(reply, "这句我接")
        self.assertEqual(len(self.store.recent_messages("group:1")), 2)

    def test_normalize_reply(self):
        self.assertEqual(normalize_reply("第一句。\n\n第二句。"), "第一句。 第二句。")
        self.assertEqual(normalize_reply("  a \n\t b  "), "a b")
        self.assertEqual(normalize_reply(None), "")

    def test_multiline_reply_is_flattened(self):
        service, _ = self.make_service([answer("第一句。\n\n第二句。\n\n第三句。")])
        reply = asyncio.run(service.handle(
            "聊聊", chat_id="user:1", scope_type="user", scope_id="1",
        ))
        self.assertNotIn("\n", reply)
        self.assertEqual(reply, "第一句。 第二句。 第三句。")

    def test_events_are_recorded(self):
        service, _ = self.make_service([
            tool_call("remember", '{"text": "用户喜欢猫"}'),
            answer("记住了"),
        ])
        asyncio.run(service.handle(
            "记住我喜欢猫", chat_id="user:10001", scope_type="user", scope_id="10001",
        ))
        summary = self.store.daily_summary()
        self.assertEqual(summary["events"]["reply"], 1)
        self.assertEqual(summary["tools"], {"remember": 1})

    def test_learning_only_when_gated(self):
        persona = StubPersona(learn=True)
        service, _ = self.make_service([answer("好的")], persona=persona)

        async def run():
            await service.handle("以后叫我老板", chat_id="user:1", scope_type="user", scope_id="1")
            await asyncio.sleep(0)

        asyncio.run(run())
        self.assertEqual(len(persona.analyzed), 1)


if __name__ == "__main__":
    unittest.main()
