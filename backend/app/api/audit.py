"""审计日志查询 API（对齐 TD §12.10 / FR-16）。

提供审计日志的检索与合规导出能力，供合规官/审计员查询。
所有查询端点均须认证（require_roles），列表仅支持分页只读查询；
导出端点（``GET /audit/export``）支持 CSV/JSON，供合规留档。
"""

from __future__ import annotations

import csv
import io
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.audit_i18n import describe_audit
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.models.audit import AuditLog
from app.models.user import User

router = APIRouter(prefix="/audit", tags=["audit"])

#: 审计读权限：管理/合规角色（viewer 等只读角色不可见含 actor/detail/PII 标记的完整审计）。
_READ_ROLES = ("platform_admin", "domain_admin", "compliance_officer")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]

#: 审计导出权限：对齐前端 audit:export 基线（仅 platform_admin/compliance_officer）——
#: domain_admin 有 audit:view 可查可看，但导出留档是合规职责，不授予避免「有权限无按钮」。
_EXPORT_DEPS = [
    Depends(require_roles("platform_admin", "compliance_officer")),
    Depends(guard_against_injection),
]

#: 导出单次上限：防一次性拉取全表压垮 DB/网络（合规留档按需分批）。
_EXPORT_LIMIT_MAX = 10_000


def _apply_filters(
    stmt: Any,
    count_stmt: Any,
    *,
    actor_id: int | None,
    actor_keyword: str | None,
    entity_type: str | None,
    entity_id: str | None,
    trace_id_filter: str | None,
    pii_access: bool | None,
) -> tuple[Any, Any]:
    """公共过滤条件（列表与导出共用，避免两处漂移）。"""
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
        count_stmt = count_stmt.where(AuditLog.actor_id == actor_id)
    if actor_keyword:
        # 操作人姓名/用户名模糊搜索（企业级检索：用户记姓名而非数字 ID）。
        # autoescape=True 转义 %/_ 并生成 ESCAPE 子句（对齐 repo 的 LIKE 转义教训）。
        cond = User.display_name.contains(actor_keyword, autoescape=True) | User.username.contains(
            actor_keyword, autoescape=True
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
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
    return stmt, count_stmt


@router.get("", dependencies=_READ_DEPS)
async def list_audit_logs(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    actor_id: int | None = Query(None, description="操作人 ID"),
    actor_keyword: str | None = Query(None, description="操作人姓名/用户名模糊搜索"),
    entity_type: str | None = Query(None, description="实体类型"),
    entity_id: str | None = Query(None, description="实体 ID（精确匹配，如指标编码）"),
    trace_id_filter: str | None = Query(None, description="链路追踪 ID"),
    pii_access: bool | None = Query(None, description="是否 PII 访问"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Any:
    """查询审计日志（支持按 actor/actor_keyword/entity/trace_id/PII 过滤，分页）。

    返回前为每条记录 enrich 两个可读字段（不修改 WORM 表）：
    - ``actor_display``：操作人显示名（联查 user.display_name，查无则回退 #id）。
    - ``action_desc``：站在用户角度的中文描述（含 detail 摘要）。
    """
    offset = (page - 1) * page_size
    stmt = select(AuditLog, User.display_name).join(
        User, User.id == AuditLog.actor_id, isouter=True
    )
    count_stmt = select(func.count()).select_from(AuditLog).outerjoin(
        User, User.id == AuditLog.actor_id
    )

    stmt, count_stmt = _apply_filters(
        stmt,
        count_stmt,
        actor_id=actor_id,
        actor_keyword=actor_keyword,
        entity_type=entity_type,
        entity_id=entity_id,
        trace_id_filter=trace_id_filter,
        pii_access=pii_access,
    )

    total = (await db.execute(count_stmt)).scalar_one() or 0
    stmt = stmt.order_by(AuditLog.created_at.desc()).offset(offset).limit(page_size)
    rows = (await db.execute(stmt)).all()

    items: list[dict[str, Any]] = []
    for log, display_name in rows:
        item = log.to_dict()
        item["actor_display"] = display_name or "未知用户"
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


@router.get("/export", dependencies=_EXPORT_DEPS)
async def export_audit_logs(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    request: Request,
    trace_id: Annotated[str, Depends(get_trace_id)],
    actor_id: int | None = Query(None, description="操作人 ID"),
    actor_keyword: str | None = Query(None, description="操作人姓名/用户名模糊搜索"),
    entity_type: str | None = Query(None, description="实体类型"),
    entity_id: str | None = Query(None, description="实体 ID（精确匹配，如指标编码）"),
    trace_id_filter: str | None = Query(None, description="链路追踪 ID"),
    pii_access: bool | None = Query(None, description="是否 PII 访问"),
    export_format: str = Query(
        "csv", alias="format", pattern="^(csv|json)$", description="导出格式"
    ),
    limit: int = Query(5000, ge=1, le=_EXPORT_LIMIT_MAX, description="导出条数上限"),
) -> Response:
    """导出审计日志（CSV/JSON，供合规留档；导出动作本身落审计）。

    与列表共用过滤条件，但不受分页限制（上限 ``limit`` 防全表拉取）。
    CSV 输出带 UTF-8 BOM（Excel 直接打开不乱码）；JSON 输出数组。
    """
    stmt = select(AuditLog, User.display_name).join(
        User, User.id == AuditLog.actor_id, isouter=True
    )
    stmt, _ = _apply_filters(
        stmt,
        select(func.count()).select_from(AuditLog).outerjoin(
            User, User.id == AuditLog.actor_id
        ),
        actor_id=actor_id,
        actor_keyword=actor_keyword,
        entity_type=entity_type,
        entity_id=entity_id,
        trace_id_filter=trace_id_filter,
        pii_access=pii_access,
    )
    rows = (
        await db.execute(stmt.order_by(AuditLog.created_at.desc()).limit(limit))
    ).all()

    records: list[dict[str, Any]] = []
    for log, display_name in rows:
        item = log.to_dict()
        item["actor_display"] = display_name or "未知用户"
        item["action_desc"] = describe_audit(log.action, log.entity_type, log.detail_json)
        records.append(item)

    await write_audit(
        db,
        actor_id=user.id,
        action="audit.export",
        entity_type="audit_log",
        entity_id=f"items:{len(records)}",
        detail={"format": export_format, "rows": len(records)},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()

    filename = f"audit_export_{trace_id or 'all'}.{export_format}"
    if export_format == "json":
        body = json.dumps(records, ensure_ascii=False, default=str)
        return Response(
            content=body,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    columns = [
        "id",
        "actor_id",
        "actor_display",
        "action",
        "action_desc",
        "entity_type",
        "entity_id",
        "detail_json",
        "ip",
        "trace_id",
        "pii_access",
        "archived",
        "created_at",
    ]
    writer.writerow(columns)

    # CSV 注入防护（S-2，第八轮）：对齐 assetmap 导出——detail_json 等字段可能含
    # 以 = / + / - / @ 开头的值（被注入到审计 detail 的恶意文本），Excel/WPS 会当
    # 公式执行，导出前统一前缀单引号消毒（OWASP CSV Injection）。
    def _sanitize(v: object) -> str:
        s = "" if v is None else str(v)
        if s.startswith(("=", "+", "-", "@")):
            return "'" + s
        return s

    for rec in records:
        writer.writerow(
            [
                rec.get("id"),
                _sanitize(rec.get("actor_id")),
                _sanitize(rec.get("actor_display")),
                _sanitize(rec.get("action")),
                _sanitize(rec.get("action_desc")),
                _sanitize(rec.get("entity_type")),
                _sanitize(rec.get("entity_id")),
                _sanitize(
                    json.dumps(rec.get("detail_json"), ensure_ascii=False, default=str)
                    if rec.get("detail_json")
                    else ""
                ),
                _sanitize(rec.get("ip")),
                _sanitize(rec.get("trace_id")),
                rec.get("pii_access"),
                rec.get("archived"),
                _sanitize(rec.get("created_at")),
            ]
        )
    # UTF-8 BOM：Excel 打开避免中文乱码
    body = "\ufeff" + buffer.getvalue()
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
