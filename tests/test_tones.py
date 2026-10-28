import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent.tones import handle_tone_admin
from app.config import settings
from app.memory.store import MemoryStore
from app.stickers import classify_tone, maybe_reply_sticker, save_sticker
from tests.test_stickers import data_uri


class ToneRuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root / "m.db")
        self._patches = [
            patch.object(settings, "QQ_PERSONA_ADMIN", "ADMIN"),
            patch.object(settings, "STICKERS_DIR", str(self.root / "stickers")),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.store.close()
        self.tmp.cleanup()

    def test_default_rules_seeded(self):
        tones = {r["tone"] for r in self.store.list_tone_rules()}
        self.assertIn("无语", tones)
        self.assertIn("开心", tones)

    def test_custom_tone_works_without_code(self):
        reply = handle_tone_admin("加语气 嘲讽 就这,呵呵", self.store, "ADMIN")
        self.assertIn("已加语气", reply)
        rules = self.store.list_tone_rules()
        self.assertIn("嘲讽", classify_tone("就这？", rules))

        asyncio_run(save_sticker(
            self.store,
            attachments=[{"type": "image", "url": data_uri()}],
            group_id="100", user_id="ADMIN",
            text="存这个表情包 命名阴阳 标签嘲讽",
        ))
        with (
            patch.object(settings, "STICKER_SEND_MODE", "reply"),
            patch.object(settings, "STICKER_SEND_CHANCE", 1.0),
            patch.object(settings, "STICKER_SEND_COOLDOWN_SECONDS", 0),
            patch.object(settings, "STICKER_SEND_DAILY_LIMIT", 100),
            patch.object(settings, "STICKER_SEND_MIN_CHARS", 1),
        ):
            cq = maybe_reply_sticker(self.store, "就这？", group_id="100")
        self.assertTrue(cq.startswith("[CQ:image"))

    def test_delete_tone(self):
        handle_tone_admin("加语气 嘲讽 就这", self.store, "ADMIN")
        self.assertIn("已删除", handle_tone_admin("删语气 嘲讽", self.store, "ADMIN"))
        self.assertNotIn("嘲讽", {r["tone"] for r in self.store.list_tone_rules()})

    def test_non_admin_ignored(self):
        self.assertIsNone(handle_tone_admin("加语气 嘲讽 就这", self.store, "USER"))


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
