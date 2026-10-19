import asyncio
import unittest
from typing import Any

from app.agent.core import AgentCore
from app.skills.base import BaseSkill


class _EchoSkill(BaseSkill):
    def __init__(self, name: str, keywords: list[str]):
        self.name = name
        self.keywords = keywords
        self.description = f"{name} skill"

    async def execute(self, message: str, context: dict[str, Any]) -> str:
        return f"{self.name}:{message}"


class _ChatLikeSkill(_EchoSkill):
    def __init__(self):
        super().__init__("chat", ["帮助"])


class AgentCoreTest(unittest.TestCase):
    def setUp(self):
        self.core = AgentCore()
        self.core.skills = [
            _EchoSkill("stock", ["股票"]),
            _EchoSkill("fund", ["基金"]),
            _ChatLikeSkill(),
        ]

    def test_first_matching_skill_wins(self):
        reply, name = asyncio.run(self.core.process_with_skill("看看股票"))
        self.assertEqual((reply, name), ("stock:看看股票", "stock"))

    def test_unmatched_message_falls_back_to_chat(self):
        reply, name = asyncio.run(self.core.process_with_skill("今天吃啥"))
        self.assertEqual((reply, name), ("chat:今天吃啥", "chat"))

    def test_process_returns_reply_only(self):
        self.assertEqual(asyncio.run(self.core.process("基金怎么样")), "fund:基金怎么样")

    def test_skill_list_exposes_name_and_description(self):
        skills = {s["name"]: s["description"] for s in self.core.skill_list}
        self.assertEqual(set(skills), {"stock", "fund", "chat"})

    def test_register_skill_appends(self):
        self.core.register_skill(_EchoSkill("extra", ["额外"]))
        self.assertIn("extra", [s.name for s in self.core.skills])


if __name__ == "__main__":
    unittest.main()
