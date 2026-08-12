"""维度管理 API（TD §12.15 / FR-05 / FR-09）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.audit import write_audit
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.dimension.schemas import (
    DimensionCreate,
    DimensionMappingCreate,
    DimensionMemberCreate,
    DimensionUpdate,
    MetricDimensionBind,
    ReconciliationReview,
    ReconciliationSubmit,
)
from app.services.dimension.service import DimensionService

router = APIRouter(prefix="/dimensions", tags=["dimension"])

_WRITE_ROLES = ("metric_owner", "domain_admin", "platform_admin")
_GOV_ROLES = ("domain_admin", "platform_admin")
_READ_ROLES = ("metric_owner", "domain_admin", "platform_admin", "reviewer", "viewer")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]


@router.post("", status_code=201, dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def create_dimension(
    payload: DimensionCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).create_dimension(payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.create",
        entity_type="dimension",
        entity_id=payload.dim_code,
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.get("", dependencies=_READ_DEPS)
async def list_dimensions(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    domain: str | None = Query(None),
    status: str | None = Query(None),
) -> Any:
    items = await DimensionService(db).list_dimensions(domain, status)
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)


@router.post("/mappings", dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def create_mapping(
    payload: DimensionMappingCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).create_mapping(payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.mapping.create",
        entity_type="dimension_mapping",
        entity_id=f"{payload.source_dim_code}:{payload.target_dim_code}",
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.get("/mappings", dependencies=_READ_DEPS)
async def list_mappings(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    source_dim_code: str | None = Query(None),
) -> Any:
    items = await DimensionService(db).list_mappings(source_dim_code)
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)


@router.post("/reconciliations", dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def submit_reconciliation(
    payload: ReconciliationSubmit,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).submit_reconciliation(payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="reconciliation.submit",
        entity_type="reconciliation",
        entity_id=f"metric:{payload.metric_id}:{payload.dim_code}",
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.get("/reconciliations", dependencies=_READ_DEPS)
async def list_reconciliations(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    status: str | None = Query(None),
) -> Any:
    items = await DimensionService(db).list_reconciliations(status)
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)


@router.post(
    "/reconciliations/{rec_id}/review",
    dependencies=[Depends(require_roles(*_GOV_ROLES))],
)
async def review_reconciliation(
    rec_id: int,
    payload: ReconciliationReview,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).review_reconciliation(rec_id, payload, reviewer_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="reconciliation.review",
        entity_type="reconciliation",
        entity_id=str(rec_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.get("/{dim_code}", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_dimension(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    return ok(data=await DimensionService(db).get_dimension(dim_code), trace_id=trace_id)


@router.put("/{dim_code}", dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def update_dimension(
    dim_code: str,
    payload: DimensionUpdate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).update_dimension(dim_code, payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.update",
        entity_type="dimension",
        entity_id=dim_code,
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post("/{dim_code}/deprecate", dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def deprecate_dimension(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).deprecate_dimension(dim_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.deprecate",
        entity_type="dimension",
        entity_id=dim_code,
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post("/{dim_code}/publish", dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def publish_dimension(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).publish_dimension(dim_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.publish",
        entity_type="dimension",
        entity_id=dim_code,
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post("/{dim_code}/members", dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def create_member(
    dim_code: str,
    payload: DimensionMemberCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).create_member(payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.member.create",
        entity_type="dimension_member",
        entity_id=f"{payload.dim_code}:{payload.member_code}",
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.get("/{dim_code}/members", dependencies=_READ_DEPS)
async def list_members(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    items = await DimensionService(db).list_members(dim_code)
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)


@router.post(
    "/{dim_code}/metrics",
    dependencies=[Depends(require_roles(*_WRITE_ROLES))],
)
async def bind_metric_dimension(
    dim_code: str,
    payload: MetricDimensionBind,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).bind_metric_dimension(payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.metric.bind",
        entity_type="metric_dimension",
        entity_id=f"{payload.metric_id}:{payload.dim_code}",
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.get("/{metric_id}/metric-dimensions", dependencies=_READ_DEPS)
async def list_metric_dimensions(
    metric_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    items = await DimensionService(db).list_metric_dimensions(metric_id)
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)
