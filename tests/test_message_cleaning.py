import unittest

from app.adapters.qq import clean_message


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


if __name__ == "__main__":
    unittest.main()
