"""滚动群事件摘要：每 N 条消息异步刷新最近一段群聊的“当前事件”。"""

import asyncio
import json
import logging
import re

from app.config import settings
from app.memory.store import MemoryStore
from app.utils.llm import complete

logger = logging.getLogger(__name__)

_refresh_lock = asyncio.Lock()


def _parse_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


async def maybe_refresh_group_event(store: MemoryStore, group_id: str) -> None:
    if not settings.GROUP_EVENT_SUMMARY_ENABLED:
        return
    event = store.get_group_event(group_id)
    after_id = int((event or {}).get("up_to_id") or 0)
    if store.count_group_context_after(group_id, after_id) < max(
        1, settings.GROUP_EVENT_SUMMARY_REFRESH
    ):
        return

    async with _refresh_lock:
        # 等锁期间可能已被别的任务刷新
        event = store.get_group_event(group_id)
        after_id = int((event or {}).get("up_to_id") or 0)
        if store.count_group_context_after(group_id, after_id) < max(
            1, settings.GROUP_EVENT_SUMMARY_REFRESH
        ):
            return

        entries = store.recent_group_context(
            group_id,
            limit=max(5, settings.GROUP_EVENT_SUMMARY_WINDOW),
            minutes=max(30, settings.GROUP_CONTEXT_MINUTES),
        )
        entries = [e for e in entries if int(e.get("id") or 0) > after_id]
        if not entries:
            return

        lines = []
        for entry in entries:
            name = entry.get("sender_name") or entry.get("user_id") or "群友"
            content = str(entry.get("content") or "").replace("\n", " ").strip()
            if content:
                lines.append(f"- {name}: {content[:200]}")

        prompt = (
            "你是群聊事件整理器。只总结最近仍在进行的那一个话题，"
            "忽略已经结束的话题、寒暄、表情包和零散插话。"
            "只根据记录，不要补充没出现的信息。\n"
            "输出 JSON："
            '{"topic":"一句话话题","summary":"100字以内摘要",'
            '"participants":["名字"],"ongoing":true}\n\n'
            "群聊记录：\n" + "\n".join(lines)
        )
        response = await complete(
            [{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.2,
            kind="event_summary",
        )
        if response is None or not response.content:
            return
        data = _parse_json(response.content)
        summary = str(data.get("summary") or data.get("topic") or "").strip()
        if not summary:
            summary = response.content.strip()[:500]
        topic = str(data.get("topic") or "").strip()
        if topic and topic not in summary:
            summary = f"{topic}：{summary}"

        up_to_id = max(int(e.get("id") or 0) for e in entries)
        store.save_group_event(
            group_id, summary,
            event_json=json.dumps(data, ensure_ascii=False),
            up_to_id=up_to_id,
        )
        logger.info("群事件摘要已刷新: %s (%d 条)", group_id, len(entries))
