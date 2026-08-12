"""指标语义定义 REST API（FR-05/06/07）。

全部成功响应套用统一信封 ``{code, message, data, trace_id}``（见 app.api.responses）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.semantic.schemas import (
    MetricCreateRequest,
    MetricListParams,
    MetricListResponse,
    MetricPublishRequest,
    MetricResponse,
    MetricUpdateRequest,
    MetricVersionResponse,
)
from app.services.semantic.service import MetricService

router = APIRouter(prefix="/metric-definitions", tags=["metric-definitions"])

# 语义定义写操作允许的角色（对齐 RBAC：平台/域管理员 + 指标 Owner）
_WRITE_ROLES = ("platform_admin", "domain_admin", "metric_owner")
# PII 合规复核须由合规/域管理员执行，禁止指标 Owner 自审（对齐治理 COMPL-2）
_PII_REVIEW_ROLES = ("platform_admin", "domain_admin")
_READ_ROLES = ALL_ROLES
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]


@router.post(
    "",
    response_model=ApiResponse[MetricResponse],
    status_code=201,
    summary="创建指标语义定义（FR-05）",
    dependencies=[Depends(require_roles(*_WRITE_ROLES))],
)
async def create_metric(
    request: MetricCreateRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """创建指标语义定义（默认 DRAFT 状态，并生成版本 1 快照）。"""
    service = MetricService(db)
    metric = await service.create_metric(request, owner_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="CREATE",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"domain": metric.domain, "type": metric.type, "pii_flag": metric.pii_flag},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.get(
    "",
    response_model=ApiResponse[MetricListResponse],
    summary="查询指标语义定义列表（FR-06）",
    dependencies=_READ_DEPS,
)
async def list_metrics(
    params: Annotated[MetricListParams, Depends()],
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[MetricListResponse]:
    """支持域/状态/分级/关键词过滤与分页。"""
    service = MetricService(db)
    metrics, total = await service.list_metrics(params)
    response = MetricListResponse(
        items=[MetricResponse.model_validate(m) for m in metrics],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )
    # 批量 PII 访问审计（对齐 TD §15.4）：列表命中任何 PII 指标即记一条汇总审计，
    # 闭合「列表接口批量暴露 PII」的合规漏洞。
    pii_codes = [m.metric_code for m in metrics if m.pii_flag]
    if pii_codes:
        await write_audit(
            db,
            actor_id=user.id,
            action="LIST",
            entity_type="metric_definition",
            entity_id=f"pii_list:{len(pii_codes)}",
            detail={
                "data_classification": "PII",
                "count": len(pii_codes),
                "codes": pii_codes[:50],
            },
            ip=client_ip(request),
            trace_id=trace_id,
            pii_access=True,
        )
    return ok(data=response, trace_id=trace_id)


@router.get(
    "/{metric_code}",
    response_model=ApiResponse[MetricResponse],
    summary="获取指标语义定义详情（FR-06）",
    dependencies=_READ_DEPS,
)
async def get_metric(
    metric_code: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[MetricResponse]:
    service = MetricService(db)
    metric = await service.get_metric_public(metric_code)
    # PII 访问审计（对齐 TD §15.4 审计合规，data_classification=PII）
    if metric.pii_flag:
        await write_audit(
            db,
            actor_id=user.id,
            action="READ",
            entity_type="metric",
            entity_id=metric_code,
            detail={"data_classification": "PII", "metric_code": metric_code},
            ip=client_ip(request),
            trace_id=trace_id,
            pii_access=True,
        )
    return ok(data=metric, trace_id=trace_id)


@router.put(
    "/{metric_code}",
    response_model=ApiResponse[MetricResponse],
    summary="更新指标语义定义（FR-05，带乐观锁与版本快照）",
    dependencies=[Depends(require_roles(*_WRITE_ROLES))],
)
async def update_metric(
    metric_code: str,
    request: MetricUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """变更口径时自动识别破坏性变更并递增版本号；乐观锁防止并发覆盖。"""
    service = MetricService(db)
    metric = await service.update_metric(metric_code, request, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="UPDATE",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"change_reason": request.change_reason, "pii_flag": metric.pii_flag},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/publish",
    response_model=ApiResponse[MetricResponse],
    summary="发布指标（FR-07，PII 合规闸门）",
    dependencies=[Depends(require_roles(*_WRITE_ROLES))],
)
async def publish_metric(
    metric_code: str,
    request: MetricPublishRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """DRAFT/EXPERIMENTAL 状态可发布；含 PII 且未过合规审核则拒绝。"""
    service = MetricService(db)
    metric = await service.publish_metric(metric_code, request, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="PUBLISH",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"version": request.version, "pii_flag": metric.pii_flag},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/deprecate",
    response_model=ApiResponse[MetricResponse],
    summary="废弃指标（FR-07，标记替代指标与 Sunset 截止日）",
    dependencies=[Depends(require_roles(*_WRITE_ROLES))],
)
async def deprecate_metric(
    metric_code: str,
    successor_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """仅非 DEPRECATED 状态可废弃，并写入 sunset_until 与 successor_code。"""
    service = MetricService(db)
    metric = await service.deprecate_metric(metric_code, successor_code, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="DEPRECATE",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"successor_code": successor_code},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.get(
    "/{metric_code}/versions",
    response_model=ApiResponse[list[MetricVersionResponse]],
    summary="查看指标版本历史（FR-05）",
    dependencies=_READ_DEPS,
)
async def get_metric_versions(
    metric_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[list[MetricVersionResponse]]:
    service = MetricService(db)
    versions = await service.get_versions(metric_code)
    response = [MetricVersionResponse.model_validate(v) for v in versions]
    return ok(data=response, trace_id=trace_id)


@router.post(
    "/{metric_code}/pii-review",
    response_model=ApiResponse[MetricResponse],
    summary="PII 合规复核（打通 PII 指标发布闸门）",
    dependencies=[Depends(require_roles(*_PII_REVIEW_ROLES))],
)
async def review_metric_compliance(
    metric_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """PII 指标合规复核：置 compliance_reviewed=True，解除发布闸门（禁 Owner 自审）。"""
    service = MetricService(db)
    metric = await service.review_compliance(metric_code, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="PII_REVIEW",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"compliance_reviewed": metric.compliance_reviewed},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )
