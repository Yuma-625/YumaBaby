import hmac
import logging
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app import shared
from app.agent.core import agent
from app.agent.scheduler import scheduler
from app.config import settings
from app.models.schemas import BroadcastRequest, MessageRequest, MessageResponse
from app.utils import usage

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
        "memory": {
            "enabled": shared.memory_store is not None,
            "memories": shared.memory_store.count_memories() if shared.memory_store else 0,
            "topics": shared.memory_store.count_all_topics() if shared.memory_store else 0,
            "embeddings_enabled": settings.EMBEDDING_ENABLED,
            "hierarchy_enabled": settings.MEMORY_HIERARCHY_ENABLED,
        },
        "usage_today": usage.today_summary(),
        "started_at": started_at.isoformat() if started_at else None,
    }


@router.get("/memory/topics", dependencies=[Depends(require_api_token)])
async def memory_topics(scope_type: str, scope_id: str):
    """某个 scope 下的主题列表。"""
    store = shared.memory_store
    if store is None:
        raise HTTPException(status_code=503, detail="记忆库未初始化")
    if scope_type not in ("user", "group"):
        raise HTTPException(status_code=422, detail="scope_type 只能是 user 或 group")
    topics = store.list_topics(scope_type, scope_id)
    for topic in topics:
        topic["facts"] = store.count_topic_memories(int(topic["id"]))
    return {"scope_type": scope_type, "scope_id": scope_id, "topics": topics}


@router.get("/memory/topics/{topic_id}", dependencies=[Depends(require_api_token)])
async def memory_topic_detail(topic_id: int):
    store = shared.memory_store
    if store is None:
        raise HTTPException(status_code=503, detail="记忆库未初始化")
    topic = store.get_topic(topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="主题不存在")
    return {"topic": topic, "memories": store.topic_memories(topic_id, limit=100)}


@router.post("/memory/extract", dependencies=[Depends(require_api_token)])
async def memory_extract(scope_type: str, scope_id: str):
    """手动触发一次记忆提炼（管理员排障用）。"""
    store = shared.memory_store
    if store is None:
        raise HTTPException(status_code=503, detail="记忆库未初始化")
    if scope_type not in ("user", "group"):
        raise HTTPException(status_code=422, detail="scope_type 只能是 user 或 group")
    from app.memory.extractor import extract_scope

    added = await extract_scope(store, scope_type, scope_id, force=True)
    return {
        "scope_type": scope_type,
        "scope_id": scope_id,
        "added": added,
        "cursor": store.get_extract_cursor(scope_type, scope_id),
    }


@router.get("/skills")
async def list_skills():
    """列出所有已注册技能"""
    return {"skills": agent.skill_list}


@router.get("/activity", dependencies=[Depends(require_api_token)])
async def activity(date: str | None = None):
    """某一天的活动：收/发消息、工具调用、好评差评、花费。"""
    store = shared.memory_store
    if store is None:
        raise HTTPException(status_code=503, detail="记忆库未初始化")
    if date is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(status_code=422, detail="date 格式应为 YYYY-MM-DD")

    summary = store.daily_summary(date)
    today = usage.today_summary()
    summary["usage"] = today if today["date"] == summary["date"] else None
    return summary


@router.post("/chat", response_model=MessageResponse,
             dependencies=[Depends(require_api_token)])
async def chat(req: MessageRequest):
    """向智能体发送消息（服务已启动时走 AgentLoop，否则回退关键词技能）"""
    service = shared.agent_service
    if service is not None:
        user_id = str(req.user_id or "api")
        reply = await service.handle(
            req.message,
            chat_id=f"user:{user_id}",
            scope_type="user",
            scope_id=user_id,
        )
        return MessageResponse(reply=reply, skill_used="agent")

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
