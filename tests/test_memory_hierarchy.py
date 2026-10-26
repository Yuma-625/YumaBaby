import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.memory.store import MemoryStore
from app.memory.vectors import pack_vector


class MemoTopicTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_topic_upsert_and_keyword_merge(self):
        first = self.store.upsert_topic("user", "1", "结婚", "婚姻,婚礼")
        second = self.store.upsert_topic("user", "1", "结婚", "彩礼")
        self.assertEqual(first, second)
        topic = self.store.get_topic(first)
        self.assertIn("婚姻", topic["keywords"])
        self.assertIn("彩礼", topic["keywords"])
        self.assertEqual(self.store.count_topics("user", "1"), 1)

    def test_topic_scoped_isolation(self):
        self.store.upsert_topic("user", "1", "结婚")
        self.store.upsert_topic("user", "2", "结婚")
        self.assertEqual(self.store.count_topics("user", "1"), 1)
        self.assertEqual(len(self.store.list_topics("user", "2")), 1)

    def test_extract_cursor_roundtrip(self):
        self.assertEqual(self.store.get_extract_cursor("user", "1"), 0)
        self.store.set_extract_cursor("user", "1", 42)
        self.assertEqual(self.store.get_extract_cursor("user", "1"), 42)
        self.store.set_extract_cursor("user", "1", 50)
        self.assertEqual(self.store.get_extract_cursor("user", "1"), 50)

    def test_migration_adds_memory_columns(self):
        columns = {
            row[1] for row in self.store._conn.execute("PRAGMA table_info(memories)")
        }
        for name in ("memory_hash", "importance", "source_message_ids", "topic_id"):
            self.assertIn(name, columns)

    def test_delete_empty_topics(self):
        topic_id = self.store.upsert_topic("user", "1", "空主题")
        self.assertEqual(self.store.delete_empty_topics(), 1)
        self.assertIsNone(self.store.get_topic(topic_id))


class HierarchicalRecallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")
        self.topic_id = self.store.upsert_topic(
            "user", "1", "结婚", "婚姻,婚礼", summary="用户计划明年结婚"
        )
        self.wedding = self.store.remember(
            "user", "1", "用户计划明年五一结婚",
            importance=0.9, topic_id=self.topic_id,
        )
        self.work_topic = self.store.upsert_topic("user", "1", "工作", "加班")
        self.work = self.store.remember(
            "user", "1", "用户最近在加班", importance=0.5,
            topic_id=self.work_topic,
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_lexical_topic_boost(self):
        items = self.store.recall_hierarchical(
            "user", "1", "婚姻", limit=2, use_topics=True,
        )
        self.assertEqual(items[0]["id"], self.wedding)
        self.assertEqual(items[0]["topic_name"], "结婚")

    def test_topic_is_soft_not_gate(self):
        items = self.store.recall_hierarchical(
            "user", "1", "工作", limit=5, use_topics=True,
        )
        ids = {item["id"] for item in items}
        self.assertIn(self.work, ids)

    def test_query_vector_ranks_semantic_match(self):
        self.store.set_vector(
            "topic", self.topic_id, settings.EMBEDDING_MODEL,
            pack_vector([1.0, 0.0]), 2,
        )
        self.store.set_vector(
            "memory", self.wedding, settings.EMBEDDING_MODEL,
            pack_vector([0.98, 0.1]), 2,
        )
        self.store.set_vector(
            "memory", self.work, settings.EMBEDDING_MODEL,
            pack_vector([0.0, 1.0]), 2,
        )
        items = self.store.recall_hierarchical(
            "user", "1", "婚礼", limit=2,
            query_vector=[1.0, 0.0], model=settings.EMBEDDING_MODEL,
            use_topics=True,
        )
        self.assertEqual(items[0]["id"], self.wedding)
        self.assertEqual(items[0]["topic_name"], "结婚")

    def test_topics_disabled_is_flat(self):
        items = self.store.recall_hierarchical(
            "user", "1", "婚礼", limit=2, use_topics=False,
        )
        self.assertTrue(all(not item["topic_name"] for item in items))

    def test_blank_scope_returns_empty(self):
        self.assertEqual(self.store.recall_hierarchical("user", "404", "x"), [])


if __name__ == "__main__":
    unittest.main()
