import unittest

from app.agent.scene import infer_addressee


class SceneInferenceTest(unittest.TestCase):
    def test_uses_previous_at_name(self):
        entries = [
            {"sender_name": "Heart.", "content": "@喔鲸鲸 继女"},
            {"sender_name": "Heart.", "content": "过来伺候我"},
        ]
        self.assertEqual(
            infer_addressee(entries, "Heart.", "过来伺候我"), "喔鲸鲸"
        )

    def test_falls_back_to_previous_sender(self):
        entries = [
            {"sender_name": "冯某某", "content": "今天打什么"},
            {"sender_name": "Heart.", "content": "过来伺候我"},
        ]
        self.assertEqual(
            infer_addressee(entries, "Heart.", "过来伺候我"), "冯某某"
        )

    def test_reverse_addressee(self):
        entries = [
            {"sender_name": "Myth", "content": "@喔鲸鲸 大肥鱼你变了吗"},
            {"sender_name": "喔鲸鲸", "content": "你要问的是我变了，还是模型把我弄残了"},
        ]
        self.assertEqual(
            infer_addressee(entries, "喔鲸鲸", "你要问的是我变了，还是模型把我弄残了"),
            "Myth",
        )

    def test_ignores_bot_name(self):
        entries = [
            {"sender_name": "Heart.", "content": "@小弗 你好"},
            {"sender_name": "Heart.", "content": "过来伺候我"},
        ]
        self.assertEqual(
            infer_addressee(entries, "Heart.", "过来伺候我"), ""
        )

    def test_current_message_at_name(self):
        entries = [{"sender_name": "Heart.", "content": "@喔鲸鲸 过来"}]
        self.assertEqual(
            infer_addressee(entries, "Heart.", "@喔鲸鲸 过来"), "喔鲸鲸"
        )


if __name__ == "__main__":
    unittest.main()
