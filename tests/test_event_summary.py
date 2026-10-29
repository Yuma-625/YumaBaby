import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent.event_summary import maybe_refresh_group_event
from app.config import settings
from app.memory.store import MemoryStore
from app.utils.llm import LLMResponse


class EventSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_refreshes_after_threshold(self):
        for i in range(10):
            self.store.add_group_context(
                "G1", f"u{i}", f"消息 {i}", [], f"用户{i}",
            )

        async def fake_complete(*args, **kwargs):
            return LLMResponse(
                content=json.dumps({
                    "topic": "打本",
                    "summary": "大家在约今晚打本",
                    "participants": ["用户1"],
                    "ongoing": True,
                })
            )

        with patch("app.agent.event_summary.complete", fake_complete):
            asyncio.run(maybe_refresh_group_event(self.store, "G1"))

        event = self.store.get_group_event("G1")
        self.assertIsNotNone(event)
        self.assertIn("打本", event["summary"])
        self.assertGreater(event["up_to_id"], 0)

    def test_does_not_refresh_below_threshold(self):
        for i in range(3):
            self.store.add_group_context("G1", "u", f"消息 {i}", [], "用户")
        asyncio.run(maybe_refresh_group_event(self.store, "G1"))
        self.assertIsNone(self.store.get_group_event("G1"))


if __name__ == "__main__":
    unittest.main()
