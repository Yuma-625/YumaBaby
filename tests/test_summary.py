import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.memory.store import MemoryStore
from app.summary import _last_run, handle_summary_command, is_summary_command, parse_count


class SummaryCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")
        self._patches = [
            patch.object(settings, "QQ_PERSONA_ADMIN", "1293585554"),
            patch.object(settings, "SUMMARY_ADMIN_ONLY", True),
            patch.object(settings, "SUMMARY_DEFAULT_COUNT", 50),
            patch.object(settings, "SUMMARY_MAX_COUNT", 120),
            patch.object(settings, "SUMMARY_COOLDOWN_SECONDS", 120),
        ]
        for p in self._patches:
            p.start()
        _last_run.clear()
        for i in range(6):
            self.store.add_group_context("100", str(i), f"消息{i}", [], f"群友{i}")

    def tearDown(self):
        _last_run.clear()
        for p in self._patches:
            p.stop()
        self.store.close()
        self.tmp.cleanup()

    def test_command_detection(self):
        self.assertTrue(is_summary_command("总结最近100条群聊"))
        self.assertTrue(is_summary_command("总结一下聊天"))
        self.assertFalse(is_summary_command("这个总结不错"))
        self.assertFalse(is_summary_command("今天天气不错"))

    def test_parse_count(self):
        self.assertEqual(parse_count("总结最近30条群聊"), 30)
        self.assertEqual(parse_count("总结群聊"), 50)
        self.assertEqual(parse_count("总结最近999条群聊"), 120)
        self.assertEqual(parse_count("总结最近1条群聊"), 5)

    def test_summary_uses_transcript(self):
        captured = {}

        async def fake_chat(prompt, system_prompt=None, **kwargs):
            captured["prompt"] = prompt
            captured["system"] = system_prompt
            return "主要聊了 1、2、3。"

        with patch("app.summary.llm_chat", fake_chat):
            reply = asyncio_run(handle_summary_command(
                "总结最近10条群聊", store=self.store,
                group_id="100", user_id="1293585554",
            ))
        self.assertIn("主要聊了", reply)
        self.assertIn("群友0: 消息0", captured["prompt"])
        self.assertIn("群聊记录整理助手", captured["system"])

    def test_non_admin_ignored(self):
        with patch("app.summary.llm_chat") as fake:
            reply = asyncio_run(handle_summary_command(
                "总结最近10条群聊", store=self.store,
                group_id="100", user_id="12345",
            ))
        self.assertIsNone(reply)
        fake.assert_not_called()

    def test_too_few_messages(self):
        empty = MemoryStore(Path(self.tmp.name) / "empty.db")
        try:
            reply = asyncio_run(handle_summary_command(
                "总结最近50条群聊", store=empty,
                group_id="999", user_id="1293585554",
            ))
            self.assertIn("太少了", reply)
        finally:
            empty.close()

    def test_cooldown(self):
        async def fake_chat(*args, **kwargs):
            return "总结结果"

        with patch("app.summary.llm_chat", fake_chat):
            first = asyncio_run(handle_summary_command(
                "总结最近10条群聊", store=self.store,
                group_id="100", user_id="1293585554",
            ))
            second = asyncio_run(handle_summary_command(
                "总结最近10条群聊", store=self.store,
                group_id="100", user_id="1293585554",
            ))
        self.assertIn("总结结果", first)
        self.assertIn("刚总结过", second)


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
