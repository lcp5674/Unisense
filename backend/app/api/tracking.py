"""埋点事件 API（对齐 US9 / FR-16）。

端点：
- POST /api/v1/tracking/event   记录埋点事件（需认证）
- GET  /api/v1/tracking/stats   查询统计（需 platform_admin/domain_admin）
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import ApiResponse, ok
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.models.tracking import TrackingEvent

router = APIRouter(prefix="/tracking", tags=["tracking"])


# ---- Schemas ----

class TrackEventRequest(BaseModel):
    """埋点事件请求体。"""

    event_type: str = Field(min_length=1, max_length=32, description="事件类型")
    target_id: str | None = Field(default=None, max_length=36, description="目标对象 ID")
    target_type: str | None = Field(default=None, max_length=32, description="目标类型")
    context: dict[str, Any] | None = Field(default=None, description="事件上下文")


class TrackEventResponse(BaseModel):
    """埋点事件响应。"""

    event_id: str


class TrackingStatsResponse(BaseModel):
    """埋点统计响应。"""

    stats: list[dict[str, Any]]


# ---- Endpoints ----

@router.post(
    "/event",
    response_model=ApiResponse[TrackEventResponse],
    dependencies=[Depends(guard_against_injection)],
)
async def create_event(
    payload: TrackEventRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
) -> ApiResponse[TrackEventResponse]:
    """记录埋点事件（需认证，自动附带 actor_id）。"""
    event_id = str(uuid4())
    event = TrackingEvent(
        id=event_id,
        event_type=payload.event_type,
        actor_id=str(user.id),
        target_id=payload.target_id,
        target_type=payload.target_type,
        context_json=payload.context,
    )
    db.add(event)
    await db.commit()
    return ok(data=TrackEventResponse(event_id=event_id))


@router.get(
    "/stats",
    response_model=ApiResponse[TrackingStatsResponse],
    dependencies=[Depends(require_roles("platform_admin", "domain_admin"))],
)
async def get_stats(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    event_type: str | None = Query(default=None, description="事件类型过滤"),
    start_date: str | None = Query(default=None, description="开始日期(YYYY-MM-DD)"),
    end_date: str | None = Query(default=None, description="结束日期(YYYY-MM-DD)"),
    group_by: str | None = Query(default="event_type", description="分组字段"),
) -> ApiResponse[TrackingStatsResponse]:
    """查询埋点统计（需 platform_admin/domain_admin 角色）。"""
    query = select(
        TrackingEvent.event_type,
        func.count(TrackingEvent.id).label("event_count"),
        func.count(func.distinct(TrackingEvent.actor_id)).label("unique_actors"),
    )

    if event_type:
        query = query.where(TrackingEvent.event_type == event_type)

    if start_date:
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=UTC)
            query = query.where(TrackingEvent.created_at >= start_dt)
        except ValueError:
            pass

    if end_date:
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=UTC)
            query = query.where(TrackingEvent.created_at <= end_dt)
        except ValueError:
            pass

    query = query.group_by(TrackingEvent.event_type)
    result = await db.execute(query)
    rows = result.all()

    stats = [
        {
            "group_key": row[0],
            "event_count": row[1],
            "unique_actors": row[2],
        }
        for row in rows
    ]

    return ok(data=TrackingStatsResponse(stats=stats))
