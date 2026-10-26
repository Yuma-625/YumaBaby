import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import settings
from app.memory import embeddings
from app.memory.store import MemoryStore
from app.memory.vectors import cosine, pack_vector, similarity_score, unpack_vector


class VectorMathTest(unittest.TestCase):
    def test_pack_unpack(self):
        blob = pack_vector([1.0, 2.0, 3.0])
        self.assertEqual(unpack_vector(blob).tolist(), [1.0, 2.0, 3.0])

    def test_cosine(self):
        self.assertAlmostEqual(cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(cosine([1, 0], [0, 1]), 0.0)
        self.assertEqual(cosine([1, 0], []), 0.0)

    def test_similarity_range(self):
        self.assertEqual(similarity_score(1.0), 1.0)
        self.assertEqual(similarity_score(-1.0), 0.0)
        self.assertAlmostEqual(similarity_score(0.0), 0.5)


class FakeEmbeddings:
    def __init__(self):
        self.calls = 0

    async def create(self, model, input):
        self.calls += 1
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=i, embedding=[float(i + 1), 0.0])
                for i, _ in enumerate(input)
            ]
        )


class FakeClient:
    def __init__(self):
        self.embeddings = FakeEmbeddings()


class EmbeddingAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")
        self._patches = [
            patch.object(settings, "EMBEDDING_ENABLED", True),
            patch.object(settings, "EMBEDDING_API_KEY", "test"),
            patch.object(settings, "EMBEDDING_MODEL", "test-embed"),
        ]
        for p in self._patches:
            p.start()
        embeddings._client = None
        embeddings._query_cache.clear()

    def tearDown(self):
        embeddings._client = None
        embeddings._query_cache.clear()
        for p in self._patches:
            p.stop()
        self.store.close()
        self.tmp.cleanup()

    def test_disabled_returns_none(self):
        with patch.object(settings, "EMBEDDING_ENABLED", False):
            self.assertIsNone(asyncio.run(embeddings.embed_texts(["x"])))

    def test_embed_and_store_memory(self):
        memory_id = self.store.remember("user", "1", "用户计划结婚")
        with patch.object(embeddings, "_client", FakeClient()):
            count = asyncio.run(embeddings.embed_memory_ids(self.store, [memory_id]))
        self.assertEqual(count, 1)
        blob = self.store.get_vectors("memory", "test-embed", [memory_id])[memory_id]
        self.assertEqual(len(unpack_vector(blob)), 2)

    def test_query_cache(self):
        client = FakeClient()
        with patch.object(embeddings, "_client", client):
            first = asyncio.run(embeddings.embed_query("婚姻"))
            second = asyncio.run(embeddings.embed_query("婚姻"))
        self.assertEqual(first, second)
        self.assertEqual(client.embeddings.calls, 1)

    def test_topic_embedding(self):
        topic_id = self.store.upsert_topic("user", "1", "结婚", "婚姻", "计划婚礼")
        with patch.object(embeddings, "_client", FakeClient()):
            count = asyncio.run(embeddings.embed_topic_ids(self.store, [topic_id]))
        self.assertEqual(count, 1)
        self.assertIn(topic_id, self.store.get_vectors("topic", "test-embed", [topic_id]))


if __name__ == "__main__":
    unittest.main()
