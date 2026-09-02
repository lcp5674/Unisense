"""逻辑度量目录 API（OneData 原子层，TD §4.2 / FR-02-08）。

照 dimension API 模式：角色依赖 + 注入守卫 + 域作用域守卫（domain_admin/metric_owner
仅可操作本域资源）+ 服务端分页。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.batch_common import (
    BatchCodesRequest,
    BatchRejectRequest,
    BatchResponse,
    BatchSubmitItem,
    BatchSubmitRequest,
    batch_audit_action,
    batch_failed_codes,
    batch_response,
    run_batch,
)
from app.api.deps import CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.exceptions import AuthError, ConflictError, NotFoundError
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.measure_catalog.schemas import (
    MeasureApproveRequest,
    MeasureAutoSuggestRequest,
    MeasureCreate,
    MeasureInferSynonymsRequest,
    MeasureRejectRequest,
    MeasureResponse,
    MeasureSubmitRequest,
    MeasureUpdate,
)
from app.services.measure_catalog.service import MeasureCatalogService

router = APIRouter(prefix="/measure-catalogs", tags=["measure_catalog"])

_WRITE_ROLES = ("metric_owner", "domain_admin", "platform_admin")
_READ_ROLES = (
    "metric_owner", "domain_admin", "platform_admin", "reviewer", "viewer",
    "compliance_officer", "analyst",
)
# 审核端点角色门禁（对齐指标审核流）：平台管理员/域管理员/评审员可审
_REVIEW_ROLES = ("platform_admin", "domain_admin", "reviewer")
# 直发通道仅平台管理员（系统/种子/管理员兜底），业务用户发布须走 submit+approve 审核流
_ADMIN_ROLES = ("platform_admin",)
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]
# 写端点统一挂注入守卫（纵深防御：ORM 参数化兜底之外拦截注入 payload）
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]
_REVIEW_DEPS = [Depends(require_roles(*_REVIEW_ROLES)), Depends(guard_against_injection)]

# 域作用域守卫（P1-10）：domain_admin/metric_owner 仅可操作本域资源
_SCOPED_ROLES = ("domain_admin", "metric_owner")


def _assert_domain_scope(user: CurrentUser, resource_domain: str) -> None:
    # 方案 A 多角色：任一角色命中作用域角色即受域约束（主角色或 user_role 扩展）。
    # 多域并集（团队继承 ∪ 显式指定，domains_all()）：资源域必须 ∈ 权限域；
    # 无任何权限域时 fail-closed 拒绝一切域操作——此前 `and user.domain` 短路
    # 会放行 domain=NULL 的 domain_admin/metric_owner 跨任意域写（越权实测）。
    if any(r in _SCOPED_ROLES for r in user.roles_all()):
        domains = user.domains_all()
        if not domains or resource_domain not in domains:
            raise AuthError(
                f"无权限操作其他域的资源（资源域 {resource_domain}，当前权限域 {domains or '无'}）",
                error_code="FORBIDDEN",
            )


async def _scope_measure(
    measure_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
) -> None:
    """路径携带 measure_code 的写操作：加载逻辑度量并校验域作用域。"""
    measure = await MeasureCatalogService(db).get_measure(measure_code)
    if measure is None:
        raise NotFoundError(f"逻辑度量不存在: {measure_code}")
    _assert_domain_scope(user, measure.domain)


_SCOPED_DEPS = _WRITE_DEPS + [Depends(_scope_measure)]


@router.post("", status_code=201, dependencies=_WRITE_DEPS)
async def create_measure(
    payload: MeasureCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # 域作用域守卫（P1-10）：域管理员仅可在本域建逻辑度量
    _assert_domain_scope(user, payload.domain)
    resp = await MeasureCatalogService(db).create_measure(payload, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="measure_catalog.create",
        entity_type="measure_catalog",
        entity_id=resp.measure_code,
        detail={},
        trace_id=trace_id,
    )
    # T4（审查修复）：并发创建同名 → IntegrityError 转 ConflictError（409），
    # 而非 500。TOCTOU 场景下预检通过后 commit 仍可能撞唯一键。
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError(
            f"逻辑度量编码已存在: {payload.measure_code}", error_code="MEASURE_EXISTS"
        ) from None
    return ok(data=MeasureResponse.from_model(resp), trace_id=trace_id)


@router.get("", dependencies=_READ_DEPS)
async def list_measures(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    domain: str | None = Query(None),
    status: str | None = Query(None),
    keyword: str | None = Query(None, description="关键词：编码/名称/描述模糊匹配"),
    owner_id: int | None = Query(None, description="负责人 ID 过滤"),
    reviewed_by: int | None = Query(
        None, description="我审过的（通过/驳回人 ID 过滤，供统一主数据审批工作台）"
    ),
    deleted: bool = Query(False, description="是否查看回收站（已软删记录）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Any:
    items, total = await MeasureCatalogService(db).list_measures(
        domain,
        status,
        keyword,
        owner_id,
        reviewed_by=reviewed_by,
        deleted=deleted,
        page=page,
        page_size=page_size,
        visible_actor_id=user.id,
        visible_role=user.role,
        visible_user_domains=user.domains_all(),
    )
    converted = [MeasureResponse.from_model(i) for i in items]
    return ok(
        data={"items": converted, "total": total, "page": page, "page_size": page_size},
        trace_id=trace_id,
    )


@router.post(
    "/auto-suggest",
    response_model=ApiResponse[Any],
    summary="逻辑度量 AI 推断（名称/描述 → 编码/格式/单位/分类/口径）",
    # LLM 额度防护：该端点触发 LLM 命名/同义词，是"新建逻辑度量"的创建辅助，
    # 收紧为写角色（platform_admin/domain_admin/metric_owner，与创建能力对齐），
    # 避免只读角色任意调用耗尽 LLM 额度。
    dependencies=_WRITE_DEPS,
)
async def auto_suggest_measure(
    payload: MeasureAutoSuggestRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """输入名称 +（可选）描述/域/源表/度量列 → 逐字段推断，来源 llm/rule + 置信度 + 理由。

    策略：规则确定性兜底（编码/格式/单位/小数位/分类），LLM 业务增强（同义词/统计口径/
    业务域/源头系统）；LLM 不可用自动降级规则，不阻断。
    """
    resp = await MeasureCatalogService(db).auto_suggest(payload)
    return ok(data=resp, trace_id=trace_id)


@router.post(
    "/infer-synonyms",
    response_model=ApiResponse[Any],
    summary="编辑逻辑度量 AI 生成同义词（不落库，回填表单）",
    # LLM 额度防护：与 auto-suggest 一致收紧为写角色（创建/编辑能力对齐），
    # 避免只读角色任意调用耗尽 LLM 额度。
    dependencies=_WRITE_DEPS,
)
async def infer_measure_synonyms(
    payload: MeasureInferSynonymsRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """输入名称 +（可选）描述 → LLM 生成同义词候选，返回 ``{"synonyms": [...]}``。

    仅生成文本回填表单，不落库（落库仍走既有 update 流程）；LLM 不可用抛
    ``LLM_INFER_UNAVAILABLE``。
    """
    synonyms = await MeasureCatalogService(db).infer_synonyms(payload.name, payload.description)
    return ok(data={"synonyms": synonyms}, trace_id=trace_id)


@router.get("/{measure_code}", dependencies=_READ_DEPS)
async def get_measure(
    measure_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await MeasureCatalogService(db).get_measure_visible(
        measure_code, actor_id=user.id, role=user.role
    )
    return ok(data=MeasureResponse.from_model(resp), trace_id=trace_id)


@router.put("/{measure_code}", dependencies=_SCOPED_DEPS)
async def update_measure(
    measure_code: str,
    payload: MeasureUpdate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await MeasureCatalogService(db).update_measure(measure_code, payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="measure_catalog.update",
        entity_type="measure_catalog",
        entity_id=measure_code,
        detail={},
        trace_id=trace_id,
    )
    # T4（审查修复）：改编码撞唯一键 → 409（并发 TOCTOU 兜底）
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError(
            f"逻辑度量编码已存在: {payload.measure_code or measure_code}",
            error_code="MEASURE_EXISTS",
        ) from None
    return ok(data=MeasureResponse.from_model(resp), trace_id=trace_id)


@router.post(
    "/{measure_code}/publish",
    # 直发通道仅平台管理员（系统/种子/管理员兜底）；业务用户发布须走 submit+approve 审核流
    dependencies=_SCOPED_DEPS + [Depends(require_roles(*_ADMIN_ROLES))],
)
async def publish_measure(
    measure_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await MeasureCatalogService(db).publish_measure(measure_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="measure_catalog.publish",
        entity_type="measure_catalog",
        entity_id=measure_code,
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=MeasureResponse.from_model(resp), trace_id=trace_id)


@router.post(
    "/{measure_code}/submit",
    response_model=ApiResponse[MeasureResponse],
    summary="提交逻辑度量审核（DRAFT → REVIEW）",
    dependencies=_SCOPED_DEPS,
)
async def submit_measure(
    measure_code: str,
    request: MeasureSubmitRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """DRAFT → REVIEW，提交审核（度量是原子指标继承源，发布须先审）。"""
    service = MeasureCatalogService(db)
    measure = await service.submit_measure(
        measure_code, request, actor_id=user.id, role=user.role, user_domains=user.domains_all()
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="measure_catalog.submit",
        entity_type="measure_catalog",
        entity_id=measure_code,
        detail={"change_reason": request.change_reason},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=MeasureResponse.from_model(measure), trace_id=trace_id)


@router.post(
    "/{measure_code}/approve",
    response_model=ApiResponse[MeasureResponse],
    summary="审核通过逻辑度量（REVIEW → PUBLISHED）",
    dependencies=_REVIEW_DEPS,
)
async def approve_measure(
    measure_code: str,
    request: MeasureApproveRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """REVIEW → PUBLISHED，审核通过（评审人身份校验 + 自审禁止）。"""
    service = MeasureCatalogService(db)
    measure = await service.approve_measure(
        measure_code, request, actor_id=user.id, role=user.role, user_domains=user.domains_all()
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="measure_catalog.approve",
        entity_type="measure_catalog",
        entity_id=measure_code,
        detail={"comment": request.comment},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=MeasureResponse.from_model(measure), trace_id=trace_id)


@router.post(
    "/{measure_code}/reject",
    response_model=ApiResponse[MeasureResponse],
    summary="审核驳回逻辑度量（REVIEW → DRAFT）",
    dependencies=_REVIEW_DEPS,
)
async def reject_measure(
    measure_code: str,
    request: MeasureRejectRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """REVIEW → DRAFT，驳回审核（驳回原因落库并通知提交人）。"""
    service = MeasureCatalogService(db)
    measure = await service.reject_measure(
        measure_code, request, actor_id=user.id, role=user.role, user_domains=user.domains_all()
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="measure_catalog.reject",
        entity_type="measure_catalog",
        entity_id=measure_code,
        detail={"reason": request.reason},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=MeasureResponse.from_model(measure), trace_id=trace_id)


@router.post("/{measure_code}/deprecate", dependencies=_SCOPED_DEPS)
async def deprecate_measure(
    measure_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await MeasureCatalogService(db).deprecate_measure(measure_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="measure_catalog.deprecate",
        entity_type="measure_catalog",
        entity_id=measure_code,
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=MeasureResponse.from_model(resp), trace_id=trace_id)


@router.post(
    "/{measure_code}/reactivate",
    response_model=ApiResponse[MeasureResponse],
    summary="重新启用已废弃逻辑度量（DEPRECATED → DRAFT，可编辑后重新走审核）",
    dependencies=_SCOPED_DEPS,
)
async def reactivate_measure(
    measure_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """DEPRECATED → DRAFT：回到草稿可编辑，重新提交审核后才发布（不绕过审核）。"""
    resp = await MeasureCatalogService(db).reactivate_measure(measure_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="measure_catalog.reactivate",
        entity_type="measure_catalog",
        entity_id=measure_code,
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=MeasureResponse.from_model(resp), trace_id=trace_id)


@router.post(
    "/{measure_code}/delete",
    response_model=ApiResponse[MeasureResponse],
    summary="软删除逻辑度量（仅 DRAFT/DEPRECATED 可删；审核中/启用中禁止）",
    dependencies=_SCOPED_DEPS,
)
async def delete_measure(
    measure_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """软删草稿/废弃逻辑度量；仅管理员或原 Owner（service 层校验）。"""
    resp = await MeasureCatalogService(db).delete_measure(
        measure_code, actor_id=user.id, role=user.role
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="measure_catalog.delete",
        entity_type="measure_catalog",
        entity_id=measure_code,
        detail={"status": resp.status},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=MeasureResponse.from_model(resp), trace_id=trace_id)


@router.post(
    "/{measure_code}/restore",
    response_model=ApiResponse[MeasureResponse],
    summary="恢复已软删逻辑度量（回收站恢复）",
    dependencies=_SCOPED_DEPS,
)
async def restore_measure(
    measure_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """回收站恢复软删度量；仅管理员或原 Owner（service 层校验）。"""
    resp = await MeasureCatalogService(db).restore_measure(
        measure_code, actor_id=user.id, role=user.role
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="measure_catalog.restore",
        entity_type="measure_catalog",
        entity_id=measure_code,
        detail={"status": resp.status},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=MeasureResponse.from_model(resp), trace_id=trace_id)


@router.post(
    "/{measure_code}/purge",
    response_model=ApiResponse[dict],
    summary="彻底删除已软删逻辑度量（回收站硬删，仅平台管理员）",
    # 彻底删除是不可恢复的危险操作：仅平台管理员（对齐直发通道 _ADMIN_ROLES）
    dependencies=[Depends(require_roles(*_ADMIN_ROLES)), Depends(guard_against_injection)],
)
async def purge_measure(
    measure_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """回收站彻底删除软删度量（物理删除不可恢复）；仅平台管理员。"""
    await MeasureCatalogService(db).purge_measure(
        measure_code, actor_id=user.id, role=user.role
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="measure_catalog.purge",
        entity_type="measure_catalog",
        entity_id=measure_code,
        detail={},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"measure_code": measure_code}, trace_id=trace_id)


# ---- 批量治理端点（TD §13：逐条收集结果不整体失败；执行语义统一 app.api.batch_common）----


@router.post(
    "/batch-submit",
    response_model=ApiResponse[BatchResponse],
    summary="批量提交逻辑度量审核（DRAFT → REVIEW，可带评审指派）",
    dependencies=_WRITE_DEPS,
)
async def batch_submit_measures(
    request: BatchSubmitRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条 DRAFT→REVIEW；单条失败不阻断其余（返回逐条结果）。"""
    service = MeasureCatalogService(db)

    async def run_one(item: BatchSubmitItem) -> None:
        # 域作用域守卫（P1-10）：domain_admin/metric_owner 仅可操作本域资源
        measure = await service.get_measure(item.code)
        if measure is None:
            raise NotFoundError(f"逻辑度量不存在: {item.code}")
        _assert_domain_scope(user, measure.domain)
        await service.submit_measure(
            item.code,
            MeasureSubmitRequest(
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
        action=batch_audit_action("measure_catalog.batch_submit", results),
        entity_type="measure_catalog",
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
    "/batch-approve",
    response_model=ApiResponse[BatchResponse],
    summary="批量审核通过逻辑度量（REVIEW → PUBLISHED，即批量发布）",
    dependencies=_REVIEW_DEPS,
)
async def batch_approve_measures(
    request: BatchCodesRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条 REVIEW→PUBLISHED；评审人指派校验由 service 层逐条执行。"""
    service = MeasureCatalogService(db)
    results = await run_batch(
        db,
        units=request.codes,
        code_of=lambda code: code,
        run=lambda code: service.approve_measure(
            code,
            MeasureApproveRequest(),
            actor_id=user.id,
            role=user.role,
            user_domains=user.domains_all(),
        ),
        abort_message="批量通过内部错误，已中止后续项",
    )
    await write_audit(
        db,
        actor_id=user.id,
        action=batch_audit_action("measure_catalog.batch_approve", results),
        entity_type="measure_catalog",
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
    summary="批量审核驳回逻辑度量（REVIEW → DRAFT）",
    dependencies=_REVIEW_DEPS,
)
async def batch_reject_measures(
    request: BatchRejectRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条 REVIEW→DRAFT；驳回原因统一作用于所有项并落库可追溯。"""
    service = MeasureCatalogService(db)
    results = await run_batch(
        db,
        units=request.codes,
        code_of=lambda code: code,
        run=lambda code: service.reject_measure(
            code,
            MeasureRejectRequest(reason=request.reason),
            actor_id=user.id,
            role=user.role,
            user_domains=user.domains_all(),
        ),
        abort_message="批量驳回内部错误，已中止后续项",
    )
    await write_audit(
        db,
        actor_id=user.id,
        action=batch_audit_action("measure_catalog.batch_reject", results),
        entity_type="measure_catalog",
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
    summary="批量重新启用已废弃逻辑度量（DEPRECATED → DRAFT）",
    dependencies=_WRITE_DEPS,
)
async def batch_reactivate_measures(
    request: BatchCodesRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条 DEPRECATED→DRAFT（重新启用后走审核流）。"""
    service = MeasureCatalogService(db)

    async def run_one(code: str) -> None:
        measure = await service.get_measure(code)
        if measure is None:
            raise NotFoundError(f"逻辑度量不存在: {code}")
        _assert_domain_scope(user, measure.domain)
        await service.reactivate_measure(code)

    results = await run_batch(
        db,
        units=request.codes,
        code_of=lambda code: code,
        run=run_one,
        abort_message="批量重新启用内部错误，已中止后续项",
    )
    await write_audit(
        db,
        actor_id=user.id,
        action=batch_audit_action("measure_catalog.batch_reactivate", results),
        entity_type="measure_catalog",
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
    summary="批量废弃逻辑度量（PUBLISHED → DEPRECATED）",
    dependencies=_WRITE_DEPS,
)
async def batch_deprecate_measures(
    request: BatchCodesRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条 PUBLISHED→DEPRECATED（被指标引用者由 service 层废弃保护拦截）。"""
    service = MeasureCatalogService(db)

    async def run_one(code: str) -> None:
        measure = await service.get_measure(code)
        if measure is None:
            raise NotFoundError(f"逻辑度量不存在: {code}")
        _assert_domain_scope(user, measure.domain)
        await service.deprecate_measure(code)

    results = await run_batch(
        db,
        units=request.codes,
        code_of=lambda code: code,
        run=run_one,
        abort_message="批量废弃内部错误，已中止后续项",
    )
    await write_audit(
        db,
        actor_id=user.id,
        action=batch_audit_action("measure_catalog.batch_deprecate", results),
        entity_type="measure_catalog",
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
    summary="批量软删除逻辑度量（仅 DRAFT/DEPRECATED 可删）",
    dependencies=_WRITE_DEPS,
)
async def batch_delete_measures(
    request: BatchCodesRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条软删草稿/废弃逻辑度量；管理员或原 Owner（service 层逐条校验）。"""
    service = MeasureCatalogService(db)

    async def run_one(code: str) -> None:
        measure = await service.get_measure(code)
        if measure is None:
            raise NotFoundError(f"逻辑度量不存在: {code}")
        _assert_domain_scope(user, measure.domain)
        await service.delete_measure(code, actor_id=user.id, role=user.role)

    results = await run_batch(
        db,
        units=request.codes,
        code_of=lambda code: code,
        run=run_one,
        abort_message="批量删除内部错误，已中止后续项",
    )
    await write_audit(
        db,
        actor_id=user.id,
        action=batch_audit_action("measure_catalog.batch_delete", results),
        entity_type="measure_catalog",
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
