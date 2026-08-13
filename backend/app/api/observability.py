"""可观测性 API（TD §12.10 / FR-16）。

P2 增强：NPS 采集 + 反馈采纳闭环。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.audit import write_audit
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.observability.schemas import FeedbackCreate, FeedbackResponse
from app.services.observability.service import ObservabilityService

router = APIRouter(prefix="/observability", tags=["observability"])

_WRITE_ROLES = ("metric_owner", "domain_admin", "platform_admin", "viewer")
_READ_ROLES = ("metric_owner", "domain_admin", "platform_admin", "reviewer", "viewer")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]


class NpsSubmitRequest(BaseModel):
    """NPS 提交请求。"""
    score: int = Field(..., ge=0, le=10, description="NPS 分数（0-10）")
    comment: str | None = Field(None, description="可选评论")
    target_type: str = Field("platform", description="目标类型")
    target_id: str | None = Field(None, description="目标 ID")


class FeedbackStatusUpdateRequest(BaseModel):
    """反馈状态更新请求。"""
    status: str = Field(..., pattern="^(adopted|rejected|in_progress)$", description="新状态")
    resolution_note: str | None = Field(None, description="处理说明")


@router.post("/feedback", status_code=201, dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def submit_feedback(
    payload: FeedbackCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await ObservabilityService(db).submit_feedback(payload, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="feedback.submit",
        entity_type="feedback",
        entity_id=str(resp.id),
        detail={},
        trace_id=trace_id,
    )
    # PLAT-3: 审计与业务同一事务原子提交
    await db.commit()
    return ok(data=FeedbackResponse.from_model(resp), trace_id=trace_id)


@router.get("/feedback", dependencies=_READ_DEPS)
async def list_feedback(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    target_type: str | None = Query(None),
    limit: int = Query(100),
) -> Any:
    items = await ObservabilityService(db).list_feedback(target_type, limit)
    return ok(
        data={
            "items": [FeedbackResponse.from_model(i) for i in items],
            "total": len(items),
        },
        trace_id=trace_id,
    )


@router.get("/metrics/quality", dependencies=_READ_DEPS)
async def quality_metrics(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    return ok(data=await ObservabilityService(db).quality_stats(), trace_id=trace_id)


@router.get("/metrics/api", dependencies=_READ_DEPS)
async def api_metrics(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    return ok(data=await ObservabilityService(db).api_stats(), trace_id=trace_id)


@router.get("/metrics/notifications", dependencies=_READ_DEPS)
async def notification_metrics(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    return ok(data=await ObservabilityService(db).notification_stats(), trace_id=trace_id)


@router.get("/metrics/lineage", dependencies=_READ_DEPS)
async def lineage_metrics(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    return ok(data=await ObservabilityService(db).lineage_stats(), trace_id=trace_id)


# ----------------------------------------------------------------
# P2 Enhancement: NPS 采集 + 反馈采纳闭环
# ----------------------------------------------------------------


@router.post("/nps", status_code=201, dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def submit_nps(
    payload: NpsSubmitRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """NPS 采集：用户提交 0-10 推荐度评分。"""
    resp = await ObservabilityService(db).submit_nps(
        user_id=user.id,
        score=payload.score,
        comment=payload.comment,
        target_type=payload.target_type,
        target_id=payload.target_id,
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="nps.submit",
        entity_type="feedback",
        entity_id=str(resp.id),
        detail={"score": payload.score},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=FeedbackResponse.from_model(resp), trace_id=trace_id)


@router.patch(
    "/feedback/{feedback_id}/status",
    dependencies=[Depends(require_roles("platform_admin", "domain_admin"))],
)
async def update_feedback_status(
    feedback_id: int,
    payload: FeedbackStatusUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """反馈采纳闭环：更新反馈状态（adopted/rejected/in_progress）。"""
    resp = await ObservabilityService(db).update_feedback_status(
        feedback_id=feedback_id,
        status=payload.status,
        resolver_id=user.id,
        resolution_note=payload.resolution_note,
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="feedback.status_update",
        entity_type="feedback",
        entity_id=str(feedback_id),
        detail={"status": payload.status, "note": payload.resolution_note},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=FeedbackResponse.from_model(resp), trace_id=trace_id)
