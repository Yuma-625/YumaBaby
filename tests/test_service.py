import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.agent.loop import AgentLoop
from app.agent.service import AgentService, normalize_reply
from app.config import settings
from app.memory.store import MemoryStore
from app.tools.builtin import build_registry
from app.utils.llm import LLMResponse, ToolCall


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages_seen = []
        self.tools_seen = []

    async def complete(self, messages, **kwargs):
        self.messages_seen.append([dict(m) for m in messages])
        self.tools_seen.append([
            item["function"]["name"] for item in (kwargs.get("tools") or [])
        ])
        return self.responses.pop(0) if self.responses else None


class StubPersona:
    def __init__(self, learn=False):
        self.learn = learn
        self.analyzed = []

    def get_system_prompt(self):
        return "你是小弗，回答简洁。"

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

    def test_group_analysis_includes_all_senders(self):
        self.store.add_group_context("G1", "uA", "A 说今晚打本", [], "用户A")
        self.store.add_group_context("G1", "uB", "B 说不想打", [], "用户B")
        service, llm = self.make_service([answer("大家在聊打本")])
        asyncio.run(service.handle(
            "分析一下刚刚群里聊了啥", chat_id="group:G1", scope_type="group",
            scope_id="G1", user_id="uB", sender_name="用户B",
        ))
        system = llm.messages_seen[0][0]["content"]
        self.assertIn("用户A", system)
        self.assertIn("用户B", system)
        self.assertIn("不是只分析当前用户", system)
        self.assertIn("按发送者归纳", system)

    def test_group_analysis_warns_when_only_current_user(self):
        self.store.add_group_context("G1", "uB", "只有我说的", [], "用户B")
        service, llm = self.make_service([answer("只有你")])
        asyncio.run(service.handle(
            "分析一下刚刚群里聊了啥", chat_id="group:G1", scope_type="group",
            scope_id="G1", user_id="uB", sender_name="用户B",
        ))
        system = llm.messages_seen[0][0]["content"]
        self.assertIn("只收到当前用户", system)

    def test_name_reference_resolves_group_member_without_mention(self):
        self.store.upsert_group_member("G1", "u2", "奶坤")
        service, llm = self.make_service([answer("收到")])
        asyncio.run(service.handle(
            "跟奶坤打个招呼", chat_id="group:G1", scope_type="group",
            scope_id="G1", user_id="u1", sender_name="奶鱼",
        ))
        system = llm.messages_seen[0][0]["content"]
        self.assertIn("奶坤", system)
        self.assertIn("不要 @", system)
        self.assertNotIn("[[at:u2]]", system)

    def test_name_reference_can_mention_when_enabled(self):
        self.store.upsert_group_member("G1", "u2", "奶坤")
        service, llm = self.make_service([answer("收到")])
        with patch.object(settings, "GROUP_NAME_MENTION", True):
            asyncio.run(service.handle(
                "跟奶坤打个招呼", chat_id="group:G1", scope_type="group",
                scope_id="G1", user_id="u1", sender_name="奶鱼",
            ))
        system = llm.messages_seen[0][0]["content"]
        self.assertIn("[[at:u2]]", system)

    def test_name_reference_ambiguous_asks(self):
        self.store.upsert_group_member("G1", "u2", "奶坤")
        self.store.upsert_group_member("G1", "u3", "奶坤")
        service, llm = self.make_service([answer("哪一个？")])
        asyncio.run(service.handle(
            "跟奶坤打个招呼", chat_id="group:G1", scope_type="group",
            scope_id="G1", user_id="u1", sender_name="奶鱼",
        ))
        system = llm.messages_seen[0][0]["content"]
        self.assertIn("多个同名", system)

    def test_actual_at_skips_name_lookup(self):
        self.store.upsert_group_member("G1", "u2", "奶坤")
        service, llm = self.make_service([answer("收到")])
        asyncio.run(service.handle(
            "跟奶坤打个招呼", chat_id="group:G1", scope_type="group",
            scope_id="G1", user_id="u1", sender_name="奶鱼",
            at_targets=[{"qq": "u3", "name": "阿三"}],
        ))
        system = llm.messages_seen[0][0]["content"]
        self.assertIn("阿三", system)
        self.assertNotIn("[[at:u2]]", system)

    def test_group_context_keeps_speakers_apart(self):
        self.store.add_group_context(
            "G1", "uA", "A 先说的话", [], "用户A",
        )
        self.store.add_group_context(
            "G1", "uB", "B 上一句", [], "用户B",
        )
        service, llm = self.make_service([answer("收到")])
        asyncio.run(service.handle(
            "现在 @ 你", chat_id="group:G1", scope_type="group",
            scope_id="G1", user_id="uB", sender_name="用户B",
        ))
        system = llm.messages_seen[0][0]["content"]
        self.assertIn("用户A: A 先说的话", system)
        self.assertIn("用户B: B 上一句", system)
        self.assertIn("当前发言人是 用户B（uB）", system)
        self.assertIn("B 上一句", system)

    def test_current_message_does_not_hide_same_substring_context(self):
        self.store.add_group_context(
            "G1", "uB", "在吗", [], "用户B",
        )
        service, llm = self.make_service([answer("在")])
        asyncio.run(service.handle(
            "在吗 @小弗", chat_id="group:G1", scope_type="group",
            scope_id="G1", user_id="uB", sender_name="用户B",
        ))
        system = llm.messages_seen[0][0]["content"]
        self.assertIn("- 用户B: 在吗", system)

    def test_compact_summary_is_in_prompt(self):
        self.store.upsert_chat_summary("user:1", "用户之前说喜欢猫", 0)
        service, llm = self.make_service([answer("好")])
        with patch.object(settings, "HISTORY_COMPACT_ENABLED", True):
            asyncio.run(service.handle(
                "在吗", chat_id="user:1", scope_type="user", scope_id="1",
            ))
        system = llm.messages_seen[0][0]["content"]
        self.assertIn("此前对话摘要", system)
        self.assertIn("喜欢猫", system)

    def test_sandbox_tools_only_admin_private(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(settings, "SANDBOX_ENABLED", True), \
             patch.object(settings, "SANDBOX_ROOT", str(Path(tmp) / "sandbox")), \
             patch.object(settings, "QQ_PERSONA_ADMIN", "1293585554"):
            registry = build_registry(self.store)
            service, llm = self.make_service([answer("好")])
            service.loop.tools = registry
            asyncio.run(service.handle(
                "看看沙箱", chat_id="user:1293585554",
                scope_type="user", scope_id="1293585554",
            ))
            self.assertIn("fs_read", llm.tools_seen[0])

            service2, llm2 = self.make_service([answer("好")])
            service2.loop.tools = registry
            asyncio.run(service2.handle(
                "看看沙箱", chat_id="group:1", scope_type="group", scope_id="1",
                user_id="普通用户",
            ))
            self.assertFalse(any(name.startswith("fs_") for name in llm2.tools_seen[0]))
            self.assertNotIn("send_message", llm2.tools_seen[0])

    def test_admin_group_can_use_sandbox_and_send_tool(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(settings, "SANDBOX_ENABLED", True), \
             patch.object(settings, "SANDBOX_ROOT", str(Path(tmp) / "sandbox")), \
             patch.object(settings, "QQ_PERSONA_ADMIN", "ADMIN"):
            registry = build_registry(self.store)
            service, llm = self.make_service([answer("好")])
            service.loop.tools = registry
            asyncio.run(service.handle(
                "看看沙箱", chat_id="group:G1", scope_type="group", scope_id="G1",
                user_id="ADMIN",
            ))
            self.assertIn("fs_read", llm.tools_seen[0])
            self.assertIn("send_message", llm.tools_seen[0])

    def test_group_history_window_is_injected(self):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        last_night = (now - timedelta(days=1)).replace(
            hour=23, minute=0, second=0, microsecond=0
        )
        self.store.add_group_context(
            "1", "7", "昨晚打本到十二点", [], "奶鱼",
            created_at=last_night.isoformat(timespec="seconds"),
        )
        service, llm = self.make_service([answer("昨晚你们打本到十二点")])
        asyncio.run(service.handle(
            "昨晚发生什么了", chat_id="group:1", scope_type="group", scope_id="1",
        ))
        messages = llm.messages_seen[0]
        self.assertTrue(any(m["role"] == "system" and "昨晚的群聊记录" in m["content"]
                            for m in messages))
        self.assertIn("昨晚打本到十二点", messages[-2]["content"])

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

    def test_group_context_is_injected(self):
        self.store.add_group_context("1", "7", "[表情:奶龙]", [{"type": "bface", "url": "u"}], "奶鱼")
        service, llm = self.make_service([answer("奶龙是吧")])
        asyncio.run(service.handle(
            "用这个", chat_id="group:1", scope_type="group", scope_id="1",
        ))
        system = llm.messages_seen[0][0]["content"]
        self.assertIn("[表情:奶龙]", system)
        self.assertIn("奶鱼", system)

    def test_current_message_is_not_duplicated_in_context(self):
        self.store.add_group_context("1", "7", "用这个", [], "奶鱼")
        service, llm = self.make_service([answer("好")])
        asyncio.run(service.handle(
            "用这个", chat_id="group:1", scope_type="group", scope_id="1",
        ))
        system = llm.messages_seen[0][0]["content"]
        self.assertEqual(system.count("用这个"), 0)

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
        reply = asyncio.run(service.handle(
            "你能干些啥", chat_id="user:1", scope_type="user", scope_id="1",
        ))
        self.assertIn("功能列表", reply)
        self.assertEqual(llm.messages_seen, [])

    def test_image_is_sent_as_multimodal_content(self):
        service, llm = self.make_service([answer("红色的")])
        asyncio.run(service.handle(
            "这是什么颜色", chat_id="user:1", scope_type="user", scope_id="1",
            attachments=[{"type": "image", "url": "data:image/png;base64,AAAA"}],
        ))
        user_message = next(m for m in llm.messages_seen[0] if m["role"] == "user")
        self.assertIsInstance(user_message["content"], list)
        self.assertEqual(user_message["content"][0]["type"], "text")
        self.assertEqual(user_message["content"][1]["type"], "image_url")

    def test_attachments_are_saved(self):
        service, _ = self.make_service([answer("看到了")])
        asyncio.run(service.handle(
            "看图", chat_id="user:1", scope_type="user", scope_id="1",
            attachments=[{"type": "image", "url": "http://x/1.jpg"}],
        ))
        row = self.store._conn.execute(
            "SELECT attachments FROM messages WHERE role='user' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIn("http://x/1.jpg", row["attachments"])

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
        self.assertEqual(normalize_reply("<@ABC123> 你好"), "你好")
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
