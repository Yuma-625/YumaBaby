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
        """推送人设变更建议：有待审项、不在对话中、每天最多一次。"""
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

        store = shared.memory_store
        if store is not None:
            if store.has_event_today("persona_push"):
                logger.info("今日已推送过人设总结，跳过")
                return
            last = store.last_activity_at()
            if last:
                try:
                    from datetime import datetime
                    last_dt = datetime.fromisoformat(last)
                    from zoneinfo import ZoneInfo
                    now = datetime.now(ZoneInfo(settings.TIMEZONE))
                    if (now - last_dt).total_seconds() < 600:
                        logger.info("刚刚还有对话，暂不打扰")
                        return
                except (TypeError, ValueError):
                    pass

        summary = persona_manager.get_pending_summary()
        shared.qq_adapter.begin_awaiting_feedback()
        await shared.qq_adapter.send_private_msg(settings.QQ_PERSONA_ADMIN, summary)
        if store is not None:
            store.add_event("persona_push", "admin", f"user:{settings.QQ_PERSONA_ADMIN}",
                            {"pending": persona_manager.pending_count})
        logger.info("人设总结已发送给管理员")

    async def memory_maintenance(self):
        """每天清理低权重/超量记忆，避免检索被旧数据淹没。"""
        from app import shared

        store = shared.memory_store
        if store is None:
            return
        try:
            before = store.count_memories()
            removed = store.prune_memories()
            logger.info(f"记忆维护：{before} -> {before - removed}（删除 {removed} 条）")
            store.add_event("memory_prune", "maintenance", "", {"removed": removed})
        except Exception:
            logger.exception("记忆维护失败")

    async def push_group_analysis(self):
        """每晚分析只读群的聊天特征，私聊给管理员。"""
        from app import shared
        from app.group_analysis import build_report

        store = shared.memory_store
        adapter = shared.qq_adapter
        if store is None or adapter is None or not adapter.is_connected:
            return
        if not settings.QQ_PERSONA_ADMIN or not settings.GROUP_ANALYSIS_GROUPS:
            return

        for group_id in settings.GROUP_ANALYSIS_GROUPS:
            if store.count_events_today("group_report", group_id):
                logger.info(f"群 {group_id} 今天已出过日报，跳过")
                continue
            try:
                report = await build_report(store, group_id)
                if not report:
                    continue
                await adapter.send_private_msg(settings.QQ_PERSONA_ADMIN, report)
                store.add_event("group_report", group_id, group_id, {"chars": len(report)})
                logger.info(f"群 {group_id} 日报已发送")
            except Exception:
                logger.exception(f"群 {group_id} 日报生成失败")


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
    scheduler.add_job(
        tasks.memory_maintenance,
        "cron",
        hour=4,
        minute=30,
        id="memory_maintenance",
        replace_existing=True,
        **JOB_DEFAULTS,
    )
    if settings.GROUP_ANALYSIS_GROUPS:
        scheduler.add_job(
            tasks.push_group_analysis,
            "cron",
            hour=23,
            minute=40,
            id="group_analysis",
            replace_existing=True,
            **JOB_DEFAULTS,
        )
        logger.info(f"群聊日报任务已启用: {settings.GROUP_ANALYSIS_GROUPS}")
    scheduler.start()
    logger.info("定时任务已启动")


def stop_scheduler():
    """停止定时任务（重复调用安全）。"""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("定时任务已停止")
