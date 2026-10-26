"""每日群聊分析：统计发言/表情习惯，再让模型写一段观察。

只对 `GROUP_ANALYSIS_GROUPS` 里的群运行；报告私聊给管理员，不往群里发。
"""

import logging
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.memory.store import MemoryStore
from app.utils.llm import chat as llm_chat

logger = logging.getLogger(__name__)

_SYSTEM = (
    "你是群聊观察助手。只根据给出的聊天记录，总结今天聊了什么、"
    "成员说话和发表情的习惯。不要编造，不要评价个人，200 字以内，分 2-3 点。"
)


def _today() -> str:
    try:
        tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date().isoformat()


def collect_stats(entries: list[dict]) -> dict:
    users: dict[str, dict] = {}
    media_total = 0
    hourly = Counter()
    phrases = Counter()

    for entry in entries:
        content = (entry.get("content") or "").strip()
        name = entry.get("sender_name") or entry.get("user_id") or "群友"
        media = bool(entry.get("attachments")) or "[图片" in content or "[表情" in content
        if media:
            media_total += 1

        stat = users.setdefault(name, {"count": 0, "chars": 0, "media": 0})
        stat["count"] += 1
        stat["chars"] += len(content)
        stat["media"] += 1 if media else 0

        created = entry.get("created_at") or ""
        if len(created) >= 13:
            try:
                hourly[int(created[11:13])] += 1
            except ValueError:
                pass

        short = content.strip("。！!？?~～ ")
        if 2 <= len(short) <= 8:
            phrases[short] += 1

    for stat in users.values():
        stat["avg_chars"] = round(stat["chars"] / stat["count"], 1) if stat["count"] else 0

    return {
        "total": len(entries),
        "users": users,
        "media_total": media_total,
        "media_ratio": round(media_total / len(entries) * 100, 1) if entries else 0,
        "hourly": hourly.most_common(3),
        "phrases": [(p, n) for p, n in phrases.most_common(5) if n >= 3],
    }


def format_stats(group_id: str, day: str, stats: dict) -> str:
    top_users = sorted(stats["users"].items(), key=lambda kv: kv[1]["count"], reverse=True)[:8]
    user_lines = "、".join(
        f"{name} {s['count']} 条（均 {s['avg_chars']} 字，媒体 {s['media']}）"
        for name, s in top_users
    )
    hours = "、".join(f"{h}-{h + 1} 点" for h, _ in stats["hourly"]) or "无"
    phrases = "、".join(f"{p}×{n}" for p, n in stats["phrases"]) or "无明显口头禅"

    lines = [
        f"【群 {group_id} · {day}】",
        f"消息 {stats['total']} 条 / {len(stats['users'])} 人；"
        f"图片表情 {stats['media_total']} 条（{stats['media_ratio']}%）",
        f"活跃时段：{hours}",
        f"发言最多：{user_lines}",
        f"口头禅：{phrases}",
    ]
    return "\n".join(lines)


def _transcript(entries: list[dict], limit: int = 300) -> str:
    lines = []
    for entry in entries[:limit]:
        content = (entry.get("content") or "").strip()[:100]
        if not content:
            continue
        name = entry.get("sender_name") or entry.get("user_id") or "群友"
        lines.append(f"{name}: {content}")
    return "\n".join(lines)


async def build_report(store: MemoryStore, group_id: str,
                       day: str | None = None) -> str | None:
    day = day or _today()
    entries = store.group_context_for_day(group_id, day)
    if len(entries) < 3:
        logger.info(f"群 {group_id} {day} 消息太少，跳过日报")
        return None

    stats = collect_stats(entries)
    header = format_stats(group_id, day, stats)

    observation = await llm_chat(
        f"群聊记录：\n{_transcript(entries)}\n\n请总结今天的话题和成员习惯。",
        _SYSTEM,
        temperature=0.3,
        max_tokens=500,
        kind="group_analysis",
    )

    report = header
    if observation:
        report += "\n观察：\n" + observation.strip()

    store.save_group_report(group_id, day, report)
    return report
