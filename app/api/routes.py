import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app import shared
from app.agent.core import agent
from app.agent.scheduler import scheduler
from app.config import settings
from app.models.schemas import BroadcastRequest, MessageRequest, MessageResponse

logger = logging.getLogger(__name__)

router = APIRouter()


def require_api_token(authorization: str | None = Header(default=None)) -> None:
    """保护会花钱/会发消息的接口。

    - 设置了 API_TOKEN：必须带 ``Authorization: Bearer <token>``
    - 未设置但 API_AUTH_REQUIRED=true：直接 503，避免误把裸接口挂到公网
    - 两者都没有：放行（仅适合本机调试），启动时会告警
    """
    if not settings.API_TOKEN:
        if settings.API_AUTH_REQUIRED:
            raise HTTPException(
                status_code=503,
                detail="API_TOKEN 未配置，已拒绝请求（API_AUTH_REQUIRED=true）",
            )
        return

    expected = f"Bearer {settings.API_TOKEN}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=401,
            detail="无效或缺失的 API Token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.get("/health")
async def health(request: Request):
    qq = shared.qq_adapter
    started_at = getattr(request.app.state, "started_at", None)
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "llm_configured": settings.llm_configured,
        "api_auth": {
            "required": settings.API_AUTH_REQUIRED,
            "token_set": bool(settings.API_TOKEN),
        },
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


@router.post("/chat", response_model=MessageResponse,
             dependencies=[Depends(require_api_token)])
async def chat(req: MessageRequest):
    """向智能体发送消息"""
    reply, skill_used = await agent.process_with_skill(req.message, context={
        "source": req.source,
        "group_id": req.group_id,
        "user_id": req.user_id,
    })
    return MessageResponse(reply=reply, skill_used=skill_used)


@router.post("/broadcast", dependencies=[Depends(require_api_token)])
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
