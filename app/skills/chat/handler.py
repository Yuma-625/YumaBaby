import asyncio
import logging
from typing import Any

from app.config import settings
from app.skills.base import BaseSkill
from app.utils.llm import chat as llm_chat
from app.utils.persona import persona_manager

logger = logging.getLogger(__name__)


def _log_task_error(task: asyncio.Task):
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("人设规则分析失败", exc_info=exc)


def build_help_text() -> str:
    lines = [
        "小弗功能列表:",
        "  [聊天] 直接说话即可",
        "  [记忆] 让我记住偏好，之后可以问我",
        "  [搜索] 联网查资料、新闻、版本更新",
        "  [网页] 发链接让我读一下",
        "  [文件] 管理员私聊/群聊可读写 data/sandbox",
        "  [表情] 存表情、发表情、按语气发表情",
        "  [群聊] @/关键词/随机接话、群总结、群日报",
        "  [定时] 20:00 人设建议；日报 23:40（配置后）",
    ]
    if settings.MARKET_PUSH_ENABLED:
        lines.append("  [行情] 每个交易日 9:30 / 15:00 市场简报")
    lines.append("回复「帮助」可随时查看本列表")
    return "\n".join(lines)


class ChatSkill(BaseSkill):
    name = "chat"
    description = "日常对话、消息通知"
    keywords = ["你好", "帮助", "help", "功能", "你是谁", "通知"]

    async def execute(self, message: str, context: dict[str, Any]) -> str:
        if any(k in message for k in ("帮助", "help", "功能")):
            return build_help_text()

        system_prompt = persona_manager.get_system_prompt()
        reply = await llm_chat(message, system_prompt)

        if reply:
            # 后台判断是否有新人设需求（persona 内部再按触发词/频次过滤）
            task = asyncio.create_task(persona_manager.analyze_for_rules(message, reply))
            task.add_done_callback(_log_task_error)
            return reply

        if "你好" in message or "你是谁" in message:
            return "你好！我是小弗，可以陪你聊天，也能帮你查行情（行情功能建设中）~"
        return "小弗已收到你的消息 :)"

    def _help_text(self) -> str:
        return build_help_text()
