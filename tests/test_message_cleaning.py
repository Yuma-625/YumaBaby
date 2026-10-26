import unittest

from app.adapters.qq import clean_message, parse_message
from app.config import settings


class CleanMessageTest(unittest.TestCase):
    def test_strips_at_code(self):
        self.assertEqual(clean_message("[CQ:at,qq=10001] 你好"), "你好")

    def test_strips_image_and_keeps_text(self):
        self.assertEqual(clean_message("看图[CQ:image,file=a.jpg]呀"), "看图 呀")

    def test_unescapes_cq_entities(self):
        self.assertEqual(clean_message("A&amp;B&#91;C&#93;"), "A&B[C]")

    def test_collapses_whitespace(self):
        self.assertEqual(clean_message("  a \n\n b  "), "a b")

    def test_handles_none_and_empty(self):
        self.assertEqual(clean_message(None), "")
        self.assertEqual(clean_message(""), "")


class ParseMessageTest(unittest.TestCase):
    def test_array_message_keeps_at_target(self):
        data = {"message": [
            {"type": "text", "data": {"text": "你和 "}},
            {"type": "at", "data": {"qq": "20002", "name": "奶鱼"}},
            {"type": "text", "data": {"text": " 打个招呼"}},
            {"type": "reply", "data": {"id": "777"}},
        ]}
        parsed = parse_message(data)
        self.assertIn("@奶鱼", parsed.text)
        self.assertIn("打个招呼", parsed.text)
        self.assertFalse(parsed.at_bot)
        self.assertEqual(parsed.reply_to, "777")
        self.assertEqual(parsed.at_targets, [{"qq": "20002", "name": "奶鱼"}])

    def test_array_message_detects_bot_mention(self):
        data = {"message": [
            {"type": "at", "data": {"qq": settings.QQ_BOT_UID, "name": "小鱼"}},
            {"type": "text", "data": {"text": " 在吗"}},
        ]}
        parsed = parse_message(data)
        self.assertTrue(parsed.at_bot)
        self.assertNotIn("@小鱼", parsed.text)
        self.assertEqual(parsed.text, "在吗")

    def test_string_message_fallback(self):
        data = {"raw_message": "[CQ:at,qq=20002] 你好 [CQ:reply,id=123]"}
        parsed = parse_message(data)
        self.assertEqual(parsed.at_targets, [{"qq": "20002", "name": ""}])
        self.assertEqual(parsed.reply_to, "123")
        self.assertEqual(parsed.text, "你好")

    def test_at_all_is_ignored(self):
        data = {"message": [{"type": "at", "data": {"qq": "all"}}]}
        parsed = parse_message(data)
        self.assertEqual(parsed.at_targets, [])
        self.assertFalse(parsed.at_bot)


if __name__ == "__main__":
    unittest.main()
