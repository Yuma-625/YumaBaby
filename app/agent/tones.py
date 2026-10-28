"""管理员语气规则：加语气 / 删语气 / 语气列表，写数据库即时生效。"""

import logging
import re

from app.config import settings
from app.memory.store import MemoryStore

logger = logging.getLogger(__name__)

_MAX_KEYWORDS = 30


def _parse_keywords(raw: str) -> str:
    parts = []
    for chunk in re.split(r"[,，、/\s]+", raw or ""):
        chunk = chunk.strip()
        if chunk and chunk not in parts:
            parts.append(chunk)
    return ",".join(parts[:_MAX_KEYWORDS])


def handle_tone_admin(text: str, store: MemoryStore, user_id: str) -> str | None:
    if user_id != settings.QQ_PERSONA_ADMIN:
        return None
    value = (text or "").strip()

    if value in ("语气列表", "语气规则"):
        rules = store.list_tone_rules(enabled_only=False)
        if not rules:
            return "还没有语气规则。"
        lines = ["语气规则："]
        for rule in rules:
            state = "" if rule.get("enabled") else "（已停用）"
            lines.append(f"  {rule['tone']}{state}: {rule.get('keywords') or ''}")
        return "\n".join(lines)

    match = re.match(r"^(?:加语气|添加语气|设置语气)\s+(\S+)\s+(.+)$", value)
    if match:
        tone = match.group(1).strip()
        keywords = _parse_keywords(match.group(2))
        if not keywords:
            return "至少给一个关键词，比如：加语气 嘲讽 就这,呵呵"
        store.upsert_tone_rule(tone, keywords, enabled=True)
        return f"已加语气「{tone}」：{keywords}。下一条回复立即生效。"

    match = re.match(r"^(?:删语气|移除语气)\s+(\S+)$", value)
    if match:
        removed = store.delete_tone_rule(match.group(1).strip())
        return "已删除语气规则。" if removed else "没有这个语气规则。"

    match = re.match(r"^(?:停用语气|启用语气)\s+(\S+)$", value)
    if match:
        tone = match.group(1).strip()
        rules = {r["tone"]: r for r in store.list_tone_rules(enabled_only=False)}
        rule = rules.get(tone)
        if rule is None:
            return "没有这个语气规则。"
        enabled = value.startswith("启用")
        store.upsert_tone_rule(tone, rule.get("keywords") or "", enabled=enabled)
        return f"已{'启用' if enabled else '停用'}语气「{tone}」。"

    return None
