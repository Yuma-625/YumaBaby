import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.adapters.qq import QQAdapter
from app.utils.persona import DEFAULT_BASE, PersonaManager


class PersonaManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "persona.json"
        self.pm = PersonaManager(self.file).load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_defaults_when_file_missing(self):
        self.assertEqual(self.pm.base, DEFAULT_BASE)
        self.assertEqual(self.pm.pending, [])

    def test_add_pending_persists_and_dedupes(self):
        self.assertTrue(self.pm.add_pending("称呼用户为老板", "叫我老板"))
        self.assertFalse(self.pm.add_pending("称呼用户为老板", "重复"))
        self.assertEqual(len(self.pm.pending), 1)

        reloaded = PersonaManager(self.file).load()
        self.assertEqual(reloaded.pending[0]["rule"], "称呼用户为老板")

    def test_evolved_rule_enters_system_prompt(self):
        self.pm.pending.append({"rule": "回复要简短", "date": "2026-01-01", "source": "x"})
        self.pm.save()
        self.pm.apply_feedback("全部保留")
        self.assertIn("回复要简短", self.pm.get_system_prompt())

    def test_keep_selected_indices(self):
        for i in range(3):
            self.pm.add_pending(f"规则{i + 1}", "src")
        result = self.pm.apply_feedback("保留 1 3")
        self.assertIn("保留 2 条", result)
        self.assertEqual([p["rule"] for p in self.pm.pending], ["规则2"])
        self.assertEqual(len(self.pm.evolved), 2)

    def test_bare_numbers_mean_keep(self):
        self.pm.add_pending("甲", "src")
        self.pm.add_pending("乙", "src")
        self.pm.apply_feedback("2")
        self.assertEqual([p["rule"] for p in self.pm.pending], ["甲"])

    def test_remove_all(self):
        self.pm.add_pending("甲", "src")
        result = self.pm.apply_feedback("全部删除")
        self.assertIn("1", result)
        self.assertEqual(self.pm.pending, [])

    def test_apply_feedback_without_pending(self):
        self.assertIn("没有待审核", self.pm.apply_feedback("全部保留"))

    def test_unrecognized_feedback_keeps_pending(self):
        self.pm.add_pending("甲", "src")
        self.assertIn("未识别", self.pm.apply_feedback("随便说点啥"))
        self.assertEqual(len(self.pm.pending), 1)

    def test_unrelated_chat_is_not_analyzed(self):
        self.assertFalse(self.pm.should_analyze("今天天气不错"))
        self.assertFalse(self.pm.should_analyze("你好"))
        self.assertTrue(self.pm.should_analyze("以后请叫我老板"))

    def test_normalize_rejects_negative_answers(self):
        self.assertIsNone(self.pm._normalize_rule("无"))
        self.assertIsNone(self.pm._normalize_rule("没有新规则"))
        self.assertIsNone(self.pm._normalize_rule(""))
        self.assertEqual(self.pm._normalize_rule("「回复要简短」"), "回复要简短")

    def test_corrupt_file_falls_back(self):
        self.file.write_text("{not json", encoding="utf-8")
        pm = PersonaManager(self.file).load()
        self.assertEqual(pm.base, DEFAULT_BASE)


class QQAdapterFilterTest(unittest.TestCase):
    def setUp(self):
        self.received = []

        async def on_message(text, context):
            self.received.append(text)
            return ""

        self.adapter = QQAdapter(on_message=on_message)

    def test_duplicate_message_ids_are_dropped(self):
        async def run():
            return (
                self.adapter._is_duplicate(1),
                self.adapter._is_duplicate(1),
                self.adapter._is_duplicate(2),
            )

        self.assertEqual(asyncio.run(run()), (False, True, False))

    def test_self_message_is_ignored(self):
        async def run():
            await self.adapter._handle_event({
                "post_type": "message",
                "message_type": "group",
                "message_id": 10,
                "self_id": 10001,
                "user_id": 10001,
                "group_id": "20002",
                "raw_message": "小鱼你好",
            })

        asyncio.run(run())
        self.assertEqual(self.received, [])
        # 被过滤的消息不应进入去重表
        self.assertNotIn("10", self.adapter._seen)

    def test_sent_event_is_ignored(self):
        async def run():
            await self.adapter._handle_event({
                "post_type": "message_sent",
                "message_type": "group",
                "message_id": 11,
                "group_id": "20002",
                "raw_message": "小鱼你好",
            })

        asyncio.run(run())
        self.assertEqual(self.received, [])
        self.assertNotIn("11", self.adapter._seen)

    def test_api_call_without_connection_fails_fast(self):
        result = asyncio.run(self.adapter.send_group_msg("1", "hi"))
        self.assertEqual(result["status"], "failed")

    def test_auth_header_uses_current_websockets_keyword(self):
        """websockets 16 移除了 extra_headers，必须用 additional_headers。"""
        captured = {}

        class _BrokenConnection:
            async def __aenter__(self):
                raise RuntimeError("stop here")

            async def __aexit__(self, *exc_info):
                return False

        def fake_connect(**kwargs):
            captured.update(kwargs)
            return _BrokenConnection()

        self.adapter.access_token = "secret"
        with patch("app.adapters.qq.websockets.connect", fake_connect):
            with self.assertRaises(RuntimeError):
                asyncio.run(self.adapter._connect_and_consume())

        self.assertEqual(captured["additional_headers"], {"Authorization": "Bearer secret"})
        self.assertNotIn("extra_headers", captured)


if __name__ == "__main__":
    unittest.main()
