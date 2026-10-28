import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import settings

logger = logging.getLogger(settings.APP_NAME)

_configured = False


def setup_logging():
    """幂等配置日志：控制台 + 滚动文件，并压掉第三方库的噪声。"""
    global _configured
    if _configured:
        return
    _configured = True

    level = getattr(logging, settings.LOG_LEVEL, logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        path = Path(settings.LOG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=max(100_000, settings.LOG_MAX_BYTES),
            backupCount=max(0, settings.LOG_BACKUPS),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    except OSError as e:
        logger.warning(f"日志文件不可写，仅输出到控制台: {e}")

    logging.basicConfig(level=level, handlers=handlers, format="%(message)s")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)

    for noisy in ("httpx", "httpcore", "websockets", "openai", "urllib3", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
