"""LLM 用量记账与每日预算守卫。

每次调用把 token 用量追加到 JSONL，并按天聚合。预算分两级：

- 软上限：只告警，不拦截
- 硬上限：``llm.chat`` 直接跳过实际调用，由调用方走兜底文案

时区与 ``TIMEZONE`` 一致，避免“跨零点”时账单和日志对不上。
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings

logger = logging.getLogger(__name__)

# 每个日志文件一份内存聚合，避免每次调用都全量扫描文件
_cache: dict[str, dict] = {}
_cumulative_cache: dict[str, tuple[tuple[int, int], dict]] = {}
_context: dict[str, dict] = {}
_warned_hard = False
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


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
        "total_tokens": 0,
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
    bucket["total_tokens"] = bucket["prompt_tokens"] + bucket["completion_tokens"]
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
    bucket["total_tokens"] += prompt_tokens + completion_tokens
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
    _cumulative_cache.clear()
    _context.clear()
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


def cumulative_summary() -> dict:
    """全量累计用量；按文件 mtime+size 缓存，避免每次命令都扫全文件。"""
    path = _usage_path()
    empty = {
        "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "total_tokens": 0, "cost_cny": 0.0,
    }
    if not path.exists():
        return empty
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return empty
    cached = _cumulative_cache.get(str(path))
    if cached and cached[0] == signature:
        return dict(cached[1])

    result = dict(empty)
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                result["calls"] += 1
                result["prompt_tokens"] += int(rec.get("prompt_tokens", 0))
                result["completion_tokens"] += int(rec.get("completion_tokens", 0))
                result["cost_cny"] += float(rec.get("cost_cny", 0.0))
    except OSError as e:
        logger.warning(f"读取累计用量失败: {e}")
        return empty
    result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]
    result["cost_cny"] = round(result["cost_cny"], 6)
    _cumulative_cache[str(path)] = (signature, result)
    return dict(result)


def record_context(chat_id: str, prompt_tokens: int) -> None:
    """记录某个会话最近一次请求的上下文 token 数（进程内，重启清零）。"""
    if not chat_id:
        return
    _context[chat_id] = {
        "prompt_tokens": int(prompt_tokens or 0),
        "at": datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds"),
    }


def last_context(chat_id: str) -> dict | None:
    return _context.get(chat_id)


def estimate_tokens(text: str) -> int:
    """粗略估算中文 token：汉字按 1 个、其他字符按 3.5 个/ token。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    return max(1, int(cjk + (len(text) - cjk) / 3.5))
