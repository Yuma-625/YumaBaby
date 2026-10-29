"""推断群消息的对象：这句话是在对谁说。"""

import re

from app.config import settings

_AT_NAME_RE = re.compile(r"@([^\s@，。！？、]{1,20})")


def _bot_names() -> set[str]:
    names = {"小弗", "小鱼", "小弗 bot"}
    names.update(kw for kw in settings.QQ_AUTO_REPLY_KEYWORDS if kw)
    return names


def infer_addressee(entries: list[dict], current_sender: str, text: str) -> str:
    """返回这句话最可能对谁说的；空字符串表示无法判断。

    entries 是按时间正序的群上下文，可能包含当前这条。
    """
    current = (text or "").strip()
    prior: list[dict] = []
    for entry in entries:
        content = str(entry.get("content") or "").strip()
        sender = str(entry.get("sender_name") or entry.get("user_id") or "")
        if content == current and sender == current_sender:
            continue
        prior.append(entry)

    # 当前消息自己带了字面量 @
    match = _AT_NAME_RE.search(current)
    if match:
        name = match.group(1)
        if name not in _bot_names():
            return name

    # 往前找最近的 @ 对象（例如 A 先 @B，再说“过来伺候我”）
    for entry in reversed(prior[-10:]):
        content = str(entry.get("content") or "")
        for name in _AT_NAME_RE.findall(content):
            if name not in _bot_names():
                return name

    # 没有 @ 时，默认对象是上一句的发言人
    if prior:
        previous = str(
            prior[-1].get("sender_name") or prior[-1].get("user_id") or ""
        )
        if previous and previous != current_sender and previous not in _bot_names():
            return previous

    return ""
