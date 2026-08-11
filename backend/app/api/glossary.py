"""术语库 API（TD §12.14 / FR-08）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.audit import write_audit
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.glossary.schemas import (
    ConflictResolve,
    TermCreate,
    TermRelationCreate,
    TermUpdate,
)
from app.services.glossary.service import GlossaryService

router = APIRouter(prefix="/terms", tags=["glossary"])

_WRITE_ROLES = ("metric_owner", "domain_admin", "platform_admin")
_GOV_ROLES = ("domain_admin", "platform_admin")
_READ_ROLES = ("metric_owner", "domain_admin", "platform_admin", "reviewer", "viewer")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]


@router.post("", status_code=201, dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def create_term(
    payload: TermCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """创建术语（DRAFT），自动触发同义词/名称冲突检测。"""
    resp = await GlossaryService(db).create_term(payload, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="term.create",
        entity_type="term",
        entity_id=payload.term_code,
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.get("", dependencies=_READ_DEPS)
async def list_terms(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    domain: str | None = Query(None),
    status: str | None = Query(None),
    search: str | None = Query(None),
    page: int = Query(1),
    page_size: int = Query(20),
) -> Any:
    items, total = await GlossaryService(db).list_terms(
        domain, status, search, page_size, (page - 1) * page_size
    )
    return ok(
        data={"items": items, "total": total, "page": page, "page_size": page_size},
        trace_id=trace_id,
    )


@router.get("/conflicts", dependencies=_READ_DEPS)
async def list_conflicts(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    status: str | None = Query(None),
) -> Any:
    items = await GlossaryService(db).list_conflicts(status)
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)


@router.get("/{term_code}", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_term(
    term_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    return ok(data=await GlossaryService(db).get_term(term_code), trace_id=trace_id)


@router.post("/{term_code}/submit", dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def submit_term(
    term_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await GlossaryService(db).submit_term(term_code, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="term.submit",
        entity_type="term",
        entity_id=term_code,
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.put("/{term_code}", dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def update_term(
    term_code: str,
    payload: TermUpdate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await GlossaryService(db).update_term(term_code, payload, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="term.update",
        entity_type="term",
        entity_id=term_code,
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post("/{term_code}/deprecate", dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def deprecate_term(
    term_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await GlossaryService(db).deprecate_term(term_code, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="term.deprecate",
        entity_type="term",
        entity_id=term_code,
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post(
    "/conflicts/{conflict_id}/resolve",
    dependencies=[Depends(require_roles(*_GOV_ROLES))],
)
async def resolve_conflict(
    conflict_id: int,
    payload: ConflictResolve,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await GlossaryService(db).resolve_conflict(conflict_id, payload.decision, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="glossary_conflict.resolve",
        entity_type="glossary_conflict",
        entity_id=str(conflict_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post("/{term_code}/relations", dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def create_relation(
    term_code: str,
    payload: TermRelationCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await GlossaryService(db).create_term_relation(term_code, payload)
    await db.commit()
    return ok(data=resp, trace_id=trace_id)
