import asyncio
import tempfile
import unittest
from pathlib import Path

from app.agent.loop import AgentLoop
from app.agent.service import AgentService
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
        return "你是鱼宝宝，回答简洁。"

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
