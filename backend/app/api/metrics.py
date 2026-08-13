"""指标语义定义 REST API（FR-05/06/07）。

全部成功响应套用统一信封 ``{code, message, data, trace_id}``（见 app.api.responses）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.semantic.schemas import (
    MetricApproveRequest,
    MetricBatchRegisterRequest,
    MetricCompareRequest,
    MetricCreateRequest,
    MetricDeprecateRequest,
    MetricEmergencyPublishRequest,
    MetricHealthResponse,
    MetricListParams,
    MetricListResponse,
    MetricPublishRequest,
    MetricRejectRequest,
    MetricResponse,
    MetricSubmitRequest,
    MetricUpdateRequest,
    MetricVersionResponse,
    VersionConfirmRequest,
    VersionExtendRequest,
    VersionRejectRequest,
)
from app.services.semantic.service import MetricService, redact_definition

router = APIRouter(prefix="/metric-definitions", tags=["metric-definitions"])

# 语义定义写操作允许的角色（对齐 RBAC：平台/域管理员 + 指标 Owner）
_WRITE_ROLES = ("platform_admin", "domain_admin", "metric_owner")
# PII 合规复核须由合规/域管理员执行，禁止指标 Owner 自审（对齐治理 COMPL-2）
_PII_REVIEW_ROLES = ("platform_admin", "domain_admin")
_READ_ROLES = ALL_ROLES
# PII 指标口径可读角色：仅管理/合规可见完整口径，其余角色读路径脱敏
_SENSITIVE_ROLES = ("platform_admin", "domain_admin", "compliance_officer")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]
# 写端点统一 RBAC + 注入守卫（对齐 semantic.py 的 _WRITE_DEPS 模式）
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]


@router.post(
    "",
    response_model=ApiResponse[MetricResponse],
    status_code=201,
    summary="创建指标语义定义（FR-05）",
    dependencies=_WRITE_DEPS,
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
    # PLAT-3: 业务写入 + 审计同事务原子提交（缺 commit 会导致事务随会话关闭被回滚）
    await db.commit()
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
    # PII 读分级：非敏感角色对 PII 指标脱敏口径（保留键结构，值替换为 ***）
    sensitive = user.role in _SENSITIVE_ROLES
    items: list[MetricResponse] = []
    for m in metrics:
        item = MetricResponse.model_validate(m)
        if item.pii_flag and not sensitive:
            item = item.model_copy(
                update={"definition_json": redact_definition(item.definition_json)}
            )
        items.append(item)
    response = MetricListResponse(
        items=items,
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
    # PLAT-3: PII 访问审计须提交持久化，否则随会话关闭被回滚（合规审计静默丢失）
    await db.commit()
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
    # PLAT-3: PII 访问审计须提交持久化，否则随会话关闭被回滚（合规审计静默丢失）
    await db.commit()
    # PII 读分级：非敏感角色脱敏口径（保留键结构，值替换为 ***）
    data: MetricResponse = metric
    if metric.pii_flag and user.role not in _SENSITIVE_ROLES:
        data = metric.model_copy(
            update={"definition_json": redact_definition(metric.definition_json)}
        )
    return ok(data=data, trace_id=trace_id)


@router.put(
    "/{metric_code}",
    response_model=ApiResponse[MetricResponse],
    summary="更新指标语义定义（FR-05，带乐观锁与版本快照）",
    dependencies=_WRITE_DEPS,
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
    metric = await service.update_metric(
        metric_code, request, actor_id=user.id, role=user.role
    )
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
    # PLAT-3: 业务写入 + 审计同事务原子提交
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/publish",
    response_model=ApiResponse[MetricResponse],
    summary="发布指标（FR-07，路由到 approve_metric）",
    dependencies=_WRITE_DEPS,
)
async def publish_metric(
    metric_code: str,
    request: MetricPublishRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """发布指标（内部路由到 approve_metric，推荐直接使用 submit+approve）。"""
    service = MetricService(db)
    approve_req = MetricApproveRequest(
        mode="standard",
        target_version=request.version,
    )
    metric = await service.approve_metric(metric_code, approve_req, actor_id=user.id)
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
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/deprecate",
    response_model=ApiResponse[MetricResponse],
    summary="废弃指标（FR-07，successor_code 必填）",
    dependencies=_WRITE_DEPS,
)
async def deprecate_metric(
    metric_code: str,
    request: MetricDeprecateRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """仅 PUBLISHED 状态可废弃，successor_code 必填且须为已发布指标。"""
    service = MetricService(db)
    metric = await service.deprecate_metric(
        metric_code,
        successor_code=request.successor_code,
        actor_id=user.id,
        role=user.role,
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="DEPRECATE",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"successor_code": request.successor_code},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/submit",
    response_model=ApiResponse[MetricResponse],
    summary="提交指标审核（FR-003，DRAFT → REVIEW）",
    dependencies=_WRITE_DEPS,
)
async def submit_metric(
    metric_code: str,
    request: MetricSubmitRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """DRAFT → REVIEW，提交审核。状态机校验，非法跃迁返回 409。"""
    service = MetricService(db)
    metric = await service.submit_metric(metric_code, request, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="SUBMIT",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"change_reason": request.change_reason},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/approve",
    response_model=ApiResponse[MetricResponse],
    summary="审核通过指标（FR-004，REVIEW → PUBLISHED/EXPERIMENTAL）",
    dependencies=[Depends(require_roles(*_PII_REVIEW_ROLES)), Depends(guard_against_injection)],
)
async def approve_metric(
    metric_code: str,
    request: MetricApproveRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """REVIEW → PUBLISHED(standard) / EXPERIMENTAL(experimental)。含 PII 门禁 + 依赖校验。"""
    service = MetricService(db)
    metric = await service.approve_metric(metric_code, request, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="APPROVE",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"mode": request.mode, "target_version": request.target_version},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/reject",
    response_model=ApiResponse[MetricResponse],
    summary="审核驳回指标（FR-005，REVIEW → DRAFT）",
    dependencies=[Depends(require_roles(*_PII_REVIEW_ROLES)), Depends(guard_against_injection)],
)
async def reject_metric(
    metric_code: str,
    request: MetricRejectRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """REVIEW → DRAFT，驳回审核。须填驳回原因，通知 Owner。"""
    service = MetricService(db)
    metric = await service.reject_metric(metric_code, request, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="REJECT",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"reason": request.reason},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/confirm-version",
    response_model=ApiResponse[MetricResponse],
    summary="消费方确认版本（FR-007）",
    dependencies=_WRITE_DEPS,
)
async def confirm_version(
    metric_code: str,
    request: VersionConfirmRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """消费方确认 PENDING_VERSION：全部确认后新版本升 CURRENT。"""
    service = MetricService(db)
    metric = await service.confirm_version(metric_code, request.version, consumer_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="CONFIRM_VERSION",
        entity_type="metric_definition",
        entity_id=metric_code,
        detail={"version": request.version},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/reject-version",
    response_model=ApiResponse[MetricResponse],
    summary="消费方拒绝版本（FR-007）",
    dependencies=_WRITE_DEPS,
)
async def reject_version(
    metric_code: str,
    request: VersionRejectRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """消费方拒绝 PENDING_VERSION：任一拒绝则版本取消，旧版本保持 CURRENT。"""
    service = MetricService(db)
    metric = await service.reject_version(
        metric_code, request.version, reason=request.reason, consumer_id=user.id
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="REJECT_VERSION",
        entity_type="metric_definition",
        entity_id=metric_code,
        detail={"version": request.version, "reason": request.reason},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/extend-version",
    response_model=ApiResponse[MetricResponse],
    summary="版本确认延期（FR-008，+7 天，最多延期 1 次）",
    dependencies=_WRITE_DEPS,
)
async def extend_version(
    metric_code: str,
    request: VersionExtendRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """Owner 请求延期确认：+7 天，最多延期 1 次。"""
    service = MetricService(db)
    metric = await service.extend_version(metric_code, request.version)
    await write_audit(
        db,
        actor_id=user.id,
        action="EXTEND_VERSION",
        entity_type="metric_definition",
        entity_id=metric_code,
        detail={"version": request.version},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.delete(
    "/{metric_code}",
    response_model=ApiResponse[None],
    summary="删除指标（FR-07，软删除，仅 DRAFT 状态）",
    dependencies=[Depends(require_roles("platform_admin")), Depends(guard_against_injection)],
)
async def delete_metric(
    metric_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[None]:
    """仅 platform_admin 可软删除 DRAFT 状态指标（非 DRAFT 拒绝）。"""
    service = MetricService(db)
    metric = await service.delete_metric(metric_code, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="DELETE",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"status": metric.status},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    # PLAT-3: 业务写入 + 审计同事务原子提交
    await db.commit()
    return ok(data=None, trace_id=trace_id)


@router.post(
    "/{metric_code}/promote",
    response_model=ApiResponse[MetricResponse],
    summary="灰度全量发布（FR-020，EXPERIMENTAL → PUBLISHED）",
    dependencies=_WRITE_DEPS,
)
async def promote_metric(
    metric_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """灰度指标全量发布：清除灰度白名单，状态升为 PUBLISHED。"""
    service = MetricService(db)
    metric = await service.promote_metric(metric_code, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="PROMOTE",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"from_status": "EXPERIMENTAL", "to_status": "PUBLISHED"},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/rollback",
    response_model=ApiResponse[MetricResponse],
    summary="灰度回滚（FR-020，EXPERIMENTAL → 回退上一 PUBLISHED 版本）",
    dependencies=_WRITE_DEPS,
)
async def rollback_metric(
    metric_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """灰度指标回滚：EXPERIMENTAL 版本标记 ARCHIVED，回退到上一 PUBLISHED 版本。"""
    service = MetricService(db)
    metric = await service.rollback_metric(metric_code, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="ROLLBACK",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"from_status": "EXPERIMENTAL", "action": "rollback_to_previous_published"},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
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
    metric = await service.review_compliance(metric_code, actor_id=user.id, role=user.role)
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
    # PLAT-3: 业务写入 + 审计同事务原子提交
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


# ----------------------------------------------------------------
# 紧急发布
# ----------------------------------------------------------------


@router.post(
    "/{metric_code}/emergency-publish",
    response_model=ApiResponse[MetricResponse],
    summary="紧急发布指标（跳过REVIEW，须填紧急原因，PII门禁不可跳）",
    dependencies=[Depends(require_roles("platform_admin", "domain_admin"))],
)
async def emergency_publish_metric(
    metric_code: str,
    request: MetricEmergencyPublishRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """domain_admin 紧急发布：跳过 REVIEW 但不跳 PII 门禁。"""
    service = MetricService(db)
    metric = await service.emergency_publish_metric(
        metric_code, request, actor_id=user.id, role=user.role,
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="EMERGENCY_PUBLISH",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={
            "reason": request.reason,
            "emergency_publish": True,
            "pii_flag": metric.pii_flag,
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


# ----------------------------------------------------------------
# 健康度评分
# ----------------------------------------------------------------


@router.get(
    "/{metric_code}/health",
    response_model=ApiResponse[MetricHealthResponse],
    summary="获取指标健康度评分（五维加权）",
    dependencies=_READ_DEPS,
)
async def get_metric_health(
    metric_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[MetricHealthResponse]:
    """五维加权健康度评分：口径完整度/活跃度/质量/Owner响应/血缘覆盖。"""
    service = MetricService(db)
    health = await service.get_metric_health(metric_code)
    await db.commit()
    return ok(data=MetricHealthResponse.model_validate(health), trace_id=trace_id)


# ----------------------------------------------------------------
# 指标对比
# ----------------------------------------------------------------


@router.post(
    "/compare",
    response_model=ApiResponse,
    summary="两指标关键字段并排对比",
    dependencies=_READ_DEPS,
)
async def compare_metrics(
    request: MetricCompareRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """两指标关键字段并排 diff + 差异标记。"""
    service = MetricService(db)
    result = await service.compare_metrics(
        request.metric_codes[0], request.metric_codes[1],
    )
    return ok(data=result, trace_id=trace_id)


# ----------------------------------------------------------------
# 批量注册
# ----------------------------------------------------------------


@router.post(
    "/batch-register",
    response_model=ApiResponse,
    summary="批量注册指标（从宽表度量列批量创建 DRAFT）",
    dependencies=[Depends(require_roles(*_WRITE_ROLES))],
)
async def batch_register_metrics(
    request: MetricBatchRegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """批量注册：LLM 预填 + 逐条校验 + 共享 batch_id。"""
    service = MetricService(db)
    result = await service.batch_register_metrics(request, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="BATCH_REGISTER",
        entity_type="metric_definition",
        entity_id=f"batch:{result['batch_id']}",
        detail={"count": len(result["candidates"]), "domain": request.domain},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)
