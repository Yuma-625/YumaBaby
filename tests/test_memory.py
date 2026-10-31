import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.memory.store import MemoryStore


class MemoryStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "memory.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_remember_and_recall(self):
        self.store.remember("user", "10001", "用户喜欢猫")
        self.store.remember("user", "10001", "用户住在杭州")
        items = self.store.recall("user", "10001", "猫")
        self.assertEqual(items[0]["text"], "用户喜欢猫")

    def test_recall_falls_back_to_recent(self):
        self.store.remember("user", "10001", "第一条")
        items = self.store.recall("user", "10001", "不存在的词")
        self.assertEqual(len(items), 1)

    def test_duplicate_is_not_inserted_twice(self):
        first = self.store.remember("user", "10001", "同名")
        second = self.store.remember("user", "10001", "同名")
        self.assertEqual(first, second)
        self.assertEqual(self.store.count_memories("user", "10001"), 1)

    def test_scope_isolation(self):
        self.store.remember("user", "10001", "A 的秘密")
        self.store.remember("group", "20002", "群规：不许发广告")
        self.assertEqual(self.store.recall("user", "10002", "秘密"), [])
        self.assertEqual(self.store.recall("group", "20002", "群规")[0]["text"], "群规：不许发广告")

    def test_forget_all_and_keyword(self):
        self.store.remember("user", "1", "喜欢猫")
        self.store.remember("user", "1", "喜欢狗")
        self.assertEqual(self.store.forget("user", "1", "猫"), 1)
        self.assertEqual(self.store.count_memories("user", "1"), 1)
        self.assertEqual(self.store.forget("user", "1"), 1)
        self.assertEqual(self.store.count_memories(), 0)
        deleted = self.store._conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE scope_id='1' AND status='deleted'"
        ).fetchone()["n"]
        self.assertEqual(deleted, 2)

    def test_blank_memory_rejected(self):
        with self.assertRaises(ValueError):
            self.store.remember("user", "1", "   ")

    def test_messages_roundtrip(self):
        for i in range(5):
            self.store.save_message("group:1", "user", f"m{i}")
        history = self.store.recent_messages("group:1", 3)
        self.assertEqual([m["content"] for m in history], ["m2", "m3", "m4"])
        self.assertEqual(self.store.clear_messages("group:1"), 5)
        self.assertEqual(self.store.recent_messages("group:1"), [])

    def test_recall_prefers_higher_weight(self):
        self.store.remember("user", "1", "普通偏好")
        self.store.remember("user", "1", "明确要求记住的偏好", source="tool")
        items = self.store.recall("user", "1", "", limit=2)
        self.assertEqual(items[0]["text"], "明确要求记住的偏好")

    def test_prune_removes_very_old_low_weight(self):
        self.store.remember("user", "1", "陈旧记忆")
        self.store._conn.execute(
            "UPDATE memories SET weight=0.01, created_at='2025-01-01T00:00:00+08:00'"
        )
        self.store._conn.commit()
        removed = self.store.prune_memories(min_effective=0.05, max_age_days=180)
        self.assertEqual(removed, 1)
        self.assertEqual(self.store.count_memories(), 0)

    def test_prune_caps_per_scope(self):
        for i in range(5):
            self.store.remember("user", "1", f"记忆{i}", source="tool")
        removed = self.store.prune_memories(max_per_scope=2)
        self.assertEqual(removed, 3)
        self.assertEqual(self.store.count_memories("user", "1"), 2)

    def test_last_activity_and_event_today(self):
        self.assertIsNone(self.store.last_activity_at())
        self.store.save_message("user:1", "user", "hi")
        self.assertIsNotNone(self.store.last_activity_at())
        self.assertFalse(self.store.has_event_today("persona_push"))
        self.store.add_event("persona_push", "admin")
        self.assertTrue(self.store.has_event_today("persona_push"))

    def test_group_context_roundtrip(self):
        self.store.add_group_context("100", "7", "先把表情发出来", [], "奶鱼")
        self.store.add_group_context("100", "8", "[表情:奶龙]", [{"type": "bface", "url": "u"}], "奶坤")
        items = self.store.recent_group_context("100", limit=5)
        self.assertEqual([i["content"] for i in items], ["先把表情发出来", "[表情:奶龙]"])
        self.assertEqual(items[1]["sender_name"], "奶坤")
        self.assertEqual(items[1]["attachments"][0]["type"], "bface")
        self.assertEqual(items[1]["attachments"][0]["sender_name"], "奶坤")

    def test_group_context_attachment_has_sender(self):
        self.store.add_group_context(
            "100", "u1", "[图片]",
            [{"type": "image", "url": "http://x/1.jpg"}],
            "奶鱼",
        )
        items = self.store.recent_group_context("100", limit=1)
        self.assertEqual(items[0]["attachments"][0]["sender_name"], "奶鱼")
        self.assertEqual(items[0]["attachments"][0]["user_id"], "u1")

    def test_group_context_is_capped(self):
        from unittest.mock import patch
        with patch.object(settings, "GROUP_CONTEXT_MAX", 3):
            for i in range(5):
                self.store.add_group_context("100", "7", f"消息{i}")
        items = self.store.recent_group_context("100", limit=10)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[-1]["content"], "消息4")

    def test_group_context_scope_isolation(self):
        self.store.add_group_context("100", "7", "A 群的话")
        self.store.add_group_context("200", "7", "B 群的话")
        self.assertEqual(len(self.store.recent_group_context("100")), 1)
        self.assertEqual(self.store.clear_group_context("100"), 1)
        self.assertEqual(self.store.recent_group_context("100"), [])

    def test_count_events_today_by_chat(self):
        self.store.add_event("summary", "group", "100")
        self.store.add_event("summary", "group", "100")
        self.store.add_event("summary", "group", "200")
        self.assertEqual(self.store.count_events_today("summary", "100"), 2)
        self.assertEqual(self.store.count_events_today("summary", "200"), 1)
        self.assertEqual(self.store.count_events_today("summary"), 3)

    def test_usage_count_increases(self):
        self.store.remember("user", "1", "重要的事")
        self.store.recall("user", "1", "重要")
        self.store.recall("user", "1", "重要")
        items = self.store.recall("user", "1", "重要")
        self.assertGreaterEqual(items[0]["use_count"], 2)


if __name__ == "__main__":
    unittest.main()
