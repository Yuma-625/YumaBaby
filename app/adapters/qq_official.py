"""QQ 开放平台官方机器人适配器。

和 OneBot 适配器保持同样的回调和策略（白名单/只读群/概率接话/连发合并），
只替换传输层：官方 WebSocket Gateway + HTTP OpenAPI。
"""

import asyncio
import base64
import json
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from app.adapters.base import BaseAdapter
from app.agent.followup import looks_like_followup
from app.config import settings

logger = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"\[\[at:([^\]]+)\]\]")
_CQ_IMAGE_RE = re.compile(r"\[CQ:image,[^\]]*\]")
_CQ_IMAGE_B64_RE = re.compile(r"\[CQ:image,file=base64://([^\]]+)\]")
_AT_TAG_RE = re.compile(r"<@!?([^>]+)>")
_OUTBOUND_AT_TAG_RE = re.compile(r"<@!?[^>]+>")


def _image_file_type(data: bytes) -> tuple[int, str]:
    if data.startswith(b"\x89PNG"):
        return 1, "png"
    if data.startswith(b"\xff\xd8"):
        return 1, "jpg"
    if data.startswith(b"GIF8"):
        return 1, "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return 1, "webp"
    if data.startswith(b"BM"):
        return 1, "bmp"
    return 4, "bin"


def format_official_mentions(text: str) -> str:
    """官方群发送不支持文本 <@openid>，发送前去掉标记，避免露出原始 ID。"""
    cleaned = _MENTION_RE.sub("", text or "")
    cleaned = _OUTBOUND_AT_TAG_RE.sub("", cleaned)
    return " ".join(cleaned.split())


def strip_cq_images(text: str) -> tuple[str, int]:
    """官方协议不支持 CQ 图片；先移除并返回数量，避免把 CQ 码发出去。"""
    matches = _CQ_IMAGE_RE.findall(text or "")
    return _CQ_IMAGE_RE.sub("", text or ""), len(matches)


@dataclass
class _PendingChat:
    group_id: str | None = None
    user_id: str | None = None
    sender_name: str = ""
    reply_msg_id: str | None = None
    reply_ref: str | None = None
    auto: bool = False
    texts: list[str] = field(default_factory=list)
    at_targets: list[dict[str, str]] = field(default_factory=list)
    attachments: list[dict[str, str]] = field(default_factory=list)
    timer: asyncio.Task | None = None


class OfficialQQAdapter(BaseAdapter):
    """QQ 开放平台机器人（AppID / AppSecret / WebSocket Gateway）。"""

    RECONNECT_MIN_DELAY = 1
    RECONNECT_MAX_DELAY = 60
    DEDUPE_TTL = 300
    DEDUPE_MAX = 500
    MAX_MESSAGE_LENGTH = 1000

    def __init__(self, on_message: Callable | None = None,
                 on_reply_sent: Callable[[str, str], None] | None = None,
                 on_group_seen: Callable[[str, str, str, str, list], None] | None = None,
                 group_policy: Callable[[str], dict | None] | None = None,
                 on_unknown_group: Callable[[str, str, str, str], None] | None = None,
                 conversation_state: Callable[[str, str], dict | None] | None = None,
                 on_bot_replied: Callable[[str, str, str], None] | None = None):
        self.on_message = on_message
        self.on_reply_sent = on_reply_sent
        self.on_group_seen = on_group_seen
        self.group_policy = group_policy
        self.on_unknown_group = on_unknown_group
        self.conversation_state = conversation_state
        self.on_bot_replied = on_bot_replied
        self._http = httpx.AsyncClient(timeout=20, follow_redirects=True)
        self._token = ""
        self._token_exp = 0.0
        self._bot_openid = settings.QQ_OFFICIAL_BOT_OPENID
        self._bot_name = ""
        self._ws: Any = None
        self._connected = False
        self._running = False
        self._session_id = ""
        self._seq: int | None = None
        self._seen: dict[str, float] = {}
        self._pending: dict[str, _PendingChat] = {}
        self._reply_seq: dict[str, int] = {}
        self._last_auto_reply: dict[str, float] = {}
        self._last_followup_reply: dict[str, float] = {}
        self._seen_groups: set[str] = set()
        self._awaiting_feedback = False
        self._awaiting_since = 0.0

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------ 连接

    async def start(self):
        if not settings.QQ_OFFICIAL_APP_ID or not settings.QQ_OFFICIAL_APP_SECRET:
            logger.error("QQ_PROVIDER=official 但未配置 QQ_OFFICIAL_APP_ID / APP_SECRET")
            return
        self._running = True
        delay = self.RECONNECT_MIN_DELAY
        while self._running:
            try:
                await self._connect_and_consume()
                delay = self.RECONNECT_MIN_DELAY
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"官方 QQ 机器人连接失败: {type(e).__name__}: {e}")
            if not self._running:
                break
            logger.warning(f"{delay}s 后重连官方 QQ 机器人")
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.RECONNECT_MAX_DELAY)

    async def _connect_and_consume(self):
        token = await self._ensure_token()
        gateway = await self._get_gateway(token)
        logger.info(f"官方 QQ 机器人连接 Gateway: {gateway}")
        async with websockets.connect(gateway, max_size=8 * 1024 * 1024) as ws:
            self._ws = ws
            self._connected = True
            heartbeat: asyncio.Task | None = None
            try:
                while self._running:
                    raw = await ws.recv()
                    payload = json.loads(raw)
                    op = payload.get("op")
                    if op == 10:
                        interval = (payload.get("d") or {}).get("heartbeat_interval", 45000) / 1000
                        heartbeat = asyncio.create_task(self._heartbeat(interval))
                        await self._authenticate(ws)
                    elif op == 0:
                        self._seq = payload.get("s", self._seq)
                        event = payload.get("t")
                        data = payload.get("d") or {}
                        if event == "READY":
                            self._session_id = data.get("session_id") or ""
                            user = data.get("user") or {}
                            if user.get("id") and not self._bot_openid:
                                self._bot_openid = str(user["id"])
                            if user.get("username"):
                                self._bot_name = str(user["username"])
                            logger.info(f"官方机器人已就绪: {user.get('username')} ({user.get('id')})")
                        elif event == "RESUMED":
                            logger.info("官方机器人已恢复会话")
                        else:
                            self._spawn(self._handle_event(event, data))
                    elif op == 7:
                        logger.warning("官方 Gateway 要求重连")
                        await ws.close()
                        break
                    elif op == 9:
                        logger.warning("官方 Gateway 会话失效，重新 Identify")
                        self._session_id = ""
                        self._seq = None
                        await ws.close()
                        break
            except ConnectionClosed:
                logger.warning("官方 Gateway 连接已断开")
            finally:
                self._connected = False
                self._ws = None
                if heartbeat is not None:
                    heartbeat.cancel()
                    with suppress(asyncio.CancelledError):
                        await heartbeat

    async def _authenticate(self, ws) -> None:
        token = await self._ensure_token()
        if self._session_id and self._seq is not None:
            await ws.send(json.dumps({
                "op": 6,
                "d": {
                    "token": f"QQBot {token}",
                    "session_id": self._session_id,
                    "seq": self._seq,
                },
            }, ensure_ascii=False))
            return
        await ws.send(json.dumps({
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
                "intents": settings.QQ_OFFICIAL_INTENTS,
                "shard": [0, 1],
                "properties": {},
            },
        }, ensure_ascii=False))

    async def _heartbeat(self, interval: float) -> None:
        while self._running and self._ws is not None:
            await asyncio.sleep(max(5.0, interval))
            ws = self._ws
            if ws is None:
                return
            try:
                await ws.send(json.dumps({"op": 1, "d": self._seq}, ensure_ascii=False))
            except Exception:
                return

    async def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_exp:
            return self._token
        resp = await self._http.post(
            f"{settings.QQ_OFFICIAL_API_BASE}/app/getAppAccessToken",
            json={
                "appId": settings.QQ_OFFICIAL_APP_ID,
                "clientSecret": settings.QQ_OFFICIAL_APP_SECRET,
            },
        )
        data = resp.json()
        if isinstance(data.get("data"), dict):
            data = data["data"]
        token = data.get("access_token") or ""
        if not token:
            raise RuntimeError(f"获取 access_token 失败: {data}")
        self._token = token
        self._token_exp = time.time() + int(data.get("expires_in", 7200)) - 60
        return token

    async def _get_gateway(self, token: str) -> str:
        resp = await self._http.get(
            f"{settings.QQ_OFFICIAL_API_BASE}/gateway",
            headers={"Authorization": f"QQBot {token}"},
        )
        data = resp.json()
        if isinstance(data.get("data"), dict):
            data = data["data"]
        url = data.get("url") or ""
        if not url:
            raise RuntimeError(f"获取 Gateway 失败: {data}")
        return url

    async def stop(self):
        self._running = False
        self._cancel_pending()
        ws, self._ws = self._ws, None
        self._connected = False
        if ws is not None:
            with suppress(Exception):
                await ws.close()
        with suppress(Exception):
            await self._http.aclose()
        logger.info("官方 QQ 机器人适配器已断开")

    # ------------------------------------------------------------------ 收事件

    async def _handle_event(self, event: str, data: dict):
        if event in ("GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT"):
            group_id = str(data.get("group_openid") or "")
            operator = str(data.get("op_member_openid") or "")
            if not group_id:
                return
            policy = (
                self.group_policy(group_id) if self.group_policy
                else self._default_group_policy(group_id)
            )
            logger.info(
                "官方群事件 %s group=%s operator=%s known=%s",
                event, group_id, operator, policy is not None,
            )
            if event == "GROUP_ADD_ROBOT" and policy is None and self.on_unknown_group:
                self.on_unknown_group(
                    group_id, operator, "机器人被拉进群", "",
                    notify=True,
                )
            return

        if event not in (
            "C2C_MESSAGE_CREATE",
            "GROUP_AT_MESSAGE_CREATE",
            "GROUP_MESSAGE_CREATE",
        ):
            return
        author = data.get("author") or {}
        if author.get("bot"):
            return
        event_id = str(data.get("id") or "")
        if event_id and self._is_duplicate(event_id):
            return

        if event == "C2C_MESSAGE_CREATE":
            await self._handle_private(data)
        else:
            at_bot = event == "GROUP_AT_MESSAGE_CREATE"
            if not at_bot:
                at_bot = any(bool(m.get("bot")) for m in (data.get("mentions") or []))
            if not at_bot and self._bot_openid:
                at_bot = any(
                    str(m.get("id") or m.get("member_openid")) == self._bot_openid
                    for m in (data.get("mentions") or [])
                )
            log = logger.info if event != "GROUP_MESSAGE_CREATE" else logger.debug
            policy = (
                self.group_policy(data.get("group_openid") or "")
                if self.group_policy else None
            )
            if policy is None:
                log = logger.info
            log(
                "官方群事件 %s group=%s user=%s at_bot=%s content=%r mentions=%s",
                event, data.get("group_openid"),
                (data.get("author") or {}).get("member_openid"), at_bot,
                data.get("content"),
                [
                    (m.get("id"), m.get("member_openid"), m.get("username"), m.get("bot"))
                    for m in (data.get("mentions") or [])
                ],
            )
            await self._handle_group(data, at_bot=at_bot)

    def _parse_event(self, data: dict) -> dict[str, Any]:
        author = data.get("author") or {}
        user_id = str(
            author.get("member_openid") or author.get("user_openid")
            or author.get("id") or ""
        )
        name = str(author.get("username") or "")
        mentions = data.get("mentions") or []
        mention_names = {
            str(m.get("id") or m.get("member_openid") or ""): str(m.get("username") or "")
            for m in mentions
        }
        bot_mention_ids = {
            str(m.get("id") or m.get("member_openid") or "")
            for m in mentions if m.get("bot")
        }
        bot_mention_ids.add(self._bot_openid)

        def repl(match):
            key = match.group(1)
            if key == "all":
                return "@全体成员 "
            if key in bot_mention_ids:
                return " "
            return f"@{mention_names.get(key, key)} "

        text = _AT_TAG_RE.sub(repl, str(data.get("content") or ""))
        # content 里可能保留字面量 @小弗，命令解析前去掉
        if self._bot_name:
            prefix = f"@{self._bot_name}"
            text = text.lstrip()
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
        # 官方有时把 content 里的 @ 占位符去掉了，但 mentions 仍带人；
        # 补回名字，避免模型只看到“和 打个招呼”。
        for target_id, target in mention_names.items():
            if target_id in bot_mention_ids:
                continue
            if target and f"@{target}" not in text:
                text = f"@{target} " + text
                break
        attachments = []
        for item in data.get("attachments") or []:
            ctype = str(item.get("content_type") or "")
            url = str(item.get("url") or "")
            if ctype.startswith("image/"):
                attachments.append({
                    "type": "image", "url": url,
                    "content_type": ctype,
                    "filename": str(item.get("filename") or ""),
                })
                text += " [图片]"
            elif ctype == "voice":
                attachments.append({"type": "voice", "url": url})
                text += " [语音]"
            elif ctype == "video/mp4":
                attachments.append({"type": "video", "url": url})
                text += " [视频]"
            else:
                attachments.append({
                    "type": "file", "url": url,
                    "filename": str(item.get("filename") or ""),
                })
                text += f" [文件:{item.get('filename') or ''}]"

        at_targets = [
            {"qq": str(m.get("member_openid") or m.get("id") or ""),
             "name": str(m.get("username") or "")}
            for m in mentions
            if str(m.get("member_openid") or m.get("id") or "") != self._bot_openid
        ]
        return {
            "user_id": user_id,
            "name": name,
            "text": " ".join(text.split()),
            "at_targets": at_targets,
            "attachments": attachments,
            "message_id": str(data.get("id") or ""),
            "reply_ref": self._extract_reply_ref(data),
        }

    @staticmethod
    def _extract_reply_ref(data: dict) -> str:
        scene = data.get("message_scene") or {}
        for item in scene.get("ext") or []:
            value = str(item or "")
            if value.startswith("ref_msg_idx="):
                return value.split("=", 1)[1].strip()
        return ""

    def _default_group_policy(self, group_id: str) -> dict | None:
        if settings.QQ_TARGET_GROUPS and group_id not in settings.QQ_TARGET_GROUPS:
            return None
        return {
            "mode": "readonly" if group_id in settings.QQ_READONLY_GROUPS else "normal",
            "participation": group_id in settings.QQ_PARTICIPATION_GROUPS,
        }

    async def _handle_group(self, data: dict, *, at_bot: bool):
        group_id = str(data.get("group_openid") or "")
        if not group_id:
            return
        parsed = self._parse_event(data)
        text = parsed["text"]
        user_id = parsed["user_id"]
        policy = (
            self.group_policy(group_id) if self.group_policy
            else self._default_group_policy(group_id)
        )
        if policy is None:
            mention_hint = (
                at_bot
                or bool(data.get("mentions"))
                or "<@" in str(data.get("content") or "")
            )
            if self.on_unknown_group:
                self.on_unknown_group(
                    group_id, user_id, text, parsed["name"],
                    notify=mention_hint,
                )
            return

        if group_id not in self._seen_groups:
            self._seen_groups.add(group_id)
            logger.info(
                "发现群 openid: %s（成员 openid 示例: %s）", group_id, user_id
            )
        logger.debug(f"收到官方群消息 [{group_id}] {user_id}: {text} | at_bot={at_bot}")
        if self.on_group_seen:
            try:
                self.on_group_seen(
                    group_id, user_id, text, parsed["name"], parsed["attachments"]
                )
            except Exception:
                logger.exception("记录群聊上下文失败")

        if policy.get("mode") == "readonly":
            return

        # 这条消息 @ 了别人、又没 @ 机器人：是在跟别人说话，不抢话
        if parsed["at_targets"] and not at_bot:
            logger.debug("跳过 @ 别人的群消息: %s", text[:60])
            return

        active = self._pending_key(group_id, user_id) in self._pending
        auto = False
        followup = False
        admin_command = (
            bool(settings.QQ_PERSONA_ADMIN)
            and user_id == settings.QQ_PERSONA_ADMIN
            and self._looks_admin_command(text)
        )
        if not active and not self._is_group_triggered(at_bot, text) and not admin_command:
            state = (
                self.conversation_state(group_id, user_id)
                if settings.FOLLOWUP_ENABLED and self.conversation_state else None
            )
            # 这条消息 @ 了别人，说明是在跟别人说话，不抢话
            if state and not parsed["at_targets"] and int(state.get("turns") or 0) < settings.FOLLOWUP_MAX_TURNS:
                followup = looks_like_followup(
                    text, str(state.get("last_reply") or "")
                )
            if followup:
                now = asyncio.get_running_loop().time()
                last = self._last_followup_reply.get(group_id, 0.0)
                if now - last < settings.FOLLOWUP_GROUP_COOLDOWN_SECONDS:
                    followup = False
                else:
                    self._last_followup_reply[group_id] = now
            if not followup:
                if not policy.get("participation"):
                    return
                if not self._should_auto_participate(group_id, text):
                    return
                auto = True
                self._last_auto_reply[group_id] = asyncio.get_running_loop().time()
        elif not at_bot and any(
            kw and kw in text for kw in settings.QQ_AUTO_REPLY_KEYWORDS
        ):
            logger.info("官方群关键词触发 [%s]: %s", group_id, text[:80])
        if followup:
            logger.info("官方群续聊 [%s] user=%s: %s", group_id, user_id, text[:80])

        if not text and at_bot:
            text = "（只是 @ 了我一下，没有说别的）"
        text = self._truncate(text)
        if not text or not self.on_message:
            return
        self._enqueue(
            text, group_id=group_id, user_id=user_id,
            reply_msg_id=parsed["message_id"], auto=auto,
            at_targets=parsed["at_targets"], attachments=parsed["attachments"],
            sender_name=parsed["name"], reply_ref=parsed["reply_ref"],
        )

    async def _handle_private(self, data: dict):
        parsed = self._parse_event(data)
        user_id = parsed["user_id"]
        if not user_id:
            return
        logger.info(
            "官方私聊 user=%s name=%s reply_ref=%s",
            user_id, parsed["name"], parsed["reply_ref"],
        )
        is_admin = bool(settings.QQ_PERSONA_ADMIN) and user_id == settings.QQ_PERSONA_ADMIN
        if user_id not in settings.QQ_PRIVATE_WHITELIST and not is_admin:
            logger.debug(f"忽略非白名单私聊 openid: {user_id}")
            return

        if is_admin and self._awaiting_feedback:
            self._awaiting_feedback = False
            if self._feedback_expired():
                await self.send_private_msg(user_id, "人设审核已超时，本次不处理。可等待今日 20:00 的再次推送。")
                return
            from app.utils.persona import persona_manager

            result = persona_manager.apply_feedback(parsed["text"])
            await self.send_private_msg(user_id, result)
            return

        text = self._truncate(parsed["text"])
        if not text or not self.on_message:
            return
        self._enqueue(
            text, user_id=user_id, reply_msg_id=parsed["message_id"],
            attachments=parsed["attachments"], reply_ref=parsed["reply_ref"],
        )

    def _is_duplicate(self, event_id: str) -> bool:
        now = asyncio.get_running_loop().time()
        if event_id in self._seen:
            return True
        self._seen[event_id] = now
        if len(self._seen) > self.DEDUPE_MAX:
            for key in [k for k, ts in self._seen.items() if now - ts > self.DEDUPE_TTL]:
                self._seen.pop(key, None)
        return False

    # ------------------------------------------------------------------ 策略

    @staticmethod
    def _looks_admin_command(text: str) -> bool:
        value = (text or "").strip().lower()
        if value.startswith(("llm", "token", "今天", "日报")):
            return True
        return value.startswith((
            "发给我", "给我发", "主动发", "加群", "删群", "群列表",
            "待审群", "扫新群", "扫一下", "语气", "表情候选",
            "表情列表", "表情标签", "收 ", "拒 ", "存这个", "保存这个",
            "收藏这个",
        ))

    def _is_group_triggered(self, mentioned: bool, text: str) -> bool:
        mode = settings.GROUP_TRIGGER_MODE
        keyword = any(kw and kw in text for kw in settings.QQ_AUTO_REPLY_KEYWORDS)
        if mode == "all":
            return True
        if mode == "mention":
            return mentioned
        if mode == "keyword":
            return keyword
        return mentioned or keyword

    def _should_auto_participate(self, group_id: str, text: str) -> bool:
        if settings.PARTICIPATION_RATE <= 0 or len(text) < 4:
            return False
        now = asyncio.get_running_loop().time()
        if now - self._last_auto_reply.get(group_id, 0.0) < settings.PARTICIPATION_COOLDOWN_SECONDS:
            return False
        rate = settings.PARTICIPATION_RATE
        if "?" in text or "？" in text or "吗" in text:
            rate = min(1.0, rate * 2)
        import random
        return random.random() < rate

    # ------------------------------------------------------------- 连发合并

    @staticmethod
    def _pending_key(group_id: str | None, user_id: str | None) -> str:
        return f"group:{group_id}:{user_id}" if group_id else f"user:{user_id}"

    def _enqueue(self, text: str, *, group_id: str | None = None,
                 user_id: str | None = None, reply_msg_id: str | None = None,
                 auto: bool = False, at_targets: list[dict[str, str]] | None = None,
                 attachments: list[dict[str, str]] | None = None,
                 sender_name: str = "", reply_ref: str = "") -> None:
        if settings.DEBOUNCE_SECONDS <= 0:
            coro = (
                self._process_group_reply(group_id, user_id or "", text,
                                          reply_msg_id, auto, at_targets, attachments,
                                          sender_name, reply_ref)
                if group_id else
                self._process_private_reply(
                    user_id or "", text, reply_msg_id, attachments, reply_ref
                )
            )
            self._spawn(coro)
            return

        key = self._pending_key(group_id, user_id)
        entry = self._pending.get(key)
        if entry is None:
            entry = _PendingChat(
                group_id=group_id, user_id=user_id,
                sender_name=sender_name, auto=auto, reply_ref=reply_ref,
            )
            self._pending[key] = entry
        else:
            entry.auto = entry.auto and auto
            if reply_ref:
                entry.reply_ref = reply_ref

        entry.texts.append(text)
        for target in at_targets or []:
            if target not in entry.at_targets:
                entry.at_targets.append(target)
        for media in attachments or []:
            if media not in entry.attachments:
                entry.attachments.append(media)
        if reply_msg_id:
            entry.reply_msg_id = reply_msg_id
        if entry.timer is not None:
            entry.timer.cancel()
            entry.timer = None

        combined = "\n".join(entry.texts)
        if (len(combined) >= settings.DEBOUNCE_MAX_CHARS
                or len(entry.texts) >= settings.DEBOUNCE_MAX_MESSAGES):
            self._pending.pop(key, None)
            self._spawn(self._dispatch(entry))
            return
        entry.timer = self._spawn(self._flush_after(key, settings.DEBOUNCE_SECONDS))

    async def _flush_after(self, key: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        entry = self._pending.pop(key, None)
        if entry is not None:
            await self._dispatch(entry)

    async def _dispatch(self, entry: _PendingChat) -> None:
        text = "\n".join(t for t in entry.texts if t).strip()
        if not text:
            return
        if entry.group_id:
            await self._process_group_reply(
                entry.group_id, entry.user_id or "", text, entry.reply_msg_id,
                entry.auto, entry.at_targets, entry.attachments,
                entry.sender_name, entry.reply_ref or "",
            )
        else:
            await self._process_private_reply(
                entry.user_id or "", text, entry.reply_msg_id, entry.attachments,
                entry.reply_ref or "",
            )

    def _cancel_pending(self):
        for entry in self._pending.values():
            if entry.timer is not None:
                entry.timer.cancel()
        self._pending.clear()

    def _truncate(self, text: str) -> str:
        text = (text or "").strip()
        if len(text) > self.MAX_MESSAGE_LENGTH:
            return text[: self.MAX_MESSAGE_LENGTH]
        return text

    def begin_awaiting_feedback(self):
        self._awaiting_feedback = True
        self._awaiting_since = asyncio.get_running_loop().time()

    def cancel_awaiting_feedback(self):
        self._awaiting_feedback = False

    def _feedback_expired(self) -> bool:
        ttl = settings.PERSONA_FEEDBACK_TTL_MINUTES * 60
        return (asyncio.get_running_loop().time() - self._awaiting_since) > ttl

    # ------------------------------------------------------------------ 发消息

    async def _process_group_reply(self, group_id: str, user_id: str, text: str,
                                   reply_msg_id: str | None = None, auto: bool = False,
                                   at_targets: list[dict[str, str]] | None = None,
                                   attachments: list[dict[str, str]] | None = None,
                                   sender_name: str = "", reply_ref: str = ""):
        result = await self.on_message(text, {
            "group_id": group_id,
            "user_id": user_id,
            "sender_name": sender_name,
            "reply_to_id": reply_msg_id,
            "reply_ref": reply_ref,
            "auto_participate": auto,
            "at_targets": at_targets or [],
            "attachments": attachments or [],
        })
        if result:
            response = await self.send_group_msg(group_id, result, msg_id=reply_msg_id)
            self._notify_reply_sent(f"group:{group_id}", response)
            # 只有被叫到/续聊才建立续聊窗口；随机旁听不建立，避免下一句被误接
            if not auto and response.get("status") == "ok" and self.on_bot_replied:
                try:
                    self.on_bot_replied(group_id, user_id, result)
                except Exception:
                    logger.exception("记录续聊状态失败")

    async def _process_private_reply(self, user_id: str, text: str,
                                     reply_msg_id: str | None = None,
                                     attachments: list[dict[str, str]] | None = None,
                                     reply_ref: str = ""):
        result = await self.on_message(text, {
            "user_id": user_id,
            "reply_to_id": reply_msg_id,
            "reply_ref": reply_ref,
            "attachments": attachments or [],
        })
        if result:
            response = await self.send_private_msg(user_id, result, msg_id=reply_msg_id)
            self._notify_reply_sent(f"user:{user_id}", response)

    async def send_group_msg(self, group_id: str, message: str,
                             msg_id: str | None = None) -> dict:
        return await self._send_message("group", group_id, message, msg_id)

    async def send_private_msg(self, user_id: str, message: str,
                               msg_id: str | None = None) -> dict:
        return await self._send_message("user", user_id, message, msg_id)

    async def _send_message(self, kind: str, target: str, content: str,
                            msg_id: str | None) -> dict:
        images = _CQ_IMAGE_B64_RE.findall(content or "")
        if not images:
            text = format_official_mentions(content)
            return await self._send_text(kind, target, text, msg_id)

        text = _CQ_IMAGE_B64_RE.sub("", content or "")
        text = format_official_mentions(text).strip()
        last: dict = {"status": "failed", "error": "empty content"}
        if text:
            last = await self._send_text(kind, target, text, msg_id)
        for encoded in images[:3]:
            try:
                data = base64.b64decode(encoded)
            except Exception:
                logger.warning("CQ 图片 base64 解码失败")
                continue
            file_info = await self._upload_image(kind, target, data)
            if not file_info:
                continue
            last = await self._send_media(kind, target, file_info, msg_id)
        return last

    async def _send_text(self, kind: str, target: str, content: str,
                         msg_id: str | None) -> dict:
        if not content.strip():
            return {"status": "failed", "error": "empty content"}
        body: dict[str, Any] = {"msg_type": 0, "content": content[:2000]}
        if msg_id:
            key = f"{kind}:{target}:{msg_id}"
            seq = self._reply_seq.get(key, 0) + 1
            self._reply_seq[key] = seq
            body["msg_id"] = msg_id
            body["msg_seq"] = seq
        return await self._post(kind, target, body)

    async def _send_media(self, kind: str, target: str, file_info: str,
                          msg_id: str | None) -> dict:
        body: dict[str, Any] = {
            "msg_type": 7,
            "media": {"file_info": file_info},
        }
        if msg_id:
            key = f"{kind}:{target}:{msg_id}"
            seq = self._reply_seq.get(key, 0) + 1
            self._reply_seq[key] = seq
            body["msg_id"] = msg_id
            body["msg_seq"] = seq
        return await self._post(kind, target, body)

    async def _upload_image(self, kind: str, target: str, data: bytes) -> str:
        file_type, ext = _image_file_type(data)
        if len(data) > 5 * 1024 * 1024:
            logger.warning("官方图片直传超过 5MB，跳过（需分片上传）")
            return ""
        token = await self._ensure_token()
        path = (
            f"/v2/groups/{target}/files" if kind == "group"
            else f"/v2/users/{target}/files"
        )
        body = {
            "file_type": file_type,
            "file_name": f"sticker.{ext}",
            "file_data": base64.b64encode(data).decode(),
            "srv_send_msg": False,
        }
        try:
            resp = await self._http.post(
                f"{settings.QQ_OFFICIAL_API_BASE}{path}",
                headers={"Authorization": f"QQBot {token}"},
                json=body,
            )
            payload = resp.json()
        except Exception as e:
            logger.error(f"官方图片上传异常: {e}")
            return ""
        data_node = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        file_info = data_node.get("file_info") or ""
        if not file_info:
            logger.error(f"官方图片上传失败: {payload}")
        return file_info

    async def _post(self, kind: str, target: str, body: dict) -> dict:
        token = await self._ensure_token()
        path = (
            f"/v2/groups/{target}/messages" if kind == "group"
            else f"/v2/users/{target}/messages"
        )
        try:
            resp = await self._http.post(
                f"{settings.QQ_OFFICIAL_API_BASE}{path}",
                headers={"Authorization": f"QQBot {token}"},
                json=body,
            )
            data = resp.json()
        except Exception as e:
            logger.error(f"官方消息发送异常 [{kind}:{target}]: {e}")
            return {"status": "failed", "error": str(e)}
        if resp.status_code >= 400 or data.get("code"):
            logger.error(f"官方消息发送失败 [{kind}:{target}]: {data}")
            return {"status": "failed", "data": data}
        return {"status": "ok", "data": data}

    async def broadcast_to_groups(self, message: str):
        results = []
        for group_id in settings.QQ_TARGET_GROUPS:
            if group_id.strip():
                results.append({
                    "group_id": group_id,
                    "result": await self.send_group_msg(group_id.strip(), message),
                })
        return results

    def _notify_reply_sent(self, chat_id: str, response: dict) -> None:
        if self.on_reply_sent is None:
            return
        data = (response or {}).get("data") or {}
        message_id = data.get("id") or data.get("message_id")
        if message_id is None:
            return
        try:
            self.on_reply_sent(chat_id, str(message_id))
        except Exception:
            logger.exception("回复落库回调失败")

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        task.add_done_callback(self._log_task_error)
        return task

    @staticmethod
    def _log_task_error(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("官方适配器后台任务失败", exc_info=exc)
