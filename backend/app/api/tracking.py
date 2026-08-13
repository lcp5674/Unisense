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
from app.core.exceptions import ValidationError
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.models.tracking import TrackingEvent

router = APIRouter(prefix="/tracking", tags=["tracking"])

#: 埋点统计允许的分组字段白名单（防任意列 GROUP BY / 注入）。
_GROUP_BY_ALLOWED = ("event_type", "target_type", "actor_id")


def _parse_stats_date(value: str, *, field: str) -> datetime:
    """解析 YYYY-MM-DD 日期查询参数；格式非法返回 422（不静默忽略）。"""
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        raise ValidationError(
            f"{field} 格式非法，应为 YYYY-MM-DD",
            ctx={"field": field, "value": value},
        ) from None


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
    dependencies=[
        Depends(require_roles("platform_admin", "domain_admin")),
        Depends(guard_against_injection),
    ],
)
async def get_stats(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    event_type: str | None = Query(default=None, description="事件类型过滤"),
    start_date: str | None = Query(default=None, description="开始日期(YYYY-MM-DD)"),
    end_date: str | None = Query(default=None, description="结束日期(YYYY-MM-DD)"),
    group_by: str | None = Query(default="event_type", description="分组字段"),
) -> ApiResponse[TrackingStatsResponse]:
    """查询埋点统计（需 platform_admin/domain_admin 角色）。

    日期参数格式非法返回 422（不再静默忽略导致「看似过滤实则全量」）；
    ``group_by`` 仅支持白名单字段（event_type/target_type/actor_id），
    其余取值返回 422（防任意列分组与标识符注入）。
    """
    group_field = group_by or "event_type"
    if group_field not in _GROUP_BY_ALLOWED:
        raise ValidationError(
            f"group_by 仅支持 {', '.join(_GROUP_BY_ALLOWED)}",
            ctx={"group_by": group_by},
        )

    group_col = getattr(TrackingEvent, group_field)
    query = select(
        group_col.label("group_key"),
        func.count(TrackingEvent.id).label("event_count"),
        func.count(func.distinct(TrackingEvent.actor_id)).label("unique_actors"),
    )

    if event_type:
        query = query.where(TrackingEvent.event_type == event_type)

    if start_date:
        start_dt = _parse_stats_date(start_date, field="start_date")
        query = query.where(TrackingEvent.created_at >= start_dt)

    if end_date:
        end_dt = _parse_stats_date(end_date, field="end_date")
        query = query.where(TrackingEvent.created_at <= end_dt)

    query = query.group_by(group_col)
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
