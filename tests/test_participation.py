import asyncio
import unittest
from unittest.mock import patch

from app.adapters.qq import QQAdapter
from app.config import settings


async def wait_until(predicate, timeout: float = 1.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


class AutoParticipationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []

        async def on_message(text, context):
            self.calls.append((text, context))
            return "接了"

        self.adapter = QQAdapter(on_message=on_message)
        self._patches = [
            patch.object(settings, "QQ_TARGET_GROUPS", ["100"]),
            patch.object(settings, "QQ_PARTICIPATION_GROUPS", ["100"]),
            patch.object(settings, "PARTICIPATION_RATE", 1.0),
            patch.object(settings, "PARTICIPATION_COOLDOWN_SECONDS", 0),
            patch.object(settings, "GROUP_TRIGGER_MODE", "mention_or_keyword"),
            patch.object(settings, "QQ_AUTO_REPLY_KEYWORDS", ["小鱼"]),
            patch.object(settings, "QQ_BOT_UID", "999"),
            patch.object(settings, "DEBOUNCE_SECONDS", 0),
        ]
        for p in self._patches:
            p.start()

    async def asyncTearDown(self):
        await self.adapter.stop()
        for p in self._patches:
            p.stop()

    async def test_auto_participates_without_trigger(self):
        await self.adapter._handle_group_message({
            "group_id": "100", "user_id": "7", "raw_message": "今天天气不错啊",
        })
        self.assertTrue(await wait_until(lambda: len(self.calls) == 1))
        self.assertTrue(self.calls[0][1]["auto_participate"])

    async def test_rate_zero_disables_auto(self):
        with patch.object(settings, "PARTICIPATION_RATE", 0):
            await self.adapter._handle_group_message({
                "group_id": "100", "user_id": "7", "raw_message": "今天天气不错啊",
            })
            await asyncio.sleep(0.1)
            self.assertEqual(self.calls, [])

    async def test_mention_always_replies(self):
        with patch.object(settings, "PARTICIPATION_RATE", 0):
            await self.adapter._handle_group_message({
                "group_id": "100", "user_id": "7",
                "raw_message": "[CQ:at,qq=999] 在吗在吗",
            })
            self.assertTrue(await wait_until(lambda: len(self.calls) == 1))
            self.assertFalse(self.calls[0][1]["auto_participate"])

    async def test_cooldown_blocks_second_auto(self):
        with patch.object(settings, "PARTICIPATION_COOLDOWN_SECONDS", 60):
            for text in ("今天天气不错啊", "晚上吃点什么好呢"):
                await self.adapter._handle_group_message({
                    "group_id": "100", "user_id": "7", "raw_message": text,
                })
            await asyncio.sleep(0.15)
            self.assertEqual(len(self.calls), 1)

    async def test_group_not_in_list_is_ignored(self):
        with patch.object(settings, "QQ_PARTICIPATION_GROUPS", []):
            await self.adapter._handle_group_message({
                "group_id": "100", "user_id": "7", "raw_message": "今天天气不错啊",
            })
            await asyncio.sleep(0.1)
            self.assertEqual(self.calls, [])

    async def test_resolve_at_names_and_cache(self):
        calls = []

        async def fake_api(action, params, timeout=10):
            calls.append((action, params))
            return {"status": "ok", "data": {"card": "奶鱼"}}

        self.adapter._call_api = fake_api
        targets = [{"qq": "20002", "name": ""}]
        await self.adapter._resolve_at_names("100", targets)
        self.assertEqual(targets[0]["name"], "奶鱼")

        cached = [{"qq": "20002", "name": ""}]
        await self.adapter._resolve_at_names("100", cached)
        self.assertEqual(cached[0]["name"], "奶鱼")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
