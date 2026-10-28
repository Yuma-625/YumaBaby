"""从“跟奶坤打个招呼”里提取名字，并在本地群成员目录里查 openid。"""

import re

from app.memory.store import MemoryStore

_SKIP_NAMES = {
    "", "我", "你", "他", "她", "它", "大家", "各位", "那个人", "这个人",
    "小弗", "小鱼", "机器人", "群里", "群友",
}

_PATTERNS = (
    re.compile(
        r"(?:和|跟|给|对|帮我和|帮我跟|帮我给)\s*"
        r"([^\s，。！？、]{1,20}?)\s*"
        r"(?:打个招呼|打声招呼|问个好|问好|说句话|聊两句|说两句)"
    ),
    re.compile(
        r"(?:帮我)?(?:叫|找)(?:一下)?\s*([^\s，。！？、]{1,20})"
    ),
)


def extract_name(text: str) -> str:
    value = (text or "").strip()
    for pattern in _PATTERNS:
        match = pattern.search(value)
        if not match:
            continue
        name = match.group(1).strip("「」『』\"'“”")
        name = re.sub(r"(?:一下|一声)$", "", name).strip()
        if name and name not in _SKIP_NAMES:
            return name
    return ""


def resolve_name(store: MemoryStore, group_id: str, text: str) -> tuple[str, list[dict]]:
    name = extract_name(text)
    if not name:
        return "", []
    return name, store.search_group_members(group_id, name)
