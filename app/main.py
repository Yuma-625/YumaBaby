import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import shared
from app.adapters.qq import QQAdapter
from app.agent.core import agent
from app.agent.scheduler import start_scheduler, stop_scheduler
from app.api.routes import router
from app.config import settings
from app.utils import usage
from app.utils.helpers import setup_logging

logger = logging.getLogger(__name__)


async def on_qq_message(message: str, context: dict) -> str:
    return await agent.process(message, context)


def _log_task_result(task: asyncio.Task):
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("QQ 适配器任务异常退出", exc_info=exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    app.state.started_at = datetime.now(timezone.utc)

    summary = usage.today_summary()
    logger.info(
        "今日 LLM 用量：%d 次，¥%.4f（软上限 ¥%.0f / 硬上限 ¥%.0f）",
        summary["calls"], summary["cost_cny"],
        summary["soft_limit_cny"], summary["hard_limit_cny"],
    )

    if not settings.llm_configured:
        logger.warning(
            "LLM_API_KEY 未配置或仍是占位符，对话将退化为固定文案。请在 .env 中填入真实 Key。"
        )

    if not settings.API_TOKEN:
        if settings.API_AUTH_REQUIRED:
            logger.error(
                "API_AUTH_REQUIRED=true 但 API_TOKEN 未配置，/chat 与 /broadcast 将一律返回 503"
            )
        elif not settings.host_is_loopback:
            logger.warning(
                "HOST=%s 且未配置 API_TOKEN：/chat 与 /broadcast 可被其他机器调用，"
                "可能被用来消耗 LLM 额度或让机器人发消息。公网部署请设置 API_TOKEN 并开启 API_AUTH_REQUIRED。",
                settings.HOST,
            )

    if settings.DEBUG:
        logger.warning("DEBUG=true：uvicorn 会启用热重载，生产环境请设为 false")

    qq_task: asyncio.Task | None = None
    if settings.QQ_ENABLED:
        if not settings.QQ_TARGET_GROUPS:
            logger.warning("QQ_TARGET_GROUPS 为空，群消息将不会被处理")
        shared.qq_adapter = QQAdapter(on_message=on_qq_message)
        qq_task = asyncio.create_task(shared.qq_adapter.start())
        qq_task.add_done_callback(_log_task_result)

    start_scheduler()
    logger.info(f"{settings.APP_NAME} v{settings.APP_VERSION} 已启动")

    try:
        yield
    finally:
        stop_scheduler()
        if qq_task is not None:
            qq_task.cancel()
            with suppress(asyncio.CancelledError):
                await qq_task
        if shared.qq_adapter is not None:
            await shared.qq_adapter.stop()
            shared.qq_adapter = None
        logger.info(f"{settings.APP_NAME} 已关闭")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router, prefix="/api/v1")
