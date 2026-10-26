import asyncio
import unittest

from app.config import settings
from app.utils.vision import attachment_to_part, build_image_parts


class VisionTest(unittest.TestCase):
    def test_data_uri_is_passed_through(self):
        part = asyncio.run(attachment_to_part({
            "type": "image",
            "url": "data:image/png;base64,AAAA",
        }))
        self.assertEqual(part["type"], "image_url")
        self.assertTrue(part["image_url"]["url"].startswith("data:image/png"))

    def test_private_url_is_rejected(self):
        part = asyncio.run(attachment_to_part({
            "type": "image",
            "url": "http://127.0.0.1:8000/secret.png",
        }))
        self.assertIsNone(part)

    def test_empty_url_is_skipped(self):
        self.assertIsNone(asyncio.run(attachment_to_part({"type": "image", "url": ""})))

    def test_limit_and_non_image_filtered(self):
        attachments = [
            {"type": "mface", "summary": "[微笑]"},
            {"type": "image", "url": "data:image/png;base64,AA"},
            {"type": "image", "url": "data:image/png;base64,BB"},
        ]
        parts = asyncio.run(build_image_parts(attachments, limit=1))
        self.assertEqual(len(parts), 1)

    def test_disabled_returns_empty(self):
        from unittest.mock import patch
        with patch.object(settings, "VISION_ENABLED", False):
            parts = asyncio.run(build_image_parts(
                [{"type": "image", "url": "data:image/png;base64,AA"}]
            ))
            self.assertEqual(parts, [])


if __name__ == "__main__":
    unittest.main()
