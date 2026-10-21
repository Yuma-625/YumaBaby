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
    QQ_WS_URL: str = os.getenv("QQ_WS_URL", "ws://127.0.0.1:6700")
    QQ_ACCESS_TOKEN: str = os.getenv("QQ_ACCESS_TOKEN", "")
    QQ_TARGET_GROUPS: list[str] = _env_list("QQ_TARGET_GROUPS")
    QQ_BOT_UID: str = os.getenv("QQ_BOT_UID", "")
    QQ_AUTO_REPLY_KEYWORDS: list[str] = _env_list("QQ_AUTO_REPLY_KEYWORDS")
    QQ_PERSONA_ADMIN: str = os.getenv("QQ_PERSONA_ADMIN", "")
    # 允许私聊的用户，逗号分隔；管理员始终允许
    QQ_PRIVATE_WHITELIST: list[str] = _env_list("QQ_PRIVATE_WHITELIST")
    # 群里什么时候响应：mention | keyword | mention_or_keyword | all
    GROUP_TRIGGER_MODE: str = os.getenv("GROUP_TRIGGER_MODE", "mention_or_keyword").strip().lower()

    # Agent（M1）
    AGENT_ENABLED: bool = _env_bool("AGENT_ENABLED", True)
    AGENT_MAX_STEPS: int = _env_int("AGENT_MAX_STEPS", 6)
    AGENT_HISTORY_TURNS: int = _env_int("AGENT_HISTORY_TURNS", 8)
    AGENT_MEMORY_LIMIT: int = _env_int("AGENT_MEMORY_LIMIT", 5)
    MEMORY_DB: str = os.getenv("MEMORY_DB", str(BASE_DIR / "data" / "memory.db"))

    # 网页抓取
    WEB_FETCH_TIMEOUT: float = float(os.getenv("WEB_FETCH_TIMEOUT", "15"))
    WEB_FETCH_MAX_BYTES: int = _env_int("WEB_FETCH_MAX_BYTES", 2_000_000)
    WEB_FETCH_MAX_CHARS: int = _env_int("WEB_FETCH_MAX_CHARS", 4000)

    # 数据 / 缓存
    STOCK_DATA_SOURCE: str = os.getenv("STOCK_DATA_SOURCE", "akshare")
    CACHE_TTL: int = _env_int("CACHE_TTL", 300)

    # 股票行情相关功能目前整体搁置：定时播报默认关闭，
    # 需要时把 MARKET_PUSH_ENABLED 设为 true 再开启。
    MARKET_PUSH_ENABLED: bool = _env_bool("MARKET_PUSH_ENABLED", False)

    # 人设审核
    PERSONA_FEEDBACK_TTL_MINUTES: int = _env_int("PERSONA_FEEDBACK_TTL_MINUTES", 180)
    PERSONA_ANALYZE_DAILY_LIMIT: int = _env_int("PERSONA_ANALYZE_DAILY_LIMIT", 20)
    PERSONA_MAX_PENDING: int = _env_int("PERSONA_MAX_PENDING", 20)

    # Log
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # LLM
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "").strip()
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-chat")
    LLM_TIMEOUT: float = float(os.getenv("LLM_TIMEOUT", "30") or 30)
    LLM_MAX_TOKENS: int = _env_int("LLM_MAX_TOKENS", 800)

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
