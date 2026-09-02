"""术语库 API（TD §12.14 / FR-08）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.batch_common import (
    BatchCodesRequest,
    BatchRejectRequest,
    BatchResponse,
    BatchSubmitRequest,
    batch_audit_action,
    batch_failed_codes,
    batch_response,
    run_batch,
)
from app.api.deps import CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.glossary.schemas import (
    ConflictResolve,
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
_READ_ROLES = (
    "metric_owner", "domain_admin", "platform_admin", "reviewer", "viewer",
    "compliance_officer", "analyst",
)
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
    resp = await GlossaryService(db).create_term(
        payload, user.id, role=user.role, user_domains=user.domains_all()
    )
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


# ---- 批量治理端点（TD §13：逐条收集结果不整体失败；执行语义统一 app.api.batch_common）----
# 业务用户批量发布须走 submit+approve 审核流；批量导入/种子走 admin 直发 batch-publish。


@router.post(
    "/batch-submit",
    response_model=ApiResponse[BatchResponse],
    summary="批量提交术语审核（DRAFT → REVIEW，可带评审指派）",
    dependencies=_WRITE_DEPS,
)
async def batch_submit_terms(
    request: BatchSubmitRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条 DRAFT→REVIEW；单条失败不阻断其余（返回逐条结果）。"""
    service = GlossaryService(db)

    async def run_one(item) -> None:
        await service.submit_term(
            item.code,
            ReviewSubmitRequest(
                change_reason=item.change_reason,
                reviewer_id=item.reviewer_id,
                reviewer_type=item.reviewer_type,
                reviewer_domain=item.reviewer_domain,
            ),
            actor_id=user.id,
            role=user.role,
            user_domains=user.domains_all(),
        )

    results = await run_batch(
        db,
        units=request.items,
        code_of=lambda item: item.code,
        run=run_one,
        abort_message="批量提交内部错误，已中止后续项",
    )
    await write_audit(
        db,
        actor_id=user.id,
        action=batch_audit_action("term.batch_submit", results),
        entity_type="term",
        entity_id=f"batch:{len(request.items)}",
        detail={
            "failed_codes": batch_failed_codes(results),
            "ok": sum(1 for r in results if r.ok),
            "fail": sum(1 for r in results if not r.ok),
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=batch_response(results), trace_id=trace_id)


@router.post(
    "/batch-publish",
    response_model=ApiResponse[BatchResponse],
    summary="批量发布术语（platform_admin 直发通道，绕过单条审核）",
    dependencies=_WRITE_DEPS + [Depends(require_roles(*_ADMIN_ROLES))],
)
async def batch_publish_terms(
    request: BatchCodesRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """批量导入/种子走 admin 直发（DRAFT/DEPRECATED → PUBLISHED，幂等跳过已发布）。"""
    service = GlossaryService(db)
    results = await run_batch(
        db,
        units=request.codes,
        code_of=lambda code: code,
        run=lambda code: service.publish_term(code, user.id),
        abort_message="批量发布内部错误，已中止后续项",
    )
    await write_audit(
        db,
        actor_id=user.id,
        action=batch_audit_action("term.batch_publish", results),
        entity_type="term",
        entity_id=f"batch:{len(request.codes)}",
        detail={
            "failed_codes": batch_failed_codes(results),
            "ok": sum(1 for r in results if r.ok),
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=batch_response(results), trace_id=trace_id)


@router.post(
    "/batch-approve",
    response_model=ApiResponse[BatchResponse],
    summary="批量审核通过术语（REVIEW → PUBLISHED，即批量发布）",
    dependencies=_REVIEW_DEPS,
)
async def batch_approve_terms(
    request: BatchCodesRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条 REVIEW→PUBLISHED；评审人指派校验由 service 层逐条执行。"""
    service = GlossaryService(db)
    results = await run_batch(
        db,
        units=request.codes,
        code_of=lambda code: code,
        run=lambda code: service.approve_term(
            code,
            ReviewApproveRequest(),
            actor_id=user.id,
            role=user.role,
            user_domains=user.domains_all(),
        ),
        abort_message="批量通过内部错误，已中止后续项",
    )
    await write_audit(
        db,
        actor_id=user.id,
        action=batch_audit_action("term.batch_approve", results),
        entity_type="term",
        entity_id=f"batch:{len(request.codes)}",
        detail={
            "failed_codes": batch_failed_codes(results),
            "ok": sum(1 for r in results if r.ok),
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=batch_response(results), trace_id=trace_id)


@router.post(
    "/batch-reject",
    response_model=ApiResponse[BatchResponse],
    summary="批量审核驳回术语（REVIEW → DRAFT）",
    dependencies=_REVIEW_DEPS,
)
async def batch_reject_terms(
    request: BatchRejectRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条 REVIEW→DRAFT；驳回原因统一作用于所有项并落库可追溯。"""
    service = GlossaryService(db)
    results = await run_batch(
        db,
        units=request.codes,
        code_of=lambda code: code,
        run=lambda code: service.reject_term(
            code,
            ReviewRejectRequest(reason=request.reason),
            actor_id=user.id,
            role=user.role,
            user_domains=user.domains_all(),
        ),
        abort_message="批量驳回内部错误，已中止后续项",
    )
    await write_audit(
        db,
        actor_id=user.id,
        action=batch_audit_action("term.batch_reject", results),
        entity_type="term",
        entity_id=f"batch:{len(request.codes)}",
        detail={
            "failed_codes": batch_failed_codes(results),
            "ok": sum(1 for r in results if r.ok),
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=batch_response(results), trace_id=trace_id)


@router.post(
    "/batch-deprecate",
    response_model=ApiResponse[BatchResponse],
    summary="批量废弃术语（PUBLISHED → DEPRECATED）",
    dependencies=_WRITE_DEPS,
)
async def batch_deprecate_terms(
    request: BatchCodesRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条 PUBLISHED→DEPRECATED（已废弃幂等跳过）。"""
    service = GlossaryService(db)
    results = await run_batch(
        db,
        units=request.codes,
        code_of=lambda code: code,
        run=lambda code: service.deprecate_term(code, user.id),
        abort_message="批量废弃内部错误，已中止后续项",
    )
    await write_audit(
        db,
        actor_id=user.id,
        action=batch_audit_action("term.batch_deprecate", results),
        entity_type="term",
        entity_id=f"batch:{len(request.codes)}",
        detail={
            "failed_codes": batch_failed_codes(results),
            "ok": sum(1 for r in results if r.ok),
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=batch_response(results), trace_id=trace_id)


@router.post(
    "/batch-reactivate",
    response_model=ApiResponse[BatchResponse],
    summary="批量重新启用已废弃术语（DEPRECATED → DRAFT）",
    dependencies=_WRITE_DEPS,
)
async def batch_reactivate_terms(
    request: BatchCodesRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条 DEPRECATED→DRAFT；权限（管理员/原 Owner）由 service 层逐条校验。"""
    service = GlossaryService(db)
    results = await run_batch(
        db,
        units=request.codes,
        code_of=lambda code: code,
        run=lambda code: service.reactivate_term(code, actor_id=user.id, role=user.role),
        abort_message="批量重新启用内部错误，已中止后续项",
    )
    await write_audit(
        db,
        actor_id=user.id,
        action=batch_audit_action("term.batch_reactivate", results),
        entity_type="term",
        entity_id=f"batch:{len(request.codes)}",
        detail={
            "failed_codes": batch_failed_codes(results),
            "ok": sum(1 for r in results if r.ok),
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=batch_response(results), trace_id=trace_id)


@router.post(
    "/batch-delete",
    response_model=ApiResponse[BatchResponse],
    summary="批量软删除术语（仅 DRAFT/DEPRECATED 可删）",
    dependencies=_WRITE_DEPS,
)
async def batch_delete_terms(
    request: BatchCodesRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条软删草稿/废弃术语；权限（管理员/原 Owner）由 service 层逐条校验。"""
    service = GlossaryService(db)
    results = await run_batch(
        db,
        units=request.codes,
        code_of=lambda code: code,
        run=lambda code: service.delete_term(code, actor_id=user.id, role=user.role),
        abort_message="批量删除内部错误，已中止后续项",
    )
    await write_audit(
        db,
        actor_id=user.id,
        action=batch_audit_action("term.batch_delete", results),
        entity_type="term",
        entity_id=f"batch:{len(request.codes)}",
        detail={
            "failed_codes": batch_failed_codes(results),
            "ok": sum(1 for r in results if r.ok),
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=batch_response(results), trace_id=trace_id)


@router.get("", dependencies=_READ_DEPS)
async def list_terms(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    domain: str | None = Query(None),
    status: str | None = Query(None),
    search: str | None = Query(None),
    owner_id: int | None = Query(None, description="责任人（Owner）ID 过滤"),
    reviewed_by: int | None = Query(
        None, description="我审过的（通过/驳回人 ID 过滤，供统一主数据审批工作台）"
    ),
    deleted: bool = Query(False, description="是否查看回收站（已软删记录）"),
    # P4 分页边界：page ge=1 防 page=0 负 offset；page_size le=200 防无界全量拉取
    page: int = Query(1, ge=1, le=10000),
    page_size: int = Query(20, ge=1, le=200),
) -> Any:
    items, total = await GlossaryService(db).list_terms(
        domain,
        status,
        search,
        page_size,
        (page - 1) * page_size,
        owner_id,
        deleted,
        reviewed_by,
        visible_actor_id=user.id,
        visible_role=user.role,
        visible_user_domains=user.domains_all(),
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
    return ok(
        data=await GlossaryService(db).get_term_visible(
            term_code, actor_id=user.id, role=user.role
        ),
        trace_id=trace_id,
    )


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
        term_code, request, user.id, role=user.role, user_domains=user.domains_all()
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
        term_code, request, user.id, role=user.role, user_domains=user.domains_all()
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
        term_code, request, user.id, role=user.role, user_domains=user.domains_all()
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
    resp = await GlossaryService(db).update_term(
        term_code, payload, user.id, role=user.role, user_domains=user.domains_all()
    )
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
    resp = await GlossaryService(db).deprecate_term(
        term_code, user.id, role=user.role, user_domains=user.domains_all()
    )
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
    "/{term_code}/reactivate",
    summary="重新启用已废弃术语（DEPRECATED → DRAFT，可编辑后重新走审核）",
    dependencies=_WRITE_DEPS,
)
async def reactivate_term(
    term_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """DEPRECATED → DRAFT：回到草稿可编辑，重新提交审核后才发布（不绕过审核）。"""
    resp = await GlossaryService(db).reactivate_term(
        term_code, actor_id=user.id, role=user.role, user_domains=user.domains_all()
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="term.reactivate",
        entity_type="term",
        entity_id=term_code,
        detail={"status": resp.status},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post(
    "/{term_code}/delete",
    summary="软删除术语（仅 DRAFT/DEPRECATED 可删；审核中/启用中禁止）",
    dependencies=_WRITE_DEPS,
)
async def delete_term(
    term_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """软删草稿/废弃术语；仅管理员或原 Owner（service 层校验）。"""
    resp = await GlossaryService(db).delete_term(
        term_code, actor_id=user.id, role=user.role, user_domains=user.domains_all()
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="term.delete",
        entity_type="term",
        entity_id=term_code,
        detail={"status": resp.status},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post(
    "/{term_code}/restore",
    summary="恢复已软删术语（回收站恢复）",
    dependencies=_WRITE_DEPS,
)
async def restore_term(
    term_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """回收站恢复软删术语；仅管理员或原 Owner（service 层校验）。"""
    resp = await GlossaryService(db).restore_term(
        term_code, actor_id=user.id, role=user.role, user_domains=user.domains_all()
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="term.restore",
        entity_type="term",
        entity_id=term_code,
        detail={"status": resp.status},
        ip=client_ip(http_req),
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
    resp = await GlossaryService(db).create_term_relation(
        term_code, payload, actor_id=user.id, role=user.role, user_domains=user.domains_all()
    )
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
