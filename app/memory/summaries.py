"""主题摘要（压缩）：把同一主题下的事实压成 1-2 句。"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.memory.store import MemoryStore
from app.utils.llm import chat as llm_chat

logger = logging.getLogger(__name__)

_SYSTEM = (
    "把下面这些关于同一主题的记忆压缩成 1-2 句中文摘要。"
    "只依据给出的事实，不补充、不推测，不超过 120 字。"
)


def _stale(summary_at: str | None, days: int = 7) -> bool:
    if not summary_at:
        return True
    try:
        created = datetime.fromisoformat(summary_at)
    except (TypeError, ValueError):
        return True
    try:
        tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        tz = ZoneInfo("UTC")
    return (datetime.now(tz) - created).days >= days


async def refresh_topic_summary(store: MemoryStore, topic_id: int,
                                force: bool = False) -> str | None:
    topic = store.get_topic(topic_id)
    if topic is None:
        return None
    facts = store.topic_memories(topic_id, limit=20)
    if len(facts) < settings.MEMORY_TOPIC_SUMMARY_MIN_FACTS:
        return None
    if not force and topic.get("summary") and not _stale(topic.get("summary_at")):
        return topic["summary"]

    prompt = "\n".join(f"- {fact['text']}" for fact in facts)
    summary = await llm_chat(
        prompt, _SYSTEM, temperature=0.2, max_tokens=200, kind="memory_summary"
    )
    if not summary:
        return None
    summary = " ".join(summary.split())[:300]
    store.update_topic_summary(topic_id, summary)
    logger.info("主题摘要已更新: #%s %s", topic_id, topic.get("name"))
    return summary


async def refresh_scope_topics(store: MemoryStore, scope_type: str,
                               scope_id: str) -> int:
    updated = 0
    for topic in store.list_topics(scope_type, scope_id):
        if await refresh_topic_summary(store, int(topic["id"])):
            updated += 1
    return updated
