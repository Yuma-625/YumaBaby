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
from app.stickers import (
    handle_sticker_command,
    handle_sticker_send,
    is_send_command,
    is_sticker_command,
    parse_send_count,
    parse_send_id,
    parse_send_ids,
    save_sticker,
)


def png_bytes(color=(255, 0, 0)) -> bytes:
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)

    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    raw = b"\x00" + bytes(color) * 2 + b"\x00" + bytes(color) * 2
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def data_uri(color=(255, 0, 0)) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes(color)).decode()


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

    def test_send_command_detection(self):
        self.assertTrue(is_send_command("发 1 号表情"))
        self.assertTrue(is_send_command("发表情 2"))
        self.assertTrue(is_send_command("发个表情"))
        self.assertFalse(is_send_command("这个表情真好笑"))
        self.assertFalse(is_send_command("存这个表情包"))
        self.assertEqual(parse_send_id("发 3 号表情"), 3)
        self.assertIsNone(parse_send_id("发个表情"))

    def test_send_by_id(self):
        asyncio.run(save_sticker(
            self.store,
            attachments=[{"type": "image", "url": data_uri(), "summary": "[动画表情]"}],
            group_id="100", user_id="1293585554",
        ))
        reply = asyncio.run(handle_sticker_send(
            "发 1 号表情", store=self.store, user_id="1293585554",
        ))
        self.assertTrue(reply.startswith("[CQ:image,file=base64://"))

    def test_send_random(self):
        asyncio.run(save_sticker(
            self.store,
            attachments=[{"type": "image", "url": data_uri()}],
            group_id="100", user_id="1293585554",
        ))
        reply = asyncio.run(handle_sticker_send(
            "发个表情", store=self.store, user_id="1293585554",
        ))
        self.assertTrue(reply.startswith("[CQ:image"))

    def _save_three(self):
        for color in ((255, 0, 0), (0, 255, 0), (0, 0, 255)):
            asyncio.run(save_sticker(
                self.store,
                attachments=[{"type": "image", "url": data_uri(color)}],
                group_id="100", user_id="1293585554",
            ))

    def test_natural_count_phrase(self):
        self._save_three()
        reply = asyncio.run(handle_sticker_send(
            "发出来。3个表情包", store=self.store, user_id="1293585554",
        ))
        self.assertEqual(reply.count("[CQ:image"), 3)

    def test_multiple_ids(self):
        self._save_three()
        reply = asyncio.run(handle_sticker_send(
            "发1号还是2号还是3号表情", store=self.store, user_id="1293585554",
        ))
        self.assertEqual(reply.count("[CQ:image"), 3)
        self.assertEqual(parse_send_ids("发1号还是2号还是3号表情"), [1, 2, 3])

    def test_two_of_them(self):
        self._save_three()
        reply = asyncio.run(handle_sticker_send(
            "两个表情包都发出来", store=self.store, user_id="1293585554",
        ))
        self.assertEqual(reply.count("[CQ:image"), 2)

    def test_parse_count(self):
        self.assertEqual(parse_send_count("发出来。3个表情包"), 3)
        self.assertEqual(parse_send_count("两个表情包都发出来"), 2)

    def test_send_missing_id(self):
        asyncio.run(save_sticker(
            self.store,
            attachments=[{"type": "image", "url": data_uri()}],
            group_id="100", user_id="1293585554",
        ))
        reply = asyncio.run(handle_sticker_send(
            "发 99 号表情", store=self.store, user_id="1293585554",
        ))
        self.assertIn("没有 #99", reply)

    def test_send_empty_library(self):
        reply = asyncio.run(handle_sticker_send(
            "发个表情", store=self.store, user_id="1293585554",
        ))
        self.assertIn("空的", reply)

    def test_send_non_admin_ignored(self):
        asyncio.run(save_sticker(
            self.store,
            attachments=[{"type": "image", "url": data_uri()}],
            group_id="100", user_id="1293585554",
        ))
        self.assertIsNone(asyncio.run(handle_sticker_send(
            "发个表情", store=self.store, user_id="12345",
        )))


if __name__ == "__main__":
    unittest.main()
