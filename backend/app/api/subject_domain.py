"""主题域 API 端点（/api/v1/domains）。

对齐 spec FR-001~FR-004, FR-013, plan.md 域管理 API。
RBAC: platform_admin + domain_admin 可管理域；ALL_ROLES 可查询。
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessError, NotFoundError
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


def _get_service(db: AsyncSession = Depends(get_session)) -> SubjectDomainService:
    return SubjectDomainService(db)


def _require_admin(role: str | None = None) -> None:
    """简易 RBAC：仅 platform_admin / domain_admin 可管理。"""
    # TODO: 接入真实 RBAC 中间件后替换
    pass


@router.get("/", response_model=list[SubjectDomainTreeNode], summary="查询域树")
async def list_domain_tree(
    status: str | None = None,
    svc: SubjectDomainService = Depends(_get_service),
) -> list[SubjectDomainTreeNode]:
    return await svc.list_tree(status)


@router.get("/{code}", response_model=SubjectDomainResponse, summary="获取域详情")
async def get_domain(
    code: str,
    svc: SubjectDomainService = Depends(_get_service),
) -> dict[str, Any]:
    return await svc.get_domain_with_count(code)


@router.post(
    "/",
    response_model=SubjectDomainResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建域节点",
)
async def create_domain(
    data: SubjectDomainCreate,
    svc: SubjectDomainService = Depends(_get_service),
) -> Any:
    _require_admin()
    try:
        domain = await svc.create_domain(data)
        return await svc.get_domain_with_count(domain.code)
    except BusinessError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.put("/{code}", response_model=SubjectDomainResponse, summary="更新域")
async def update_domain(
    code: str,
    data: SubjectDomainUpdate,
    svc: SubjectDomainService = Depends(_get_service),
) -> Any:
    _require_admin()
    try:
        domain = await svc.update_domain(code, data)
        return await svc.get_domain_with_count(domain.code)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.patch("/{code}/status", response_model=SubjectDomainResponse, summary="启用/停用域")
async def toggle_domain_status(
    code: str,
    action: str,  # "activate" or "deactivate"
    svc: SubjectDomainService = Depends(_get_service),
) -> Any:
    _require_admin()
    try:
        if action == "activate":
            domain = await svc.activate_domain(code)
        else:
            domain = await svc.deactivate_domain(code)
        return await svc.get_domain_with_count(domain.code)
    except (NotFoundError, BusinessError) as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.delete("/{code}", status_code=status.HTTP_204_NO_CONTENT, summary="删除域")
async def delete_domain(
    code: str,
    svc: SubjectDomainService = Depends(_get_service),
) -> None:
    _require_admin()
    try:
        await svc.delete_domain(code)
    except (NotFoundError, BusinessError) as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.get("/{code}/defaults", summary="获取域默认值预设")
async def get_domain_defaults(
    code: str,
    svc: SubjectDomainService = Depends(_get_service),
) -> dict[str, Any]:
    return await svc.get_defaults(code)


@router.put("/{code}/defaults", summary="更新域默认值预设")
async def update_domain_defaults(
    code: str,
    data: SubjectDomainDefaultsUpdate,
    svc: SubjectDomainService = Depends(_get_service),
) -> dict[str, Any]:
    _require_admin()
    try:
        domain = await svc.update_defaults(code, data)
        return domain.defaults_json or {}
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.get("/{code}/metrics", summary="获取该域下指标列表")
async def get_domain_metrics(
    code: str,
    svc: SubjectDomainService = Depends(_get_service),
) -> list[dict[str, Any]]:
    return await svc.get_domain_metrics(code)
