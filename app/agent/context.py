"""上下文组装：人设 + 记忆 + 最近对话。

刻意保持简单：只做截断和格式化，不做检索排序（检索在 MemoryStore 里）。
"""

from typing import Any

MEMORY_HEADER = "以下是你记得的、与本次对话相关的信息（可能过时，以用户当前说法为准）："


def build_system_prompt(base: str, memories: list[dict[str, Any]] | None = None) -> str:
    base = (base or "").strip()
    if not memories:
        return base

    lines = [base, "", MEMORY_HEADER]
    for item in memories:
        text = " ".join(str(item.get("text", "")).split())[:200]
        if text:
            lines.append(f"- {text}")
    return "\n".join(lines)
