"""逻辑度量目录 API（OneData 原子层，TD §4.2 / FR-02-08）。

照 dimension API 模式：角色依赖 + 注入守卫 + 域作用域守卫（domain_admin/metric_owner
仅可操作本域资源）+ 服务端分页。
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
from app.services.measure_catalog.schemas import MeasureCreate, MeasureResponse, MeasureUpdate
from app.services.measure_catalog.service import MeasureCatalogService

router = APIRouter(prefix="/measure-catalogs", tags=["measure_catalog"])

_WRITE_ROLES = ("metric_owner", "domain_admin", "platform_admin")
_READ_ROLES = ("metric_owner", "domain_admin", "platform_admin", "reviewer", "viewer")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]
# 写端点统一挂注入守卫（纵深防御：ORM 参数化兜底之外拦截注入 payload）
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]

# 域作用域守卫（P1-10）：domain_admin/metric_owner 仅可操作本域资源
_SCOPED_ROLES = ("domain_admin", "metric_owner")


def _assert_domain_scope(user: CurrentUser, resource_domain: str) -> None:
    if user.role in _SCOPED_ROLES and user.domain and resource_domain != user.domain:
        raise AuthError(
            f"无权限操作其他域的资源（资源域 {resource_domain}，当前域 {user.domain}）",
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
    await db.commit()
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
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Any:
    items, total = await MeasureCatalogService(db).list_measures(
        domain, status, keyword, owner_id, page=page, page_size=page_size
    )
    converted = [MeasureResponse.from_model(i) for i in items]
    return ok(
        data={"items": converted, "total": total, "page": page, "page_size": page_size},
        trace_id=trace_id,
    )


@router.get("/{measure_code}", dependencies=_READ_DEPS)
async def get_measure(
    measure_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await MeasureCatalogService(db).get_measure(measure_code)
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
    await db.commit()
    return ok(data=MeasureResponse.from_model(resp), trace_id=trace_id)


@router.post("/{measure_code}/publish", dependencies=_SCOPED_DEPS)
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
