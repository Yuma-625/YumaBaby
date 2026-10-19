import logging

from fastapi import APIRouter, Request

from app import shared
from app.agent.core import agent
from app.agent.scheduler import scheduler
from app.config import settings
from app.models.schemas import BroadcastRequest, MessageRequest, MessageResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health(request: Request):
    qq = shared.qq_adapter
    started_at = getattr(request.app.state, "started_at", None)
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "llm_configured": settings.llm_configured,
        "qq": {
            "enabled": settings.QQ_ENABLED,
            "connected": bool(qq and qq.is_connected),
        },
        "scheduler": {
            "running": scheduler.running,
            "jobs": [job.id for job in scheduler.get_jobs()] if scheduler.running else [],
        },
        "started_at": started_at.isoformat() if started_at else None,
    }


@router.get("/skills")
async def list_skills():
    """列出所有已注册技能"""
    return {"skills": agent.skill_list}


@router.post("/chat", response_model=MessageResponse)
async def chat(req: MessageRequest):
    """向智能体发送消息"""
    reply, skill_used = await agent.process_with_skill(req.message, context={
        "source": req.source,
        "group_id": req.group_id,
        "user_id": req.user_id,
    })
    return MessageResponse(reply=reply, skill_used=skill_used)


@router.post("/broadcast")
async def broadcast(req: BroadcastRequest):
    """向 QQ 群推送消息"""
    qq = shared.qq_adapter
    if not settings.QQ_ENABLED or qq is None:
        return {"status": "error", "message": "QQ 功能未启用"}
    if not qq.is_connected:
        return {"status": "error", "message": "QQ 适配器未连接"}

    groups = [g.strip() for g in (req.groups or settings.QQ_TARGET_GROUPS) if g and g.strip()]
    if not groups:
        return {"status": "error", "message": "未指定目标群"}

    results = []
    for gid in groups:
        results.append({"group_id": gid, "result": await qq.send_group_msg(gid, req.message)})
    return {"status": "ok", "results": results}
