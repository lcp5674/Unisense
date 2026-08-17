"""系统字典 API 端点（/api/v1/dicts）。

对齐 spec FR-005~FR-007, FR-014, plan.md 字典管理 API。
RBAC: platform_admin 可管理字典；ALL_ROLES 可查询。
统一响应信封：{code, message, data, trace_id}。
审计：全部写操作落 audit_log（action=DICT_CREATE/DICT_UPDATE/DICT_STATUS/DICT_DELETE）。
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.exceptions import BusinessError, NotFoundError
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session as get_session
from app.services.system_dict.schemas import (
    DictItemCreate,
    DictItemResponse,
    DictItemUpdate,
    DictUnknownNotifyRequest,
    DictUnknownRejectRequest,
    DictValuesVerifyRequest,
    DictValuesVerifyResponse,
)
from app.services.system_dict.service import SystemDictService

logger = structlog.get_logger("unisense.api.system_dict")

router = APIRouter(prefix="/dicts", tags=["系统字典管理"])

#: 字典管理写权限：仅 platform_admin（与 docstring/plan 声明一致）。
_ADMIN_DEPS = [Depends(require_roles("platform_admin")), Depends(guard_against_injection)]
#: 字典查询读权限：全部已登录角色。
_READ_DEPS = [Depends(require_roles(*ALL_ROLES)), Depends(guard_against_injection)]


def _get_service(db: AsyncSession = Depends(get_session)) -> SystemDictService:
    return SystemDictService(db)


@router.get(
    "/types",
    response_model=ApiResponse[list[str]],
    summary="列出所有字典类型",
    dependencies=_READ_DEPS,
)
async def list_dict_types(
    svc: SystemDictService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[list[str]]:
    data = await svc.list_dict_types()
    return ok(data=data, trace_id=trace_id)


@router.get(
    "/{dict_type}",
    response_model=ApiResponse[list[DictItemResponse]],
    summary="获取某类型字典列表（仅active）",
    dependencies=_READ_DEPS,
)
async def list_dict_items(
    dict_type: str,
    svc: SystemDictService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[list[DictItemResponse]]:
    data = await svc.list_by_type(dict_type, status="active")
    items = [_item_response(item, await svc.get_ref_count(dict_type, item.code)) for item in data]
    return ok(data=items, trace_id=trace_id)


@router.get(
    "/{dict_type}/all",
    response_model=ApiResponse[list[DictItemResponse]],
    summary="获取某类型全部字典项（含inactive）",
    dependencies=_READ_DEPS,
)
async def list_all_dict_items(
    dict_type: str,
    svc: SystemDictService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[list[DictItemResponse]]:
    data = await svc.list_all_by_type(dict_type)
    items = [_item_response(item, await svc.get_ref_count(dict_type, item.code)) for item in data]
    return ok(data=items, trace_id=trace_id)


@router.post(
    "/verify-values",
    response_model=ApiResponse[DictValuesVerifyResponse],
    summary="批量校验字典值是否未收录（指标保存前权威检测）",
    dependencies=_READ_DEPS,
)
async def verify_dict_values(
    data: DictValuesVerifyRequest,
    svc: SystemDictService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[DictValuesVerifyResponse]:
    """批量检测 (dict_type, value) 中哪些未收录于系统字典。

    指标编辑弹窗保存前调用：前端字典快照可能过期，以 DB 实时判定为准。
    仅读操作，不落审计。
    """
    unknown = await svc.verify_values(
        [{"dict_type": v.dict_type, "value": v.value} for v in data.values]
    )
    return ok(data=DictValuesVerifyResponse(unknown=unknown), trace_id=trace_id)


@router.post(
    "/unknown/notify",
    response_model=ApiResponse[dict[str, int]],
    summary="无收录权限用户保存未收录值时，通知管理员收录/打回",
    dependencies=_READ_DEPS,
)
async def notify_unknown_dict_values(
    data: DictUnknownNotifyRequest,
    user: CurrentUser,
    request: Request,
    svc: SystemDictService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[dict[str, int]]:
    """提交人无收录权限（非 platform_admin）时，把未收录值定向通知全部平台管理员。

    服务端复核确实未收录（防伪造）；同一未收录值窗口内对同一管理员去重。
    返回 ``{"notified": 通知条数, "unknown": 未收录值数}``。
    """
    result = await svc.notify_unknown_values(
        metric_code=data.metric_code,
        values=[{"dict_type": v.dict_type, "value": v.value} for v in data.values],
        actor_id=user.id,
        actor_name=user.display_name or user.username,
        note=data.note,
    )
    await write_audit(
        svc._db,
        actor_id=user.id,
        action="dict.notify_unknown",
        entity_type="system_dict",
        entity_id=data.metric_code or "-",
        detail={
            "values": [v.model_dump() for v in data.values],
            "note": data.note,
            "result": result,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await svc._db.commit()
    return ok(data=result, trace_id=trace_id)


@router.post(
    "/unknown/reject",
    response_model=ApiResponse[dict[str, Any]],
    summary="管理员打回字典收录申请（通知提交人改用字典内值）",
    dependencies=_ADMIN_DEPS,
)
async def reject_unknown_dict_value(
    data: DictUnknownRejectRequest,
    user: CurrentUser,
    request: Request,
    svc: SystemDictService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[dict[str, Any]]:
    """平台管理员把「字典未收录值待收录」通知打回：通知提交人改用字典内值。

    仅 platform_admin 可调用（``_ADMIN_DEPS``）；原通知办结（不再出现在待处理）。
    """
    notif = await svc.reject_unknown_value(
        notification_id=data.notification_id,
        reason=data.reason,
        actor_id=user.id,
        actor_name=user.display_name or user.username,
    )
    await write_audit(
        svc._db,
        actor_id=user.id,
        action="dict.reject_unknown",
        entity_type="notification",
        entity_id=str(data.notification_id),
        detail={"reason": data.reason},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await svc._db.commit()
    return ok(
        data={"notification_id": notif.id, "handled": notif.handled_at is not None},
        trace_id=trace_id,
    )


@router.post(
    "/{dict_type}",
    response_model=ApiResponse[DictItemResponse],
    status_code=status.HTTP_201_CREATED,
    summary="新增字典项",
    dependencies=_ADMIN_DEPS,
)
async def create_dict_item(
    dict_type: str,
    data: DictItemCreate,
    user: CurrentUser,
    request: Request,
    svc: SystemDictService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[DictItemResponse]:
    try:
        item = await svc.create_item(dict_type, data)
        ref_count = await svc.get_ref_count(dict_type, item.code)
        await write_audit(
            svc._db,
            actor_id=user.id,
            action="dict.create",
            entity_type="dict_item",
            entity_id=f"{dict_type}:{item.code}",
            detail={"dict_type": dict_type, "code": item.code, "label": item.label},
            ip=client_ip(request),
            trace_id=trace_id,
        )
        await svc._db.commit()
        return ok(data=_item_response(item, ref_count), trace_id=trace_id)
    except BusinessError as exc:
        await svc._db.rollback()
        raise HTTPException(status_code=409, detail=exc.message) from exc


@router.put(
    "/{dict_type}/{code}",
    response_model=ApiResponse[DictItemResponse],
    summary="更新字典项",
    dependencies=_ADMIN_DEPS,
)
async def update_dict_item(
    dict_type: str,
    code: str,
    data: DictItemUpdate,
    user: CurrentUser,
    request: Request,
    svc: SystemDictService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[DictItemResponse]:
    try:
        item = await svc.update_item(dict_type, code, data)
        ref_count = await svc.get_ref_count(dict_type, code)
        await write_audit(
            svc._db,
            actor_id=user.id,
            action="dict.update",
            entity_type="dict_item",
            entity_id=f"{dict_type}:{code}",
            detail={"dict_type": dict_type, "code": code, "label": item.label},
            ip=client_ip(request),
            trace_id=trace_id,
        )
        await svc._db.commit()
        return ok(data=_item_response(item, ref_count), trace_id=trace_id)
    except NotFoundError as exc:
        await svc._db.rollback()
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.patch(
    "/{dict_type}/{code}/status",
    response_model=ApiResponse[DictItemResponse],
    summary="启用/停用字典项",
    dependencies=_ADMIN_DEPS,
)
async def toggle_dict_item_status(
    dict_type: str,
    code: str,
    action: str,  # "activate" or "deactivate"
    user: CurrentUser,
    request: Request,
    svc: SystemDictService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[DictItemResponse]:
    try:
        if action == "activate":
            item = await svc.activate_item(dict_type, code)
        else:
            item = await svc.deactivate_item(dict_type, code)
        ref_count = await svc.get_ref_count(dict_type, code)
        await write_audit(
            svc._db,
            actor_id=user.id,
            action="dict.update_status",
            entity_type="dict_item",
            entity_id=f"{dict_type}:{code}",
            detail={"dict_type": dict_type, "code": code, "action": action, "status": item.status},
            ip=client_ip(request),
            trace_id=trace_id,
        )
        await svc._db.commit()
        return ok(data=_item_response(item, ref_count), trace_id=trace_id)
    except NotFoundError as exc:
        await svc._db.rollback()
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.delete(
    "/{dict_type}/{code}",
    response_model=ApiResponse[dict[str, str]],
    summary="删除字典项",
    dependencies=_ADMIN_DEPS,
)
async def delete_dict_item(
    dict_type: str,
    code: str,
    user: CurrentUser,
    request: Request,
    svc: SystemDictService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[dict[str, str]]:
    try:
        await svc.delete_item(dict_type, code)
        await write_audit(
            svc._db,
            actor_id=user.id,
            action="dict.delete",
            entity_type="dict_item",
            entity_id=f"{dict_type}:{code}",
            detail={"dict_type": dict_type, "code": code},
            ip=client_ip(request),
            trace_id=trace_id,
        )
        await svc._db.commit()
        return ok(data={"detail": "deleted"}, trace_id=trace_id)
    except (NotFoundError, BusinessError) as exc:
        await svc._db.rollback()
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.get(
    "/{dict_type}/{code}/ref-count",
    response_model=ApiResponse[dict[str, int]],
    summary="获取字典项引用计数",
    dependencies=_READ_DEPS,
)
async def get_dict_item_ref_count(
    dict_type: str,
    code: str,
    svc: SystemDictService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[dict[str, int]]:
    ref_count = await svc.get_ref_count(dict_type, code)
    return ok(data={"ref_count": ref_count}, trace_id=trace_id)


def _item_response(item: Any, ref_count: int) -> DictItemResponse:
    return DictItemResponse(
        id=item.id,
        dict_type=item.dict_type,
        code=item.code,
        label=item.label,
        sort_order=item.sort_order,
        status=item.status,
        description=item.description,
        ref_count=ref_count,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )
