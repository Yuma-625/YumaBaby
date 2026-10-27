import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.group_analysis import build_report, collect_stats, format_stats
from app.memory.store import MemoryStore


class GroupAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")
        self.entries = [
            {"sender_name": "奶鱼", "user_id": "1", "content": "今晚打本吗", "attachments": [], "created_at": "2026-10-27T20:10:00+08:00"},
            {"sender_name": "奶坤", "user_id": "2", "content": "ez", "attachments": [], "created_at": "2026-10-27T20:11:00+08:00"},
            {"sender_name": "奶鱼", "user_id": "1", "content": "[表情:奶龙]", "attachments": [{"type": "mface"}], "created_at": "2026-10-27T20:12:00+08:00"},
            {"sender_name": "奶坤", "user_id": "2", "content": "ez", "attachments": [], "created_at": "2026-10-27T20:13:00+08:00"},
            {"sender_name": "奶坤", "user_id": "2", "content": "ez", "attachments": [], "created_at": "2026-10-27T21:01:00+08:00"},
        ]
        for entry in self.entries:
            self.store.add_group_context(
                "100", entry["user_id"], entry["content"],
                entry["attachments"], entry["sender_name"],
                created_at=entry["created_at"],
            )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_collect_stats(self):
        stats = collect_stats(self.entries)
        self.assertEqual(stats["total"], 5)
        self.assertEqual(len(stats["users"]), 2)
        self.assertEqual(stats["media_total"], 1)
        self.assertEqual(stats["users"]["奶坤"]["count"], 3)
        self.assertIn(("ez", 3), stats["phrases"])
        self.assertEqual(stats["hourly"][0][0], 20)

    def test_format_stats(self):
        text = format_stats("100", "2026-10-27", collect_stats(self.entries))
        self.assertIn("消息 5 条", text)
        self.assertIn("奶坤 3 条", text)
        self.assertIn("ez×3", text)

    def test_build_report_saves_and_observes(self):
        async def fake_chat(*args, **kwargs):
            return "今天主要在聊打本，奶坤习惯用短句。"

        with patch("app.group_analysis.llm_chat", fake_chat):
            report = asyncio_run(build_report(self.store, "100", "2026-10-27"))

        self.assertIn("消息 5 条", report)
        self.assertIn("观察", report)
        self.assertIn("奶坤习惯用短句", report)
        self.assertEqual(
            self.store.get_group_report("100", "2026-10-27"), report
        )

    def test_build_report_skips_quiet_day(self):
        report = asyncio_run(build_report(self.store, "999", "2026-10-27"))
        self.assertIsNone(report)

    def test_report_overwrites_same_day(self):
        self.store.save_group_report("100", "2026-10-27", "旧报告")
        self.store.save_group_report("100", "2026-10-27", "新报告")
        self.assertEqual(self.store.get_group_report("100", "2026-10-27"), "新报告")


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
