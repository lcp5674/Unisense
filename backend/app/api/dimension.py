"""维度管理 API（TD §12.15 / FR-05 / FR-09）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
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
from app.core.exceptions import AuthError, NotFoundError
from app.core.guard import (
    guard_against_injection,
    guard_against_injection_exempt,
)
from app.db.mysql import get_db_session
from app.services.dimension.schemas import (
    DimensionCreate,
    DimensionMappingCreate,
    DimensionMappingResponse,
    DimensionMappingUpdate,
    DimensionMemberCreate,
    DimensionMemberResponse,
    DimensionMemberUpdate,
    DimensionMetricBinding,
    DimensionReferenceBind,
    DimensionResponse,
    DimensionUpdate,
    MappingCoverageResponse,
    MappingValueCreate,
    MappingValueResponse,
    MemberBatchRequest,
    MetricDimensionBind,
    MetricDimensionResponse,
    PreviewValuesRequest,
    PreviewValuesResponse,
    ReconciliationResponse,
    ReconciliationReview,
    ReconciliationSubmit,
    SnapshotRunResponse,
    SnapshotValueResponse,
    SourceColumnsRequest,
    SourceColumnsResponse,
    SourceDatabasesRequest,
    SourceTablesRequest,
    SourceTablesResponse,
    TranslateRequest,
    TranslateResponse,
)
from app.services.dimension.service import DimensionService
from app.services.master_data_review.schemas import (
    ReviewApproveRequest,
    ReviewRejectRequest,
    ReviewSubmitRequest,
)

router = APIRouter(prefix="/dimensions", tags=["dimension"])

_WRITE_ROLES = ("metric_owner", "domain_admin", "platform_admin")
_GOV_ROLES = ("domain_admin", "platform_admin")
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
_GOV_DEPS = [Depends(require_roles(*_GOV_ROLES)), Depends(guard_against_injection)]
_REVIEW_DEPS = [Depends(require_roles(*_REVIEW_ROLES)), Depends(guard_against_injection)]
# 维度映射表达式豁免：expression 承载映射/转换表达式文本（可含 SQL 注释/函数），
# 仅落库存储与回显，不执行、不拼接进任何 DB 查询——全量扫描会误伤合法表达式。
# 其余字段仍全量扫描，纵深防御不削弱。
_MAPPING_DEPS = [
    Depends(require_roles(*_WRITE_ROLES)),
    Depends(guard_against_injection_exempt("expression")),
]

# 域作用域守卫（P1-10）：domain_admin/metric_owner 仅可操作本域资源，
# 防跨域越权——此前 API 仅校验角色、无 user.domain 作用域，域管理员可任意增删改他域维度。
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


async def _scope_dimension(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
) -> None:
    """路径携带 dim_code 的写操作：加载维度并校验域作用域。"""
    dim = await DimensionService(db).get_dimension(dim_code)
    if dim is None:
        raise NotFoundError(f"维度不存在: {dim_code}")
    _assert_domain_scope(user, dim.domain)


async def _scope_dimension_by_code(
    code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
) -> None:
    """body 携带 dim_code 的写操作：解析维度域并校验作用域。"""
    dim = await DimensionService(db).get_dimension(code)
    if dim is None:
        raise NotFoundError(f"维度不存在: {code}")
    _assert_domain_scope(user, dim.domain)


async def _scope_mapping(
    mapping_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
) -> None:
    """映射按 id 写操作：解析源维度域并校验作用域。"""
    mapping = await DimensionService(db).get_mapping(mapping_id)
    if mapping is None:
        raise NotFoundError(f"映射不存在: {mapping_id}")
    dim = await DimensionService(db).get_dimension(mapping.source_dim_code)
    if dim is not None:
        _assert_domain_scope(user, dim.domain)


# 写操作 + 域作用域守卫（P1-10）：域管理员/指标 Owner 仅可操作本域资源
_WRITE_SCOPED_DEPS = _WRITE_DEPS + [Depends(_scope_dimension)]
_MAPPING_SCOPED_DEPS = _MAPPING_DEPS + [Depends(_scope_mapping)]


@router.post("", status_code=201, dependencies=_WRITE_DEPS)
async def create_dimension(
    payload: DimensionCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # 域作用域守卫（P1-10）：域管理员仅可在本域建维度
    _assert_domain_scope(user, payload.domain)
    resp = await DimensionService(db).create_dimension(payload, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.create",
        entity_type="dimension",
        entity_id=str(payload.dim_code),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    # P0-3: 直接返回 ORM 对象会触发 FastAPI 序列化 500，须经 DimensionResponse 转换
    return ok(data=DimensionResponse.from_model(resp), trace_id=trace_id)


@router.get("", dependencies=_READ_DEPS)
async def list_dimensions(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    domain: str | None = Query(None),
    status: str | None = Query(None),
    keyword: str | None = Query(None, description="关键词：编码/名称/描述模糊匹配"),
    owner_id: int | None = Query(None, description="责任人（Owner）ID 过滤"),
    reviewed_by: int | None = Query(
        None, description="我审过的（通过/驳回人 ID 过滤，供统一主数据审批工作台）"
    ),
    deleted: bool = Query(False, description="是否查看回收站（已软删记录）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Any:
    items, total = await DimensionService(db).list_dimensions(
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
    converted = []
    for dim, metric_count in items:
        resp = DimensionResponse.from_model(dim)
        resp.metric_count = metric_count
        converted.append(resp)
    return ok(
        data={"items": converted, "total": total, "page": page, "page_size": page_size},
        trace_id=trace_id,
    )


@router.post("/mappings", dependencies=_MAPPING_DEPS)
async def create_mapping(
    payload: DimensionMappingCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # 域作用域守卫（P1-10）：映射源维度须在本域
    _scope_resource = await DimensionService(db).get_dimension(payload.source_dim_code)
    if _scope_resource is None:
        raise NotFoundError(f"维度不存在: {payload.source_dim_code}")
    _assert_domain_scope(user, _scope_resource.domain)
    resp = await DimensionService(db).create_mapping(payload, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension_mapping.create",
        entity_type="dimension_mapping",
        entity_id=f"{payload.source_dim_code}:{payload.target_dim_code}",
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionMappingResponse.from_model(resp), trace_id=trace_id)


@router.get("/mappings", dependencies=_READ_DEPS)
async def list_mappings(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    source_dim_code: str | None = Query(None),
    # P10 服务端分页（对齐主表分页模式，防大映射集全量拉取）
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Any:
    items, total = await DimensionService(db).list_mappings(
        source_dim_code, page=page, page_size=page_size
    )
    converted = [DimensionMappingResponse.from_model(i) for i in items]
    return ok(
        data={"items": converted, "total": total, "page": page, "page_size": page_size},
        trace_id=trace_id,
    )


@router.put("/mappings/{mapping_id}", dependencies=_MAPPING_SCOPED_DEPS)
async def update_mapping(
    mapping_id: int,
    payload: DimensionMappingUpdate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).update_mapping(mapping_id, payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension_mapping.update",
        entity_type="dimension_mapping",
        entity_id=str(mapping_id),
        detail=payload.model_dump(exclude_none=True),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionMappingResponse.from_model(resp), trace_id=trace_id)


@router.delete("/mappings/{mapping_id}", dependencies=_MAPPING_SCOPED_DEPS)
async def delete_mapping(
    mapping_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    await DimensionService(db).delete_mapping(mapping_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension_mapping.delete",
        entity_type="dimension_mapping",
        entity_id=str(mapping_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=None, trace_id=trace_id)


@router.post("/reconciliations", dependencies=_WRITE_DEPS)
async def submit_reconciliation(
    payload: ReconciliationSubmit,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # 域作用域守卫（P1-10）：对账维度须在本域
    _scope_resource = await DimensionService(db).get_dimension(payload.dim_code)
    if _scope_resource is None:
        raise NotFoundError(f"维度不存在: {payload.dim_code}")
    _assert_domain_scope(user, _scope_resource.domain)
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
    return ok(data=ReconciliationResponse.from_model(resp), trace_id=trace_id)


@router.get("/reconciliations", dependencies=_READ_DEPS)
async def list_reconciliations(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    status: str | None = Query(None),
    # P10 服务端分页（防治理记录增长导致的全量拉取）
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Any:
    items, total = await DimensionService(db).list_reconciliations(
        status, page=page, page_size=page_size
    )
    converted = []
    for rec, metric in items:
        resp = ReconciliationResponse.from_model(rec)
        if metric is not None:
            resp.metric_code = metric.metric_code
            resp.metric_name = metric.name
        converted.append(resp)
    return ok(
        data={"items": converted, "total": total, "page": page, "page_size": page_size},
        trace_id=trace_id,
    )


@router.post(
    "/reconciliations/{rec_id}/review",
    dependencies=_GOV_DEPS,
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
    return ok(data=ReconciliationResponse.from_model(resp), trace_id=trace_id)


@router.post("/preview-values", dependencies=_WRITE_DEPS)
async def preview_dimension_values(
    payload: PreviewValuesRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """从数据源表列拉取去重枚举值（维度值自动获取的预览）。"""
    resp = await DimensionService(db).preview_column_values(
        source_id=payload.source_id,
        table=payload.table,
        column=payload.column,
        limit=payload.limit,
    )
    # 审计（P1-11）：数据探测动作留痕，防任意用户对任意源任意表列无痕 SELECT DISTINCT
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.preview_values",
        entity_type="data_source",
        entity_id=f"{payload.source_id}:{payload.table}.{payload.column}",
        detail={"row_count": resp.get("row_count")},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=PreviewValuesResponse(**resp), trace_id=trace_id)


@router.post("/source-tables", dependencies=_WRITE_DEPS)
async def list_source_tables(
    payload: SourceTablesRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """列出数据源指定库（或全部库）非系统表（维度值来源表选项框）。"""
    tables = await DimensionService(db).list_source_tables(
        payload.source_id, databases=payload.databases
    )
    await db.commit()
    return ok(data=SourceTablesResponse(tables=tables), trace_id=trace_id)


@router.post("/source-databases", dependencies=_WRITE_DEPS)
async def list_source_databases(
    payload: SourceDatabasesRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """列出数据源全部非系统库（级联选表的「目标库」选项框，轻量快）。"""
    databases = await DimensionService(db).list_source_databases(payload.source_id)
    await db.commit()
    return ok(data={"databases": databases}, trace_id=trace_id)


@router.post("/source-columns", dependencies=_WRITE_DEPS)
async def list_source_columns(
    payload: SourceColumnsRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """列出指定表的全部列（维度值来源列选项框）。"""
    columns = await DimensionService(db).list_source_columns(
        source_id=payload.source_id, table=payload.table
    )
    await db.commit()
    return ok(data=SourceColumnsResponse(columns=columns), trace_id=trace_id)


@router.get("/{dim_code}", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_dimension(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).get_dimension_visible(
        dim_code, actor_id=user.id, role=user.role
    )
    return ok(data=DimensionResponse.from_model(resp), trace_id=trace_id)


@router.put("/{dim_code}", dependencies=_WRITE_DEPS + [Depends(_scope_dimension)])
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
    return ok(data=DimensionResponse.from_model(resp), trace_id=trace_id)


@router.post("/{dim_code}/deprecate", dependencies=_WRITE_DEPS + [Depends(_scope_dimension)])
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
    return ok(data=DimensionResponse.from_model(resp), trace_id=trace_id)


@router.post(
    "/{dim_code}/reactivate",
    response_model=ApiResponse[DimensionResponse],
    summary="重新启用已废弃维度（DEPRECATED → DRAFT，可编辑后重新走审核）",
    dependencies=_WRITE_DEPS + [Depends(_scope_dimension)],
)
async def reactivate_dimension(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """DEPRECATED → DRAFT：回到草稿可编辑，重新提交审核后才发布（不绕过审核）。"""
    resp = await DimensionService(db).reactivate_dimension(dim_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.reactivate",
        entity_type="dimension",
        entity_id=dim_code,
        detail={},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionResponse.from_model(resp), trace_id=trace_id)


@router.post(
    "/{dim_code}/delete",
    response_model=ApiResponse[DimensionResponse],
    summary="软删除维度（仅 DRAFT/DEPRECATED 可删；审核中/启用中禁止）",
    dependencies=_WRITE_DEPS + [Depends(_scope_dimension)],
)
async def delete_dimension(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """软删草稿/废弃维度；仅管理员或原 Owner（service 层校验）。"""
    resp = await DimensionService(db).delete_dimension(
        dim_code, actor_id=user.id, role=user.role
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.delete",
        entity_type="dimension",
        entity_id=dim_code,
        detail={"status": resp.status},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionResponse.from_model(resp), trace_id=trace_id)


@router.post(
    "/{dim_code}/restore",
    response_model=ApiResponse[DimensionResponse],
    summary="恢复已软删维度（回收站恢复）",
    dependencies=_WRITE_DEPS + [Depends(_scope_dimension)],
)
async def restore_dimension(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """回收站恢复软删维度；仅管理员或原 Owner（service 层校验）。"""
    resp = await DimensionService(db).restore_dimension(
        dim_code, actor_id=user.id, role=user.role
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.restore",
        entity_type="dimension",
        entity_id=dim_code,
        detail={"status": resp.status},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionResponse.from_model(resp), trace_id=trace_id)


@router.post(
    "/{dim_code}/publish",
    # 直发通道仅平台管理员（系统/种子/管理员兜底）；业务用户发布须走 submit+approve 审核流
    dependencies=(
        _WRITE_DEPS + [Depends(_scope_dimension)] + [Depends(require_roles(*_ADMIN_ROLES))]
    ),
)
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
    return ok(data=DimensionResponse.from_model(resp), trace_id=trace_id)


@router.post(
    "/{dim_code}/submit",
    response_model=Any,
    summary="提交维度审核（DRAFT → REVIEW）",
    dependencies=_WRITE_DEPS + [Depends(_scope_dimension)],
)
async def submit_dimension(
    dim_code: str,
    request: ReviewSubmitRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """DRAFT → REVIEW，提交审核（维度是下游指标绑定/消费校验的权威来源，发布须先审）。"""
    service = DimensionService(db)
    dim = await service.submit_dimension(
        dim_code, request, actor_id=user.id, role=user.role, user_domains=user.domains_all()
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.submit",
        entity_type="dimension",
        entity_id=dim_code,
        detail={"change_reason": request.change_reason},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionResponse.from_model(dim), trace_id=trace_id)


@router.post(
    "/{dim_code}/approve",
    response_model=Any,
    summary="审核通过维度（REVIEW → PUBLISHED）",
    dependencies=_REVIEW_DEPS,
)
async def approve_dimension(
    dim_code: str,
    request: ReviewApproveRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """REVIEW → PUBLISHED，审核通过（评审人身份校验 + 自审禁止）。"""
    service = DimensionService(db)
    dim = await service.approve_dimension(
        dim_code, request, actor_id=user.id, role=user.role, user_domains=user.domains_all()
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.approve",
        entity_type="dimension",
        entity_id=dim_code,
        detail={"comment": request.comment},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionResponse.from_model(dim), trace_id=trace_id)


@router.post(
    "/{dim_code}/reject",
    response_model=Any,
    summary="审核驳回维度（REVIEW → DRAFT）",
    dependencies=_REVIEW_DEPS,
)
async def reject_dimension(
    dim_code: str,
    request: ReviewRejectRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> Any:
    """REVIEW → DRAFT，驳回审核（驳回原因落库并通知提交人）。"""
    service = DimensionService(db)
    dim = await service.reject_dimension(
        dim_code, request, actor_id=user.id, role=user.role, user_domains=user.domains_all()
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.reject",
        entity_type="dimension",
        entity_id=dim_code,
        detail={"reason": request.reason},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionResponse.from_model(dim), trace_id=trace_id)


# ---- 批量治理端点（TD §13：逐条收集结果不整体失败；执行语义统一 app.api.batch_common）----


@router.post(
    "/batch-submit",
    response_model=ApiResponse[BatchResponse],
    summary="批量提交维度审核（DRAFT → REVIEW，可带评审指派）",
    dependencies=_WRITE_DEPS,
)
async def batch_submit_dimensions(
    request: BatchSubmitRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条 DRAFT→REVIEW；单条失败不阻断其余（返回逐条结果）。"""
    service = DimensionService(db)

    async def run_one(item: BatchSubmitItem) -> None:
        # 域作用域守卫（P1-10）：domain_admin/metric_owner 仅可操作本域资源
        dim = await service.get_dimension(item.code)
        if dim is None:
            raise NotFoundError(f"维度不存在: {item.code}")
        _assert_domain_scope(user, dim.domain)
        await service.submit_dimension(
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
        action=batch_audit_action("dimension.batch_submit", results),
        entity_type="dimension",
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
    summary="批量审核通过维度（REVIEW → PUBLISHED，即批量发布）",
    dependencies=_REVIEW_DEPS,
)
async def batch_approve_dimensions(
    request: BatchCodesRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条 REVIEW→PUBLISHED；评审人指派校验由 service 层逐条执行。"""
    service = DimensionService(db)
    results = await run_batch(
        db,
        units=request.codes,
        code_of=lambda code: code,
        run=lambda code: service.approve_dimension(
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
        action=batch_audit_action("dimension.batch_approve", results),
        entity_type="dimension",
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
    summary="批量审核驳回维度（REVIEW → DRAFT）",
    dependencies=_REVIEW_DEPS,
)
async def batch_reject_dimensions(
    request: BatchRejectRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条 REVIEW→DRAFT；驳回原因统一作用于所有项并落库可追溯。"""
    service = DimensionService(db)
    results = await run_batch(
        db,
        units=request.codes,
        code_of=lambda code: code,
        run=lambda code: service.reject_dimension(
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
        action=batch_audit_action("dimension.batch_reject", results),
        entity_type="dimension",
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
    summary="批量废弃维度（PUBLISHED → DEPRECATED）",
    dependencies=_WRITE_DEPS,
)
async def batch_deprecate_dimensions(
    request: BatchCodesRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条 PUBLISHED→DEPRECATED（被指标绑定者由 service 层废弃保护拦截）。"""
    service = DimensionService(db)

    async def run_one(code: str) -> None:
        dim = await service.get_dimension(code)
        if dim is None:
            raise NotFoundError(f"维度不存在: {code}")
        _assert_domain_scope(user, dim.domain)
        await service.deprecate_dimension(code)

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
        action=batch_audit_action("dimension.batch_deprecate", results),
        entity_type="dimension",
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
    summary="批量重新启用已废弃维度（DEPRECATED → DRAFT）",
    dependencies=_WRITE_DEPS,
)
async def batch_reactivate_dimensions(
    request: BatchCodesRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条 DEPRECATED→DRAFT（重新启用后走审核流）。"""
    service = DimensionService(db)

    async def run_one(code: str) -> None:
        dim = await service.get_dimension(code)
        if dim is None:
            raise NotFoundError(f"维度不存在: {code}")
        _assert_domain_scope(user, dim.domain)
        await service.reactivate_dimension(code)

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
        action=batch_audit_action("dimension.batch_reactivate", results),
        entity_type="dimension",
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
    summary="批量软删除维度（仅 DRAFT/DEPRECATED 可删）",
    dependencies=_WRITE_DEPS,
)
async def batch_delete_dimensions(
    request: BatchCodesRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[BatchResponse]:
    """逐条软删草稿/废弃维度；管理员或原 Owner（service 层逐条校验）。"""
    service = DimensionService(db)

    async def run_one(code: str) -> None:
        dim = await service.get_dimension(code)
        if dim is None:
            raise NotFoundError(f"维度不存在: {code}")
        _assert_domain_scope(user, dim.domain)
        await service.delete_dimension(code, actor_id=user.id, role=user.role)

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
        action=batch_audit_action("dimension.batch_delete", results),
        entity_type="dimension",
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


@router.post("/{dim_code}/members", dependencies=_WRITE_DEPS + [Depends(_scope_dimension)])
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
        action="dimension_member.create",
        entity_type="dimension_member",
        entity_id=f"{payload.dim_code}:{payload.member_code}",
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionMemberResponse.from_model(resp), trace_id=trace_id)


@router.get("/{dim_code}/members", dependencies=_READ_DEPS)
async def list_members(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    dim_code: str,
) -> Any:
    # 越权审查修复：父维度可见性守卫——他人 DRAFT/REVIEW 维度的成员值属私有
    # 工作区，非 owner/管理/评审人不泄露（对齐 get_dimension_visible）。
    await DimensionService(db).get_dimension_visible(
        dim_code, actor_id=user.id, role=user.role
    )
    items = await DimensionService(db).list_members(dim_code)
    return ok(
        data={"items": [DimensionMemberResponse.from_model(i) for i in items], "total": len(items)},
        trace_id=trace_id,
    )


@router.put("/{dim_code}/members/{member_code}", dependencies=_WRITE_SCOPED_DEPS)
async def update_member(
    dim_code: str,
    member_code: str,
    payload: DimensionMemberUpdate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).update_member(dim_code, member_code, payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension_member.update",
        entity_type="dimension_member",
        entity_id=f"{dim_code}:{member_code}",
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionMemberResponse.from_model(resp), trace_id=trace_id)


@router.delete("/{dim_code}/members/{member_code}", dependencies=_WRITE_SCOPED_DEPS)
async def delete_member(
    dim_code: str,
    member_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # 服务端级联删除：删除父级连带整个子树（成员表无软删列，物理删除）
    deleted = await DimensionService(db).delete_member(dim_code, member_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension_member.delete",
        entity_type="dimension_member",
        entity_id=f"{dim_code}:{member_code}",
        detail={"cascade_count": len(deleted)},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=None, trace_id=trace_id)


@router.post(
    "/{dim_code}/members/{member_code}/publish",
    dependencies=_WRITE_DEPS + [Depends(_scope_dimension)],
)
async def publish_member(
    dim_code: str,
    member_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """发布维度成员（DRAFT → PUBLISHED），对齐维度主体状态机。"""
    resp = await DimensionService(db).publish_member(dim_code, member_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension_member.publish",
        entity_type="dimension_member",
        entity_id=f"{dim_code}:{member_code}",
        detail={"status": resp.status},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=None, trace_id=trace_id)


@router.post(
    "/{dim_code}/members/batch-publish",
    dependencies=_WRITE_DEPS + [Depends(_scope_dimension)],
)
async def publish_all_members(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """批量发布维度全部 DRAFT 成员（从表导入工作流闭环）。"""
    result = await DimensionService(db).publish_all_members(dim_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension_member.batch_publish",
        entity_type="dimension_member",
        entity_id=dim_code,
        detail=result,
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@router.post(
    "/{dim_code}/members/{member_code}/deprecate",
    dependencies=_WRITE_DEPS + [Depends(_scope_dimension)],
)
async def deprecate_member(
    dim_code: str,
    member_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """废弃维度成员（→ DEPRECATED）；存在子成员时拒绝（层级权威保护）。"""
    resp = await DimensionService(db).deprecate_member(dim_code, member_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension_member.deprecate",
        entity_type="dimension_member",
        entity_id=f"{dim_code}:{member_code}",
        detail={"status": resp.status},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=None, trace_id=trace_id)


@router.post(
    "/{dim_code}/metrics",
    dependencies=_WRITE_DEPS + [Depends(_scope_dimension)],
)
async def bind_metric_dimension(
    dim_code: str,
    payload: MetricDimensionBind,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).bind_metric_dimension(payload, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="metric.bind_dimension",
        entity_type="metric_dimension",
        entity_id=f"{payload.metric_id}:{payload.dim_code}",
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=MetricDimensionResponse.from_model(resp), trace_id=trace_id)


@router.delete(
    "/{dim_code}/metrics/{metric_id}",
    dependencies=_WRITE_DEPS + [Depends(_scope_dimension)],
)
async def unbind_metric_dimension(
    dim_code: str,
    metric_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """解除指标-维度绑定（撤销误绑/改绑）：删除绑定记录 + 同步移除指标声明维度。"""
    await DimensionService(db).unbind_metric_dimension(metric_id, dim_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="metric.unbind_dimension",
        entity_type="metric_dimension",
        entity_id=f"{metric_id}:{dim_code}",
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=None, trace_id=trace_id)


@router.get("/{dim_code}/metrics", dependencies=_READ_DEPS)
async def list_dimension_metrics(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # 治理追溯：查看该维度被哪些指标消费（绑定关系 + 指标信息）
    # 越权审查修复：父维度可见性守卫（对齐 list_members）——他人 DRAFT/REVIEW
    # 维度的绑定指标不泄露。
    await DimensionService(db).get_dimension_visible(
        dim_code, actor_id=user.id, role=user.role
    )
    items = await DimensionService(db).list_dimension_metrics(dim_code)
    converted = [
        DimensionMetricBinding(
            metric_id=binding.metric_id,
            dim_code=binding.dim_code,
            role=binding.role,
            default_member=binding.default_member,
            metric_code=metric.metric_code,
            metric_name=metric.name,
            metric_status=metric.status,
        )
        for binding, metric in items
    ]
    return ok(data={"items": converted, "total": len(converted)}, trace_id=trace_id)


@router.get("/{metric_id}/metric-dimensions", dependencies=_READ_DEPS)
async def list_metric_dimensions(
    metric_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    items = await DimensionService(db).list_metric_dimensions(metric_id)
    return ok(
        data={"items": [MetricDimensionResponse.from_model(i) for i in items], "total": len(items)},
        trace_id=trace_id,
    )


# ---------------------------------------------------------------- 引用型维度


@router.post(
    "/{dim_code}/reference",
    response_model=ApiResponse[DimensionResponse],
    summary="绑定引用型值来源（维度值 = 源表列快照）",
    dependencies=_WRITE_SCOPED_DEPS,
)
async def bind_dimension_reference(
    dim_code: str,
    request: DimensionReferenceBind,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[DimensionResponse]:
    """将维度切换为引用型：值集合来自源表列快照（不再维护 member 表）。"""
    dim = await DimensionService(db).bind_dimension_reference(dim_code, request)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.bind_reference",
        entity_type="dimension",
        entity_id=dim_code,
        detail={
            "source_id": request.source_id,
            "table": request.table,
            "column": request.column,
            "refresh_interval_hours": request.refresh_interval_hours,
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionResponse.from_model(dim), trace_id=trace_id)


@router.post(
    "/{dim_code}/snapshots/refresh",
    response_model=ApiResponse[dict],
    summary="刷新引用型维度值快照",
    dependencies=_WRITE_SCOPED_DEPS,
)
async def refresh_dimension_snapshot(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[dict]:
    """手动触发快照刷新：拉全量 → diff → 写新批 + run 记录。"""
    result = await DimensionService(db).refresh_dimension_snapshot(dim_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.refresh_snapshot",
        entity_type="dimension",
        entity_id=dim_code,
        detail={
            "snapshot_at": result.get("snapshot_at"),
            "total": result.get("total"),
            "added": len(result.get("added", [])),
            "removed": len(result.get("removed", [])),
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@router.get(
    "/{dim_code}/snapshots",
    response_model=ApiResponse[dict],
    summary="分页列出引用型维度快照值",
    dependencies=_READ_DEPS,
)
async def list_dimension_snapshots(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
) -> ApiResponse[dict]:
    items, total = await DimensionService(db).list_dimension_snapshots(
        dim_code, page=page, page_size=page_size
    )
    return ok(
        data={
            "items": [
                SnapshotValueResponse(
                    id=s.id,
                    dim_code=s.dim_code,
                    value=s.value,
                    snapshot_at=s.snapshot_at,
                    status=s.status,
                )
                for s in items
            ],
            "total": total,
        },
        trace_id=trace_id,
    )


@router.get(
    "/{dim_code}/snapshots/latest-run",
    response_model=ApiResponse[SnapshotRunResponse | None],
    summary="最近一次快照刷新运行记录",
    dependencies=_READ_DEPS,
)
async def get_latest_snapshot_run(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[SnapshotRunResponse | None]:
    run = await DimensionService(db).get_latest_snapshot_run(dim_code)
    return ok(
        data=SnapshotRunResponse.from_model(run) if run is not None else None,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------- 成员批量操作


@router.post(
    "/{dim_code}/members/batch-publish",
    response_model=ApiResponse[dict],
    summary="批量发布维度成员",
    dependencies=_WRITE_SCOPED_DEPS,
)
async def batch_publish_members(
    dim_code: str,
    request: MemberBatchRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[dict]:
    result = await DimensionService(db).batch_publish_members(
        dim_code, request.member_codes
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.batch_publish_members",
        entity_type="dimension",
        entity_id=f"{dim_code}:batch:{len(request.member_codes)}",
        detail={
            "published": result.get("published"),
            "skipped": result.get("skipped"),
            "failed_codes": [f["code"] for f in result.get("failed", [])],
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@router.post(
    "/{dim_code}/members/batch-deprecate",
    response_model=ApiResponse[dict],
    summary="批量废弃维度成员",
    dependencies=_WRITE_SCOPED_DEPS,
)
async def batch_deprecate_members(
    dim_code: str,
    request: MemberBatchRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[dict]:
    result = await DimensionService(db).batch_deprecate_members(
        dim_code, request.member_codes
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.batch_deprecate_members",
        entity_type="dimension",
        entity_id=f"{dim_code}:batch:{len(request.member_codes)}",
        detail={
            "deprecated": result.get("deprecated"),
            "skipped": result.get("skipped"),
            "failed_codes": [f["code"] for f in result.get("failed", [])],
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@router.post(
    "/{dim_code}/members/batch-delete",
    response_model=ApiResponse[dict],
    summary="批量删除维度成员（级联子树并集）",
    dependencies=_WRITE_SCOPED_DEPS,
)
async def batch_delete_members(
    dim_code: str,
    request: MemberBatchRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[dict]:
    result = await DimensionService(db).batch_delete_members(
        dim_code, request.member_codes
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.batch_delete_members",
        entity_type="dimension",
        entity_id=f"{dim_code}:batch:{len(request.member_codes)}",
        detail={
            "deleted": result.get("deleted"),
            "failed_codes": [f["code"] for f in result.get("failed", [])],
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


# ---------------------------------------------------------------- 值级映射


@router.post(
    "/mappings/{mapping_id}/values",
    response_model=ApiResponse[MappingValueResponse],
    summary="新增值级映射（source_value → target_value）",
    dependencies=_MAPPING_SCOPED_DEPS,
)
async def create_mapping_value(
    mapping_id: int,
    request: MappingValueCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MappingValueResponse]:
    mv = await DimensionService(db).create_mapping_value(mapping_id, request, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.create_mapping_value",
        entity_type="dimension_mapping",
        entity_id=str(mapping_id),
        detail={"source_value": request.source_value, "target_value": request.target_value},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MappingValueResponse(
            id=mv.id,
            mapping_id=mv.mapping_id,
            source_value=mv.source_value,
            target_value=mv.target_value,
            created_by=mv.created_by,
            created_at=mv.created_at,
        ),
        trace_id=trace_id,
    )


@router.get(
    "/mappings/{mapping_id}/values",
    response_model=ApiResponse[dict],
    summary="分页列出值级映射",
    dependencies=_READ_DEPS,
)
async def list_mapping_values(
    mapping_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
) -> ApiResponse[dict]:
    items, total = await DimensionService(db).list_mapping_values(
        mapping_id, page=page, page_size=page_size
    )
    return ok(
        data={
            "items": [
                MappingValueResponse(
                    id=mv.id,
                    mapping_id=mv.mapping_id,
                    source_value=mv.source_value,
                    target_value=mv.target_value,
                    created_by=mv.created_by,
                    created_at=mv.created_at,
                )
                for mv in items
            ],
            "total": total,
        },
        trace_id=trace_id,
    )


@router.delete(
    "/mapping-values/{value_id}",
    response_model=ApiResponse[dict],
    summary="删除值级映射",
    dependencies=_MAPPING_DEPS,
)
async def delete_mapping_value(
    value_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[dict]:
    await DimensionService(db).delete_mapping_value(value_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.delete_mapping_value",
        entity_type="dimension_mapping",
        entity_id=str(value_id),
        detail={},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"deleted": True}, trace_id=trace_id)


@router.get(
    "/mappings/{mapping_id}/coverage",
    response_model=ApiResponse[MappingCoverageResponse],
    summary="值级映射覆盖率（未映射源值清单）",
    dependencies=_READ_DEPS,
)
async def mapping_coverage(
    mapping_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[MappingCoverageResponse]:
    coverage = await DimensionService(db).mapping_coverage(mapping_id)
    return ok(data=coverage, trace_id=trace_id)


@router.post(
    "/translate",
    response_model=ApiResponse[TranslateResponse],
    summary="批量值翻译（源维度值 → 目标维度值）",
    dependencies=_READ_DEPS,
)
async def translate_values(
    request: TranslateRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[TranslateResponse]:
    results = await DimensionService(db).translate_values(
        request.source_dim_code, request.target_dim_code, request.values
    )
    return ok(data=TranslateResponse(results=results), trace_id=trace_id)
