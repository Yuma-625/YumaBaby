import asyncio
import json
import unittest
from contextlib import suppress
from unittest.mock import patch

import websockets

from app.adapters.qq import QQAdapter
from app.config import settings


class QQAdapterIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received: list[tuple[str, dict]] = []
        self.api_request: dict | None = None
        self.reply_received = asyncio.Event()

        async def on_message(text: str, context: dict) -> str:
            self.received.append((text, context))
            return "收到啦"

        self.adapter = QQAdapter(on_message=on_message)

    async def _handler(self, ws):
        await ws.send(json.dumps({
            "post_type": "message",
            "message_type": "group",
            "message_id": 1001,
            "self_id": 999,
            "group_id": 100,
            "user_id": 42,
            "raw_message": "小弗 [CQ:at,qq=999] 在吗",
        }, ensure_ascii=False))

        raw = await ws.recv()
        self.api_request = json.loads(raw)
        self.reply_received.set()
        await ws.send(json.dumps({
            "status": "ok",
            "retcode": 0,
            "data": {"message_id": 1},
            "echo": self.api_request["echo"],
        }))

        with suppress(Exception):
            await ws.wait_closed()

    async def test_group_message_roundtrip(self):
        async with websockets.serve(self._handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            self.adapter.ws_url = f"ws://127.0.0.1:{port}"

            with patch.object(settings, "QQ_TARGET_GROUPS", ["100"]), \
                 patch.object(settings, "QQ_AUTO_REPLY_KEYWORDS", ["小弗"]), \
                 patch.object(settings, "QQ_BOT_UID", "999"):
                task = asyncio.create_task(self.adapter.start())
                try:
                    await asyncio.wait_for(self.reply_received.wait(), timeout=5)
                finally:
                    await self.adapter.stop()
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

        # CQ 码应被清洗后再交给业务层
        self.assertEqual(self.received[0][0], "小弗 在吗")
        self.assertEqual(self.received[0][1]["group_id"], "100")
        # 回复应通过 send_group_msg 发出
        self.assertEqual(self.api_request["action"], "send_group_msg")
        self.assertEqual(str(self.api_request["params"]["group_id"]), "100")
        self.assertEqual(self.api_request["params"]["message"], "收到啦")

    async def test_start_keeps_retrying_instead_of_exiting(self):
        """连不上时必须留在重连循环里，而不是让 start() 直接返回。"""
        self.adapter.ws_url = "ws://127.0.0.1:1"

        task = asyncio.create_task(self.adapter.start())
        await asyncio.sleep(0.3)
        self.assertFalse(task.done(), "start() 不应在首次连接失败后退出")

        await self.adapter.stop()
        await asyncio.wait_for(task, timeout=5)
        self.assertTrue(task.done())


if __name__ == "__main__":
    unittest.main()
