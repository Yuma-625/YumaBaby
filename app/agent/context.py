"""人设 + 长期记忆 + 最近对话的上下文组装。"""

from typing import Any

MEMORY_HEADER = "以下是你记得的、与本次对话相关的信息（可能过时，以用户当前说法为准）："


def build_system_prompt(base: str, memories: list[dict[str, Any]] | None = None) -> str:
    base = (base or "").strip()
    if not memories:
        return base

    lines: list[str] = []
    groups: dict[str, dict[str, Any]] = {}
    ungrouped: list[str] = []

    for item in memories:
        text = " ".join(str(item.get("text", "")).split())[:200]
        if not text:
            continue
        topic = (item.get("topic_name") or "").strip()
        if not topic:
            ungrouped.append(f"- {text}")
            continue
        group = groups.setdefault(topic, {"summary": "", "facts": []})
        if not group["summary"] and item.get("topic_summary"):
            group["summary"] = str(item["topic_summary"]).strip()[:200]
        group["facts"].append(f"- {text}")

    for topic, group in groups.items():
        head = f"[{topic}]"
        if group["summary"]:
            head += f" 摘要：{group['summary']}"
        lines.append(head)
        lines.extend(group["facts"])

    lines.extend(ungrouped)

    prefix = f"{base}\n\n{MEMORY_HEADER}"
    return prefix + "\n" + "\n".join(lines)[:1000]
