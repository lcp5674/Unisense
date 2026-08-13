"""主题域 API 端点（/api/v1/domains）。

对齐 spec FR-001~FR-004, FR-013, plan.md 域管理 API。
RBAC: platform_admin + domain_admin 可管理域；ALL_ROLES 可查询。
统一响应信封：{code, message, data, trace_id}。
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.exceptions import BusinessError, ConflictError, NotFoundError
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session as get_session
from app.services.subject_domain.schemas import (
    SubjectDomainCreate,
    SubjectDomainDefaultsUpdate,
    SubjectDomainResponse,
    SubjectDomainTreeNode,
    SubjectDomainUpdate,
)
from app.services.subject_domain.service import SubjectDomainService

logger = structlog.get_logger("unisense.api.subject_domain")

router = APIRouter(prefix="/domains", tags=["主题域管理"])

#: 域管理写权限：platform_admin + domain_admin（与 docstring/plan 声明一致）。
_ADMIN_DEPS = [
    Depends(require_roles("platform_admin", "domain_admin")),
    Depends(guard_against_injection),
]
#: 域查询读权限：全部已登录角色。
_READ_DEPS = [Depends(require_roles(*ALL_ROLES)), Depends(guard_against_injection)]


def _get_service(db: AsyncSession = Depends(get_session)) -> SubjectDomainService:
    return SubjectDomainService(db)


@router.get(
    "",
    response_model=ApiResponse[list[SubjectDomainTreeNode]],
    summary="查询域树",
    dependencies=_READ_DEPS,
)
@router.get(
    "/",
    response_model=ApiResponse[list[SubjectDomainTreeNode]],
    summary="查询域树",
    dependencies=_READ_DEPS,
)
async def list_domain_tree(
    status: str | None = None,
    svc: SubjectDomainService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[list[SubjectDomainTreeNode]]:
    data = await svc.list_tree(status)
    return ok(data=data, trace_id=trace_id)


@router.get(
    "/{code}",
    response_model=ApiResponse[SubjectDomainResponse],
    summary="获取域详情",
    dependencies=_READ_DEPS,
)
async def get_domain(
    code: str,
    svc: SubjectDomainService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[SubjectDomainResponse]:
    data = await svc.get_domain_with_count(code)
    return ok(data=data, trace_id=trace_id)


@router.post(
    "",
    response_model=ApiResponse[SubjectDomainResponse],
    status_code=status.HTTP_201_CREATED,
    summary="创建域节点",
    dependencies=_ADMIN_DEPS,
)
@router.post(
    "/",
    response_model=ApiResponse[SubjectDomainResponse],
    status_code=status.HTTP_201_CREATED,
    summary="创建域节点",
    dependencies=_ADMIN_DEPS,
)
async def create_domain(
    data: SubjectDomainCreate,
    user: CurrentUser,
    svc: SubjectDomainService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[SubjectDomainResponse]:
    try:
        # P2-3: 域管理员以认证身份为准（PLAT-2），不信任客户端传入的 owner_id
        domain = await svc.create_domain(data, owner_id=user.id)
        await svc._db.commit()
        result = await svc.get_domain_with_count(domain.code)
        return ok(data=result, trace_id=trace_id)
    except ConflictError as exc:
        await svc._db.rollback()
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except BusinessError as exc:
        await svc._db.rollback()
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except NotFoundError as exc:
        await svc._db.rollback()
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.put(
    "/{code}",
    response_model=ApiResponse[SubjectDomainResponse],
    summary="更新域",
    dependencies=_ADMIN_DEPS,
)
async def update_domain(
    code: str,
    data: SubjectDomainUpdate,
    svc: SubjectDomainService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[SubjectDomainResponse]:
    try:
        domain = await svc.update_domain(code, data)
        await svc._db.commit()
        result = await svc.get_domain_with_count(domain.code)
        return ok(data=result, trace_id=trace_id)
    except ConflictError as exc:
        await svc._db.rollback()
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except NotFoundError as exc:
        await svc._db.rollback()
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.patch(
    "/{code}/status",
    response_model=ApiResponse[SubjectDomainResponse],
    summary="启用/停用域",
    dependencies=_ADMIN_DEPS,
)
async def toggle_domain_status(
    code: str,
    action: str,  # "activate" or "deactivate"
    svc: SubjectDomainService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[SubjectDomainResponse]:
    try:
        if action == "activate":
            domain = await svc.activate_domain(code)
        else:
            domain = await svc.deactivate_domain(code)
        await svc._db.commit()
        result = await svc.get_domain_with_count(domain.code)
        return ok(data=result, trace_id=trace_id)
    except (NotFoundError, BusinessError) as exc:
        await svc._db.rollback()
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.delete(
    "/{code}",
    response_model=ApiResponse[dict[str, str]],
    summary="删除域",
    dependencies=_ADMIN_DEPS,
)
async def delete_domain(
    code: str,
    svc: SubjectDomainService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[dict[str, str]]:
    try:
        await svc.delete_domain(code)
        await svc._db.commit()
        return ok(data={"detail": "deleted"}, trace_id=trace_id)
    except (NotFoundError, BusinessError) as exc:
        await svc._db.rollback()
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.get(
    "/{code}/defaults",
    response_model=ApiResponse[dict[str, Any]],
    summary="获取域默认值预设",
    dependencies=_READ_DEPS,
)
async def get_domain_defaults(
    code: str,
    svc: SubjectDomainService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[dict[str, Any]]:
    data = await svc.get_defaults(code)
    return ok(data=data, trace_id=trace_id)


@router.put(
    "/{code}/defaults",
    response_model=ApiResponse[dict[str, Any]],
    summary="更新域默认值预设",
    dependencies=_ADMIN_DEPS,
)
async def update_domain_defaults(
    code: str,
    data: SubjectDomainDefaultsUpdate,
    svc: SubjectDomainService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[dict[str, Any]]:
    try:
        domain = await svc.update_defaults(code, data)
        await svc._db.commit()
        return ok(data=domain.defaults_json or {}, trace_id=trace_id)
    except NotFoundError as exc:
        await svc._db.rollback()
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.get(
    "/{code}/metrics",
    response_model=ApiResponse[list[dict[str, Any]]],
    summary="获取该域下指标列表",
    dependencies=_READ_DEPS,
)
async def get_domain_metrics(
    code: str,
    svc: SubjectDomainService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[list[dict[str, Any]]]:
    data = await svc.get_domain_metrics(code)
    return ok(data=data, trace_id=trace_id)
