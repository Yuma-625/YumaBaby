"""按时间词回看群聊记录，例如「昨晚发生什么」。"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.config import settings


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.TIMEZONE)
    except Exception:
        return ZoneInfo("UTC")


def _at(day, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=_tz())


def resolve_window(text: str, now: datetime | None = None
                   ) -> tuple[datetime, datetime, str] | None:
    """把「昨晚/昨天/今早…」解析成时间区间；没有时间词返回 None。"""
    value = text or ""
    now = now or datetime.now(_tz())
    today = now.date()
    yesterday = today - timedelta(days=1)

    if "昨晚" in value or "昨天晚上" in value:
        return _at(yesterday, 18), _at(today, 6), "昨晚"
    if "前天" in value:
        day = today - timedelta(days=2)
        return _at(day, 0), _at(day, 23, 59), "前天"
    if "昨天" in value:
        return _at(yesterday, 0), _at(yesterday, 23, 59), "昨天"
    if "今早" in value or "今天早上" in value or "早上" in value:
        return _at(today, 5), _at(today, 12), "今天早上"
    if "中午" in value:
        return _at(today, 11), _at(today, 14), "今天中午"
    if "下午" in value:
        return _at(today, 12), _at(today, 18), "今天下午"
    if "晚上" in value:
        return _at(today, 18), _at(today, 23, 59), "今天晚上"
    if "刚才" in value or "刚刚" in value:
        return now - timedelta(hours=1), now, "刚才"
    return None


def format_history(entries: list[dict], max_chars: int = 3000) -> str:
    lines = []
    for entry in entries:
        created = entry.get("created_at") or ""
        clock = created[11:16] if len(created) >= 16 else ""
        name = entry.get("sender_name") or entry.get("user_id") or "群友"
        content = (entry.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"[{clock}] {name}: {content[:200]}")
    text = "\n".join(lines)
    return text[-max_chars:] if len(text) > max_chars else text
