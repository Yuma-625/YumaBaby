import asyncio
import json
import time
import unittest

import httpx

from app.adapters.qq_official import (
    OfficialQQAdapter,
    format_official_mentions,
    strip_cq_images,
)


class OfficialParseTest(unittest.TestCase):
    def test_mentions_are_converted(self):
        self.assertEqual(
            format_official_mentions("你好 [[at:ABC123]] 在吗"),
            "你好 <@ABC123> 在吗",
        )

    def test_cq_image_is_stripped(self):
        text, count = strip_cq_images("看图 [CQ:image,file=base64://xx] 好")
        self.assertEqual(count, 1)
        self.assertNotIn("CQ:", text)
        self.assertIn("看图", text)

    def test_parse_group_event(self):
        adapter = OfficialQQAdapter()
        adapter._bot_openid = "BOT"
        data = {
            "id": "m1",
            "content": " <@BOT> 你好 <@U2> ",
            "author": {"member_openid": "U1", "username": "奶鱼", "bot": False},
            "mentions": [
                {"id": "BOT", "username": "小鱼"},
                {"id": "U2", "username": "奶坤"},
            ],
            "attachments": [
                {"content_type": "image/png", "url": "https://x/a.png", "filename": "a.png"},
            ],
        }
        parsed = adapter._parse_event(data)
        self.assertEqual(parsed["user_id"], "U1")
        self.assertEqual(parsed["name"], "奶鱼")
        self.assertIn("@小鱼", parsed["text"])
        self.assertIn("@奶坤", parsed["text"])
        self.assertIn("[图片]", parsed["text"])
        self.assertEqual(parsed["attachments"][0]["type"], "image")
        self.assertTrue(all(t["qq"] != "BOT" for t in parsed["at_targets"]))


class FakeTransport:
    def __init__(self):
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"id": "sent-1", "timestamp": "2026-10-28T01:00:00+08:00"})


class OfficialSendTest(unittest.TestCase):
    def setUp(self):
        self.adapter = OfficialQQAdapter()
        self.transport = FakeTransport()
        self.adapter._http = httpx.AsyncClient(transport=httpx.MockTransport(self.transport.handler))
        self.adapter._token = "token"
        self.adapter._token_exp = time.time() + 3600

    def tearDown(self):
        asyncio.run(self.adapter._http.aclose())

    def test_passive_group_send(self):
        result = asyncio.run(self.adapter.send_group_msg("G1", "你好", msg_id="m1"))
        self.assertEqual(result["status"], "ok")
        body = json.loads(self.transport.requests[0].content)
        self.assertEqual(body["msg_id"], "m1")
        self.assertEqual(body["msg_seq"], 1)
        self.assertEqual(body["content"], "你好")

    def test_second_reply_uses_next_seq(self):
        asyncio.run(self.adapter.send_group_msg("G1", "一", msg_id="m1"))
        asyncio.run(self.adapter.send_group_msg("G1", "二", msg_id="m1"))
        bodies = [json.loads(r.content) for r in self.transport.requests]
        self.assertEqual([b["msg_seq"] for b in bodies], [1, 2])

    def test_active_message_has_no_msg_id(self):
        asyncio.run(self.adapter.send_private_msg("U1", "主动消息"))
        body = json.loads(self.transport.requests[0].content)
        self.assertNotIn("msg_id", body)
        self.assertEqual(self.transport.requests[0].url.path, "/v2/users/U1/messages")


if __name__ == "__main__":
    unittest.main()
