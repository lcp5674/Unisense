"""权限与合规 API（TD §3.5 / §12.5，FR-11）。

端点：

- POST   /roles                    角色登记（六角色，PRD 4.9.2）
- POST   /grants                   域授权 + 指标白名单
- GET    /grants                   授权列表（过滤 + 分页）
- POST   /grants/batch             批量授权/回收（R3-07：逐条审计 + 失败回滚）
- POST   /grants/batch/dry-run     批量影响预览（不落库）
- DELETE /grants/{grant_id}        单条回收
- POST   /pii/review               合规官复核（COMPL-1，留痕）
- POST   /classification/rescan    分级重扫（COMPL-2）
- GET    /me/permissions           当前用户权限快照
- POST   /permissions/check        PDP 决策（内部：consume/semantic 鉴权）

审计：所有写操作落 ``audit_log``；PII 复核额外置 ``pii_access=True``。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.exceptions import ValidationError
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.governance.events import GovernanceEventPublisher
from app.services.governance.schemas import (
    ActionRegistryItem,
    ClassificationFalsePositiveRequest,
    ClassificationRescanRequest,
    ErasureRequestCreate,
    ErasureResult,
    GrantBatchRequest,
    GrantCreate,
    GrantListParams,
    GrantResponse,
    PermissionCheckRequest,
    PiiReviewRequest,
    RoleCreate,
    RolePermissionItem,
    RolePermissionUpdate,
    RoleResponse,
    UserPermissionResponse,
    UserPermissionUpdateRequest,
)
from app.services.governance.service import GovernanceService

router = APIRouter(tags=["governance"])

#: 授权管理角色（跨域运维 / 本域管理）。
_GRANT_ADMIN_ROLES = ("platform_admin", "domain_admin")
#: 合规复核角色（PII 门禁必须由合规官执行）。
_COMPLIANCE_ROLES = ("compliance_officer", "platform_admin")
_READ_ROLES = ALL_ROLES
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]
# 写端点统一挂注入守卫（纵深防御：ORM 参数化兜底之外拦截注入 payload）
_ROLE_ADMIN_DEPS = [Depends(require_roles("platform_admin")), Depends(guard_against_injection)]
_GRANT_ADMIN_DEPS = [Depends(require_roles(*_GRANT_ADMIN_ROLES)), Depends(guard_against_injection)]
_COMPLIANCE_DEPS = [Depends(require_roles(*_COMPLIANCE_ROLES)), Depends(guard_against_injection)]


def _svc(db: AsyncSession, request: Request) -> GovernanceService:
    notify_url = getattr(request.app.state, "notify_url", None)
    return GovernanceService(db, events=GovernanceEventPublisher(notify_url))


async def _is_manageable_role(db: AsyncSession, role: str) -> bool:
    """角色是否可配置权限点：内置七角色 或 已登记的自定义角色。"""
    from sqlalchemy import select

    from app.models.governance import Role

    if role in ALL_ROLES:
        return True
    row = (
        await db.execute(
            select(Role).where(Role.name == role, Role.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    return row is not None


@router.post("/roles", dependencies=_ROLE_ADMIN_DEPS)
async def create_role(
    payload: RoleCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """登记角色（幂等：同名角色返回既有记录）。

    自定义角色（``is_custom=True``）走细粒度权限管控的创建校验：名称须为
    ``[a-z][a-z0-9_]{2,32}`` 且不得与内置角色重名；创建后可经
    ``PUT /roles/{role}/permissions`` 可视化配置按钮级权限点。
    """
    svc = _svc(db, request)
    if payload.is_custom:
        role = await svc.create_custom_role(payload.name, payload.description)
    else:
        role = await svc.create_role(payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="role.create",
        entity_type="role",
        entity_id=str(role.id),
        detail={"name": str(role.name), "is_custom": bool(getattr(role, "is_custom", False))},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=RoleResponse.model_validate(role).model_dump(), trace_id=trace_id)


@router.get("/roles/action-registry", dependencies=_ROLE_ADMIN_DEPS)
async def list_action_registry(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """动作点注册表（按钮级权限点清单，角色管理可视化配置数据源）。"""
    svc = _svc(db, request)
    items = await svc.action_registry()
    return ok(
        data=[ActionRegistryItem.model_validate(i).model_dump() for i in items],
        trace_id=trace_id,
    )


@router.get("/roles/options", dependencies=_GRANT_ADMIN_DEPS)
async def list_role_options(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """角色行下拉选项（id→name 映射，授权管理「角色」下拉数据源）。

    返回 ``role`` 表全部未删除行（内置登记 + 自定义角色），供 ``grants.role_id``
    以角色名而非数字 ID 呈现给授权者。
    """
    svc = _svc(db, request)
    data = await svc.list_role_options()
    return ok(data=data, trace_id=trace_id)


@router.delete("/roles/{role}", dependencies=_ROLE_ADMIN_DEPS)
async def delete_role(
    role: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """删除自定义角色（内置角色 / 仍被用户占用的角色不可删除）。

    删除为软删；被占用时返回 ``ROLE_IN_USE``，需先改派用户角色。
    """
    svc = _svc(db, request)
    await svc.delete_custom_role(role)
    await write_audit(
        db,
        actor_id=user.id,
        action="role.delete",
        entity_type="role",
        entity_id=role,
        detail={"role": role, "is_custom": True},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok({"role": role, "deleted": True}, trace_id=trace_id)


@router.get("/roles", dependencies=_GRANT_ADMIN_DEPS)
async def list_role_permissions(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """角色 × 权限点配置列表（默认基线 + ``role_permission`` 覆盖的合并视图）。

    RBAC 可配置化（TD §12.5 增强）：前端据此渲染角色权限点矩阵，只读。
    """
    svc = _svc(db, request)
    items = await svc.list_role_permissions()
    data = [RolePermissionItem.model_validate(i).model_dump() for i in items]
    return ok(data=data, trace_id=trace_id)


@router.put("/roles/{role}/permissions", dependencies=_ROLE_ADMIN_DEPS)
async def set_role_permissions(
    role: str,
    payload: RolePermissionUpdate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """覆盖某角色的权限点（RBAC 可配置化）。

    仅 ``platform_admin`` 可配置；platform_admin 角色本身受保护（硬编码跨域直通），
    覆盖其权限点会被拒绝。支持内置角色与自定义角色。写操作落审计。
    """
    if not await _is_manageable_role(db, role):
        raise ValidationError(
            f"未知角色: {role}", error_code="ROLE_PERMISSION_INVALID", ctx={"role": role}
        )
    svc = _svc(db, request)
    item = await svc.set_role_permissions(role, payload.actions)
    await write_audit(
        db,
        actor_id=user.id,
        action="role.update_permissions",
        entity_type="role",
        entity_id=role,
        detail={"actions": payload.actions},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=item, trace_id=trace_id)


@router.get("/users/{user_id}/permissions", dependencies=_ROLE_ADMIN_DEPS)
async def get_user_permissions(
    user_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """查询用户按钮权限点（角色继承 + 直挂并集，供「按用户授权」矩阵回显）。

    仅 ``platform_admin`` 可查。直挂权限点来自 ``user_permission`` 表（TD §12.5 增强）。
    """
    svc = _svc(db, request)
    data = await svc.get_user_ui_permissions(user_id)
    return ok(data=UserPermissionResponse.model_validate(data).model_dump(), trace_id=trace_id)


@router.put("/users/{user_id}/permissions", dependencies=_ROLE_ADMIN_DEPS)
async def set_user_permissions(
    user_id: int,
    payload: UserPermissionUpdateRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """整表替换用户直挂的按钮权限点（空列表=清空直挂，回退为仅角色继承）。

    仅 ``platform_admin`` 可配；未知权限点返回 ``USER_PERMISSION_INVALID``。
    ``deny_actions`` 为负向收窄（用户级禁用，优先于角色继承与直挂授权）。
    写操作落审计。
    """
    svc = _svc(db, request)
    data = await svc.set_user_ui_permissions(
        user_id,
        payload.actions,
        actor_id=user.id,
        reason=payload.reason,
        deny_actions=payload.deny_actions,
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="user.update_permissions",
        entity_type="user",
        entity_id=str(user_id),
        detail={
            "actions": payload.actions,
            "deny_actions": payload.deny_actions,
            "reason": payload.reason,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=UserPermissionResponse.model_validate(data).model_dump(), trace_id=trace_id)


@router.delete("/roles/{role}/permissions", dependencies=_ROLE_ADMIN_DEPS)
async def reset_role_permissions(
    role: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """清除某角色的权限点覆盖，恢复 ``policy.ROLE_ACTIONS`` 默认基线。"""
    if not await _is_manageable_role(db, role):
        raise ValidationError(
            f"未知角色: {role}", error_code="ROLE_PERMISSION_INVALID", ctx={"role": role}
        )
    svc = _svc(db, request)
    item = await svc.reset_role_permissions(role)
    await write_audit(
        db,
        actor_id=user.id,
        action="role.reset_permissions",
        entity_type="role",
        entity_id=role,
        detail={"reset": True},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=item, trace_id=trace_id)


@router.post("/grants", dependencies=_GRANT_ADMIN_DEPS)
async def create_grant(
    payload: GrantCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """域授权 + 指标白名单（支持临时授权 TTL）。"""
    svc = _svc(db, request)
    row = await svc.grant(payload, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="grant.create",
        entity_type="grants",
        entity_id=str(row.id),
        detail={
            "user_id": row.user_id,
            "domain": row.domain,
            "grant_type": str(row.grant_type),
            "metric_whitelist": row.metric_whitelist,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=GrantResponse.model_validate(row).model_dump(), trace_id=trace_id)


@router.get("/grants", dependencies=_READ_DEPS)
async def list_grants(
    params: Annotated[GrantListParams, Depends()],
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """授权列表；非管理员仅可查看自己的授权。"""
    svc = _svc(db, request)
    if user.role not in _GRANT_ADMIN_ROLES:
        params = params.model_copy(update={"user_id": user.id})
    rows, total = await svc.list_grants(params)
    return ok(
        data={
            "items": [GrantResponse.model_validate(r).model_dump() for r in rows],
            "total": total,
            "page": params.page,
            "page_size": params.page_size,
        },
        trace_id=trace_id,
    )


@router.post("/grants/batch", dependencies=_GRANT_ADMIN_DEPS)
async def batch_grants(
    payload: GrantBatchRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """批量授权/回收：逐条审计，任一失败整批回滚（R3-07）。"""
    svc = _svc(db, request)
    try:
        result = await svc.batch(payload, actor_id=user.id, dry_run=False)
    except Exception:
        await db.rollback()
        raise
    for item in result.items:
        await write_audit(
            db,
            actor_id=user.id,
            action=f"grant.batch_{payload.operation}",
            entity_type="grants",
            entity_id=str(item.user_id),
            detail={"domain": item.domain, "detail": item.detail},
            ip=client_ip(request),
            trace_id=trace_id,
        )
    await db.commit()
    return ok(data=result.model_dump(), trace_id=trace_id)


@router.post("/grants/batch/dry-run", dependencies=_GRANT_ADMIN_DEPS)
async def dry_run_batch_grants(
    payload: GrantBatchRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """批量操作影响预览：只读，不产生任何写入。"""
    svc = _svc(db, request)
    result = await svc.batch(payload, actor_id=user.id, dry_run=True)
    await db.rollback()
    return ok(data=result.model_dump(), trace_id=trace_id)


@router.delete("/grants/{grant_id}", dependencies=_GRANT_ADMIN_DEPS)
async def revoke_grant(
    grant_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    reason: str | None = None,
) -> ApiResponse[Any]:
    """回收单条授权。

    范围由服务层 ``_assert_revoke_scope`` 收敛：平台管理员全局可回收；域管理员仅本域；
    其它角色仅可回收本人授权（owner 自管）。越权一律 403。
    """
    svc = _svc(db, request)
    row = await svc.revoke(grant_id, actor_id=user.id, reason=reason)
    await write_audit(
        db,
        actor_id=user.id,
        action="grant.revoke",
        entity_type="grants",
        entity_id=str(grant_id),
        detail={"user_id": row.user_id, "reason": reason},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=GrantResponse.model_validate(row).model_dump(), trace_id=trace_id)


@router.post("/pii/review", dependencies=_COMPLIANCE_DEPS)
async def pii_review(
    payload: PiiReviewRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """合规官 PII 复核（COMPL-1）；通过后 semantic 方可发布。"""
    svc = _svc(db, request)
    result = await svc.pii_review(payload, reviewer=user)
    await write_audit(
        db,
        actor_id=user.id,
        action="metric.review_pii",
        entity_type="metric",
        entity_id=payload.metric_code,
        detail={
            "decision": payload.decision,
            "sensitivity_level": payload.sensitivity_level.value,
            "masking_policy": result.masking_policy,
            "comment": payload.comment,
        },
        ip=client_ip(request),
        trace_id=trace_id,
        pii_access=True,
    )
    await db.commit()
    return ok(data=result.model_dump(), trace_id=trace_id)


class PiiValidationRequest(BaseModel):
    """PII 字段级脱敏二次校验请求体。"""

    metric_code: str = Field(min_length=1, max_length=128, description="待校验指标编码")
    pii_columns: list[str] | None = Field(default=None, description="待校验 PII 字段（可选）")


@router.post(
    "/pii/validate",
    dependencies=_COMPLIANCE_DEPS,
)
async def pii_validate(
    payload: PiiValidationRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """PII 字段级脱敏二次校验（落库外 / 查询侧补强，依赖 governance）。"""

    svc = _svc(db, request)
    result = await svc.validate_pii_masking(payload.metric_code, pii_columns=payload.pii_columns)
    await write_audit(
        db,
        actor_id=user.id,
        action="metric.secondary_validate_pii",
        entity_type="metric",
        entity_id=payload.metric_code,
        detail={"passed": result.passed, "findings": result.findings},
        ip=client_ip(request),
        trace_id=trace_id,
        pii_access=True,
    )
    await db.commit()
    return ok(data=result.model_dump(), trace_id=trace_id)


@router.post("/classification/rescan", dependencies=_COMPLIANCE_DEPS)
async def classification_rescan(
    payload: ClassificationRescanRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """分级重扫（COMPL-2）；引擎异常时标 UNKNOWN 降级，不阻断整批。"""
    svc = _svc(db, request)
    result = await svc.classification_rescan(payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="db_catalog.rescan_classification",
        entity_type="db_catalog",
        entity_id=payload.source_id or "batch",
        detail={
            "scanned": result.scanned,
            "changed": result.changed,
            "pii_found": result.pii_found,
            "degraded": result.degraded,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result.model_dump(), trace_id=trace_id)


@router.post(
    "/catalogs/classification/{catalog_id}/false-positive",
    dependencies=_COMPLIANCE_DEPS,
)
async def classification_false_positive(
    catalog_id: int,
    payload: ClassificationFalsePositiveRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """误报反馈（COMPL-3）：字段/前缀写入 pii_vocab 豁免词表并重算实体降级。

    治理者在资产地图待复核明细发现误判后一键反馈，无需改代码发版。
    """
    svc = _svc(db, request)
    result = await svc.classification_false_positive(
        catalog_id=catalog_id,
        column=payload.column,
        scope=payload.scope,
        reason=payload.reason,
        actor_id=user.id,
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="classification.false_positive",
        entity_type="db_catalog",
        entity_id=str(catalog_id),
        detail={
            "entity_name": result.entity_name,
            "column": result.column,
            "scope": result.scope,
            "exempted_as": result.exempted_as,
            "sensitivity_before": result.sensitivity_before,
            "sensitivity_after": result.sensitivity_after,
            "reason": payload.reason,
        },
        ip=client_ip(request),
        trace_id=trace_id,
        pii_access=True,
    )
    await db.commit()
    return ok(data=result.model_dump(), trace_id=trace_id)


@router.get("/me/permissions")
async def my_permissions(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """当前用户权限快照（域授权 / 指标白名单 / 临期提醒）。"""
    svc = _svc(db, request)
    snapshot = await svc.my_permissions(user)
    return ok(data=snapshot.model_dump(), trace_id=trace_id)


@router.post("/permissions/check", dependencies=[Depends(guard_against_injection)])
async def check_permission(
    payload: PermissionCheckRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """PDP 决策（默认拒绝）；非管理员只能查询自己的权限。"""
    svc = _svc(db, request)
    if user.role not in _GRANT_ADMIN_ROLES:
        payload = payload.model_copy(update={"user_id": user.id})
    result = await svc.check_permission(payload)
    return ok(data=result.model_dump(), trace_id=trace_id)


@router.post(
    "/erasure",
    dependencies=[Depends(require_roles(*_COMPLIANCE_ROLES)), Depends(guard_against_injection)],
    summary="执行被遗忘权（D9 / R7-09③）",
)
async def erasure_execute(
    payload: ErasureRequestCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[ErasureResult]:
    """执行被遗忘权（数据主体删除请求）。

    仅合规官或平台管理员可对被指定数据主体执行审计去标识化。
    WORM 约束下审计行**物理删除被禁止**，本接口以覆写脱敏实现——
    命中主体的审计行 ``ip`` / ``detail_json`` 个人标识覆写为 ``ANONYMIZED_<hash>``，
    并写入 ``PII_ANONYMIZED`` 审计留存与 ``erasure_request`` 台账。
    """
    # S20（审查修复）：禁止自抹审计——执行者不能对自身执行 erasure，
    # 防止合规官抹去自己的历史审计痕迹（审计完整性兜底）。
    if payload.subject_user_id == user.id:
        from app.core.exceptions import ValidationError

        raise ValidationError(
            "不能对当前登录账号执行被遗忘权（防自抹审计痕迹）",
            error_code="SELF_ERASURE_FORBIDDEN",
        )
    svc = _svc(db, request)
    erasure = await svc.execute_erasure(payload.subject_user_id, user.id, payload.reason)
    await write_audit(
        db,
        actor_id=user.id,
        action="erasure.execute",
        entity_type="erasure_request",
        entity_id=str(erasure.id),
        detail={"subject_user_id": payload.subject_user_id, "affected_rows": erasure.affected_rows},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=ErasureResult(
            subject_user_id=erasure.subject_user_id,
            status=erasure.status.value,
            token_prefix=erasure.token[:12],
            affected_rows=erasure.affected_rows,
            requested_at=erasure.created_at,
        ),
        trace_id=trace_id,
    )
