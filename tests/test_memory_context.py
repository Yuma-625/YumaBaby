import unittest

from app.agent.context import build_system_prompt


class PromptGroupingTest(unittest.TestCase):
    def test_groups_by_topic_with_summary(self):
        memories = [
            {"text": "用户计划明年结婚", "topic_name": "结婚",
             "topic_summary": "用户准备婚礼"},
            {"text": "婚礼预算十万", "topic_name": "结婚",
             "topic_summary": "用户准备婚礼"},
            {"text": "用户喜欢猫", "topic_name": "", "topic_summary": ""},
        ]
        prompt = build_system_prompt("人设", memories)
        self.assertIn("[结婚] 摘要：用户准备婚礼", prompt)
        self.assertIn("- 用户计划明年结婚", prompt)
        self.assertIn("- 用户喜欢猫", prompt)
        self.assertLess(prompt.index("[结婚]"), prompt.index("用户喜欢猫"))

    def test_no_memories_returns_base(self):
        self.assertEqual(build_system_prompt("人设", []), "人设")

    def test_truncates_only_memory_block(self):
        memories = [
            {"text": "x" * 500, "topic_name": f"主题{i}", "topic_summary": ""}
            for i in range(10)
        ]
        prompt = build_system_prompt("人设" * 100, memories)
        self.assertIn("人设", prompt)
        self.assertLessEqual(len(prompt), 1000 + 400 + 200)


if __name__ == "__main__":
    unittest.main()
