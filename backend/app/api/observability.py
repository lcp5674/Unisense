"""可观测性 API（TD §12.10 / FR-16）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.audit import write_audit
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.observability.schemas import FeedbackCreate
from app.services.observability.service import ObservabilityService

router = APIRouter(prefix="/observability", tags=["observability"])

_WRITE_ROLES = ("metric_owner", "domain_admin", "platform_admin", "viewer")
_READ_ROLES = ("metric_owner", "domain_admin", "platform_admin", "reviewer", "viewer")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]


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
    return ok(data=resp, trace_id=trace_id)


@router.get("/feedback", dependencies=_READ_DEPS)
async def list_feedback(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    target_type: str | None = Query(None),
    limit: int = Query(100),
) -> Any:
    items = await ObservabilityService(db).list_feedback(target_type, limit)
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)


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
