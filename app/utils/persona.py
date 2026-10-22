import json
import logging
import os
import re
import tempfile
from datetime import date
from pathlib import Path

from app.config import BASE_DIR, settings

logger = logging.getLogger(__name__)

DATA_DIR = BASE_DIR / "data"
PERSONA_FILE = DATA_DIR / "persona.json"

DEFAULT_BASE = [
    "你是小鱼，一个可爱、友好的助手机器人。",
    "你擅长日常闲聊，也能记住用户说过的事、读取网页内容。",
    "回答风格：简洁、亲切，像朋友聊天一样，不要长篇大论。偶尔有一点小毒舌，小傲娇，但为人可爱",
    "像真人发消息一样说话：不要每条都带 emoji 或颜文字。多数回复一个都不带，整段最多一个；用户先发表情时才跟着用。",
    "少堆语气词：不要「～」「呀」「哦」「啦」连用，正常句子收尾即可。",
    "股票、基金等行情数据尚未接入：遇到这类请求直接说明暂时查不了，绝不编造价格或涨跌。",
    "不要承诺「稍后帮你查」「我去看看」这类当下做不到的事；做不到就直说做不到。",
    "自称「小鱼」或「我」。",
]

# 只有消息里出现这些词时，才值得花一次 LLM 调用去提炼人设规则，
# 否则每句闲聊都会触发一次额外请求。
_RULE_TRIGGERS = (
    "以后", "今后", "下次", "每次", "记住", "记得", "不要", "别", "请",
    "叫我", "称呼", "喜欢", "讨厌", "希望", "能不能", "可以", "改成",
    "语气", "风格", "说话", "回复", "人设", "设定", "规则", "习惯",
)

# 明显是「没有规则」的模型回复
_NEGATIVE_REPLIES = ("无", "没有", "无新规则", "没有新规则", "暂无", "没有发现")

_QUOTE_CHARS = "\"'“”‘’「」『』 \t\r\n"


class PersonaManager:
    def __init__(self, data_file: Path | None = None):
        self.data_file = Path(data_file) if data_file else PERSONA_FILE
        self.base: list[str] = []
        self.evolved: list[dict] = []      # {"rule": str, "date": str}
        self.pending: list[dict] = []      # {"rule": str, "date": str, "source": str}
        self._analyze_day = ""
        self._analyze_count = 0

    # ------------------------------------------------------------------ 持久化

    def load(self):
        if self.data_file.exists():
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.base = data.get("base") or DEFAULT_BASE
                self.evolved = data.get("evolved", [])
                self.pending = data.get("pending", [])
            except Exception as e:
                logger.error(f"加载人设失败，使用默认设定: {e}")
                self.base = list(DEFAULT_BASE)
        else:
            self.base = list(DEFAULT_BASE)
        return self

    def save(self):
        """原子写入，避免进程中断时把人设文件写坏。"""
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "base": self.base,
            "evolved": self.evolved,
            "pending": self.pending,
        }
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.data_file.parent), prefix=".persona-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.data_file)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ 读取

    def get_system_prompt(self) -> str:
        lines = list(self.base)
        for item in self.evolved:
            lines.append(item["rule"])
        return "\n".join(lines)

    @property
    def pending_count(self) -> int:
        return len(self.pending)

    def get_today_pending(self) -> list[dict]:
        today = date.today().isoformat()
        return [p for p in self.pending if p.get("date") == today]

    def get_pending_summary(self) -> str:
        if not self.pending:
            return "暂无待审核的人设变更。"

        lines = [f"待审核人设变更（共 {len(self.pending)} 条）："]
        for i, item in enumerate(self.pending, start=1):
            source = (item.get("source") or "").strip() or "未知来源"
            lines.append(f"  [{i}] {item['rule']}  (日期: {item.get('date', '?')} / 来源: {source})")
        lines.append("")
        lines.append("回复「保留 1 3」或「删除 2」操作，也可「全部保留」「全部删除」。")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 写入

    def add_pending(self, rule: str, source: str) -> bool:
        """收录一条待审核规则，返回是否真的新增。"""
        rule = (rule or "").strip()
        if not rule:
            return False

        # 去重
        for item in self.pending + self.evolved:
            if item.get("rule", "").strip() == rule:
                return False

        if len(self.pending) >= settings.PERSONA_MAX_PENDING:
            logger.warning(
                f"待审核人设已达上限 {settings.PERSONA_MAX_PENDING} 条，丢弃新规则: {rule}"
            )
            return False

        self.pending.append({
            "rule": rule,
            "date": date.today().isoformat(),
            "source": (source or "")[:50],
        })
        self.save()
        logger.info(f"新增待审核人设: {rule}")
        return True

    # ------------------------------------------------------------------ 规则提炼

    def should_analyze(self, user_msg: str) -> bool:
        """判断这条消息是否值得调用 LLM 提炼人设规则。"""
        text = (user_msg or "").strip()
        if not 4 <= len(text) <= 200:
            return False
        if not any(trigger in text for trigger in _RULE_TRIGGERS):
            return False
        if len(self.pending) >= settings.PERSONA_MAX_PENDING:
            return False

        today = date.today().isoformat()
        if self._analyze_day != today:
            self._analyze_day = today
            self._analyze_count = 0
        return self._analyze_count < settings.PERSONA_ANALYZE_DAILY_LIMIT

    async def analyze_for_rules(self, user_msg: str, bot_reply: str):
        """用 LLM 分析对话中是否有新的行为规则需求（后台调用，失败静默）。"""
        if not self.should_analyze(user_msg):
            return
        self._analyze_count += 1

        from app.utils.llm import chat as llm_chat

        prompt = f"""分析以下对话，判断用户是否对机器人提出了新的行为偏好或人设要求。
如果有，提取为一条不超过 30 字的简短规则（如「用户希望用颜文字」「称呼用户为老板」）。
如果没有任何新规则需求，只回复「无」。

用户消息: {user_msg}
机器人回复: {bot_reply}

只回复规则或「无」，不要解释。"""

        result = await llm_chat(prompt, temperature=0.2, max_tokens=80, kind="analysis")
        rule = self._normalize_rule(result)
        if rule:
            self.add_pending(rule, user_msg[:50])

    @staticmethod
    def _normalize_rule(raw: str | None) -> str | None:
        if not raw:
            return None
        rule = raw.strip().strip(_QUOTE_CHARS)
        # 只取第一行
        rule = rule.splitlines()[0].strip() if rule else ""
        if not rule or len(rule) < 2 or len(rule) > 60:
            return None
        if rule in _NEGATIVE_REPLIES:
            return None
        if any(rule.startswith(neg) for neg in _NEGATIVE_REPLIES) and len(rule) <= 6:
            return None
        if rule.startswith(("规则", "规则：")):
            rule = re.sub(r"^规则[:：]?\s*", "", rule).strip()
        return rule or None

    # ------------------------------------------------------------------ 审核反馈

    def apply_feedback(self, reply: str) -> str:
        reply = (reply or "").strip()
        today = date.today().isoformat()

        if not self.pending:
            return "当前没有待审核的人设变更。"

        if any(k in reply for k in ("全部保留", "全部通过", "都保留", "都要")):
            for item in self.pending:
                self.evolved.append({"rule": item["rule"], "date": today})
            count = len(self.pending)
            self.pending.clear()
            self.save()
            return f"已保留全部 {count} 条人设。"

        if any(k in reply for k in ("全部删除", "全部拒绝", "都不要", "都删除")):
            count = len(self.pending)
            self.pending.clear()
            self.save()
            return f"已删除全部 {count} 条人设。"

        keep_indices, remove_indices = self._parse_indices(reply)
        if not keep_indices and not remove_indices:
            return "未识别到操作，请用「保留 1 3」或「删除 2」格式。"

        remaining = []
        moved = removed = 0
        for i, item in enumerate(self.pending, start=1):
            if i in keep_indices:
                self.evolved.append({"rule": item["rule"], "date": today})
                moved += 1
            elif i in remove_indices:
                removed += 1
            else:
                remaining.append(item)

        self.pending = remaining
        self.save()
        return f"已处理 — 保留 {moved} 条，删除 {removed} 条，剩余 {len(remaining)} 条待审核。"

    @staticmethod
    def _parse_indices(reply: str) -> tuple[set[int], set[int]]:
        keep: set[int] = set()
        remove: set[int] = set()

        for part in re.findall(r"(?:保留|留下|通过)\s*([\d\s,，、]+)", reply):
            keep.update(int(n) for n in re.split(r"[,\s，、]+", part) if n.isdigit())

        for part in re.findall(r"(?:删除|移除|去掉|不要)\s*([\d\s,，、]+)", reply):
            remove.update(int(n) for n in re.split(r"[,\s，、]+", part) if n.isdigit())

        # 只回一串序号（如「1 3」）时按「保留」处理
        if not keep and not remove and re.fullmatch(r"[\d\s,，、]+", reply):
            keep.update(int(n) for n in re.split(r"[,\s，、]+", reply) if n.isdigit())

        return keep, remove


persona_manager = PersonaManager().load()
