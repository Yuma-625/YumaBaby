import asyncio
import json
import logging
import re
import uuid
from contextlib import suppress
from typing import Any, Callable

import websockets
from websockets.exceptions import ConnectionClosed

from app.config import settings
from app.adapters.base import BaseAdapter

logger = logging.getLogger(__name__)

# OneBot v11 消息中的 CQ 码，例如 [CQ:at,qq=123]、[CQ:image,file=xxx]
_CQ_CODE_RE = re.compile(r"\[CQ:[^\]]*\]")
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

    def __init__(self, on_message: Callable | None = None):
        self.ws_url = settings.QQ_WS_URL
        self.access_token = settings.QQ_ACCESS_TOKEN
        self.on_message = on_message
        self._ws: Any = None
        self._running = False
        self._connected = False
        self._futures: dict[str, asyncio.Future] = {}
        self._seen: dict[str, float] = {}
        self._awaiting_feedback = False
        self._awaiting_since = 0.0

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

        await self._handle_event(data)

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
        raw_message = data.get("raw_message", "")

        if settings.QQ_TARGET_GROUPS and group_id not in settings.QQ_TARGET_GROUPS:
            return

        text = clean_message(raw_message)
        logger.info(f"收到群消息 [{group_id}] {user_id}: {text}")

        auto_reply = any(kw and kw in text for kw in settings.QQ_AUTO_REPLY_KEYWORDS)
        mentioned = bool(settings.QQ_BOT_UID) and f"[CQ:at,qq={settings.QQ_BOT_UID}" in raw_message
        if not auto_reply and not mentioned:
            return

        text = self._truncate(text)
        if not text:
            return
        if self.on_message:
            self._spawn(self._process_and_reply(group_id, user_id, text))

    async def _handle_private_message(self, data: dict):
        user_id = str(data.get("user_id", ""))
        raw_message = data.get("raw_message", "")
        text = clean_message(raw_message)
        logger.info(f"收到私聊消息 {user_id}: {text}")

        if not settings.QQ_PERSONA_ADMIN:
            return
        if user_id != settings.QQ_PERSONA_ADMIN:
            logger.debug(f"忽略非管理员私聊: {user_id}")
            return

        # 仅管理员私聊可进入人设审核流程
        if self._awaiting_feedback:
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
        self._spawn(self._process_private_reply(user_id, text))

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

    async def _process_and_reply(self, group_id: str, user_id: str, text: str):
        result = await self.on_message(text, {
            "group_id": group_id,
            "user_id": user_id,
        })
        if result:
            await self.send_group_msg(group_id, result)

    async def _process_private_reply(self, user_id: str, text: str):
        result = await self.on_message(text, {"user_id": user_id})
        if result:
            await self.send_private_msg(user_id, result)

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
            "message": message,
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
