"""血缘 API 路由。

对齐 TD §12.2 与 DEV_GUIDE §8b：RBAC 写闸门、SQL 注入守卫、审计、trace_id 可观测。
影响分析读路径图优先 + MySQL 兜底，结果分页返回；what-if 预览走写闸门 + 审计。
"""

from __future__ import annotations

import contextlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.guard import guard_against_injection
from app.core.resilience import CircuitBreaker
from app.db.mysql import get_db_session
from app.db.redis import get_redis
from app.services.lineage.events import LineageEventPublisher
from app.services.lineage.graph import LineageGraphClient
from app.services.lineage.schemas import (
    ImpactPreviewRequest,
    LineageEdgeListParams,
    LineageImpactParams,
    LineageParseRequest,
)
from app.services.lineage.service import LineageService, paginate_edges

router = APIRouter(prefix="/lineage", tags=["lineage"])

_WRITE_ROLES = ("platform_admin", "domain_admin", "metric_owner")
_READ_ROLES = ALL_ROLES
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]


def _svc(db: Any) -> LineageService:
    redis = None
    with contextlib.suppress(RuntimeError):
        redis = get_redis()
    return LineageService(
        db,
        graph=LineageGraphClient(),
        events=LineageEventPublisher(redis, CircuitBreaker()),
        redis=redis,
    )


@router.post("/parse", dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def parse_lineage(
    body: LineageParseRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """解析 SQL 并持久化血缘（表级 + 字段级）。"""
    svc = _svc(db)
    result = await svc.parse_and_store(body, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="LINEAGE_PARSE",
        entity_type="lineage",
        entity_id=body.source_node or "sql",
        detail={
            "table_edges": result.table_edges,
            "field_edges": result.field_edges,
            "graph_written": result.graph_written,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result.model_dump(), trace_id=trace_id)


@router.post(
    "/impact-preview",
    dependencies=[
        Depends(require_roles(*_WRITE_ROLES)),
        Depends(guard_against_injection),
    ],
)
async def impact_preview(
    body: ImpactPreviewRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """变更影响预览 what-if：估算影响面（指标/物理表/消费方）与风险等级。"""
    svc = _svc(db)
    result = await svc.impact_preview(body.metric_code, body.change_type)
    await write_audit(
        db,
        actor_id=user.id,
        action="LINEAGE_IMPACT_PREVIEW",
        entity_type="lineage",
        entity_id=f"metric:{body.metric_code}",
        detail=result,
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@router.get("/impact", dependencies=_READ_DEPS)
async def impact(
    params: Annotated[LineageImpactParams, Depends()],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """影响分析：给定节点向上/向下/双向展开血缘（分页返回）。"""
    svc = _svc(db)
    edges = await svc.query_impact(params)
    return ok(data=paginate_edges(edges, params.page, params.page_size), trace_id=trace_id)


@router.get("/edges", dependencies=_READ_DEPS)
async def list_edges(
    params: Annotated[LineageEdgeListParams, Depends()],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """列出与节点相关的血缘边（分页返回，含 total）。"""
    svc = _svc(db)
    edges = await svc.list_edges(params.node, params.direction)
    return ok(data=paginate_edges(edges, params.page, params.page_size), trace_id=trace_id)


@router.delete(
    "/edges",
    dependencies=[Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)],
)
async def delete_edges_by_node(
    params: Annotated[LineageEdgeListParams, Depends()],
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """级联软删某节点相关的全部血缘边（数据源删除时维护一致性）。"""
    svc = _svc(db)
    deleted = await svc.delete_by_node(params.node)
    await write_audit(
        db,
        actor_id=user.id,
        action="LINEAGE_DELETE",
        entity_type="lineage",
        entity_id=params.node,
        detail={"deleted_edges": deleted},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"deleted": deleted}, trace_id=trace_id)
