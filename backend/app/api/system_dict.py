"""系统字典 API 端点（/api/v1/dicts）。

对齐 spec FR-005~FR-007, FR-014, plan.md 字典管理 API。
RBAC: platform_admin 可管理字典；ALL_ROLES 可查询。
统一响应信封：{code, message, data, trace_id}。
审计：全部写操作落 audit_log（action=DICT_CREATE/DICT_UPDATE/DICT_STATUS/DICT_DELETE）。
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.exceptions import BusinessError, NotFoundError
from app.core.guard import guard_against_injection, guard_against_injection_exempt
from app.db.mysql import get_db_session as get_session
from app.services.system_dict.schemas import (
    DictBatchCreateRequest,
    DictBatchDeleteRequest,
    DictBatchResult,
    DictBatchStatusRequest,
    DictInferDescriptionRequest,
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
#: 描述 LLM 推断：platform_admin 写权限；label/dict_type/dict_type_label 是合法业务
#: 输入，仅作 LLM prompt 上下文、不拼接进 DB 查询，豁免注入扫描（对齐 refine-definition）。
_INFER_DEPS = [
    Depends(require_roles("platform_admin")),
    Depends(guard_against_injection_exempt("label", "dict_type", "dict_type_label")),
]
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


@router.post(
    "/infer-description",
    response_model=ApiResponse[dict[str, str]],
    summary="参照数据项描述 LLM 推断（新增/编辑弹窗 AI 生成描述）",
    dependencies=_INFER_DEPS,
)
async def infer_dict_item_description(
    request: DictInferDescriptionRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    user: CurrentUser,
    http_req: Request,
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[dict[str, str]]:
    """根据参照数据项显示名（+字典类型上下文）用 LLM 推断生成一段简洁描述。

    仅生成文本回填表单，不落库（落库仍走既有 create/update 流程）；LLM 不可用
    或返回空内容抛 ``LLM_INFER_UNAVAILABLE``。
    """
    from app.services.llm.config_service import LlmConfigService

    llm_client = await LlmConfigService(db).build_client()
    if not getattr(llm_client, "enabled", False):
        raise BusinessError(
            "LLM 不可用：请检查 LLM 配置或稍后重试",
            error_code="LLM_INFER_UNAVAILABLE",
            ctx={"dict_type": request.dict_type},
        )
    prompt = _build_dict_description_prompt(request)
    try:
        resp = await llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            # 描述是纯文本，显式 text 避免被 chat 缺省 json_object 约束污染为空 JSON
            response_format={"type": "text"},
        )
    except Exception as exc:  # noqa: BLE001 - LLM 网络/超时等统一转业务错误
        logger.warning(
            "dict_infer_description_llm_failed",
            dict_type=request.dict_type,
            label=request.label,
            error=str(exc)[:200],
        )
        raise BusinessError(
            "LLM 调用失败，请稍后重试",
            error_code="LLM_INFER_UNAVAILABLE",
            ctx={"dict_type": request.dict_type},
        ) from exc

    from app.services.llm.parse import strip_code_fence

    description = (resp.get("content") or "").strip()
    description = strip_code_fence(description).strip().strip("\"'")
    if not description:
        raise BusinessError(
            "LLM 未返回有效内容，请重试",
            error_code="LLM_INFER_UNAVAILABLE",
            ctx={"dict_type": request.dict_type},
        )
    await write_audit(
        db,
        actor_id=user.id,
        action="dict.infer_description",
        entity_type="dict_item",
        entity_id=f"{request.dict_type}:{request.label}",
        detail={"dict_type": request.dict_type, "label": request.label},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"description": description}, trace_id=trace_id)


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
    except BusinessError:
        await svc._db.rollback()
        raise


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
    except NotFoundError:
        await svc._db.rollback()
        raise


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
    except NotFoundError:
        await svc._db.rollback()
        raise


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
    except (NotFoundError, BusinessError):
        await svc._db.rollback()
        raise


@router.post(
    "/{dict_type}/batch",
    response_model=ApiResponse[DictBatchResult],
    summary="批量新增字典项",
    dependencies=_ADMIN_DEPS,
)
async def batch_create_dict_items(
    dict_type: str,
    data: DictBatchCreateRequest,
    user: CurrentUser,
    request: Request,
    svc: SystemDictService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[DictBatchResult]:
    """批量新增同一类型字典项（207 语义：单条失败逐项标注，不影响其余）。

    ``code`` 缺省时逐条按显示名自动生成英文编码；与既有项编码重复的条目
    记为失败项（DUPLICATE_DICT_CODE），其余正常创建。
    """
    result = await svc.batch_create_items(dict_type, data.items)
    await write_audit(
        svc._db,
        actor_id=user.id,
        action="dict.batch_create",
        entity_type="dict_item",
        entity_id=f"items:{len(data.items)}",
        detail={
            "dict_type": dict_type,
            "succeeded": len(result.succeeded),
            "failed": len(result.failed),
            "failed_items": [
                {"code": f.code, "error_code": f.error_code, "message": f.message}
                for f in result.failed
            ],
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await svc._db.commit()
    return ok(data=result, trace_id=trace_id)


@router.post(
    "/{dict_type}/batch-status",
    response_model=ApiResponse[DictBatchResult],
    summary="批量启用/停用字典项",
    dependencies=_ADMIN_DEPS,
)
async def batch_toggle_dict_items(
    dict_type: str,
    data: DictBatchStatusRequest,
    user: CurrentUser,
    request: Request,
    svc: SystemDictService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[DictBatchResult]:
    """批量启用/停用同一类型字典项（207 语义）。

    ``action`` 为 ``activate``（启用）或 ``deactivate``（停用）；不存在的
    编码记为 NOT_FOUND 失败项，其余逐条切换状态。
    """
    result = await svc.batch_toggle_items(dict_type, data.codes, data.action)
    await write_audit(
        svc._db,
        actor_id=user.id,
        action="dict.batch_enable" if data.action == "activate" else "dict.batch_disable",
        entity_type="dict_item",
        entity_id=f"items:{len(data.codes)}",
        detail={
            "dict_type": dict_type,
            "action": data.action,
            "succeeded": len(result.succeeded),
            "failed": len(result.failed),
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await svc._db.commit()
    return ok(data=result, trace_id=trace_id)


@router.post(
    "/{dict_type}/batch-delete",
    response_model=ApiResponse[DictBatchResult],
    summary="批量删除字典项",
    dependencies=_ADMIN_DEPS,
)
async def batch_delete_dict_items(
    dict_type: str,
    data: DictBatchDeleteRequest,
    user: CurrentUser,
    request: Request,
    svc: SystemDictService = Depends(_get_service),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[DictBatchResult]:
    """批量删除同一类型字典项（软删，207 语义）。

    被指标引用的项记为 HAS_REFERENCES 失败项（提示先停用），其余软删除。
    """
    result = await svc.batch_delete_items(dict_type, data.codes)
    await write_audit(
        svc._db,
        actor_id=user.id,
        action="dict.batch_delete",
        entity_type="dict_item",
        entity_id=f"items:{len(data.codes)}",
        detail={
            "dict_type": dict_type,
            "succeeded": len(result.succeeded),
            "failed": len(result.failed),
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await svc._db.commit()
    return ok(data=result, trace_id=trace_id)


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


def _build_dict_description_prompt(req: DictInferDescriptionRequest) -> str:
    """构建参照数据项描述 LLM 推断提示词。

    字典类型中文名（dict_type_label）与显示名作为上下文；要求输出一段简洁、
    面向数据治理的中文描述（说明该取值含义/用途/适用场景），不带表名/技术细节。
    """
    type_name = req.dict_type_label or req.dict_type
    return (
        f"请为数据字典「{type_name}」新增的参照数据项写一段简洁的中文描述"
        f"（50 字以内），说明该取值在数据治理/指标定义中的含义与用途。"
        f"只输出描述本身，不要任何前缀、引号或解释。\n\n"
        f"参照数据项显示名：{req.label}"
    )


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
