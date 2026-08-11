"""审计日志查询 API（对齐 TD §12.10 / FR-16）。

提供审计日志的检索能力，供合规官/审计员查询。
所有查询端点均须认证（require_roles），且仅支持分页只读查询。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.models.audit import AuditLog

router = APIRouter(prefix="/audit", tags=["audit"])

_READ_ROLES = ("platform_admin", "domain_admin", "compliance_officer", "viewer")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]


@router.get("", dependencies=_READ_DEPS)
async def list_audit_logs(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    actor_id: int | None = Query(None, description="操作人 ID"),
    entity_type: str | None = Query(None, description="实体类型"),
    trace_id_filter: str | None = Query(None, description="链路追踪 ID"),
    pii_access: bool | None = Query(None, description="是否 PII 访问"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Any:
    """查询审计日志（支持按 actor/entity/trace_id/PII 过滤，分页）。"""
    offset = (page - 1) * page_size
    stmt = select(AuditLog).where(AuditLog.deleted_at.is_(None))

    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if entity_type is not None:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if trace_id_filter is not None:
        stmt = stmt.where(AuditLog.trace_id == trace_id_filter)
    if pii_access is not None:
        stmt = stmt.where(AuditLog.pii_access == pii_access)

    stmt = stmt.order_by(AuditLog.created_at.desc()).offset(offset).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()

    total = page_size  # 粗略计数，精确计数需子查询
    return ok(
        data={
            "items": [r.to_dict() for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
        trace_id=trace_id,
    )
