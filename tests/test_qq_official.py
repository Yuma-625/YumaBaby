import asyncio
import base64
import json
import time
import unittest

import httpx

from app.adapters.qq_official import (
    OfficialQQAdapter,
    format_official_mentions,
    strip_cq_images,
)


class OfficialPolicyTest(unittest.TestCase):
    def _event(self, group="G12345678"):
        return {
            "id": "e1", "content": "在吗", "group_openid": group,
            "author": {"member_openid": "U1", "username": "奶鱼"},
            "mentions": [{"id": "BOT", "username": "小弗", "bot": True}],
        }

    def test_add_robot_notifies_unknown_group(self):
        seen = []
        adapter = OfficialQQAdapter(
            group_policy=lambda gid: None,
            on_unknown_group=lambda *args, **kwargs: seen.append(args),
        )
        asyncio.run(adapter._handle_event(
            "GROUP_ADD_ROBOT",
            {"group_openid": "G12345678", "op_member_openid": "U1"},
        ))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "G12345678")
        self.assertEqual(seen[0][1], "U1")

    def test_unknown_group_notifies_admin_once(self):
        seen = []
        replies = []

        async def on_message(text, context):
            replies.append(text)
            return ""

        adapter = OfficialQQAdapter(
            on_message=on_message,
            group_policy=lambda gid: None,
            on_unknown_group=lambda *args, **kwargs: seen.append(args),
        )
        adapter._bot_openid = "BOT"
        asyncio.run(adapter._handle_group(self._event(), at_bot=True))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "G12345678")
        self.assertEqual(replies, [])

    def test_readonly_group_records_but_does_not_reply(self):
        seen = []
        replies = []

        async def on_message(text, context):
            replies.append(text)
            return ""

        adapter = OfficialQQAdapter(
            on_message=on_message,
            on_group_seen=lambda *args: seen.append(args),
            group_policy=lambda gid: {"mode": "readonly", "participation": False},
        )
        asyncio.run(adapter._handle_group(self._event(), at_bot=True))
        self.assertEqual(len(seen), 1)
        self.assertEqual(replies, [])

    def test_normal_group_replies_when_at(self):
        replies = []

        async def on_message(text, context):
            replies.append(text)
            return ""

        adapter = OfficialQQAdapter(
            on_message=on_message,
            group_policy=lambda gid: {"mode": "normal", "participation": False},
        )
        adapter._bot_openid = "BOT"
        asyncio.run(adapter._handle_group(self._event(), at_bot=True))
        asyncio.run(adapter._flush_after("group:G12345678:U1", 0))
        self.assertEqual(replies, ["在吗"])


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

    def test_parse_mentions_without_content_placeholder(self):
        adapter = OfficialQQAdapter()
        adapter._bot_openid = "BOT"
        data = {
            "id": "m2",
            "content": "和 打个招呼",
            "author": {"member_openid": "U1", "username": "奶鱼"},
            "mentions": [
                {"id": "BOT", "username": "小弗", "bot": True},
                {"id": "U2", "username": "奶坤"},
            ],
        }
        parsed = adapter._parse_event(data)
        self.assertIn("@奶坤", parsed["text"])
        self.assertEqual(parsed["at_targets"], [{"qq": "U2", "name": "奶坤"}])

    def test_parse_group_event(self):
        adapter = OfficialQQAdapter()
        adapter._bot_openid = "BOT"
        data = {
            "id": "m1",
            "content": " <@BOT> 你好 <@U2> ",
            "author": {"member_openid": "U1", "username": "奶鱼", "bot": False},
            "mentions": [
                {"id": "BOT", "username": "小弗"},
                {"id": "U2", "username": "奶坤"},
            ],
            "attachments": [
                {"content_type": "image/png", "url": "https://x/a.png", "filename": "a.png"},
            ],
        }
        parsed = adapter._parse_event(data)
        self.assertEqual(parsed["user_id"], "U1")
        self.assertEqual(parsed["name"], "奶鱼")
        self.assertIn("@小弗", parsed["text"])
        self.assertIn("@奶坤", parsed["text"])
        self.assertIn("[图片]", parsed["text"])
        self.assertEqual(parsed["attachments"][0]["type"], "image")
        self.assertTrue(all(t["qq"] != "BOT" for t in parsed["at_targets"]))


class FakeTransport:
    def __init__(self):
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json={"file_info": "FILEINFO", "ttl": 300})
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

    def test_cq_image_is_uploaded_then_sent(self):
        png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 32).decode()
        result = asyncio.run(self.adapter.send_group_msg(
            "G1", f"[CQ:image,file=base64://{png}]", msg_id="m1",
        ))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(self.transport.requests), 2)
        upload = json.loads(self.transport.requests[0].content)
        self.assertEqual(upload["file_type"], 1)
        self.assertTrue(upload["file_data"])
        sent = json.loads(self.transport.requests[1].content)
        self.assertEqual(sent["msg_type"], 7)
        self.assertEqual(sent["media"]["file_info"], "FILEINFO")


if __name__ == "__main__":
    unittest.main()
