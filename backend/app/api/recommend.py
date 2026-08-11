"""推荐服务 API（TD §12.12 / FR-19）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.recommend.service import RecommendService

router = APIRouter(prefix="/recommend", tags=["recommend"])

_READ_ROLES = ("metric_owner", "domain_admin", "platform_admin", "reviewer", "viewer")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]


@router.get("/metrics/{metric_id}/related", dependencies=_READ_DEPS)
async def related_metrics(
    metric_id: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    limit: int = Query(20),
) -> Any:
    items = await RecommendService(db).related_metrics(metric_id, limit)
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)


@router.get("/metrics", dependencies=_READ_DEPS)
async def recommend_metrics(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    limit: int = Query(20),
) -> Any:
    # PLAT-2: 以认证身份 user.id 替代 client 传入的 user_id，杜绝 IDOR 越权读取
    items = await RecommendService(db).recommend_metrics(user.id, limit)
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)


@router.get("/terms", dependencies=_READ_DEPS)
async def recommend_terms(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    limit: int = Query(20),
) -> Any:
    items = await RecommendService(db).recommend_terms(limit)
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)
