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

#: 合法事件类型白名单（P2-8 画像污染加固）。此前 event_type 任意字符串——任何登录
#: 用户可伪造海量行为事件，污染协同过滤画像、推荐失真。现收敛为前端实际发送的
#: 事件类型集合 + 推荐消费的指标事件类型；未知类型 422 拒绝。
_ALLOWED_EVENT_TYPES = frozenset(
    {
        # 推荐画像消费的指标行为事件（recommend/repository.METRIC_EVENT_TYPES）
        "metric_detail_view",
        "metric_search",
        "consume_query",
        "consume_dry_run",
        "consume_semantic",
        "consumption_guide_view",
        # 前端 useTracking 实际埋点的事件类型
        "consumption_guide_update",
        "dashboard_view",
        "favorites_view",
        "lineage_channel_runs",
        "lineage_coverage_view",
        "lineage_edge_detail",
        "lineage_graph_view",
        "lineage_parse",
        "lineage_preview",
        "lineage_query",
        "lineage_run_detail",
        "lineage_stale_confirm",
        "lineage_stale_restore",
        "lineage_table_detail",
        "recommend_click",
        "recommend_dismiss",
        "recommend_view",
        "review_arbitrate",
        "review_escalate",
        "review_reopen",
        "template_edit",
        "template_instantiate",
        "todo_center_view",
        "view",
    }
)
#: context 有界（防超大 context 撑爆/污染画像）：键数上限 + 单键长上限 + 值类型限定。
_CONTEXT_MAX_KEYS = 16
_CONTEXT_KEY_MAX_LEN = 64
_CONTEXT_VALUE_MAX_LEN = 256


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
    """埋点统计响应。

    ``total_unique_actors`` 为当前过滤条件下全量去重用户数（不随分组变化）；
    ``stats[].unique_actors`` 仅为各分组内去重用户数，直接相加会重复计数。
    """

    stats: list[dict[str, Any]]
    total_unique_actors: int = 0


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
    # P2-8 画像污染加固：事件类型白名单 + context 有界（防伪造行为事件污染协同过滤）
    if payload.event_type not in _ALLOWED_EVENT_TYPES:
        raise ValidationError(
            f"非法事件类型: {payload.event_type}",
            ctx={"allowed": sorted(_ALLOWED_EVENT_TYPES)},
        )
    if payload.context is not None:
        if len(payload.context) > _CONTEXT_MAX_KEYS:
            raise ValidationError(
                f"context 键数超限（最多 {_CONTEXT_MAX_KEYS}）",
                ctx={"max_keys": _CONTEXT_MAX_KEYS},
            )
        for k, v in payload.context.items():
            if len(str(k)) > _CONTEXT_KEY_MAX_LEN:
                raise ValidationError(
                    f"context 键超长（最多 {_CONTEXT_KEY_MAX_LEN}）",
                    ctx={"key": str(k)[:_CONTEXT_KEY_MAX_LEN]},
                )
            if v is not None and len(str(v)) > _CONTEXT_VALUE_MAX_LEN:
                raise ValidationError(
                    f"context 值超长（最多 {_CONTEXT_VALUE_MAX_LEN}）",
                    ctx={"key": str(k)[:_CONTEXT_KEY_MAX_LEN]},
                )
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

    # 过滤条件（分组统计与全量去重用户数共用，保证口径一致）
    clauses: list[Any] = []
    if event_type:
        clauses.append(TrackingEvent.event_type == event_type)
    if start_date:
        start_dt = _parse_stats_date(start_date, field="start_date")
        clauses.append(TrackingEvent.created_at >= start_dt)
    if end_date:
        end_dt = _parse_stats_date(end_date, field="end_date")
        clauses.append(TrackingEvent.created_at <= end_dt)

    # 全量去重用户数：相同过滤条件下 COUNT(DISTINCT actor_id)，与分组无关，
    # 避免前端把各组 unique_actors 相加导致同一用户跨分组重复计数。
    total_query = select(func.count(func.distinct(TrackingEvent.actor_id))).where(*clauses)
    total_unique_actors = (await db.execute(total_query)).scalar() or 0

    query = (
        select(
            group_col.label("group_key"),
            func.count(TrackingEvent.id).label("event_count"),
            func.count(func.distinct(TrackingEvent.actor_id)).label("unique_actors"),
        )
        .where(*clauses)
        .group_by(group_col)
    )
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

    return ok(
        data=TrackingStatsResponse(stats=stats, total_unique_actors=total_unique_actors)
    )
