import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agent.scheduler import scheduler
from app.config import settings
from app.main import app

client = TestClient(app)


class ApiTest(unittest.TestCase):
    def test_health(self):
        resp = client.get("/api/v1/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("qq", body)
        self.assertIn("llm_configured", body)

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
        self.assertIn("鱼宝宝", body["reply"])

    def test_chat_rejects_empty_message(self):
        resp = client.post("/api/v1/chat", json={"message": ""})
        self.assertEqual(resp.status_code, 422)

    def test_broadcast_reports_disabled_or_disconnected(self):
        resp = client.post("/api/v1/broadcast", json={"message": "hi"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(resp.json()["status"], ("ok", "error"))

    def test_lifespan_starts_and_stops_cleanly(self):
        """不启 QQ 的前提下走完 startup/shutdown，确认调度器能正常起停。"""
        with patch.object(settings, "QQ_ENABLED", False):
            with TestClient(app) as c:
                body = c.get("/api/v1/health").json()
                self.assertTrue(body["scheduler"]["running"])
                self.assertIn("persona_summary", body["scheduler"]["jobs"])
        self.assertFalse(scheduler.running)


if __name__ == "__main__":
    unittest.main()
