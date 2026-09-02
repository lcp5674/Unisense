"""指标挂载实体 API（OneData 挂载层，TD §4.2 dataset_metric）。

照 dimension API 模式：角色依赖 + 注入守卫 + 服务端分页。
域作用域守卫以挂载的 domain 为准（domain_admin/metric_owner 仅可操作本域挂载）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.audit import write_audit
from app.core.exceptions import AuthError, NotFoundError
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.metric_mount.schemas import (
    MetricMountCreate,
    MetricMountResponse,
    MetricMountUpdate,
)
from app.services.metric_mount.service import MetricMountService
from app.services.semantic.visibility import metric_is_visible

router = APIRouter(prefix="/metric-mounts", tags=["metric_mount"])

_WRITE_ROLES = ("metric_owner", "domain_admin", "platform_admin")
_READ_ROLES = ("metric_owner", "domain_admin", "platform_admin", "reviewer", "viewer")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]
# 写端点统一挂注入守卫（纵深防御：ORM 参数化兜底之外拦截注入 payload）
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]

# 域作用域守卫（P1-10）：domain_admin/metric_owner 仅可操作本域资源
_SCOPED_ROLES = ("domain_admin", "metric_owner")


def _assert_domain_scope(user: CurrentUser, resource_domain: str) -> None:
    # 方案 A 多角色：任一角色命中作用域角色即受域约束（主角色或 user_role 扩展）。
    # 多域并集（团队继承 ∪ 显式指定，domains_all()）：有权限域时资源域必须 ∈ 权限域；
    # 无任何权限域 = 不限域（方案 A，前端展示「不限域」）→ 放行，不做域收敛。
    if any(r in _SCOPED_ROLES for r in user.roles_all()):
        domains = user.domains_all()
        if domains and resource_domain not in domains:
            raise AuthError(
                f"无权限操作其他域的资源（资源域 {resource_domain}，当前权限域 {domains}）",
                error_code="FORBIDDEN",
            )


async def _scope_mount(
    mount_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
) -> None:
    """路径携带 mount_id 的写操作：加载挂载并校验域作用域。"""
    mount = await MetricMountService(db).get_mount(mount_id)
    if mount is None:
        raise NotFoundError(f"挂载不存在: {mount_id}")
    _assert_domain_scope(user, mount.domain)


_SCOPED_DEPS = _WRITE_DEPS + [Depends(_scope_mount)]


@router.post("", status_code=201, dependencies=_WRITE_DEPS)
async def create_mount(
    payload: MetricMountCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # 域作用域守卫（P1-10）：域管理员仅可在本域建挂载
    _assert_domain_scope(user, payload.domain)
    resp = await MetricMountService(db).create_mount(payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="metric_mount.create",
        entity_type="metric_mount",
        entity_id=str(resp.id),
        detail={"metric_id": payload.metric_id},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=MetricMountResponse.from_model(resp), trace_id=trace_id)


@router.get("", dependencies=_READ_DEPS)
async def list_mounts(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    metric_id: int | None = Query(None, description="按指标过滤（一个指标一个挂载点）"),
    domain: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Any:
    items, total = await MetricMountService(db).list_mounts(
        metric_id,
        domain,
        page=page,
        page_size=page_size,
        visible_actor_id=user.id,
        visible_role=user.role,
        visible_user_domains=user.domains_all(),
    )
    converted = [
        MetricMountResponse.from_model(mount, metric) for mount, metric in items
    ]
    return ok(
        data={"items": converted, "total": total, "page": page, "page_size": page_size},
        trace_id=trace_id,
    )


@router.get("/{mount_id}", dependencies=_READ_DEPS)
async def get_mount(
    mount_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    row = await MetricMountService(db).get_mount_with_metric(mount_id)
    if row is None:
        raise NotFoundError(f"挂载不存在: {mount_id}")
    mount, metric = row
    # 用户级可见性（对齐列表口径）：非管理角色不得查看他人私有指标（DRAFT/REVIEW
    # 未指派）的挂载详情——挂载含源表/业务限定/责任方，属指标元数据一部分。
    # 指标物理不存在（metric is None）时同样拒绝：挂载指向的指标已不可追溯，
    # 不允许仅凭挂载行继续读取源表/业务限定（第三轮审查补严）。
    if metric is None or not metric_is_visible(
        metric, user.id, user.role, user.domains_all()
    ):
        raise NotFoundError(f"挂载不存在: {mount_id}")
    return ok(data=MetricMountResponse.from_model(mount, metric), trace_id=trace_id)


@router.put("/{mount_id}", dependencies=_SCOPED_DEPS)
async def update_mount(
    mount_id: int,
    payload: MetricMountUpdate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await MetricMountService(db).update_mount(mount_id, payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="metric_mount.update",
        entity_type="metric_mount",
        entity_id=str(mount_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=MetricMountResponse.from_model(resp), trace_id=trace_id)


@router.delete("/{mount_id}", dependencies=_SCOPED_DEPS)
async def delete_mount(
    mount_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    await MetricMountService(db).delete_mount(mount_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="metric_mount.delete",
        entity_type="metric_mount",
        entity_id=str(mount_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=None, trace_id=trace_id)
