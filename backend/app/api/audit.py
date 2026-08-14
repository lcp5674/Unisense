"""审计日志查询 API（对齐 TD §12.10 / FR-16）。

提供审计日志的检索能力，供合规官/审计员查询。
所有查询端点均须认证（require_roles），且仅支持分页只读查询。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.audit_i18n import describe_audit
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.models.audit import AuditLog
from app.models.user import User

router = APIRouter(prefix="/audit", tags=["audit"])

#: 审计读权限：管理/合规角色（viewer 等只读角色不可见含 actor/detail/PII 标记的完整审计）。
_READ_ROLES = ("platform_admin", "domain_admin", "compliance_officer")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]


@router.get("", dependencies=_READ_DEPS)
async def list_audit_logs(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    actor_id: int | None = Query(None, description="操作人 ID"),
    entity_type: str | None = Query(None, description="实体类型"),
    entity_id: str | None = Query(None, description="实体 ID（精确匹配，如指标编码）"),
    trace_id_filter: str | None = Query(None, description="链路追踪 ID"),
    pii_access: bool | None = Query(None, description="是否 PII 访问"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Any:
    """查询审计日志（支持按 actor/entity/trace_id/PII 过滤，分页）。

    返回前为每条记录 enrich 两个可读字段（不修改 WORM 表）：
    - ``actor_display``：操作人显示名（联查 user.display_name，查无则回退 #id）。
    - ``action_desc``：站在用户角度的中文描述（含 detail 摘要）。
    """
    offset = (page - 1) * page_size
    stmt = select(AuditLog, User.display_name).join(
        User, User.id == AuditLog.actor_id, isouter=True
    )
    count_stmt = select(func.count()).select_from(AuditLog)

    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
        count_stmt = count_stmt.where(AuditLog.actor_id == actor_id)
    if entity_type is not None:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
        count_stmt = count_stmt.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
        count_stmt = count_stmt.where(AuditLog.entity_id == entity_id)
    if trace_id_filter is not None:
        stmt = stmt.where(AuditLog.trace_id == trace_id_filter)
        count_stmt = count_stmt.where(AuditLog.trace_id == trace_id_filter)
    if pii_access is not None:
        stmt = stmt.where(AuditLog.pii_access == pii_access)
        count_stmt = count_stmt.where(AuditLog.pii_access == pii_access)

    total = (await db.execute(count_stmt)).scalar_one() or 0
    stmt = stmt.order_by(AuditLog.created_at.desc()).offset(offset).limit(page_size)
    rows = (await db.execute(stmt)).all()

    items: list[dict[str, Any]] = []
    for log, display_name in rows:
        item = log.to_dict()
        item["actor_display"] = display_name or f"用户 #{log.actor_id}"
        item["action_desc"] = describe_audit(log.action, log.entity_type, log.detail_json)
        items.append(item)

    return ok(
        data={
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        },
        trace_id=trace_id,
    )
