"""资产地图 API（TD §12.11 / FR-18）。"""

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
