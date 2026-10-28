"""判断一条群消息是不是在继续和机器人对话。"""

_COMMAND_WORDS = (
    "发送", "存这个", "保存这个", "收藏这个", "表情候选", "表情列表",
    "表情标签", "加群", "删群", "拒绝", "扫新群", "扫一下", "待审群",
    "群列表", "总结", "收 ", "拒 ", "llm", "/", "#",
)
_FOLLOWUP_STARTS = (
    "那", "你", "我", "为什么", "怎么", "然后", "所以", "可是", "但是",
    "还有", "不是", "真的", "继续", "再说", "刚才", "这个", "那个",
    "嗯", "哦", "对", "好", "行", "可以", "别", "再", "其实", "不过",
)
_QUESTION_WORDS = ("吗", "呢", "什么", "怎么", "为什么", "要不要", "好不好")


def looks_like_followup(text: str, last_reply: str = "") -> bool:
    value = (text or "").strip()
    if not value or len(value) > 100:
        return False
    if any(value.startswith(word) for word in _COMMAND_WORDS):
        return False
    if value.startswith("@"):
        return False

    last = (last_reply or "").strip()
    if last.endswith(("?", "？")):
        return True
    if any(word in last for word in _QUESTION_WORDS) and len(value) <= 60:
        return True

    if len(value) >= 2 and any(value.startswith(word) for word in _FOLLOWUP_STARTS):
        return True
    return len(value) <= 40 and ("?" in value or "？" in value)
