"""按时间词回看群聊记录，例如「昨晚发生什么」「27号0点到2点」。"""

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.config import settings

_CN_NUM = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_HOUR = r"(\d{1,2}|[零一二两三四五六七八九十]{1,3})"
_RANGE_RE = re.compile(
    rf"{_HOUR}\s*点?\s*(?:到|至|~|-|—)\s*{_HOUR}\s*点?"
)
_DAY_RE = re.compile(r"(\d{1,2})\s*[号日]")
_NIGHT_WORDS = ("半夜", "凌晨", "深夜", "夜里", "午夜")


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.TIMEZONE)
    except Exception:
        return ZoneInfo("UTC")


def _at(day, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=_tz())


def _to_int(token: str) -> int | None:
    token = (token or "").strip()
    if token.isdigit():
        return int(token)
    if token in _CN_NUM:
        return _CN_NUM[token]
    if "十" in token:
        left, _, right = token.partition("十")
        tens = _CN_NUM.get(left, 1) if left else 1
        ones = _CN_NUM.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def _target_day(value: str, now: datetime) -> date:
    match = _DAY_RE.search(value)
    if match:
        day = int(match.group(1))
        try:
            target = date(now.year, now.month, day)
        except ValueError:
            return now.date()
        if target > now.date():
            # 例如 1 号问 30 号，按上个月理解
            first = now.date().replace(day=1)
            target = first - timedelta(days=1)
            try:
                target = target.replace(day=day)
            except ValueError:
                return now.date()
        return target
    if "前天" in value:
        return now.date() - timedelta(days=2)
    if "昨天" in value and "昨晚" not in value:
        return now.date() - timedelta(days=1)
    return now.date()


def _resolve_explicit_range(value: str, now: datetime
                            ) -> tuple[datetime, datetime, str] | None:
    match = _RANGE_RE.search(value)
    if not match:
        return None
    start_h = _to_int(match.group(1))
    end_h = _to_int(match.group(2))
    if start_h is None or end_h is None:
        return None
    if not (0 <= start_h <= 24 and 0 <= end_h <= 24):
        return None

    night = (
        any(word in value for word in _NIGHT_WORDS)
        or "晚上" in value
        or "昨晚" in value
    )
    midnight = night and start_h == 12
    if midnight:
        start_h = 0
    start_h = 0 if start_h == 24 else start_h
    end_h = 0 if end_h == 24 else end_h

    target = _target_day(value, now)
    # “26号晚上12点”指的是 26 号夜里跨到 27 号 0 点
    if midnight and _DAY_RE.search(value) and "昨晚" not in value:
        target = target + timedelta(days=1)
    start = _at(target, start_h)
    end = _at(target, end_h)
    if end <= start:
        if start_h >= 18 and end_h <= 6:
            end = _at(target + timedelta(days=1), end_h)
        else:
            end = _at(target + timedelta(days=1), end_h)
    label = f"{target.month}月{target.day}日 {start_h:02d}:00-{end_h:02d}:00"
    return start, end, label


def resolve_window(text: str, now: datetime | None = None
                   ) -> tuple[datetime, datetime, str] | None:
    """把时间说法解析成区间；没有时间词返回 None。"""
    value = text or ""
    now = now or datetime.now(_tz())
    today = now.date()
    yesterday = today - timedelta(days=1)

    explicit = _resolve_explicit_range(value, now)
    if explicit is not None:
        return explicit

    if "昨晚" in value or "昨天晚上" in value:
        return _at(yesterday, 18), _at(today, 6), "昨晚"
    if "前天" in value:
        day = today - timedelta(days=2)
        return _at(day, 0), _at(day, 23, 59), "前天"
    if "昨天" in value:
        return _at(yesterday, 0), _at(yesterday, 23, 59), "昨天"
    if any(word in value for word in _NIGHT_WORDS):
        # 没写具体小时：默认今天 0 点到 6 点
        return _at(today, 0), _at(today, 6), "今天半夜"
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
