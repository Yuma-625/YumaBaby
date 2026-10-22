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


class DebounceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls: list[tuple[str, dict]] = []

        async def on_message(text, context):
            self.calls.append((text, context))
            return ""

        self.adapter = QQAdapter(on_message=on_message)
        self._patches = [
            patch.object(settings, "DEBOUNCE_SECONDS", 0.08),
            patch.object(settings, "DEBOUNCE_MAX_CHARS", 400),
            patch.object(settings, "DEBOUNCE_MAX_MESSAGES", 5),
            patch.object(settings, "QQ_TARGET_GROUPS", ["100"]),
            patch.object(settings, "QQ_AUTO_REPLY_KEYWORDS", ["小鱼"]),
            patch.object(settings, "GROUP_TRIGGER_MODE", "mention_or_keyword"),
            patch.object(settings, "QQ_BOT_UID", "999"),
        ]
        for p in self._patches:
            p.start()

    async def asyncTearDown(self):
        await self.adapter.stop()
        for p in self._patches:
            p.stop()

    async def test_merges_consecutive_messages(self):
        self.adapter._enqueue("第一句", group_id="100", user_id="7")
        self.adapter._enqueue("第二句", group_id="100", user_id="7")
        self.assertTrue(await wait_until(lambda: len(self.calls) == 1))
        self.assertEqual(self.calls[0][0], "第一句\n第二句")
        self.assertEqual(self.calls[0][1]["group_id"], "100")

    async def test_different_chats_not_merged(self):
        self.adapter._enqueue("群里的", group_id="100", user_id="7")
        self.adapter._enqueue("私聊的", user_id="8")
        self.assertTrue(await wait_until(lambda: len(self.calls) == 2))
        texts = {c[0] for c in self.calls}
        self.assertEqual(texts, {"群里的", "私聊的"})

    async def test_long_message_dispatches_immediately(self):
        with patch.object(settings, "DEBOUNCE_SECONDS", 5), \
             patch.object(settings, "DEBOUNCE_MAX_CHARS", 10):
            self.adapter._enqueue("1234567890", group_id="100", user_id="7")
            self.assertTrue(await wait_until(lambda: len(self.calls) == 1, 0.3))

    async def test_too_many_messages_dispatches_immediately(self):
        with patch.object(settings, "DEBOUNCE_SECONDS", 5), \
             patch.object(settings, "DEBOUNCE_MAX_MESSAGES", 2):
            self.adapter._enqueue("一", group_id="100", user_id="7")
            self.adapter._enqueue("二", group_id="100", user_id="7")
            self.assertTrue(await wait_until(lambda: len(self.calls) == 1, 0.3))

    async def test_disabled_debounce_processes_each_message(self):
        with patch.object(settings, "DEBOUNCE_SECONDS", 0):
            self.adapter._enqueue("一", user_id="8")
            self.adapter._enqueue("二", user_id="8")
            self.assertTrue(await wait_until(lambda: len(self.calls) == 2))
            self.assertEqual(sorted(c[0] for c in self.calls), ["一", "二"])

    async def test_stop_cancels_pending(self):
        self.adapter._enqueue("还没到时候", user_id="8")
        await self.adapter.stop()
        await asyncio.sleep(0.15)
        self.assertEqual(self.calls, [])

    async def test_group_trigger_then_merge(self):
        await self.adapter._handle_group_message({
            "group_id": "100", "user_id": "7", "raw_message": "小鱼你好",
        })
        await self.adapter._handle_group_message({
            "group_id": "100", "user_id": "7", "raw_message": "在吗",
        })
        self.assertTrue(await wait_until(lambda: len(self.calls) == 1))
        self.assertEqual(self.calls[0][0], "小鱼你好\n在吗")

    async def test_group_message_without_trigger_is_ignored(self):
        await self.adapter._handle_group_message({
            "group_id": "100", "user_id": "7", "raw_message": "今天天气不错",
        })
        await asyncio.sleep(0.15)
        self.assertEqual(self.calls, [])

    async def test_other_user_is_not_merged_into_active_chat(self):
        self.adapter._enqueue("A 的话", group_id="100", user_id="7")
        # B 没有触发词，不应被并进 A 的缓冲
        await self.adapter._handle_group_message({
            "group_id": "100", "user_id": "8", "raw_message": "随便说说",
        })
        self.assertTrue(await wait_until(lambda: len(self.calls) == 1))
        self.assertEqual(self.calls[0][0], "A 的话")

    async def test_private_message_context(self):
        self.adapter._enqueue("私聊内容", user_id="8")
        self.assertTrue(await wait_until(lambda: len(self.calls) == 1))
        context = self.calls[0][1]
        self.assertEqual(context["user_id"], "8")
        self.assertIsNone(context["reply_to_id"])


if __name__ == "__main__":
    unittest.main()
