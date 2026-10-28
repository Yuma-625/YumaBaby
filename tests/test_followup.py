import tempfile
import unittest
from pathlib import Path

from app.agent.followup import looks_like_followup
from app.memory.store import MemoryStore


class FollowupHeuristicTest(unittest.TestCase):
    def test_followup_after_question(self):
        self.assertTrue(looks_like_followup("随便聊聊", "你想聊什么？"))
        self.assertTrue(looks_like_followup("那你呢", "我挺好的"))

    def test_not_followup(self):
        self.assertFalse(looks_like_followup("今天天气不错"))
        self.assertFalse(looks_like_followup("发送大狗无语"))
        self.assertFalse(looks_like_followup("加群 ABC12345"))
        self.assertFalse(looks_like_followup("@某人 你好"))
        self.assertFalse(looks_like_followup("好", "我挺好的"))


class ConversationStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_mark_and_get(self):
        self.store.mark_conversation_active("group:G", "u1", 120, "你想聊什么？")
        state = self.store.get_active_conversation("group:G", "u1")
        self.assertIsNotNone(state)
        self.assertEqual(state["turns"], 1)
        self.assertIn("想聊什么", state["last_reply"])

    def test_turns_increment(self):
        self.store.mark_conversation_active("group:G", "u1", 120, "一")
        self.store.mark_conversation_active("group:G", "u1", 120, "二")
        self.assertEqual(self.store.get_active_conversation("group:G", "u1")["turns"], 2)

    def test_multiple_users_have_independent_sessions(self):
        self.store.mark_conversation_active("group:G", "u1", 120, "A 的回复")
        self.store.mark_conversation_active("group:G", "u2", 120, "B 的回复")
        a = self.store.get_active_conversation("group:G", "u1")
        b = self.store.get_active_conversation("group:G", "u2")
        self.assertEqual(a["last_reply"], "A 的回复")
        self.assertEqual(b["last_reply"], "B 的回复")
        self.assertEqual(a["turns"], 1)
        self.assertEqual(b["turns"], 1)


if __name__ == "__main__":
    unittest.main()
