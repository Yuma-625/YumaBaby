import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（app/config.py 的上两级），所有数据文件都以此为基准，
# 避免从不同工作目录启动时把人设写到别处。
BASE_DIR = Path(__file__).resolve().parents[1]


def _load_env():
    """优先加载项目根目录的 .env，不依赖当前工作目录。"""
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        load_dotenv(env_file)
    else:
        load_dotenv()


_load_env()


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_list(name: str, default: str = "", sep: str = ",") -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(sep) if item.strip()]


class Settings:
    APP_NAME: str = "YumaBaby"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = _env_bool("DEBUG", False)

    # Web Server
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = _env_int("PORT", 8000)

    # 时区：定时任务按此时间执行
    TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Shanghai")

    # HTTP API 鉴权：公网部署必须设置 API_TOKEN 并打开 API_AUTH_REQUIRED
    API_TOKEN: str = os.getenv("API_TOKEN", "").strip()
    API_AUTH_REQUIRED: bool = _env_bool("API_AUTH_REQUIRED", False)

    # QQ Bot
    QQ_ENABLED: bool = _env_bool("QQ_ENABLED", False)
    # onebot = NapCat/OneBot v11；official = QQ 开放平台机器人
    QQ_PROVIDER: str = os.getenv("QQ_PROVIDER", "onebot").strip().lower()
    QQ_OFFICIAL_APP_ID: str = os.getenv("QQ_OFFICIAL_APP_ID", "").strip()
    QQ_OFFICIAL_APP_SECRET: str = os.getenv("QQ_OFFICIAL_APP_SECRET", "").strip()
    QQ_OFFICIAL_API_BASE: str = os.getenv(
        "QQ_OFFICIAL_API_BASE", "https://api.bot.qq.com"
    ).rstrip("/")
    QQ_OFFICIAL_INTENTS: int = _env_int("QQ_OFFICIAL_INTENTS", 1 << 25)
    QQ_OFFICIAL_BOT_OPENID: str = os.getenv("QQ_OFFICIAL_BOT_OPENID", "").strip()
    QQ_WS_URL: str = os.getenv("QQ_WS_URL", "ws://127.0.0.1:6700")
    QQ_ACCESS_TOKEN: str = os.getenv("QQ_ACCESS_TOKEN", "")
    QQ_TARGET_GROUPS: list[str] = _env_list("QQ_TARGET_GROUPS")
    # 只读群：照常记录消息，但永不发言
    QQ_READONLY_GROUPS: list[str] = _env_list("QQ_READONLY_GROUPS")
    # 每日群聊分析（结果私聊给管理员）
    GROUP_ANALYSIS_GROUPS: list[str] = _env_list("GROUP_ANALYSIS_GROUPS")
    QQ_BOT_UID: str = os.getenv("QQ_BOT_UID", "")
    QQ_AUTO_REPLY_KEYWORDS: list[str] = _env_list("QQ_AUTO_REPLY_KEYWORDS")
    QQ_PERSONA_ADMIN: str = os.getenv("QQ_PERSONA_ADMIN", "")
    # 允许私聊的用户，逗号分隔；管理员始终允许
    QQ_PRIVATE_WHITELIST: list[str] = _env_list("QQ_PRIVATE_WHITELIST")
    # 群里什么时候响应：mention | keyword | mention_or_keyword | all
    GROUP_TRIGGER_MODE: str = os.getenv("GROUP_TRIGGER_MODE", "mention_or_keyword").strip().lower()
    # 按昵称找到人后，回复里是否真的 @ 对方（默认只称呼名字）
    GROUP_NAME_MENTION: bool = _env_bool("GROUP_NAME_MENTION", False)
    # 关键词触发也要判断是不是在称呼机器人（避免“小弗真安静”这种第三人称误触发）
    GROUP_KEYWORD_ADDRESS_ONLY: bool = _env_bool(
        "GROUP_KEYWORD_ADDRESS_ONLY", True
    )
    # 机器人刚回过某人后，短时间内把同一个人的后续消息当作续聊
    FOLLOWUP_ENABLED: bool = _env_bool("FOLLOWUP_ENABLED", True)
    FOLLOWUP_WINDOW_SECONDS: int = _env_int("FOLLOWUP_WINDOW_SECONDS", 120)
    FOLLOWUP_MAX_TURNS: int = _env_int("FOLLOWUP_MAX_TURNS", 3)
    FOLLOWUP_GROUP_COOLDOWN_SECONDS: int = _env_int(
        "FOLLOWUP_GROUP_COOLDOWN_SECONDS", 3
    )
    # 自动旁听接话的群（@ 与关键词仍必定回复）
    QQ_PARTICIPATION_GROUPS: list[str] = _env_list("QQ_PARTICIPATION_GROUPS")
    PARTICIPATION_RATE: float = float(os.getenv("PARTICIPATION_RATE", "0.15") or 0)
    PARTICIPATION_QUESTION_ONLY: bool = _env_bool(
        "PARTICIPATION_QUESTION_ONLY", True
    )
    PARTICIPATION_COOLDOWN_SECONDS: int = _env_int("PARTICIPATION_COOLDOWN_SECONDS", 120)
    # 群聊短期上下文：群里最近消息的滚动缓冲（不进数据集）
    GROUP_CONTEXT_ENABLED: bool = _env_bool("GROUP_CONTEXT_ENABLED", True)
    GROUP_CONTEXT_MAX: int = _env_int("GROUP_CONTEXT_MAX", 300)
    GROUP_CONTEXT_MINUTES: int = _env_int("GROUP_CONTEXT_MINUTES", 30)
    GROUP_CONTEXT_INJECT: int = _env_int("GROUP_CONTEXT_INJECT", 10)
    GROUP_EVENT_SUMMARY_ENABLED: bool = _env_bool("GROUP_EVENT_SUMMARY_ENABLED", True)
    GROUP_EVENT_SUMMARY_WINDOW: int = _env_int("GROUP_EVENT_SUMMARY_WINDOW", 20)
    GROUP_EVENT_SUMMARY_REFRESH: int = _env_int("GROUP_EVENT_SUMMARY_REFRESH", 10)
    # 只读/分析群按天保留，避免活跃群 300 条几十分钟就被覆盖
    GROUP_CONTEXT_KEEP_DAYS: int = _env_int("GROUP_CONTEXT_KEEP_DAYS", 3)
    # 问“昨晚/昨天”时，按时间回看群聊记录
    GROUP_HISTORY_LIMIT: int = _env_int("GROUP_HISTORY_LIMIT", 60)
    GROUP_HISTORY_CHARS: int = _env_int("GROUP_HISTORY_CHARS", 3000)
    # 群聊总结
    SUMMARY_ADMIN_ONLY: bool = _env_bool("SUMMARY_ADMIN_ONLY", False)
    SUMMARY_DEFAULT_COUNT: int = _env_int("SUMMARY_DEFAULT_COUNT", 50)
    SUMMARY_MAX_COUNT: int = _env_int("SUMMARY_MAX_COUNT", 120)
    SUMMARY_COOLDOWN_SECONDS: int = _env_int("SUMMARY_COOLDOWN_SECONDS", 600)
    SUMMARY_DAILY_LIMIT: int = _env_int("SUMMARY_DAILY_LIMIT", 10)
    # 表情库
    STICKERS_DIR: str = os.getenv("STICKERS_DIR", str(BASE_DIR / "data" / "stickers"))
    STICKER_CANDIDATES_DIR: str = os.getenv(
        "STICKER_CANDIDATES_DIR", str(BASE_DIR / "data" / "sticker_candidates")
    )
    STICKER_MAX_BYTES: int = _env_int("STICKER_MAX_BYTES", 5_000_000)
    STICKER_LOOKBACK_MINUTES: int = _env_int("STICKER_LOOKBACK_MINUTES", 10)
    # 图片来源收集：默认关闭，先只做底子
    STICKER_COLLECT_ENABLED: bool = _env_bool("STICKER_COLLECT_ENABLED", False)
    STICKER_COLLECT_GROUPS: list[str] = _env_list("STICKER_COLLECT_GROUPS")
    STICKER_COLLECT_USERS: list[str] = _env_list("STICKER_COLLECT_USERS")
    STICKER_COLLECT_MAX_PER_DAY: int = _env_int("STICKER_COLLECT_MAX_PER_DAY", 20)
    # 发送策略：manual=只响应指令；future: random/keyword
    STICKER_SEND_MODE: str = os.getenv("STICKER_SEND_MODE", "manual").strip().lower()
    STICKER_SEND_CHANCE: float = float(os.getenv("STICKER_SEND_CHANCE", "0") or 0)
    STICKER_SEND_COOLDOWN_SECONDS: int = _env_int("STICKER_SEND_COOLDOWN_SECONDS", 900)
    STICKER_SEND_DAILY_LIMIT: int = _env_int("STICKER_SEND_DAILY_LIMIT", 15)
    STICKER_SEND_MIN_CHARS: int = _env_int("STICKER_SEND_MIN_CHARS", 4)
    STICKER_SEND_GROUPS: list[str] = _env_list("STICKER_SEND_GROUPS")
    # 语气 -> 指定表情，格式：无语:大狗无语,开心:奶龙钓鱼
    STICKER_TONE_MAP: str = os.getenv(
        "STICKER_TONE_MAP", "无语:大狗无语"
    ).strip()
    # 连发合并：同一会话在这个窗口内的多条消息合并成一次处理；0 = 关闭
    DEBOUNCE_SECONDS: float = float(os.getenv("DEBOUNCE_SECONDS", "1.5") or 0)
    DEBOUNCE_MAX_CHARS: int = _env_int("DEBOUNCE_MAX_CHARS", 400)
    DEBOUNCE_MAX_MESSAGES: int = _env_int("DEBOUNCE_MAX_MESSAGES", 5)

    # Agent（M1）
    AGENT_ENABLED: bool = _env_bool("AGENT_ENABLED", True)
    AGENT_MAX_STEPS: int = _env_int("AGENT_MAX_STEPS", 6)
    AGENT_MAX_TOKENS: int = _env_int("AGENT_MAX_TOKENS", 300)
    AGENT_HISTORY_TURNS: int = _env_int("AGENT_HISTORY_TURNS", 8)
    AGENT_MEMORY_LIMIT: int = _env_int("AGENT_MEMORY_LIMIT", 5)
    # 会话压缩：超过阈值后把旧对话压成摘要，只保留最近若干条原文
    HISTORY_COMPACT_ENABLED: bool = _env_bool("HISTORY_COMPACT_ENABLED", False)
    HISTORY_COMPACT_AFTER: int = _env_int("HISTORY_COMPACT_AFTER", 30)
    HISTORY_COMPACT_KEEP: int = _env_int("HISTORY_COMPACT_KEEP", 12)
    # 沙箱文件工具：只在管理员私聊可用，路径限制在 SANDBOX_ROOT
    SANDBOX_ENABLED: bool = _env_bool("SANDBOX_ENABLED", False)
    SANDBOX_ROOT: str = os.getenv("SANDBOX_ROOT", str(BASE_DIR / "data" / "sandbox"))
    SANDBOX_MAX_READ_BYTES: int = _env_int("SANDBOX_MAX_READ_BYTES", 200_000)
    SANDBOX_MAX_WRITE_BYTES: int = _env_int("SANDBOX_MAX_WRITE_BYTES", 1_000_000)
    MEMORY_DECAY_DAYS: int = _env_int("MEMORY_DECAY_DAYS", 30)
    MEMORY_MAX_PER_SCOPE: int = _env_int("MEMORY_MAX_PER_SCOPE", 200)
    MEMORY_DB: str = os.getenv("MEMORY_DB", str(BASE_DIR / "data" / "memory.db"))
    # 记忆提炼 / 层级（默认关闭，先在本地验证）
    MEMORY_EXTRACT_ENABLED: bool = _env_bool("MEMORY_EXTRACT_ENABLED", False)
    MEMORY_EXTRACT_EVERY: int = _env_int("MEMORY_EXTRACT_EVERY", 10)
    MEMORY_EXTRACT_MAX_MESSAGES: int = _env_int("MEMORY_EXTRACT_MAX_MESSAGES", 40)
    MEMORY_EXTRACT_DAILY_LIMIT: int = _env_int("MEMORY_EXTRACT_DAILY_LIMIT", 20)
    MEMORY_HIERARCHY_ENABLED: bool = _env_bool("MEMORY_HIERARCHY_ENABLED", False)
    AGENT_MEMORY_TOPICS: int = _env_int("AGENT_MEMORY_TOPICS", 3)
    MEMORY_TOPICS_MAX: int = _env_int("MEMORY_TOPICS_MAX", 50)
    MEMORY_TOPIC_SUMMARY_MIN_FACTS: int = _env_int("MEMORY_TOPIC_SUMMARY_MIN_FACTS", 5)
    # 向量检索（OpenAI 兼容 embedding；DeepSeek 本身没有 embedding）
    EMBEDDING_ENABLED: bool = _env_bool("EMBEDDING_ENABLED", False)
    EMBEDDING_BASE_URL: str = os.getenv("EMBEDDING_BASE_URL", "").strip()
    EMBEDDING_API_KEY: str = os.getenv("EMBEDDING_API_KEY", "").strip()
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3").strip()
    EMBEDDING_DIM: int = _env_int("EMBEDDING_DIM", 1024)
    EMBEDDING_TIMEOUT: float = float(os.getenv("EMBEDDING_TIMEOUT", "15") or 15)
    DATASETS_DIR: str = os.getenv("DATASETS_DIR", str(BASE_DIR / "data" / "datasets"))
    REVIEW_HOST: str = os.getenv("REVIEW_HOST", "127.0.0.1")
    REVIEW_PORT: int = _env_int("REVIEW_PORT", 8100)

    # 网页抓取
    WEB_FETCH_TIMEOUT: float = float(os.getenv("WEB_FETCH_TIMEOUT", "15"))
    WEB_FETCH_MAX_BYTES: int = _env_int("WEB_FETCH_MAX_BYTES", 2_000_000)
    WEB_FETCH_MAX_CHARS: int = _env_int("WEB_FETCH_MAX_CHARS", 4000)
    WEB_SEARCH_PROVIDER: str = os.getenv("WEB_SEARCH_PROVIDER", "deepseek").strip().lower()

    # 图片理解
    VISION_ENABLED: bool = _env_bool("VISION_ENABLED", True)
    VISION_MAX_IMAGES: int = _env_int("VISION_MAX_IMAGES", 2)
    VISION_CONTEXT_IMAGES: int = _env_int("VISION_CONTEXT_IMAGES", 1)
    VISION_MAX_BYTES: int = _env_int("VISION_MAX_BYTES", 5_000_000)
    VISION_TIMEOUT: float = float(os.getenv("VISION_TIMEOUT", "15"))

    # 数据 / 缓存
    STOCK_DATA_SOURCE: str = os.getenv("STOCK_DATA_SOURCE", "akshare")
    CACHE_TTL: int = _env_int("CACHE_TTL", 300)

    # 股票行情相关功能目前整体搁置：定时播报默认关闭，
    # 需要时把 MARKET_PUSH_ENABLED 设为 true 再开启。
    MARKET_PUSH_ENABLED: bool = _env_bool("MARKET_PUSH_ENABLED", False)

    # 人设审核
    PERSONA_FEEDBACK_TTL_MINUTES: int = _env_int("PERSONA_FEEDBACK_TTL_MINUTES", 180)
    # 20:00 人设推送前，若最近 N 秒有对话则跳过；0 = 不检查，照常推送
    PERSONA_PUSH_IDLE_SECONDS: int = _env_int("PERSONA_PUSH_IDLE_SECONDS", 600)
    PERSONA_ANALYZE_DAILY_LIMIT: int = _env_int("PERSONA_ANALYZE_DAILY_LIMIT", 20)
    PERSONA_MAX_PENDING: int = _env_int("PERSONA_MAX_PENDING", 20)

    # Log
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
    LOG_FILE: str = os.getenv("LOG_FILE", str(BASE_DIR / "data" / "logs" / "yumababy.log"))
    LOG_MAX_BYTES: int = _env_int("LOG_MAX_BYTES", 5_000_000)
    LOG_BACKUPS: int = _env_int("LOG_BACKUPS", 5)

    # LLM
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "").strip()
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-flash")
    LLM_TIMEOUT: float = float(os.getenv("LLM_TIMEOUT", "30") or 30)
    LLM_MAX_TOKENS: int = _env_int("LLM_MAX_TOKENS", 800)
    # V4.1-Flash 默认开思考；聊天机器人默认关，降低延迟并避免工具调用回传 reasoning_content
    LLM_THINKING: str = os.getenv("LLM_THINKING", "disabled").strip().lower()

    # 用量与预算（人民币元；价格按百万 token 计）
    LLM_PRICE_INPUT_PER_M: float = float(os.getenv("LLM_PRICE_INPUT_PER_M", "2"))
    LLM_PRICE_OUTPUT_PER_M: float = float(os.getenv("LLM_PRICE_OUTPUT_PER_M", "8"))
    DAILY_BUDGET_SOFT: float = float(os.getenv("DAILY_BUDGET_SOFT", "10"))
    DAILY_BUDGET_HARD: float = float(os.getenv("DAILY_BUDGET_HARD", "30"))
    USAGE_LOG: str = os.getenv("USAGE_LOG", str(BASE_DIR / "data" / "usage.jsonl"))

    @property
    def llm_configured(self) -> bool:
        """是否配置了看起来可用的 LLM Key（占位符视为未配置）。"""
        key = self.LLM_API_KEY
        if not key:
            return False
        lowered = key.lower()
        return not any(marker in lowered for marker in ("your-", "xxx", "changeme", "placeholder"))

    @property
    def host_is_loopback(self) -> bool:
        return self.HOST in ("127.0.0.1", "localhost", "::1")


settings = Settings()
