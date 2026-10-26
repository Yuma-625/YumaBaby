import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.memory.store import MemoryStore
from app.memory.summaries import refresh_topic_summary


class TopicSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")
        self._patch = patch.object(settings, "MEMORY_TOPIC_SUMMARY_MIN_FACTS", 5)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.store.close()
        self.tmp.cleanup()

    def test_needs_enough_facts(self):
        topic_id = self.store.upsert_topic("user", "1", "结婚")
        for i in range(3):
            self.store.remember("user", "1", f"事实{i}", topic_id=topic_id)
        self.assertIsNone(asyncio.run(refresh_topic_summary(self.store, topic_id)))

    def test_generates_and_caches(self):
        topic_id = self.store.upsert_topic("user", "1", "结婚")
        for i in range(5):
            self.store.remember("user", "1", f"婚礼事实{i}", topic_id=topic_id)

        async def fake_chat(*args, **kwargs):
            return "用户正在准备婚礼。"

        with patch("app.memory.summaries.llm_chat", fake_chat):
            summary = asyncio.run(refresh_topic_summary(self.store, topic_id))
        self.assertEqual(summary, "用户正在准备婚礼。")
        self.assertEqual(self.store.get_topic(topic_id)["summary"], "用户正在准备婚礼。")

        with patch("app.memory.summaries.llm_chat", side_effect=AssertionError("不应重复调用")):
            cached = asyncio.run(refresh_topic_summary(self.store, topic_id))
        self.assertEqual(cached, "用户正在准备婚礼。")


if __name__ == "__main__":
    unittest.main()
