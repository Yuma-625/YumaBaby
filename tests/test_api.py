import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import shared
from app.agent.scheduler import scheduler
from app.config import settings
from app.main import app
from app.memory.store import MemoryStore

client = TestClient(app)


class ApiTest(unittest.TestCase):
    def test_health(self):
        resp = client.get("/api/v1/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("qq", body)
        self.assertIn("llm_configured", body)
        self.assertEqual(body["api_auth"], {"required": False, "token_set": False})
        self.assertIn("usage_today", body)
        self.assertIn("level", body["usage_today"])

    def test_skills(self):
        resp = client.get("/api/v1/skills")
        self.assertEqual(resp.status_code, 200)
        names = [s["name"] for s in resp.json()["skills"]]
        self.assertEqual(names, ["stock", "fund", "chat"])

    def test_chat_help_does_not_need_llm(self):
        resp = client.post("/api/v1/chat", json={"message": "帮助"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["skill_used"], "chat")
        self.assertIn("小鱼", body["reply"])

    def test_chat_rejects_empty_message(self):
        resp = client.post("/api/v1/chat", json={"message": ""})
        self.assertEqual(resp.status_code, 422)

    def test_broadcast_reports_disabled_or_disconnected(self):
        resp = client.post("/api/v1/broadcast", json={"message": "hi"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(resp.json()["status"], ("ok", "error"))

    def test_activity_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "a.db")
            store.save_message("user:1", "user", "你好")
            store.save_message("user:1", "assistant", "在")
            try:
                with patch.object(shared, "memory_store", store):
                    resp = client.get("/api/v1/activity")
                self.assertEqual(resp.status_code, 200)
                body = resp.json()
                self.assertEqual(body["messages"]["received"], 1)
                self.assertEqual(body["messages"]["sent"], 1)
            finally:
                store.close()

    def test_activity_requires_store(self):
        with patch.object(shared, "memory_store", None):
            self.assertEqual(client.get("/api/v1/activity").status_code, 503)

    def test_memory_topics_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "m.db")
            topic_id = store.upsert_topic("user", "1", "结婚", "婚姻", "准备婚礼")
            store.remember("user", "1", "用户明年结婚", topic_id=topic_id)
            try:
                with patch.object(shared, "memory_store", store):
                    resp = client.get(
                        "/api/v1/memory/topics",
                        params={"scope_type": "user", "scope_id": "1"},
                    )
                    detail = client.get(f"/api/v1/memory/topics/{topic_id}")
                self.assertEqual(resp.status_code, 200)
                topics = resp.json()["topics"]
                self.assertEqual(topics[0]["name"], "结婚")
                self.assertEqual(topics[0]["facts"], 1)
                self.assertEqual(detail.status_code, 200)
                self.assertEqual(detail.json()["memories"][0]["text"], "用户明年结婚")
            finally:
                store.close()

    def test_memory_topics_validates_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "m.db")
            try:
                with patch.object(shared, "memory_store", store):
                    resp = client.get(
                        "/api/v1/memory/topics",
                        params={"scope_type": "bad", "scope_id": "1"},
                    )
                self.assertEqual(resp.status_code, 422)
            finally:
                store.close()

    def test_memory_extract_endpoint_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "m.db")
            store.save_message("user:1", "user", "只有一条")
            try:
                with patch.object(shared, "memory_store", store):
                    resp = client.post(
                        "/api/v1/memory/extract",
                        params={"scope_type": "user", "scope_id": "1"},
                    )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["added"], 0)
            finally:
                store.close()

    def test_memory_topic_missing_returns_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "m.db")
            try:
                with patch.object(shared, "memory_store", store):
                    resp = client.get("/api/v1/memory/topics/9999")
                self.assertEqual(resp.status_code, 404)
            finally:
                store.close()

    def test_lifespan_starts_and_stops_cleanly(self):
        """不启 QQ 的前提下走完 startup/shutdown，确认调度器能正常起停。"""
        with patch.object(settings, "QQ_ENABLED", False):
            with TestClient(app) as c:
                body = c.get("/api/v1/health").json()
                self.assertTrue(body["scheduler"]["running"])
                self.assertIn("persona_summary", body["scheduler"]["jobs"])
        self.assertFalse(scheduler.running)


class ApiAuthTest(unittest.TestCase):
    def test_token_required_when_configured(self):
        with patch.object(settings, "API_TOKEN", "s3cret"):
            self.assertEqual(
                client.post("/api/v1/chat", json={"message": "帮助"}).status_code, 401
            )
            self.assertEqual(
                client.post(
                    "/api/v1/chat",
                    json={"message": "帮助"},
                    headers={"Authorization": "Bearer wrong"},
                ).status_code,
                401,
            )
            resp = client.post(
                "/api/v1/chat",
                json={"message": "帮助"},
                headers={"Authorization": "Bearer s3cret"},
            )
            self.assertEqual(resp.status_code, 200)

    def test_broadcast_is_protected_too(self):
        with patch.object(settings, "API_TOKEN", "s3cret"):
            self.assertEqual(
                client.post("/api/v1/broadcast", json={"message": "hi"}).status_code, 401
            )

    def test_auth_required_without_token_returns_503(self):
        with patch.object(settings, "API_TOKEN", ""), \
             patch.object(settings, "API_AUTH_REQUIRED", True):
            self.assertEqual(
                client.post("/api/v1/chat", json={"message": "帮助"}).status_code, 503
            )

    def test_health_reports_auth_mode(self):
        with patch.object(settings, "API_TOKEN", "s3cret"), \
             patch.object(settings, "API_AUTH_REQUIRED", True):
            body = client.get("/api/v1/health").json()
            self.assertEqual(body["api_auth"], {"required": True, "token_set": True})


if __name__ == "__main__":
    unittest.main()
