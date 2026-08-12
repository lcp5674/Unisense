"""资产地图 API（TD §12.11 / FR-18）。

P2 增强：GET /graph（图谱）、GET /heatmap（热力）、GET /owner-view（责任人视图）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.assetmap.service import AssetMapService

router = APIRouter(prefix="/assetmap", tags=["assetmap"])

_READ_ROLES = ("metric_owner", "domain_admin", "platform_admin", "reviewer", "viewer")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]


@router.get("/summary", dependencies=_READ_DEPS)
async def catalog_summary(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    return ok(data=await AssetMapService(db).catalog_summary(), trace_id=trace_id)


@router.get("/classification", dependencies=_READ_DEPS)
async def classification_summary(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    return ok(data=await AssetMapService(db).classification_summary(), trace_id=trace_id)


@router.get("/metrics", dependencies=_READ_DEPS)
async def metric_summary(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    return ok(data=await AssetMapService(db).metric_summary(), trace_id=trace_id)


@router.get("/tables", dependencies=_READ_DEPS)
async def list_tables(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    source_id: str | None = Query(None),
    sensitivity: str | None = Query(None),
    limit: int = Query(100),
) -> Any:
    items = await AssetMapService(db).list_tables(source_id, sensitivity, limit)
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)


@router.get("/orphans", dependencies=_READ_DEPS)
async def orphan_assets(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    items = await AssetMapService(db).orphan_assets()
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)


# ----------------------------------------------------------------
# P2 Enhancement: 图谱 / 热力 / 责任人视图
# ----------------------------------------------------------------


@router.get("/graph", dependencies=_READ_DEPS)
async def get_graph(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    domain: str | None = Query(None, description="按域过滤"),
    depth: int = Query(3, ge=1, le=10, description="图遍历深度"),
    pii_only: bool = Query(False, description="仅返回含 PII 标记的节点"),
) -> Any:
    """资产图谱：返回节点+边数据，前端力导向图渲染。"""
    data = await AssetMapService(db).get_graph(domain=domain, depth=depth, pii_only=pii_only)
    return ok(data=data, trace_id=trace_id)


@router.get("/heatmap", dependencies=_READ_DEPS)
async def get_heatmap(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    dimension: str = Query("domain", description="聚合维度: domain/sensitivity/owner/dw_layer"),
) -> Any:
    """敏感分布热力图：按维度聚合返回分桶数据。"""
    data = await AssetMapService(db).get_heatmap(dimension=dimension)
    return ok(data=data, trace_id=trace_id)


@router.get("/owner-view", dependencies=_READ_DEPS)
async def get_owner_view(
    owner_id: Annotated[int, Query(description="责任人 ID")],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """责任人视图：按 owner_id 聚合资产统计。"""
    data = await AssetMapService(db).get_owner_view(owner_id=owner_id)
    return ok(data=data, trace_id=trace_id)
