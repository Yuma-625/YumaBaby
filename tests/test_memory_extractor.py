import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.memory.extractor import extract_scope, parse_extraction
from app.memory.store import MemoryStore


def reply(facts: list[dict]) -> str:
    return json.dumps({"memories": facts}, ensure_ascii=False)


class ParseExtractionTest(unittest.TestCase):
    def test_plain_json(self):
        items = parse_extraction(reply([
            {"text": "用户计划明年结婚", "importance": 0.9, "confidence": 0.9},
        ]))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "用户计划明年结婚")

    def test_code_fence(self):
        raw = "```json\n" + reply([{"text": "用户喜欢猫", "confidence": 0.8}]) + "\n```"
        items = parse_extraction(raw)
        self.assertEqual(items[0]["text"], "用户喜欢猫")

    def test_invalid_returns_none(self):
        self.assertIsNone(parse_extraction("not json"))
        self.assertIsNone(parse_extraction(None))
        self.assertIsNone(parse_extraction('{"memories": "x"}'))

    def test_empty_list(self):
        self.assertEqual(parse_extraction('{"memories": []}'), [])

    def test_low_confidence_filtered(self):
        items = parse_extraction(reply([
            {"text": "低置信", "confidence": 0.2},
            {"text": "高置信", "confidence": 0.9},
        ]))
        self.assertEqual([i["text"] for i in items], ["高置信"])

    def test_caps_at_eight(self):
        items = parse_extraction(reply([
            {"text": f"事实{i}", "confidence": 0.9} for i in range(12)
        ]))
        self.assertEqual(len(items), 8)

    def test_topic_and_keywords(self):
        items = parse_extraction(reply([
            {"text": "用户明年结婚", "confidence": 0.9, "topic": "结婚",
             "keywords": ["婚姻", "婚礼"]},
        ]))
        self.assertEqual(items[0]["topic"], "结婚")
        self.assertEqual(items[0]["keywords"], "婚姻,婚礼")


class ExtractScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")
        self._patches = [
            patch.object(settings, "MEMORY_EXTRACT_ENABLED", True),
            patch.object(settings, "MEMORY_HIERARCHY_ENABLED", False),
            patch.object(settings, "MEMORY_EXTRACT_DAILY_LIMIT", 20),
            patch.object(settings, "MEMORY_EXTRACT_MAX_MESSAGES", 40),
            patch.object(settings, "MEMORY_TOPICS_MAX", 50),
            patch.object(settings, "EMBEDDING_ENABLED", False),
        ]
        for p in self._patches:
            p.start()
        for i in range(5):
            role = "user" if i % 2 == 0 else "assistant"
            self.store.save_message("user:1", role, f"消息{i}")

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.store.close()
        self.tmp.cleanup()

    def test_extracts_and_advances_cursor(self):
        async def fake_chat(*args, **kwargs):
            return reply([
                {"text": "用户计划明年结婚", "importance": 0.9, "confidence": 0.9},
                {"text": "婚礼在老家办", "importance": 0.7, "confidence": 0.8},
            ])

        with patch("app.memory.extractor.llm_chat", fake_chat):
            added = asyncio.run(extract_scope(self.store, "user", "1"))

        self.assertEqual(added, 2)
        self.assertEqual(self.store.count_memories("user", "1"), 2)
        self.assertEqual(self.store.get_extract_cursor("user", "1"), 5)
        self.assertEqual(
            self.store.list_memories("user", "1")[0]["source_message_ids"], "[1, 2, 3, 4, 5]"
        )

    def test_invalid_json_keeps_cursor(self):
        async def fake_chat(*args, **kwargs):
            return "不是 JSON"

        with patch("app.memory.extractor.llm_chat", fake_chat):
            added = asyncio.run(extract_scope(self.store, "user", "1"))

        self.assertEqual(added, 0)
        self.assertEqual(self.store.get_extract_cursor("user", "1"), 0)
        self.assertEqual(self.store.count_memories("user", "1"), 0)

    def test_repeated_extraction_does_not_duplicate(self):
        async def fake_chat(*args, **kwargs):
            return reply([
                {"text": "用户计划明年结婚", "importance": 0.9, "confidence": 0.9},
            ])

        with patch("app.memory.extractor.llm_chat", fake_chat):
            first = asyncio.run(extract_scope(self.store, "user", "1"))
            second = asyncio.run(extract_scope(self.store, "user", "1"))

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(self.store.count_memories("user", "1"), 1)

    def test_near_duplicate_merges(self):
        async def fake_chat(*args, **kwargs):
            return reply([
                {"text": "用户计划明年五一结婚", "importance": 0.5, "confidence": 0.9},
            ])

        with patch("app.memory.extractor.llm_chat", fake_chat):
            asyncio.run(extract_scope(self.store, "user", "1"))
        async def fake_chat2(*args, **kwargs):
            return reply([
                {"text": "用户计划明年五一结婚。", "importance": 0.9, "confidence": 0.9},
            ])
        with patch("app.memory.extractor.llm_chat", fake_chat2):
            added = asyncio.run(extract_scope(self.store, "user", "1"))

        self.assertEqual(added, 0)
        self.assertEqual(self.store.count_memories("user", "1"), 1)

    def test_daily_limit_blocks(self):
        async def fake_chat(*args, **kwargs):
            return reply([{"text": "事实", "confidence": 0.9}])

        self.store.add_event("memory_extract", "1", "user:1")
        with patch.object(settings, "MEMORY_EXTRACT_DAILY_LIMIT", 1), \
             patch("app.memory.extractor.llm_chat", fake_chat):
            added = asyncio.run(extract_scope(self.store, "user", "1", force=True))
        self.assertEqual(added, 0)

    def test_topic_created_when_hierarchy_enabled(self):
        async def fake_chat(*args, **kwargs):
            return reply([
                {"text": "用户计划明年结婚", "importance": 0.9, "confidence": 0.9,
                 "topic": "结婚", "keywords": ["婚姻", "婚礼"]},
            ])

        with patch.object(settings, "MEMORY_HIERARCHY_ENABLED", True), \
             patch("app.memory.extractor.llm_chat", fake_chat):
            added = asyncio.run(extract_scope(self.store, "user", "1"))

        self.assertEqual(added, 1)
        topics = self.store.list_topics("user", "1")
        self.assertEqual(topics[0]["name"], "结婚")
        self.assertIn("婚姻", topics[0]["keywords"])
        memory = self.store.list_memories("user", "1")[0]
        self.assertEqual(memory["topic_id"], topics[0]["id"])


if __name__ == "__main__":
    unittest.main()
