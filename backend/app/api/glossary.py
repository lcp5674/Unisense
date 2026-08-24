"""术语库 API（TD §12.14 / FR-08）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.glossary.schemas import (
    ConflictResolve,
    TermBatchOp,
    TermCreate,
    TermNameInfer,
    TermRelationCreate,
    TermUpdate,
)
from app.services.glossary.service import GlossaryService
from app.services.master_data_review.schemas import (
    ReviewApproveRequest,
    ReviewRejectRequest,
    ReviewSubmitRequest,
)

router = APIRouter(prefix="/terms", tags=["glossary"])

_WRITE_ROLES = ("metric_owner", "domain_admin", "platform_admin")
_GOV_ROLES = ("domain_admin", "platform_admin")
_READ_ROLES = ("metric_owner", "domain_admin", "platform_admin", "reviewer", "viewer")
# 审核端点角色门禁（对齐指标审核流）：平台管理员/域管理员/评审员可审
_REVIEW_ROLES = ("platform_admin", "domain_admin", "reviewer")
# 直发通道仅平台管理员（系统/种子/批量导入兜底），业务用户发布须走 submit+approve 审核流
_ADMIN_ROLES = ("platform_admin",)
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]
# 写端点统一挂注入守卫（纵深防御：ORM 参数化兜底之外拦截注入 payload）
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]
_GOV_DEPS = [Depends(require_roles(*_GOV_ROLES)), Depends(guard_against_injection)]
_REVIEW_DEPS = [Depends(require_roles(*_REVIEW_ROLES)), Depends(guard_against_injection)]


@router.post("", status_code=201, dependencies=_WRITE_DEPS)
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
        entity_id=str(payload.term_code),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post("/infer", dependencies=_WRITE_DEPS)
async def infer_term_suggestion(
    payload: TermNameInfer,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """基于术语名称用 LLM 推断定义/同义词/边界说明（建议，不落库）。"""
    data = await GlossaryService(db).infer_term_suggestion(payload.name)
    await write_audit(
        db,
        actor_id=user.id,
        action="term.infer",
        entity_type="term",
        entity_id=payload.name[:64],
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=data, trace_id=trace_id)


@router.post("/batch-submit", dependencies=_WRITE_DEPS + [Depends(require_roles(*_ADMIN_ROLES))])
async def batch_submit_terms(
    payload: TermBatchOp,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """批量发布术语（207 语义：逐条处理，部分失败不阻断成功项）。

    批量导入走 admin 直发通道（绕过单条审核）；业务用户单条发布须走 submit+approve 审核流。
    """
    data = await GlossaryService(db).batch_submit_terms(payload.term_codes, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="term.batch_submit",
        entity_type="term",
        entity_id=f"count={len(payload.term_codes)}",
        detail={"term_codes": payload.term_codes},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=data, trace_id=trace_id)


@router.post("/batch-deprecate", dependencies=_WRITE_DEPS)
async def batch_deprecate_terms(
    payload: TermBatchOp,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """批量废弃术语（207 语义：逐条处理，部分失败不阻断成功项）。"""
    data = await GlossaryService(db).batch_deprecate_terms(payload.term_codes, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="term.batch_deprecate",
        entity_type="term",
        entity_id=f"count={len(payload.term_codes)}",
        detail={"term_codes": payload.term_codes},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=data, trace_id=trace_id)


@router.get("", dependencies=_READ_DEPS)
async def list_terms(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    domain: str | None = Query(None),
    status: str | None = Query(None),
    search: str | None = Query(None),
    owner_id: int | None = Query(None, description="责任人（Owner）ID 过滤"),
    page: int = Query(1),
    page_size: int = Query(20),
) -> Any:
    items, total = await GlossaryService(db).list_terms(
        domain, status, search, page_size, (page - 1) * page_size, owner_id
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


@router.post("/{term_code}/submit", dependencies=_WRITE_DEPS)
async def submit_term(
    term_code: str,
    request: ReviewSubmitRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """提交术语审核（DRAFT → REVIEW，术语是业务概念标准层，发布须先审）。"""
    resp = await GlossaryService(db).submit_term(
        term_code, request, user.id, role=user.role, user_domain=user.domain
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="term.submit",
        entity_type="term",
        entity_id=term_code,
        detail={"change_reason": request.change_reason},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post(
    "/{term_code}/publish",
    dependencies=_WRITE_DEPS + [Depends(require_roles(*_ADMIN_ROLES))],
)
async def publish_term(
    term_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """直接发布术语（平台管理员直发通道，含"再次发布"能力；业务用户走 submit+approve 审核流）。"""
    resp = await GlossaryService(db).publish_term(term_code, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="term.publish",
        entity_type="term",
        entity_id=term_code,
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post("/{term_code}/approve", dependencies=_REVIEW_DEPS)
async def approve_term(
    term_code: str,
    request: ReviewApproveRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """审核通过术语（REVIEW → PUBLISHED，评审人身份校验 + 自审禁止）。"""
    resp = await GlossaryService(db).approve_term(
        term_code, request, user.id, role=user.role, user_domain=user.domain
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="term.approve",
        entity_type="term",
        entity_id=term_code,
        detail={"comment": request.comment},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post("/{term_code}/reject", dependencies=_REVIEW_DEPS)
async def reject_term(
    term_code: str,
    request: ReviewRejectRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """审核驳回术语（REVIEW → DRAFT，驳回原因落库并通知提交人）。"""
    resp = await GlossaryService(db).reject_term(
        term_code, request, user.id, role=user.role, user_domain=user.domain
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="term.reject",
        entity_type="term",
        entity_id=term_code,
        detail={"reason": request.reason},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.put("/{term_code}", dependencies=_WRITE_DEPS)
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


@router.post("/{term_code}/deprecate", dependencies=_WRITE_DEPS)
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
    dependencies=_GOV_DEPS,
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
        action="glossary.resolve_conflict",
        entity_type="glossary_conflict",
        entity_id=str(conflict_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post("/{term_code}/relations", dependencies=_WRITE_DEPS)
async def create_relation(
    term_code: str,
    payload: TermRelationCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await GlossaryService(db).create_term_relation(term_code, payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="term.create_relation",
        entity_type="term_relation",
        entity_id=f"{term_code}->{payload.target_term_id}",
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.get("/{term_code}/relations", dependencies=_READ_DEPS)
async def list_term_relations(
    term_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """查某术语的全部关系（作为源或目标），供前端关系图谱/详情展示。"""
    data = await GlossaryService(db).list_term_relations(term_code)
    return ok(data={"items": data, "total": len(data)}, trace_id=trace_id)
