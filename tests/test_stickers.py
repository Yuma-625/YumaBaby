import asyncio
import base64
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.memory.store import MemoryStore
from app.stickers import handle_sticker_command, is_sticker_command, save_sticker


def png_bytes() -> bytes:
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)

    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    raw = b"\x00" + bytes([255, 0, 0]) * 2 + b"\x00" + bytes([0, 0, 255]) * 2
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def data_uri() -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes()).decode()


class StickerCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root / "m.db")
        self._patches = [
            patch.object(settings, "STICKERS_DIR", str(self.root / "stickers")),
            patch.object(settings, "QQ_PERSONA_ADMIN", "1293585554"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.store.close()
        self.tmp.cleanup()

    def test_command_detection(self):
        self.assertTrue(is_sticker_command("存这个表情包"))
        self.assertTrue(is_sticker_command("保存这个表情"))
        self.assertTrue(is_sticker_command("收藏这个表情包。"))
        self.assertFalse(is_sticker_command("这个表情真好笑"))
        self.assertFalse(is_sticker_command("今天天气不错"))

    def test_saves_from_attachment(self):
        reply = asyncio.run(save_sticker(
            self.store,
            attachments=[{"type": "image", "url": data_uri(), "summary": "[动画表情]"}],
            group_id="100", user_id="1293585554",
        ))
        self.assertIn("存好了", reply)
        self.assertEqual(self.store.count_stickers(), 1)
        sticker = self.store.list_stickers()[0]
        self.assertTrue(Path(sticker["path"]).exists())

    def test_dedupes_same_image(self):
        attachment = {"type": "image", "url": data_uri(), "summary": "[动画表情]"}
        asyncio.run(save_sticker(self.store, attachments=[attachment], group_id="100", user_id="u"))
        reply = asyncio.run(save_sticker(self.store, attachments=[attachment], group_id="100", user_id="u"))
        self.assertIn("之前存过", reply)
        self.assertEqual(self.store.count_stickers(), 1)

    def test_falls_back_to_recent_context(self):
        self.store.add_group_context(
            "100", "7", "[图片]",
            [{"type": "image", "url": data_uri(), "summary": "[动画表情]"}],
        )
        reply = asyncio.run(save_sticker(
            self.store, attachments=[], group_id="100", user_id="1293585554",
        ))
        self.assertIn("存好了", reply)

    def test_no_sticker_found(self):
        reply = asyncio.run(save_sticker(
            self.store, attachments=[], group_id="100", user_id="1293585554",
        ))
        self.assertIn("没找到", reply)

    def test_non_admin_is_ignored(self):
        reply = asyncio.run(handle_sticker_command(
            "存这个表情包", store=self.store,
            attachments=[{"type": "image", "url": data_uri()}],
            group_id="100", user_id="12345",
        ))
        self.assertIsNone(reply)
        self.assertEqual(self.store.count_stickers(), 0)

    def test_admin_command_saves(self):
        reply = asyncio.run(handle_sticker_command(
            "存这个表情包", store=self.store,
            attachments=[{"type": "image", "url": data_uri()}],
            group_id="100", user_id="1293585554",
        ))
        self.assertIn("存好了", reply)


if __name__ == "__main__":
    unittest.main()
