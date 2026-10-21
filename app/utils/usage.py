"""LLM 用量记账与每日预算守卫。

每次调用把 token 用量追加到 JSONL，并按天聚合。预算分两级：

- 软上限：只告警，不拦截
- 硬上限：``llm.chat`` 直接跳过实际调用，由调用方走兜底文案

时区与 ``TIMEZONE`` 一致，避免“跨零点”时账单和日志对不上。
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings

logger = logging.getLogger(__name__)

# 每个日志文件一份内存聚合，避免每次调用都全量扫描文件
_cache: dict[str, dict] = {}
_warned_hard = False


def _today() -> str:
    try:
        tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date().isoformat()


def _usage_path() -> Path:
    return Path(settings.USAGE_LOG)


def estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """按配置单价估算费用（元）。"""
    return (
        prompt_tokens / 1_000_000 * settings.LLM_PRICE_INPUT_PER_M
        + completion_tokens / 1_000_000 * settings.LLM_PRICE_OUTPUT_PER_M
    )


def _empty_bucket(day: str) -> dict:
    return {
        "date": day,
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_cny": 0.0,
        "by_kind": {},
    }


def _scan(day: str, path: Path) -> dict:
    bucket = _empty_bucket(day)
    if not path.exists():
        return bucket

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("date") != day:
                    continue
                bucket["calls"] += 1
                bucket["prompt_tokens"] += int(rec.get("prompt_tokens", 0))
                bucket["completion_tokens"] += int(rec.get("completion_tokens", 0))
                bucket["cost_cny"] += float(rec.get("cost_cny", 0.0))
                kind = rec.get("kind", "chat")
                k = bucket["by_kind"].setdefault(kind, {"calls": 0, "cost_cny": 0.0})
                k["calls"] += 1
                k["cost_cny"] += float(rec.get("cost_cny", 0.0))
    except OSError as e:
        logger.warning(f"读取用量日志失败: {e}")
    return bucket


def _bucket(path: Path) -> dict:
    key = str(path)
    day = _today()
    bucket = _cache.get(key)
    if bucket is None or bucket["date"] != day:
        bucket = _scan(day, path)
        _cache[key] = bucket
    return bucket


def record_usage(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    kind: str = "chat",
) -> dict:
    """追加一条用量记录并更新当天聚合。"""
    path = _usage_path()
    day = _today()
    cost = estimate_cost(prompt_tokens, completion_tokens)
    rec = {
        "date": day,
        "ts": datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds"),
        "model": model,
        "kind": kind,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_cny": round(cost, 6),
    }

    bucket = _bucket(path)
    bucket["calls"] += 1
    bucket["prompt_tokens"] += prompt_tokens
    bucket["completion_tokens"] += completion_tokens
    bucket["cost_cny"] += cost
    kind_stat = bucket["by_kind"].setdefault(kind, {"calls": 0, "cost_cny": 0.0})
    kind_stat["calls"] += 1
    kind_stat["cost_cny"] += cost

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning(f"写入用量日志失败: {e}")
    return rec


def budget_level() -> str:
    """返回 ok / soft / hard。"""
    cost = _bucket(_usage_path())["cost_cny"]
    if settings.DAILY_BUDGET_HARD > 0 and cost >= settings.DAILY_BUDGET_HARD:
        return "hard"
    if settings.DAILY_BUDGET_SOFT > 0 and cost >= settings.DAILY_BUDGET_SOFT:
        return "soft"
    return "ok"


def reset_state():
    """清空内存聚合（测试或跨天重置用）。"""
    global _warned_hard
    _cache.clear()
    _warned_hard = False


def should_block_call() -> bool:
    """硬上限命中时拦截调用；只告警一次，避免刷屏。"""
    global _warned_hard
    if budget_level() != "hard":
        return False
    if not _warned_hard:
        _warned_hard = True
        logger.warning(
            f"今日 LLM 花费已达硬上限 ¥{settings.DAILY_BUDGET_HARD}，"
            "暂停调用（修改 DAILY_BUDGET_HARD 或等明天）"
        )
    return True


def today_summary() -> dict:
    """给 /health 用的当日汇总。"""
    bucket = dict(_bucket(_usage_path()))
    bucket["cost_cny"] = round(bucket["cost_cny"], 6)
    bucket["by_kind"] = {
        k: {"calls": v["calls"], "cost_cny": round(v["cost_cny"], 6)}
        for k, v in bucket["by_kind"].items()
    }
    bucket["soft_limit_cny"] = settings.DAILY_BUDGET_SOFT
    bucket["hard_limit_cny"] = settings.DAILY_BUDGET_HARD
    bucket["level"] = budget_level()
    return bucket
