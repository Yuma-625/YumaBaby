import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.memory.compactor import compact
from app.memory.store import MemoryStore


class CompactorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")
        self._patches = [
            patch.object(settings, "HISTORY_COMPACT_ENABLED", True),
            patch.object(settings, "HISTORY_COMPACT_AFTER", 10),
            patch.object(settings, "HISTORY_COMPACT_KEEP", 4),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.store.close()
        self.tmp.cleanup()

    def _fill(self, count: int, prefix: str = "m"):
        for i in range(count):
            role = "user" if i % 2 == 0 else "assistant"
            self.store.save_message("user:1", role, f"{prefix}{i}")

    def test_compacts_old_messages(self):
        self._fill(20)

        async def fake_chat(*args, **kwargs):
            return "用户在和助手聊天，聊到了测试。"

        with patch("app.memory.compactor.llm_chat", fake_chat):
            changed = asyncio.run(compact(self.store, "user:1"))

        self.assertTrue(changed)
        row = self.store.get_chat_summary("user:1")
        self.assertIn("测试", row["summary"])
        self.assertEqual(row["up_to_message_id"], 16)

    def test_below_threshold_does_nothing(self):
        self._fill(8)
        changed = asyncio.run(compact(self.store, "user:1"))
        self.assertFalse(changed)
        self.assertIsNone(self.store.get_chat_summary("user:1"))

    def test_incremental_summary_uses_previous(self):
        self.store.upsert_chat_summary("user:1", "旧摘要：用户喜欢猫", 5)
        self._fill(20, prefix="n")
        captured = {}

        async def fake_chat(prompt, *args, **kwargs):
            captured["prompt"] = prompt
            return "新摘要"

        with patch("app.memory.compactor.llm_chat", fake_chat):
            changed = asyncio.run(compact(self.store, "user:1"))

        self.assertTrue(changed)
        self.assertIn("旧摘要", captured["prompt"])

    def test_llm_failure_keeps_summary(self):
        self._fill(20)

        async def fake_chat(*args, **kwargs):
            return None

        with patch("app.memory.compactor.llm_chat", fake_chat):
            changed = asyncio.run(compact(self.store, "user:1"))
        self.assertFalse(changed)
        self.assertIsNone(self.store.get_chat_summary("user:1"))

    def test_disabled_does_nothing(self):
        self._fill(20)
        with patch.object(settings, "HISTORY_COMPACT_ENABLED", False):
            self.assertFalse(asyncio.run(compact(self.store, "user:1")))


if __name__ == "__main__":
    unittest.main()
