import unittest

from app.agent.member_lookup import extract_name


class MemberLookupTest(unittest.TestCase):
    def test_extract_patterns(self):
        self.assertEqual(extract_name("跟奶坤打个招呼"), "奶坤")
        self.assertEqual(extract_name("和奶坤说句话"), "奶坤")
        self.assertEqual(extract_name("帮我叫一下奶坤"), "奶坤")
        self.assertEqual(extract_name("找奶坤"), "奶坤")

    def test_skip_pronouns_and_bot(self):
        self.assertEqual(extract_name("跟你打个招呼"), "")
        self.assertEqual(extract_name("跟小弗打个招呼"), "")
        self.assertEqual(extract_name("今天天气不错"), "")


if __name__ == "__main__":
    unittest.main()
