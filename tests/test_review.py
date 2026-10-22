import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.memory.store import MemoryStore
from app.review.server import app


class ReviewApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "m.db"
        self.datasets = Path(self.tmp.name) / "datasets"
        self._patches = [
            patch.object(settings, "MEMORY_DB", str(self.db)),
            patch.object(settings, "DATASETS_DIR", str(self.datasets)),
        ]
        for p in self._patches:
            p.start()

        store = MemoryStore(self.db)
        store.save_message("user:1", "user", "今天好累")
        self.first = store.save_message("user:1", "assistant", "那就早点休息")
        store.save_message("user:1", "user", "现在几点")
        self.second = store.save_message("user:1", "assistant", "现在是凌晨一点")
        self.day = store.daily_summary()["date"]
        store.close()

        self._client_ctx = TestClient(app)
        self.client = self._client_ctx.__enter__()

    def tearDown(self):
        self._client_ctx.__exit__(None, None, None)
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_items_include_context(self):
        resp = self.client.get(f"/api/items?date={self.day}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["items"][0]["user_message"], "今天好累")
        self.assertIsNone(data["items"][0]["score"])

    def test_score_and_stats(self):
        resp = self.client.post("/api/score", json={"message_id": self.first, "score": 8})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["stats"]["scored"], 1)
        self.assertEqual(resp.json()["stats"]["average"], 8.0)

        items = self.client.get(f"/api/items?date={self.day}").json()["items"]
        self.assertEqual(items[0]["score"], 8)

    def test_rescore_updates(self):
        self.client.post("/api/score", json={"message_id": self.first, "score": 3})
        self.client.post("/api/score", json={"message_id": self.first, "score": 9})
        stats = self.client.get(f"/api/stats?date={self.day}").json()
        self.assertEqual(stats["scored"], 1)
        self.assertEqual(stats["average"], 9.0)

    def test_rejects_out_of_range(self):
        self.assertEqual(
            self.client.post("/api/score", json={"message_id": self.first, "score": 11}).status_code,
            422,
        )

    def test_rejects_user_message(self):
        store = MemoryStore(self.db)
        user_id = store._conn.execute(
            "SELECT id FROM messages WHERE role='user' LIMIT 1"
        ).fetchone()["id"]
        store.close()
        self.assertEqual(
            self.client.post("/api/score", json={"message_id": user_id, "score": 5}).status_code,
            400,
        )

    def test_finish_without_scores_fails(self):
        resp = self.client.post("/api/finish", json={"date": self.day})
        self.assertEqual(resp.status_code, 400)

    def test_finish_exports_dataset(self):
        self.client.post("/api/score", json={"message_id": self.first, "score": 9, "note": "语气好"})
        self.client.post("/api/score", json={"message_id": self.second, "score": 2, "note": "答非所问"})

        resp = self.client.post("/api/finish", json={"date": self.day})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["exported"], 2)

        path = Path(data["path"])
        self.assertTrue(path.exists())
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["score"], 9)
        self.assertEqual(rows[0]["note"], "语气好")
        self.assertIn("user", rows[0])
        self.assertIn("assistant", rows[0])

        status = self.client.get(f"/api/items?date={self.day}").json()["finished"]
        self.assertEqual(status["scored_count"], 2)


if __name__ == "__main__":
    unittest.main()
