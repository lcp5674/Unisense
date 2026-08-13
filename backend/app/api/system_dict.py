"""系统字典 API 端点（/api/v1/dicts）。

对齐 spec FR-005~FR-007, FR-014, plan.md 字典管理 API。
RBAC: platform_admin 可管理字典；ALL_ROLES 可查询。
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessError, NotFoundError
from app.db.mysql import get_db_session as get_session
from app.services.system_dict.schemas import DictItemCreate, DictItemResponse, DictItemUpdate
from app.services.system_dict.service import SystemDictService

logger = structlog.get_logger("unisense.api.system_dict")

router = APIRouter(prefix="/dicts", tags=["系统字典管理"])


def _get_service(db: AsyncSession = Depends(get_session)) -> SystemDictService:
    return SystemDictService(db)


def _require_admin() -> None:
    """简易 RBAC：仅 platform_admin 可管理字典。"""
    # TODO: 接入真实 RBAC 中间件后替换
    pass


@router.get("/types", response_model=list[str], summary="列出所有字典类型")
async def list_dict_types(
    svc: SystemDictService = Depends(_get_service),
) -> list[str]:
    return await svc.list_dict_types()


@router.get(
    "/{dict_type}",
    response_model=list[DictItemResponse],
    summary="获取某类型字典列表（仅active）",
)
async def list_dict_items(
    dict_type: str,
    svc: SystemDictService = Depends(_get_service),
) -> list[Any]:
    return await svc.list_by_type(dict_type, status="active")


@router.get(
    "/{dict_type}/all",
    response_model=list[DictItemResponse],
    summary="获取某类型全部字典项（含inactive）",
)
async def list_all_dict_items(
    dict_type: str,
    svc: SystemDictService = Depends(_get_service),
) -> list[Any]:
    return await svc.list_all_by_type(dict_type)


@router.post(
    "/{dict_type}",
    response_model=DictItemResponse,
    status_code=status.HTTP_201_CREATED,
    summary="新增字典项",
)
async def create_dict_item(
    dict_type: str,
    data: DictItemCreate,
    svc: SystemDictService = Depends(_get_service),
) -> Any:
    _require_admin()
    try:
        item = await svc.create_item(dict_type, data)
        ref_count = await svc.get_ref_count(dict_type, item.code)
        return {**_item_to_dict(item), "ref_count": ref_count}
    except BusinessError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc


@router.put("/{dict_type}/{code}", response_model=DictItemResponse, summary="更新字典项")
async def update_dict_item(
    dict_type: str,
    code: str,
    data: DictItemUpdate,
    svc: SystemDictService = Depends(_get_service),
) -> Any:
    _require_admin()
    try:
        item = await svc.update_item(dict_type, code, data)
        ref_count = await svc.get_ref_count(dict_type, code)
        return {**_item_to_dict(item), "ref_count": ref_count}
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.patch(
    "/{dict_type}/{code}/status",
    response_model=DictItemResponse,
    summary="启用/停用字典项",
)
async def toggle_dict_item_status(
    dict_type: str,
    code: str,
    action: str,  # "activate" or "deactivate"
    svc: SystemDictService = Depends(_get_service),
) -> Any:
    _require_admin()
    try:
        if action == "activate":
            item = await svc.activate_item(dict_type, code)
        else:
            item = await svc.deactivate_item(dict_type, code)
        ref_count = await svc.get_ref_count(dict_type, code)
        return {**_item_to_dict(item), "ref_count": ref_count}
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.delete("/{dict_type}/{code}", status_code=status.HTTP_204_NO_CONTENT, summary="删除字典项")
async def delete_dict_item(
    dict_type: str,
    code: str,
    svc: SystemDictService = Depends(_get_service),
) -> None:
    _require_admin()
    try:
        await svc.delete_item(dict_type, code)
    except (NotFoundError, BusinessError) as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.get("/{dict_type}/{code}/ref-count", summary="获取字典项引用计数")
async def get_dict_item_ref_count(
    dict_type: str,
    code: str,
    svc: SystemDictService = Depends(_get_service),
) -> dict[str, int]:
    ref_count = await svc.get_ref_count(dict_type, code)
    return {"ref_count": ref_count}


def _item_to_dict(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "dict_type": item.dict_type,
        "code": item.code,
        "label": item.label,
        "sort_order": item.sort_order,
        "status": item.status,
        "description": item.description,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }
