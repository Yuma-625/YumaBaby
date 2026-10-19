import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.agent.core import agent
from app.config import settings

logger = logging.getLogger(__name__)


def _timezone() -> ZoneInfo:
    try:
        return ZoneInfo(settings.TIMEZONE)
    except Exception:
        logger.warning(f"无效时区 {settings.TIMEZONE!r}，回退到 Asia/Shanghai")
        return ZoneInfo("Asia/Shanghai")


scheduler = AsyncIOScheduler(timezone=_timezone())

JOB_DEFAULTS = {
    "coalesce": True,       # 错过的多次执行合并为一次
    "max_instances": 1,     # 同一任务不并发
    "misfire_grace_time": 300,
}


class ScheduledTasks:
    """定时任务管理"""

    async def push_market_summary(self):
        """定时推送市场简报到 QQ 群（股票功能搁置期间默认不注册本任务）"""
        from app import shared

        if not settings.MARKET_PUSH_ENABLED:
            logger.debug("MARKET_PUSH_ENABLED 未开启，跳过市场简报")
            return
        if not shared.qq_adapter:
            logger.warning("QQ 适配器未就绪，跳过市场简报")
            return

        summary = await agent.process("今日大盘行情", context={"scheduled": True})
        await shared.qq_adapter.broadcast_to_groups(summary)

    async def push_persona_summary(self):
        """每日推送人设变更建议给管理员；没有待审核项时不打扰。"""
        from app import shared
        from app.utils.persona import persona_manager

        if not settings.QQ_PERSONA_ADMIN:
            logger.debug("未配置 QQ_PERSONA_ADMIN，跳过人设总结")
            return
        if persona_manager.pending_count == 0:
            logger.info("暂无待审核人设，跳过推送")
            return
        if not shared.qq_adapter:
            logger.warning("QQ 适配器未就绪，跳过人设总结")
            return

        summary = persona_manager.get_pending_summary()
        shared.qq_adapter.begin_awaiting_feedback()
        await shared.qq_adapter.send_private_msg(settings.QQ_PERSONA_ADMIN, summary)
        logger.info("人设总结已发送给管理员")


tasks = ScheduledTasks()


def start_scheduler():
    """启动定时任务（重复调用安全）。"""
    if scheduler.running:
        logger.debug("定时任务已在运行，跳过重复启动")
        return

    if settings.MARKET_PUSH_ENABLED:
        scheduler.add_job(
            tasks.push_market_summary,
            "cron",
            day_of_week="mon-fri",
            hour=9,
            minute=30,
            id="market_open",
            replace_existing=True,
            **JOB_DEFAULTS,
        )
        scheduler.add_job(
            tasks.push_market_summary,
            "cron",
            day_of_week="mon-fri",
            hour=15,
            minute=0,
            id="market_close",
            replace_existing=True,
            **JOB_DEFAULTS,
        )
        logger.info("市场简报任务已启用")
    else:
        logger.info("市场简报任务未启用（MARKET_PUSH_ENABLED=false）")

    scheduler.add_job(
        tasks.push_persona_summary,
        "cron",
        hour=20,
        minute=0,
        id="persona_summary",
        replace_existing=True,
        **JOB_DEFAULTS,
    )
    scheduler.start()
    logger.info("定时任务已启动")


def stop_scheduler():
    """停止定时任务（重复调用安全）。"""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("定时任务已停止")
