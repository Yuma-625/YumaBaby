import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent.admin import (
    extract_reply_id,
    format_daily_summary,
    handle_admin_message,
    is_llm_check,
    parse_rating,
)
from app.config import settings
from app.memory.store import MemoryStore


class ActivityStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_platform_message_id_roundtrip(self):
        self.store.save_message("user:1", "assistant", "回复一")
        row_id = self.store.attach_platform_message_id("user:1", "555")
        self.assertIsNotNone(row_id)

        found = self.store.find_message_by_platform_id("555")
        self.assertEqual(found["content"], "回复一")
        self.assertEqual(found["role"], "assistant")

    def test_last_assistant_message(self):
        self.store.save_message("user:1", "user", "问题")
        self.store.save_message("user:1", "assistant", "答案")
        self.store.save_message("user:1", "user", "再问")
        self.assertEqual(self.store.last_assistant_message("user:1")["content"], "答案")

    def test_feedback_and_summary(self):
        self.store.save_message("user:1", "user", "问题")
        self.store.save_message("user:1", "assistant", "答案")
        target = self.store.last_assistant_message("user:1")
        self.store.add_feedback(target["id"], "user:1", "positive", reviewer="1293585554")
        self.store.add_event("tool", "get_time", "user:1", {"ok": True})
        self.store.add_event("reply", "done", "user:1", {"steps": 2})

        summary = self.store.daily_summary()
        self.assertEqual(summary["messages"]["received"], 1)
        self.assertEqual(summary["messages"]["sent"], 1)
        self.assertEqual(summary["feedback"], {"positive": 1, "negative": 0})
        self.assertEqual(summary["tools"], {"get_time": 1})
        self.assertEqual(summary["events"]["reply"], 1)

    def test_summary_empty_day(self):
        summary = self.store.daily_summary("2020-01-01")
        self.assertEqual(summary["messages"]["received"], 0)
        self.assertEqual(summary["events"], {})


class AdminCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")
        self.store.save_message("user:1", "user", "问题")
        self.store.save_message("user:1", "assistant", "这是第一条回复")
        self.store.save_message("user:1", "assistant", "这是第二条回复")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_parse_rating(self):
        self.assertEqual(parse_rating("好"), "positive")
        self.assertEqual(parse_rating(" 好评！"), "positive")
        self.assertEqual(parse_rating("差评。"), "negative")
        self.assertIsNone(parse_rating("今天天气不错"))  # 不是精确匹配

    def test_rate_last_assistant(self):
        reply = handle_admin_message("好", chat_id="user:1", store=self.store)
        self.assertIn("已记录好评", reply)
        row = self.store._conn.execute(
            "SELECT message_id, rating FROM reply_feedback ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["rating"], "positive")
        self.assertEqual(row["message_id"], self.store.last_assistant_message("user:1")["id"])

    def test_rate_specific_message_by_reply_id(self):
        msg_id = self.store.save_message("user:9", "assistant", "被引用的回复")
        self.store.attach_platform_message_id("user:9", "777")

        reply = handle_admin_message(
            "差", chat_id="user:9", store=self.store, reply_to_id="777"
        )
        self.assertIn("已记录差评", reply)
        row = self.store._conn.execute(
            "SELECT message_id FROM reply_feedback ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["message_id"], msg_id)

    def test_no_target(self):
        self.store.clear_messages("user:2")
        self.assertIn("还没有", handle_admin_message("好", chat_id="user:2", store=self.store))

    def test_daily_summary_command(self):
        text = handle_admin_message("今天干了什么", chat_id="user:1", store=self.store)
        self.assertIn("对话", text)
        self.assertIn("评价", text)

    def test_llm_check_command(self):
        from app.utils import usage

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(settings, "USAGE_LOG", str(Path(tmp) / "u.jsonl")):
            usage.reset_state()
            usage.record_usage("m", 1000, 100)
            usage.record_context("user:1", 888)
            text = handle_admin_message("llm check", chat_id="user:1", store=self.store)
        usage.reset_state()

        self.assertIn("LLM 用量", text)
        self.assertIn("今日", text)
        self.assertIn("累计", text)
        self.assertIn("888", text)
        self.assertIn("预算", text)

    def test_llm_check_aliases(self):
        self.assertTrue(is_llm_check("llm check"))
        self.assertTrue(is_llm_check("LLMCheck"))
        self.assertTrue(is_llm_check("llm 用量"))
        self.assertFalse(is_llm_check("帮我查天气"))

    def test_normal_message_passes_through(self):
        self.assertIsNone(handle_admin_message("帮我查天气", chat_id="user:1", store=self.store))

    def test_extract_reply_id(self):
        self.assertEqual(extract_reply_id("[CQ:reply,id=123] 好"), "123")
        self.assertIsNone(extract_reply_id("好"))

    def test_format_summary_empty(self):
        self.assertIn("还没有", format_daily_summary(self.store, "2020-01-01"))


if __name__ == "__main__":
    unittest.main()
