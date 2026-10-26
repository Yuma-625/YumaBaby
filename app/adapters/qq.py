import asyncio
import json
import logging
import random
import re
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable

import websockets
from websockets.exceptions import ConnectionClosed

from app.config import settings
from app.adapters.base import BaseAdapter

logger = logging.getLogger(__name__)

# OneBot v11 消息中的 CQ 码，例如 [CQ:at,qq=123]、[CQ:image,file=xxx]
_CQ_CODE_RE = re.compile(r"\[CQ:[^\]]*\]")
# 模型输出里的 @ 标记，发送前转成真正的 CQ 码
_MENTION_RE = re.compile(r"\[\[at:(\d+)\]\]")
# CQ 码转义表（OneBot v11 规范）
_CQ_UNESCAPE = (
    ("&amp;", "&"),
    ("&#91;", "["),
    ("&#93;", "]"),
    ("&#44;", ","),
)


def clean_message(raw: str | None) -> str:
    """去掉 CQ 码并还原转义字符，得到可直接交给 LLM 的纯文本。"""
    text = _CQ_CODE_RE.sub(" ", raw or "")
    for escaped, char in _CQ_UNESCAPE:
        text = text.replace(escaped, char)
    return " ".join(text.split())


def extract_reply_id(raw: str | None) -> str | None:
    """取引用回复指向的消息 id（[CQ:reply,id=...]）。"""
    match = re.search(r"\[CQ:reply,id=(\d+)", raw or "")
    return match.group(1) if match else None


def format_mentions(text: str) -> str:
    """把 [[at:QQ]] 转成 OneBot 的 [CQ:at,qq=QQ]。"""
    return _MENTION_RE.sub(r"[CQ:at,qq=\1]", text or "")


@dataclass
class ParsedMessage:
    text: str
    at_targets: list[dict[str, str]] = field(default_factory=list)
    at_bot: bool = False
    reply_to: str | None = None
    attachments: list[dict[str, str]] = field(default_factory=list)


def _image_placeholder(payload: dict) -> str:
    summary = str(payload.get("summary", "")).strip().strip("[]")
    if summary and summary not in ("图片", "image"):
        return f"[图片:{summary}]"
    return "[图片]"


def _emoji_placeholder(payload: dict) -> str:
    summary = str(payload.get("summary", "")).strip().strip("[]")
    return f"[表情:{summary}]" if summary else "[表情]"


def parse_message(data: dict) -> ParsedMessage:
    """同时兼容 messagePostFormat=array 与 string。

    文本之外还保留 @ 目标、引用 id，以及图片/表情元数据。
    图片本身不下载，只记 url/file/summary，留给后续视觉处理。
    """
    bot_uid = str(settings.QQ_BOT_UID or "")
    targets: list[dict[str, str]] = []
    attachments: list[dict[str, str]] = []
    reply_to: str | None = None
    message = data.get("message")

    if isinstance(message, list):
        parts: list[str] = []
        for segment in message:
            if not isinstance(segment, dict):
                continue
            seg_type = segment.get("type")
            payload = segment.get("data") or {}
            if seg_type == "text":
                parts.append(str(payload.get("text", "")))
            elif seg_type == "at":
                qq = str(payload.get("qq", "")).strip()
                name = str(payload.get("name", "")).strip()
                if not qq or qq == "all":
                    continue
                targets.append({"qq": qq, "name": name})
                if qq != bot_uid:
                    parts.append(f"@{name or qq} ")
            elif seg_type == "reply":
                rid = str(payload.get("id", "")).strip()
                if rid:
                    reply_to = rid
            elif seg_type == "image":
                attachments.append({
                    "type": "image",
                    "url": str(payload.get("url", "")),
                    "file": str(payload.get("file", "")),
                    "summary": str(payload.get("summary", "")),
                })
                parts.append(" " + _image_placeholder(payload) + " ")
            elif seg_type in ("mface", "bface", "marketface"):
                attachments.append({
                    "type": seg_type,
                    "url": str(payload.get("url", "") or payload.get("emoji_url", "")),
                    "summary": str(payload.get("summary", "") or payload.get("face_name", "")),
                })
                parts.append(" " + _emoji_placeholder(payload) + " ")
            elif seg_type == "face":
                attachments.append({"type": "face", "summary": str(payload.get("id", ""))})
                parts.append(" [表情] ")
            else:
                # 未知段保留类型和摘要，既方便排查，也不至于把内容整个丢掉
                logger.warning(
                    "未识别的消息段类型: %s payload=%s",
                    seg_type, str(payload)[:160],
                )
                attachments.append({
                    "type": str(seg_type or "unknown"),
                    "raw": str(payload)[:200],
                })
                parts.append(" [未知消息] ")
        text = " ".join("".join(parts).split())
    else:
        raw = data.get("raw_message", "") or ""
        text = clean_message(raw)
        for qq in re.findall(r"\[CQ:at,qq=(\d+)", raw):
            targets.append({"qq": qq, "name": ""})
        reply_to = extract_reply_id(raw)
        for match in re.finditer(r"\[CQ:(image|mface|bface|marketface|face)([^\]]*)\]", raw):
            seg_type = match.group(1)
            params = dict(re.findall(r"(\w+)=([^,\]]+)", match.group(2)))
            if seg_type == "image":
                attachments.append({
                    "type": "image",
                    "url": params.get("url", ""),
                    "file": params.get("file", ""),
                    "summary": params.get("summary", ""),
                })
                text = f"{text} [图片]".strip()
            elif seg_type in ("mface", "bface", "marketface"):
                attachments.append({
                    "type": seg_type,
                    "url": params.get("url", ""),
                    "summary": params.get("summary", ""),
                })
                text = f"{text} {_emoji_placeholder(params)}".strip()
            else:
                attachments.append({"type": "face", "summary": params.get("id", "")})
                text = f"{text} [表情]".strip()

    at_bot = bool(bot_uid) and any(t["qq"] == bot_uid for t in targets)
    return ParsedMessage(
        text=text,
        at_targets=targets,
        at_bot=at_bot,
        reply_to=reply_to,
        attachments=attachments,
    )


@dataclass
class _PendingChat:
    """一个会话的连发缓冲。"""

    texts: list[str] = field(default_factory=list)
    timer: asyncio.Task | None = None
    group_id: str | None = None
    user_id: str | None = None
    reply_to: str | None = None
    auto: bool = False
    at_targets: list[dict[str, str]] = field(default_factory=list)
    attachments: list[dict[str, str]] = field(default_factory=list)


class QQAdapter(BaseAdapter):
    """QQ 适配器 - 对接 NapCatQQ (OneBot v11 协议)"""

    # 断线重连退避区间（秒）
    RECONNECT_MIN_DELAY = 1
    RECONNECT_MAX_DELAY = 60
    # 消息去重窗口（秒）与容量
    DEDUPE_TTL = 300
    DEDUPE_MAX = 500
    # 单条 QQ 消息最长处理长度，避免超长文本拖垮 LLM
    MAX_MESSAGE_LENGTH = 1000

    def __init__(self, on_message: Callable | None = None,
                 on_reply_sent: Callable[[str, str], None] | None = None,
                 on_group_seen: Callable[[str, str, str, str, list], None] | None = None):
        self.ws_url = settings.QQ_WS_URL
        self.access_token = settings.QQ_ACCESS_TOKEN
        self.on_message = on_message
        self.on_reply_sent = on_reply_sent
        self.on_group_seen = on_group_seen
        self._ws: Any = None
        self._running = False
        self._connected = False
        self._futures: dict[str, asyncio.Future] = {}
        self._seen: dict[str, float] = {}
        self._awaiting_feedback = False
        self._awaiting_since = 0.0
        self._pending: dict[str, _PendingChat] = {}
        self._last_auto_reply: dict[str, float] = {}
        self._at_name_cache: dict[str, str] = {}
        self._quoted_cache: dict[str, ParsedMessage] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------ 连接

    async def start(self):
        """连接 NapCat 并按需重连，直到 stop() 被调用。"""
        self._running = True
        if not self.ws_url.startswith("ws"):
            logger.warning(f"QQ_WS_URL 不是有效的 ws:// 地址: {self.ws_url!r}，适配器不会连接")
            return

        delay = self.RECONNECT_MIN_DELAY
        while self._running:
            try:
                await self._connect_and_consume()
                # 正常退出（服务端关闭或 stop()），从最短退避重新开始
                delay = self.RECONNECT_MIN_DELAY
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"连接 NapCat 失败: {e}")

            if not self._running:
                break
            logger.warning(f"{delay}s 后重连 NapCat")
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.RECONNECT_MAX_DELAY)

    async def _connect_and_consume(self):
        kwargs: dict[str, Any] = {"uri": self.ws_url}
        if self.access_token:
            # websockets >= 14 用 additional_headers，旧版本用 extra_headers
            kwargs["additional_headers"] = {"Authorization": f"Bearer {self.access_token}"}

        logger.info(f"QQ 适配器正在连接 NapCat: {self.ws_url}")
        async with websockets.connect(**kwargs) as ws:
            self._ws = ws
            self._connected = True
            logger.info("QQ 适配器已连接 NapCat")
            try:
                while self._running:
                    raw = await ws.recv()
                    await self._dispatch_raw(raw)
            except ConnectionClosed:
                logger.warning("NapCat 连接已断开")
            finally:
                self._connected = False
                self._ws = None
                self._fail_pending_futures("连接已断开")

    async def stop(self):
        """停止适配器并关闭连接。"""
        self._running = False
        self._cancel_pending()
        ws, self._ws = self._ws, None
        self._connected = False
        if ws is not None:
            with suppress(Exception):
                await ws.close()
        self._fail_pending_futures("adapter stopped")
        logger.info("QQ 适配器已断开")

    # ------------------------------------------------------------------ 收消息

    async def _dispatch_raw(self, raw: str | bytes):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("收到无法解析的消息: %r", raw)
            return

        # API 调用回包
        echo = data.get("echo")
        if echo is not None and echo in self._futures:
            future = self._futures.pop(echo)
            if not future.done():
                future.set_result(data)
            return

        # 事件处理放后台，保证读循环能继续接收 API 回包
        # （否则 _call_api 等待的响应永远读不到，会死锁到超时）
        self._spawn(self._handle_event(data))

    async def _handle_event(self, data: dict):
        """处理收到的事件, 分发群消息和私聊消息"""
        # 过滤自身发送的消息，避免「播报 -> 命中关键词 -> 再播报」的回声循环
        if data.get("post_type") == "message_sent":
            return
        if data.get("post_type") != "message":
            return

        user_id = str(data.get("user_id", ""))
        self_id = str(data.get("self_id", ""))
        if self_id and user_id == self_id:
            return

        if self._is_duplicate(data.get("message_id")):
            logger.debug(f"忽略重复消息: {data.get('message_id')}")
            return

        if data.get("message_type") == "group":
            await self._handle_group_message(data)
        elif data.get("message_type") == "private":
            await self._handle_private_message(data)

    def _is_duplicate(self, message_id: Any) -> bool:
        if message_id is None:
            return False
        key = str(message_id)
        now = asyncio.get_running_loop().time()
        if key in self._seen:
            return True
        self._seen[key] = now
        if len(self._seen) > self.DEDUPE_MAX:
            expired = [k for k, ts in self._seen.items() if now - ts > self.DEDUPE_TTL]
            for k in expired:
                self._seen.pop(k, None)
            if len(self._seen) > self.DEDUPE_MAX:
                # 仍然过多时丢弃最早的一半
                for k in sorted(self._seen, key=self._seen.get)[: len(self._seen) // 2]:
                    self._seen.pop(k, None)
        return False

    async def _handle_group_message(self, data: dict):
        group_id = str(data.get("group_id", ""))
        user_id = str(data.get("user_id", ""))

        if settings.QQ_TARGET_GROUPS and group_id not in settings.QQ_TARGET_GROUPS:
            return

        parsed = parse_message(data)
        await self._resolve_at_names(group_id, parsed.at_targets)
        if parsed.reply_to:
            quoted = await self._fetch_quoted(parsed.reply_to)
            if quoted:
                quoted_text = quoted.text.strip()
                if quoted_text:
                    parsed.text = f"（引用：{quoted_text}）{parsed.text}".strip()
                if quoted.attachments:
                    parsed.attachments = quoted.attachments + parsed.attachments
        text = parsed.text
        sender = data.get("sender") or {}
        sender_name = str(sender.get("card") or sender.get("nickname") or "")
        logger.info(
            f"收到群消息 [{group_id}] {user_id}: {text} | "
            f"at={[t['name'] or t['qq'] for t in parsed.at_targets]}"
        )
        if self.on_group_seen:
            try:
                self.on_group_seen(group_id, user_id, text, sender_name, parsed.attachments)
            except Exception:
                logger.exception("记录群聊上下文失败")

        # 只读群：记录完就结束，任何情况下都不发言
        if group_id in settings.QQ_READONLY_GROUPS:
            return

        # 已经在这个会话的合并窗口里时，后续碎片消息不再要求触发词
        active = self._pending_key(group_id, user_id) in self._pending
        auto = False
        if not active and not self._is_group_triggered(parsed.at_bot, text):
            if not self._should_auto_participate(group_id, text):
                return
            auto = True
            # 先记冷却再入队，避免连发消息在任务完成前绕过节流
            self._last_auto_reply[group_id] = asyncio.get_running_loop().time()

        if not text and parsed.at_bot:
            text = "（只是 @ 了我一下，没有说别的）"

        text = self._truncate(text)
        if not text:
            return
        if self.on_message:
            self._enqueue(
                text, group_id=group_id, user_id=user_id,
                reply_to=parsed.reply_to, auto=auto,
                at_targets=parsed.at_targets, attachments=parsed.attachments,
            )

    async def _fetch_quoted(self, message_id: str) -> ParsedMessage | None:
        """引用回复时，把被引用消息的文字和图片也取回来。"""
        cached = self._quoted_cache.get(message_id)
        if cached is not None:
            return cached
        try:
            response = await self._call_api(
                "get_msg",
                {"message_id": int(message_id) if message_id.isdigit() else message_id},
                timeout=5,
            )
            data = response.get("data") or {}
            if not data or "message" not in data:
                return None
            quoted = parse_message(data)
        except Exception:
            logger.debug(f"获取引用消息失败: {message_id}", exc_info=True)
            return None

        if len(self._quoted_cache) > 100:
            self._quoted_cache.clear()
        self._quoted_cache[message_id] = quoted
        return quoted

    async def _resolve_at_names(self, group_id: str, targets: list[dict[str, str]]) -> None:
        """at 段没带昵称时，用群成员信息补齐；结果按 QQ 号缓存。"""
        for target in targets:
            qq = target.get("qq", "")
            if target.get("name") or not qq:
                continue
            if qq == str(settings.QQ_BOT_UID):
                continue
            cached = self._at_name_cache.get(qq)
            if cached:
                target["name"] = cached
                continue
            try:
                response = await self._call_api(
                    "get_group_member_info",
                    {
                        "group_id": int(group_id) if group_id.isdigit() else group_id,
                        "user_id": int(qq) if qq.isdigit() else qq,
                        "no_cache": False,
                    },
                    timeout=5,
                )
                data = response.get("data") or {}
                name = str(data.get("card") or data.get("nickname") or "").strip()
                if name:
                    target["name"] = name
                    self._at_name_cache[qq] = name
            except Exception:
                logger.debug(f"获取群成员昵称失败: {qq}", exc_info=True)

    def _should_auto_participate(self, group_id: str, text: str) -> bool:
        """白名单群里的旁听接话：概率触发 + 冷却，@/关键词不经过这里。"""
        if group_id not in settings.QQ_PARTICIPATION_GROUPS:
            return False
        if settings.PARTICIPATION_RATE <= 0 or len(text) < 4:
            return False

        now = asyncio.get_running_loop().time()
        if now - self._last_auto_reply.get(group_id, 0.0) < settings.PARTICIPATION_COOLDOWN_SECONDS:
            return False

        rate = settings.PARTICIPATION_RATE
        if "?" in text or "？" in text or "吗" in text:
            rate = min(1.0, rate * 2)
        return random.random() < rate

    def _is_group_triggered(self, mentioned: bool, text: str) -> bool:
        """按 GROUP_TRIGGER_MODE 判断群里这条消息要不要回应。"""
        mode = settings.GROUP_TRIGGER_MODE
        keyword = any(kw and kw in text for kw in settings.QQ_AUTO_REPLY_KEYWORDS)

        if mode == "all":
            return True
        if mode == "mention":
            return mentioned
        if mode == "keyword":
            return keyword
        return mentioned or keyword

    def _is_private_allowed(self, user_id: str) -> bool:
        """管理员始终允许；其余人必须在 QQ_PRIVATE_WHITELIST 里。"""
        if settings.QQ_PERSONA_ADMIN and user_id == settings.QQ_PERSONA_ADMIN:
            return True
        return user_id in settings.QQ_PRIVATE_WHITELIST

    async def _handle_private_message(self, data: dict):
        user_id = str(data.get("user_id", ""))
        parsed = parse_message(data)
        text = parsed.text
        logger.info(f"收到私聊消息 {user_id}: {text}")

        if not self._is_private_allowed(user_id):
            logger.debug(f"忽略非白名单私聊: {user_id}")
            return

        is_admin = bool(settings.QQ_PERSONA_ADMIN) and user_id == settings.QQ_PERSONA_ADMIN
        if is_admin and self._awaiting_feedback:
            self._awaiting_feedback = False
            if self._feedback_expired():
                await self.send_private_msg(user_id, "人设审核已超时，本次不处理。可等待今日 20:00 的再次推送。")
                return
            from app.utils.persona import persona_manager

            result = persona_manager.apply_feedback(text)
            await self.send_private_msg(user_id, result)
            return

        text = self._truncate(text)
        if not text or not self.on_message:
            return
        self._enqueue(
            text, user_id=user_id, reply_to=parsed.reply_to,
            attachments=parsed.attachments,
        )

    # ------------------------------------------------------------- 连发合并

    @staticmethod
    def _pending_key(group_id: str | None, user_id: str | None) -> str:
        # 群聊按「群 + 人」隔离，避免把别人的话并进来；私聊按人
        return f"group:{group_id}:{user_id}" if group_id else f"user:{user_id}"

    def _enqueue(self, text: str, *, group_id: str | None = None,
                 user_id: str | None = None, reply_to: str | None = None,
                 auto: bool = False, at_targets: list[dict[str, str]] | None = None,
                 attachments: list[dict[str, str]] | None = None) -> None:
        """把消息放进对应会话的缓冲，窗口结束后合并处理。

        DEBOUNCE_SECONDS<=0 时退化为立即处理。
        """
        if settings.DEBOUNCE_SECONDS <= 0:
            coro = (
                self._process_and_reply(
                    group_id, user_id or "", text, reply_to, auto, at_targets, attachments
                )
                if group_id
                else self._process_private_reply(user_id or "", text, reply_to, attachments)
            )
            self._spawn(coro)
            return

        key = self._pending_key(group_id, user_id)
        entry = self._pending.get(key)
        if entry is None:
            entry = _PendingChat(group_id=group_id, user_id=user_id, auto=auto)
            self._pending[key] = entry
        else:
            # 只要中间有一条是「被叫到」，整组就按必定回复处理
            entry.auto = entry.auto and auto

        entry.texts.append(text)
        for target in at_targets or []:
            if target not in entry.at_targets:
                entry.at_targets.append(target)
        for media in attachments or []:
            if media not in entry.attachments:
                entry.attachments.append(media)
        if reply_to:
            entry.reply_to = reply_to
        if entry.timer is not None:
            entry.timer.cancel()
            entry.timer = None

        combined = "\n".join(entry.texts)
        if (len(combined) >= settings.DEBOUNCE_MAX_CHARS
                or len(entry.texts) >= settings.DEBOUNCE_MAX_MESSAGES):
            # 够长/够多就不等了，立即处理
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
        logger.debug(f"合并 {len(entry.texts)} 条消息后处理: {text[:60]}")
        if entry.group_id:
            await self._process_and_reply(
                entry.group_id, entry.user_id or "", text, entry.reply_to,
                entry.auto, entry.at_targets, entry.attachments,
            )
        else:
            await self._process_private_reply(
                entry.user_id or "", text, entry.reply_to, entry.attachments
            )

    def _cancel_pending(self) -> None:
        for entry in self._pending.values():
            if entry.timer is not None:
                entry.timer.cancel()
        self._pending.clear()

    def _truncate(self, text: str) -> str:
        text = (text or "").strip()
        if len(text) > self.MAX_MESSAGE_LENGTH:
            logger.warning(f"消息过长已截断: {len(text)} -> {self.MAX_MESSAGE_LENGTH}")
            return text[: self.MAX_MESSAGE_LENGTH]
        return text

    # ------------------------------------------------------------ 人设审核状态

    def begin_awaiting_feedback(self):
        self._awaiting_feedback = True
        self._awaiting_since = asyncio.get_running_loop().time()

    def cancel_awaiting_feedback(self):
        self._awaiting_feedback = False

    def _feedback_expired(self) -> bool:
        ttl = settings.PERSONA_FEEDBACK_TTL_MINUTES * 60
        return (asyncio.get_running_loop().time() - self._awaiting_since) > ttl

    # ------------------------------------------------------------------ 发消息

    async def _process_and_reply(self, group_id: str, user_id: str, text: str,
                                 reply_to: str | None = None, auto: bool = False,
                                 at_targets: list[dict[str, str]] | None = None,
                                 attachments: list[dict[str, str]] | None = None):
        result = await self.on_message(text, {
            "group_id": group_id,
            "user_id": user_id,
            "reply_to_id": reply_to,
            "auto_participate": auto,
            "at_targets": at_targets or [],
            "attachments": attachments or [],
        })
        if result:
            response = await self.send_group_msg(group_id, result)
            self._notify_reply_sent(f"group:{group_id}", response)

    async def _process_private_reply(self, user_id: str, text: str,
                                     reply_to: str | None = None,
                                     attachments: list[dict[str, str]] | None = None):
        result = await self.on_message(text, {
            "user_id": user_id,
            "reply_to_id": reply_to,
            "attachments": attachments or [],
        })
        if result:
            response = await self.send_private_msg(user_id, result)
            self._notify_reply_sent(f"user:{user_id}", response)

    def _notify_reply_sent(self, chat_id: str, response: dict) -> None:
        """把平台返回的 message_id 交给上层，用于后续引用评价。"""
        if self.on_reply_sent is None:
            return
        message_id = ((response or {}).get("data") or {}).get("message_id")
        if message_id is None:
            return
        try:
            self.on_reply_sent(chat_id, str(message_id))
        except Exception:
            logger.exception("回复落库回调失败")

    def _spawn(self, coro):
        """创建后台任务并保证异常被记录，而不是静默丢失。"""
        task = asyncio.create_task(coro)
        task.add_done_callback(self._log_task_error)
        return task

    @staticmethod
    def _log_task_error(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("后台消息处理失败", exc_info=exc)

    async def _call_api(self, action: str, params: dict[str, Any],
                        timeout: float = 10) -> dict[str, Any]:
        ws = self._ws
        if ws is None or not self._connected:
            return {"status": "failed", "error": "WebSocket 未连接"}

        echo = f"{action}_{uuid.uuid4().hex}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._futures[echo] = future
        try:
            await ws.send(json.dumps({
                "action": action,
                "params": params,
                "echo": echo,
            }, ensure_ascii=False))
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"API 调用超时 [{action}]")
            return {"status": "timeout"}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"API 调用失败 [{action}]: {e}")
            return {"status": "failed", "error": str(e)}
        finally:
            self._futures.pop(echo, None)

    def _fail_pending_futures(self, reason: str):
        for future in self._futures.values():
            if not future.done():
                future.set_result({"status": "failed", "error": reason})
        self._futures.clear()

    async def send_group_msg(self, group_id: str, message: str):
        result = await self._call_api("send_group_msg", {
            "group_id": group_id,
            "message": format_mentions(message),
        })
        if result.get("status") == "failed":
            logger.error(f"群消息发送失败 [{group_id}]: {result.get('error')}")
        return result

    async def send_private_msg(self, user_id: str, message: str):
        result = await self._call_api("send_private_msg", {
            "user_id": user_id,
            "message": message,
        })
        if result.get("status") == "failed":
            logger.error(f"私聊消息发送失败 [{user_id}]: {result.get('error')}")
        return result

    async def broadcast_to_groups(self, message: str):
        results = []
        for group_id in settings.QQ_TARGET_GROUPS:
            if group_id.strip():
                r = await self.send_group_msg(group_id.strip(), message)
                results.append({"group_id": group_id, "result": r})
        return results
