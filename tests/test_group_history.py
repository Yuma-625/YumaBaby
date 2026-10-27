import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.agent.group_history import format_history, resolve_window
from app.memory.store import MemoryStore


class GroupHistoryWindowTest(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Asia/Shanghai")
        self.now = datetime(2026, 10, 27, 12, 50, tzinfo=self.tz)

    def test_last_night(self):
        start, end, label = resolve_window("昨晚发生什么了", self.now)
        self.assertEqual(label, "昨晚")
        self.assertEqual(start.date().isoformat(), "2026-10-26")
        self.assertEqual(start.hour, 18)
        self.assertEqual(end.date().isoformat(), "2026-10-27")
        self.assertEqual(end.hour, 6)

    def test_early_morning_explicit_range(self):
        start, end, label = resolve_window("27号0点到2点左右", self.now)
        self.assertEqual(start.date().isoformat(), "2026-10-27")
        self.assertEqual(start.hour, 0)
        self.assertEqual(end.date().isoformat(), "2026-10-27")
        self.assertEqual(end.hour, 2)

    def test_midnight_12_means_zero(self):
        start, end, _ = resolve_window("半夜12点到2点聊了啥", self.now)
        self.assertEqual(start.date().isoformat(), "2026-10-27")
        self.assertEqual(start.hour, 0)
        self.assertEqual(end.hour, 2)

    def test_last_night_12_range(self):
        start, end, _ = resolve_window("昨晚12点到2点", self.now)
        self.assertEqual(start.date().isoformat(), "2026-10-27")
        self.assertEqual(start.hour, 0)
        self.assertEqual(end.hour, 2)

    def test_early_morning_hours(self):
        start, end, _ = resolve_window("凌晨1点到3点", self.now)
        self.assertEqual(start.hour, 1)
        self.assertEqual(end.hour, 3)

    def test_day_evening_midnight_crosses_to_next_day(self):
        start, end, _ = resolve_window("26号晚上12点到2点", self.now)
        self.assertEqual(start.date().isoformat(), "2026-10-27")
        self.assertEqual(start.hour, 0)
        self.assertEqual(end.hour, 2)

    def test_yesterday(self):
        start, end, label = resolve_window("昨天的事", self.now)
        self.assertEqual(label, "昨天")
        self.assertEqual(start.date().isoformat(), "2026-10-26")

    def test_morning_and_none(self):
        self.assertEqual(resolve_window("今早聊了啥", self.now)[2], "今天早上")
        self.assertIsNone(resolve_window("今天天气不错", self.now))


class GroupHistoryFormatTest(unittest.TestCase):
    def test_format_and_truncate(self):
        entries = [
            {"created_at": "2026-10-26T23:39:07+08:00", "sender_name": "奶鱼", "content": "今晚打本"},
            {"created_at": "2026-10-26T23:40:00+08:00", "sender_name": "奶坤", "content": "九点"},
        ]
        text = format_history(entries, max_chars=1000)
        self.assertIn("[23:39] 奶鱼: 今晚打本", text)
        self.assertIn("[23:40] 奶坤: 九点", text)
        self.assertLessEqual(len(format_history(entries, max_chars=10)), 10)

    def test_store_between(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "m.db")
            try:
                store.add_group_context(
                    "100", "1", "昨晚的消息", [], "奶鱼",
                    created_at="2026-10-26T23:50:00+08:00",
                )
                store.add_group_context(
                    "100", "1", "今天中午的消息", [], "奶鱼",
                    created_at="2026-10-27T12:00:00+08:00",
                )
                rows = store.group_context_between(
                    "100", "2026-10-26T18:00:00+08:00", "2026-10-27T06:00:00+08:00"
                )
                self.assertEqual([r["content"] for r in rows], ["昨晚的消息"])
            finally:
                store.close()

    def test_analysis_group_keeps_more_than_max(self):
        from app.config import settings
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "m.db")
            try:
                with patch.object(settings, "GROUP_ANALYSIS_GROUPS", ["100"]), \
                     patch.object(settings, "GROUP_CONTEXT_MAX", 2), \
                     patch.object(settings, "GROUP_CONTEXT_KEEP_DAYS", 3):
                    for i in range(6):
                        store.add_group_context("100", "1", f"m{i}")
                count = store._conn.execute(
                    "SELECT COUNT(*) FROM group_context WHERE group_id='100'"
                ).fetchone()[0]
                self.assertEqual(count, 6)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
