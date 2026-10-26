"""把 QQ 消息接到 AgentLoop：取历史、取记忆、跑循环、落库。"""

import asyncio
import logging
import re

from app.agent.context import build_system_prompt
from app.agent.loop import AgentLoop
from app.config import settings
from app.memory.store import MemoryStore
from app.tools.builtin import reset_scope, set_scope

logger = logging.getLogger(__name__)

_HELP_REPLIES = {"帮助", "help", "功能"}
_WHITESPACE = re.compile(r"\s+")


def normalize_reply(text: str) -> str:
    """QQ 里不需要排版：把空行、换行、连续空格压成单个空格。"""
    return _WHITESPACE.sub(" ", text or "").strip()


def _log_task_error(task: asyncio.Task):
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("后台任务失败", exc_info=exc)


class AgentService:
    def __init__(self, loop: AgentLoop, store: MemoryStore, persona=None):
        self.loop = loop
        self.store = store
        if persona is None:
            from app.utils.persona import persona_manager
            persona = persona_manager
        self.persona = persona

    async def handle(
        self,
        text: str,
        *,
        chat_id: str,
        scope_type: str,
        scope_id: str,
        auto_participate: bool = False,
        at_targets: list[dict] | None = None,
    ) -> str:
        text = (text or "").strip()
        if not text:
            return ""

        # 确定性命令不花 token
        if text.lower() in _HELP_REPLIES:
            from app.skills.chat.handler import build_help_text
            reply = build_help_text()
            self._save(chat_id, text, reply)
            return reply

        history = self.store.recent_messages(chat_id, settings.AGENT_HISTORY_TURNS * 2)
        memories = []
        if settings.AGENT_MEMORY_LIMIT > 0:
            memories = self.store.recall(
                scope_type, scope_id, text, limit=settings.AGENT_MEMORY_LIMIT
            )

        system_prompt = build_system_prompt(self.persona.get_system_prompt(), memories)
        if at_targets:
            names = "、".join(
                str(t.get("name") or t.get("qq")) for t in at_targets if t.get("qq")
            )
            if names:
                system_prompt += f"\n\n本条消息里 @ 了：{names}。可以直接用这些名字回应。"
        if auto_participate:
            system_prompt += (
                "\n\n这是群里别人的聊天，你在旁听。只有确实有想说的、能自然接上话时才开口；"
                "不想接就只输出 [SKIP]，不要解释。"
            )
        token = set_scope((scope_type, scope_id))
        try:
            result = await self.loop.run(text, system_prompt=system_prompt, history=history)
        finally:
            reset_scope(token)

        reply = normalize_reply(result.reply)
        if auto_participate and reply.upper() in ("[SKIP]", "SKIP", "（跳过）", "跳过"):
            logger.info("群聊旁听：模型选择不接话 chat=%s", chat_id)
            try:
                self.store.add_event("skip", "auto", chat_id, {"text": text[:80]})
            except Exception:
                logger.exception("记录 skip 事件失败")
            return ""

        logger.info(
            "agent done: stopped=%s steps=%d tools=%d chat=%s",
            result.stopped, result.steps, len(result.trace), chat_id,
        )
        self._save(chat_id, text, reply)
        self._log_events(chat_id, result)
        self._maybe_learn(text, reply)
        return reply

    def _log_events(self, chat_id: str, result) -> None:
        """把这一轮做了什么写进 events，供日报和排障使用。"""
        try:
            self.store.add_event(
                "reply",
                result.stopped,
                chat_id,
                {"steps": result.steps, "tools": [t["tool"] for t in result.trace]},
                ok=result.stopped == "done",
            )
            for item in result.trace:
                self.store.add_event(
                    "tool",
                    item["tool"],
                    chat_id,
                    {
                        "ok": item["ok"],
                        "ms": item["duration_ms"],
                        "args": item["arguments"][:200],
                    },
                    ok=item["ok"],
                )
        except Exception:
            logger.exception("记录行为日志失败")

    def _maybe_learn(self, user_text: str, reply: str) -> None:
        """保留原有的人设规则提炼：触发词 + 每日上限由 persona 内部过滤。"""
        should = getattr(self.persona, "should_analyze", None)
        analyze = getattr(self.persona, "analyze_for_rules", None)
        if not callable(should) or not callable(analyze):
            return
        try:
            if not should(user_text):
                return
        except Exception:
            logger.exception("人设学习判断失败")
            return
        task = asyncio.create_task(analyze(user_text, reply))
        task.add_done_callback(_log_task_error)

    def _save(self, chat_id: str, user_text: str, reply: str) -> int | None:
        try:
            self.store.save_message(chat_id, "user", user_text)
            return self.store.save_message(chat_id, "assistant", reply)
        except Exception:
            logger.exception("保存对话失败")
            return None
