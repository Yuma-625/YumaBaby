import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import shared
from app.adapters.qq import QQAdapter
from app.adapters.qq_official import OfficialQQAdapter
from app.agent.core import agent
from app.agent.loop import AgentLoop
from app.agent.scheduler import start_scheduler, stop_scheduler
from app.agent.service import AgentService
from app.api.routes import router
from app.config import settings
from app.memory import MemoryStore
from app.tools.builtin import build_registry
from app.utils import usage
from app.utils.helpers import setup_logging

logger = logging.getLogger(__name__)


async def on_qq_message(message: str, context: dict) -> str:
    """QQ 消息统一走 AgentLoop；未初始化时回退到旧的关键词分发。"""
    service = shared.agent_service
    if service is None:
        return await agent.process(message, context)

    group_id = context.get("group_id")
    user_id = str(context.get("user_id") or "unknown")

    # 管理员命令（好评/差评、今日活动、收藏表情），不进 LLM
    store = shared.memory_store
    if store is not None and settings.QQ_PERSONA_ADMIN == user_id:
        if not group_id:
            from app.agent.admin import handle_admin_message

            handled = handle_admin_message(
                message,
                chat_id=f"user:{user_id}",
                store=store,
                reviewer=user_id,
                reply_to_id=context.get("reply_to_id"),
            )
            if handled is not None:
                return handled

            from app.agent.groups import handle_group_admin

            group_reply = handle_group_admin(message, store, user_id)
            if group_reply is not None:
                return group_reply

            from app.agent.tones import handle_tone_admin

            tone_reply = handle_tone_admin(message, store, user_id)
            if tone_reply is not None:
                return tone_reply

        from app.stickers import handle_sticker_command

        sticker_reply = await handle_sticker_command(
            message,
            store=store,
            attachments=context.get("attachments") or [],
            group_id=group_id,
            user_id=user_id,
            chat_id=f"group:{group_id}" if group_id else f"user:{user_id}",
        )
        if sticker_reply is not None:
            return sticker_reply

        from app.stickers import handle_sticker_admin

        candidate_reply = handle_sticker_admin(message, store, user_id)
        if candidate_reply is not None:
            return candidate_reply

    # 发表情：所有能跟机器人说话的人都可以
    if store is not None:
        from app.stickers import handle_sticker_send

        sticker_send = await handle_sticker_send(
            message, store=store, user_id=user_id
        )
        if sticker_send is not None:
            return sticker_send

    if store is not None and group_id:
        from app.summary import handle_summary_command

        summary_reply = await handle_summary_command(
            message, store=store, group_id=group_id, user_id=user_id
        )
        if summary_reply is not None:
            return summary_reply

    if group_id:
        return await service.handle(
            message,
            chat_id=f"group:{group_id}",
            scope_type="group",
            scope_id=str(group_id),
            auto_participate=bool(context.get("auto_participate")),
            at_targets=context.get("at_targets") or [],
            attachments=context.get("attachments") or [],
            user_id=user_id,
            sender_name=str(context.get("sender_name") or ""),
        )

    return await service.handle(
        message,
        chat_id=f"user:{user_id}",
        scope_type="user",
        scope_id=user_id,
        attachments=context.get("attachments") or [],
        user_id=user_id,
        sender_name=str(context.get("sender_name") or ""),
    )


def on_reply_sent(chat_id: str, platform_message_id: str) -> None:
    """记录机器人发出消息的平台 id，供「引用回复 + 好评」定位。"""
    if shared.memory_store is not None:
        shared.memory_store.attach_platform_message_id(chat_id, platform_message_id)


def on_group_seen(group_id: str, user_id: str, text: str,
                  sender_name: str, attachments: list) -> None:
    """所有目标群消息都记一份短期上下文，供被 @ 时回看。"""
    if shared.memory_store is None or not settings.GROUP_CONTEXT_ENABLED:
        return
    if not text.strip() and not attachments:
        return
    shared.memory_store.add_group_context(
        group_id, user_id, text, attachments, sender_name
    )
    if settings.STICKER_COLLECT_ENABLED and attachments:
        from app.stickers import collect_candidates

        task = asyncio.create_task(collect_candidates(
            shared.memory_store,
            attachments,
            group_id=group_id,
            user_id=user_id,
            sender_name=sender_name,
        ))
        task.add_done_callback(_log_task_result)


def group_policy(group_id: str) -> dict | None:
    """数据库运行时配置优先，其次 .env；None 表示未知群。"""
    store = shared.memory_store
    if store is not None:
        runtime = store.get_runtime_group(group_id)
        if runtime is not None:
            return {
                "mode": runtime.get("mode") or "normal",
                "participation": bool(runtime.get("participation")),
            }
    if group_id in settings.QQ_TARGET_GROUPS:
        return {
            "mode": "readonly" if group_id in settings.QQ_READONLY_GROUPS else "normal",
            "participation": group_id in settings.QQ_PARTICIPATION_GROUPS,
        }
    return None


def on_unknown_group(group_id: str, user_id: str, text: str, sender_name: str) -> None:
    store = shared.memory_store
    if store is None:
        return
    if not store.record_unknown_group(group_id, user_id, sender_name, text):
        return
    task = asyncio.create_task(_notify_unknown_group(group_id, user_id, sender_name, text))
    task.add_done_callback(_log_task_result)


async def _notify_unknown_group(group_id: str, user_id: str,
                                sender_name: str, text: str) -> None:
    adapter = shared.qq_adapter
    admin = settings.QQ_PERSONA_ADMIN
    if adapter is None or not admin:
        return
    reply = (
        f"发现新群：{group_id}\n"
        f"首次 @：{sender_name or user_id}：{text[:100]}\n"
        f"要加入配置吗？回复：\n"
        f"加群 {group_id} 正常\n"
        f"加群 {group_id} 只读\n"
        f"拒绝 {group_id}"
    )
    result = await adapter.send_private_msg(admin, reply)
    if result.get("status") != "ok":
        logger.error("新群通知发送失败: %s", result)


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

    if settings.AGENT_ENABLED:
        shared.memory_store = MemoryStore(settings.MEMORY_DB)
        from app.stickers import sync_sticker_files
        synced = sync_sticker_files(shared.memory_store)
        if synced:
            logger.info(f"表情文件同步完成: 扫描 {synced} 个文件")
        registry = build_registry(shared.memory_store)
        agent_loop = AgentLoop(
            registry,
            max_steps=settings.AGENT_MAX_STEPS,
            max_tokens=settings.AGENT_MAX_TOKENS,
        )
        shared.agent_service = AgentService(agent_loop, shared.memory_store)
        logger.info("Agent 已启用：记忆库 %s，工具 %s", settings.MEMORY_DB, registry.names)
    else:
        logger.info("AGENT_ENABLED=false，使用旧的关键词分发")

    qq_task: asyncio.Task | None = None
    if settings.QQ_ENABLED:
        if not settings.QQ_TARGET_GROUPS:
            logger.warning("QQ_TARGET_GROUPS 为空，群消息将不会被处理")
        shared.qq_adapter = (
            OfficialQQAdapter(
                on_message=on_qq_message,
                on_reply_sent=on_reply_sent,
                on_group_seen=on_group_seen,
                group_policy=group_policy,
                on_unknown_group=on_unknown_group,
            )
            if settings.QQ_PROVIDER == "official"
            else QQAdapter(
                on_message=on_qq_message,
                on_reply_sent=on_reply_sent,
                on_group_seen=on_group_seen,
            )
        )
        logger.info("QQ 适配器: %s", settings.QQ_PROVIDER)
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
        if shared.memory_store is not None:
            shared.memory_store.close()
            shared.memory_store = None
        shared.agent_service = None
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
