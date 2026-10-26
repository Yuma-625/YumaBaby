"""群聊总结：把最近 N 条群消息交给模型，输出话题/结论/待办。

不走人设、不调工具，纯粹是记录整理；管理员默认专用，可配置放开。
"""

import logging
import re
import time

from app.config import settings
from app.memory.store import MemoryStore
from app.utils.llm import chat as llm_chat

logger = logging.getLogger(__name__)

_COUNT_RE = re.compile(r"(\d+)\s*条")
_last_run: dict[str, float] = {}

_SYSTEM = (
    "你是群聊记录整理助手。只依据给出的聊天记录总结，不要补充记录之外的信息。"
    "输出中文，分 2-4 点，总长不超过 200 字。"
)


def is_summary_command(text: str) -> bool:
    value = (text or "").strip()
    if len(value) > 40:
        return False
    if "总结" not in value and "汇总" not in value:
        return False
    return any(word in value for word in ("群聊", "聊天", "对话", "最近"))


def parse_count(text: str) -> int:
    match = _COUNT_RE.search(text or "")
    count = int(match.group(1)) if match else settings.SUMMARY_DEFAULT_COUNT
    return max(5, min(count, settings.SUMMARY_MAX_COUNT))


def _transcript(entries: list[dict]) -> str:
    lines = []
    for entry in entries:
        content = (entry.get("content") or "").strip()
        if not content:
            continue
        created = entry.get("created_at") or ""
        clock = created[11:16] if len(created) >= 16 else ""
        name = entry.get("sender_name") or entry.get("user_id") or "群友"
        lines.append(f"[{clock}] {name}: {content}")
    return "\n".join(lines)


async def handle_summary_command(
    text: str,
    *,
    store: MemoryStore,
    group_id: str | None,
    user_id: str,
) -> str | None:
    if not is_summary_command(text) or not group_id:
        return None

    if settings.SUMMARY_ADMIN_ONLY and user_id != settings.QQ_PERSONA_ADMIN:
        return None

    now = time.monotonic()
    if now - _last_run.get(group_id, 0.0) < settings.SUMMARY_COOLDOWN_SECONDS:
        return "刚总结过，过一会儿再来吧。"

    count = parse_count(text)
    entries = store.recent_group_context(group_id, limit=count, minutes=24 * 60)
    if len(entries) < 5:
        return f"最近只有 {len(entries)} 条消息，太少了，攒一攒再总结。"

    prompt = (
        f"下面是群 {group_id} 最近 {len(entries)} 条消息：\n\n"
        f"{_transcript(entries)}\n\n"
        "请总结：主要聊了什么、有没有结论或决定、有没有需要跟进的事。"
    )

    reply = await llm_chat(
        prompt, _SYSTEM, temperature=0.3, max_tokens=500, kind="summary"
    )
    if not reply:
        return "总结失败了，稍后再试一次。"

    _last_run[group_id] = now
    logger.info(f"群聊总结完成: group={group_id} count={len(entries)}")
    try:
        store.add_event("summary", "group", group_id,
                        {"count": len(entries), "by": user_id})
    except Exception:
        logger.exception("记录总结事件失败")
    return reply
