"""管理员私聊命令：给回复打分、查看今日活动。

这些命令不经过 LLM，纯本地查询，零 token。
"""

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.memory.store import MemoryStore

logger = logging.getLogger(__name__)

_POSITIVE = {"好", "好评", "赞", "不错", "棒", "可以", "行", "满意"}
_NEGATIVE = {"不好", "差", "差评", "差劲", "不行", "烂", "垃圾", "不满意"}
_SUMMARY_COMMANDS = {"今天干了什么", "今日总结", "今天做了什么", "日报", "今天做了什么"}
_LLM_COMMANDS = {
    "llmcheck", "llm状态", "llm用量", "llm查询", "token", "token用量", "用量",
}

_PUNCT = "。！!？?~～…、,，. "


def _normalize(text: str) -> str:
    text = (text or "").strip()
    return text.strip(_PUNCT).strip()


def parse_rating(text: str) -> str | None:
    """返回 positive / negative / None。"""
    value = _normalize(text)
    if value in _POSITIVE:
        return "positive"
    if value in _NEGATIVE:
        return "negative"
    return None


def is_llm_check(text: str) -> bool:
    value = _normalize(text).lower().replace(" ", "")
    return value in _LLM_COMMANDS


def _local_time(iso_utc: str) -> str:
    try:
        moment = datetime.fromisoformat(iso_utc)
        tz = ZoneInfo(settings.TIMEZONE)
        return moment.astimezone(tz).strftime("%H:%M")
    except (TypeError, ValueError):
        return "—"


def format_llm_usage(store: MemoryStore, chat_id: str) -> str:
    from app.utils import usage

    today = usage.today_summary()
    total = usage.cumulative_summary()
    lines = ["LLM 用量"]
    lines.append(
        f"今日：{today['calls']} 次，输入 {today['prompt_tokens']:,} / "
        f"输出 {today['completion_tokens']:,} tokens，约 ¥{today['cost_cny']:.4f}"
    )
    lines.append(
        f"累计：{total['calls']} 次，输入 {total['prompt_tokens']:,} / "
        f"输出 {total['completion_tokens']:,} tokens，约 ¥{total['cost_cny']:.4f}"
    )

    context = usage.last_context(chat_id)
    if context:
        lines.append(
            f"本期上下文：最近一次请求 {context['prompt_tokens']:,} tokens"
            f"（{_local_time(context['at'])}）"
        )
    else:
        history = store.recent_messages(chat_id, settings.AGENT_HISTORY_TURNS * 2)
        text = "\n".join(item.get("content", "") for item in history)
        lines.append(
            f"本期上下文：历史 {len(history)} 条，约 "
            f"{usage.estimate_tokens(text):,} tokens（估算，尚未调用）"
        )

    lines.append(
        f"预算：软 ¥{settings.DAILY_BUDGET_SOFT:g} / 硬 ¥{settings.DAILY_BUDGET_HARD:g}，"
        f"状态 {today['level']}"
    )
    return "\n".join(lines)


def format_daily_summary(store: MemoryStore, day: str | None = None) -> str:
    summary = store.daily_summary(day)
    messages = summary["messages"]
    if not any([messages["received"], messages["sent"], summary["events"]]):
        return f"{summary['date']} 还没有任何记录。"

    lines = [f"{summary['date']} 的活动："]
    lines.append(
        f"  对话：收 {messages['received']} 条 / 发 {messages['sent']} 条，"
        f"涉及 {messages['chats']} 个会话"
    )

    tools = summary["tools"]
    replies = summary["events"].get("reply", 0)
    if tools:
        top = "、".join(f"{name}×{count}" for name, count in list(tools.items())[:6])
        lines.append(f"  回复 {replies} 次，调用工具：{top}")
    else:
        lines.append(f"  回复 {replies} 次，没有调用工具")

    feedback = summary["feedback"]
    lines.append(f"  评价：好评 {feedback['positive']} / 差评 {feedback['negative']}")

    try:
        from app.utils import usage
        cost = usage.today_summary()
        if cost["date"] == summary["date"]:
            lines.append(f"  LLM 花费：¥{cost['cost_cny']:.4f}（{cost['calls']} 次调用）")
    except Exception:
        logger.exception("读取用量失败")

    return "\n".join(lines)


def handle_admin_message(
    text: str,
    *,
    chat_id: str,
    store: MemoryStore,
    reviewer: str = "",
    reply_to_id: str | None = None,
) -> str | None:
    """命中管理员命令则返回回复文本，否则返回 None 交给 Agent。"""
    if is_llm_check(text):
        return format_llm_usage(store, chat_id)

    rating = parse_rating(text)
    if rating is not None:
        target = store.find_message_by_platform_id(reply_to_id) if reply_to_id else None
        if target is None:
            target = store.last_assistant_message(chat_id)
        if target is None:
            return "还没有可以评价的回复。"
        if target.get("role") != "assistant":
            return "只能评价我的回复。"

        store.add_feedback(target["id"], chat_id, rating, reviewer=reviewer)
        store.add_event("feedback", rating, chat_id, {"message_id": target["id"]})
        label = "好评" if rating == "positive" else "差评"
        snippet = (target.get("content") or "")[:18]
        return f"已记录{label}：{snippet}…"

    if _normalize(text) in _SUMMARY_COMMANDS:
        return format_daily_summary(store)

    return None


def extract_reply_id(raw_message: str) -> str | None:
    """从 CQ 码里取引用的消息 id，用于“引用回复 + 好评”。"""
    match = re.search(r"\[CQ:reply,id=(\d+)", raw_message or "")
    return match.group(1) if match else None
