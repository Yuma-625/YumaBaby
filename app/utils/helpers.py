import logging

from app.config import settings

logger = logging.getLogger(settings.APP_NAME)

_configured = False


def setup_logging():
    """幂等配置日志，并压掉第三方库的噪声。"""
    global _configured
    if _configured:
        return
    _configured = True

    level = getattr(logging, settings.LOG_LEVEL, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "websockets", "openai", "urllib3", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
