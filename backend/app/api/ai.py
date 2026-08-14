"""AI 问数 API（TD §12.7 / FR-14）。

除 NL2SQL 外，提供 LLM 平台配置的多实例管理端点（系统配置页）：
- GET  /ai/config            读取全部 LLM 实例 + 路由策略 + 生效配置（脱敏）
- POST /ai/config            新增 LLM 实例（platform_admin/domain_admin）
- PUT  /ai/config/{id}       更新 LLM 实例（api_key 留空保持原密钥）
- DELETE /ai/config/{id}     删除 LLM 实例（软删除）
- POST /ai/config/test       测试连通性（已保存实例 或 临时载荷）
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.audit import write_audit
from app.core.exceptions import AuthError
from app.core.feature_flags import is_feature_enabled_or_default
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.ai.schemas import NL2SQLRequest
from app.services.ai.service import AiService
from app.services.llm.client import normalize_base_url
from app.services.llm.config_service import LlmConfigService
from app.services.llm.schemas import (
    LlmConfigListResponse,
    LlmConfigPayload,
    LlmConfigResponse,
    LlmConfigSecretResponse,
    LlmConfigTestRequest,
    LlmConfigTestResult,
    LlmModelsRequest,
    LlmModelsResult,
)

router = APIRouter(prefix="/ai", tags=["ai"])

_WRITE_ROLES = ("metric_owner", "domain_admin", "platform_admin", "analyst", "viewer")
_READ_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]
#: LLM 平台配置为平台级集成，仅管理员可读写/测试
_CONFIG_ADMIN_DEPS = [
    Depends(require_roles("platform_admin", "domain_admin")),
    Depends(guard_against_injection),
]


@router.post("/nl2sql", dependencies=_READ_DEPS)
async def nl2sql(
    payload: NL2SQLRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # OPS-09 特性开关：AI 问数能力可被平台管理员灰度关闭（kill switch）。
    if not is_feature_enabled_or_default("ai.nl2sql"):
        raise AuthError(
            "AI 问数能力已被平台管理员关闭",
            error_code="FORBIDDEN",
            ctx={"feature_flag": "ai.nl2sql"},
        )
    cfg = LlmConfigService(db)
    llm = await cfg.build_client()
    svc = AiService(db, llm=llm)
    try:
        result = await svc.ask(
            payload.nl_query, execute=payload.execute, metric_scope=payload.metric_scope
        )
        await write_audit(
            db,
            actor_id=user.id,
            action="ai.nl2sql",
            entity_type="nl_query",
            entity_id=payload.nl_query[:64],
            detail={"safe": result["safe"], "method": result.get("method")},
            trace_id=trace_id,
        )
        # PLAT-3: 审计须提交持久化，否则随会话关闭被回滚（合规审计静默丢失）
        await db.commit()
        return ok(data=result, trace_id=trace_id)
    finally:
        await svc.close()


@router.get("/config", dependencies=_READ_DEPS)
async def get_llm_config(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """读取全部 LLM 实例（脱敏）+ 路由策略 + 生效配置。

    任意登录用户可读（用于展示"是否已配置 LLM"），can_edit 标识当前用户
    是否可写（platform_admin / domain_admin）。
    """
    svc = LlmConfigService(db)
    rows = await svc.list_configs()
    can_edit = user.role in ("platform_admin", "domain_admin")
    items = [
        LlmConfigResponse.build(
            id=row.id,
            name=row.name,
            provider=row.provider or "custom",
            # 展示统一归一化：兼容存量完整 URL（如 .../v1/chat/completions）与基础 URL，
            # 列表/编辑回显均为干净 base_url；对裸 URL 幂等。
            base_url=normalize_base_url(row.base_url),
            model=row.model,
            has_api_key=bool(row.api_key_enc),
            timeout=row.timeout or 30,
            enabled=row.enabled,
            priority=row.priority or 0,
            source="db",
            can_edit=can_edit,
            updated_by=row.updated_by,
            updated_at=str(row.updated_at) if row.updated_at else None,
        )
        for row in rows
    ]
    effective = await svc.get_effective()
    # 生效配置脱敏（不回传明文密钥）；db 来源的 base_url 同样归一化展示
    effective_masked = {**effective, "api_key": ""}
    if effective_masked.get("source") == "db" and effective_masked.get("base_url"):
        effective_masked["base_url"] = normalize_base_url(effective_masked["base_url"])
    resp = LlmConfigListResponse(
        items=items,
        strategy="round_robin",
        effective=effective_masked,
        can_edit=can_edit,
    )
    return ok(data=resp.model_dump(), trace_id=trace_id)


@router.get("/config/{instance_id}/secret", dependencies=_CONFIG_ADMIN_DEPS)
async def get_llm_config_secret(
    instance_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """按需解密返回实例明文 API Key（编辑回显用）。

    列表与常规 GET 一律脱敏；仅管理员在此端点按需取明文，且每次查看都写审计
    日志（密钥属敏感信息）。实例不存在 / 未配置密钥均返回 404。
    """
    svc = LlmConfigService(db)
    row = await svc.get_row(instance_id)
    if row is None:
        raise HTTPException(status_code=404, detail="LLM 实例不存在")
    secret = await svc.get_secret(instance_id)
    if not secret:
        raise HTTPException(status_code=404, detail="该实例未配置 API Key")
    await write_audit(
        db,
        actor_id=user.id,
        action="ai.config.secret.reveal",
        entity_type="llm_config",
        entity_id=str(instance_id),
        detail={"name": row.name},
        trace_id=trace_id,
    )
    await db.commit()
    data = LlmConfigSecretResponse(id=instance_id, api_key=secret).model_dump()
    return ok(data=data, trace_id=trace_id)


@router.post("/config", dependencies=_CONFIG_ADMIN_DEPS, status_code=201)
async def create_llm_config(
    payload: LlmConfigPayload,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """新增一个 LLM 实例（api_key 必填，加密落库）。"""
    if not payload.api_key.strip():
        raise HTTPException(status_code=422, detail="新增实例必须填写 api_key")
    svc = LlmConfigService(db)
    row = await svc.create(payload, updated_by=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="ai.config.create",
        entity_type="llm_config",
        entity_id=str(row.id),
        detail={"name": payload.name, "provider": payload.provider, "enabled": payload.enabled},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"id": row.id}, trace_id=trace_id)


@router.put("/config/{instance_id}", dependencies=_CONFIG_ADMIN_DEPS)
async def update_llm_config(
    instance_id: int,
    payload: LlmConfigPayload,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """更新 LLM 实例；api_key 留空保持原密钥不变。"""
    svc = LlmConfigService(db)
    row = await svc.update(instance_id, payload, updated_by=user.id)
    if row is None:
        raise HTTPException(status_code=404, detail="LLM 实例不存在")
    await write_audit(
        db,
        actor_id=user.id,
        action="ai.config.update",
        entity_type="llm_config",
        entity_id=str(row.id),
        detail={"name": payload.name, "provider": payload.provider, "enabled": payload.enabled},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"id": row.id}, trace_id=trace_id)


@router.delete("/config/{instance_id}", dependencies=_CONFIG_ADMIN_DEPS)
async def delete_llm_config(
    instance_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """删除 LLM 实例（软删除，保留审计痕迹）。"""
    svc = LlmConfigService(db)
    deleted = await svc.delete(instance_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="LLM 实例不存在")
    await write_audit(
        db,
        actor_id=user.id,
        action="ai.config.delete",
        entity_type="llm_config",
        entity_id=str(instance_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"id": instance_id, "deleted": True}, trace_id=trace_id)


@router.post("/config/models", dependencies=_CONFIG_ADMIN_DEPS)
async def fetch_llm_models(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    trace_id: Annotated[str, Depends(get_trace_id)],
    payload: LlmModelsRequest | None = None,
) -> Any:
    """一键获取提供商可用模型列表（配置表单「获取模型」按钮用）。

    支持两种入参（与 /config/test 一致）：
    - instance_id：获取已保存实例（用其落库密钥）；
    - base_url/model/api_key/timeout：临时获取（不落库；api_key 留空回落已保存/环境密钥）。

    网关不支持 ``GET /models`` 端点时返回 ``supported=False`` + error，
    前端提示用户手动输入模型名。
    """
    svc = LlmConfigService(db)
    if payload is not None and payload.instance_id is not None:
        result: LlmModelsResult = await svc.fetch_models_for_instance(payload.instance_id)
    elif payload is not None:
        result = await svc.fetch_models(
            base_url=payload.base_url,
            api_key=payload.api_key,
            timeout=float(payload.timeout or 30),
        )
    else:
        effective = await svc.get_effective()
        result = await svc.fetch_models(
            base_url=effective["base_url"],
            api_key=effective["api_key"],
            timeout=float(effective["timeout"] or 30),
        )
    return ok(data=result.model_dump(), trace_id=trace_id)


@router.post("/config/test", dependencies=_CONFIG_ADMIN_DEPS)
async def test_llm_config(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    trace_id: Annotated[str, Depends(get_trace_id)],
    payload: LlmConfigTestRequest | None = None,
) -> Any:
    """测试 LLM 连通性。

    支持两种入参：
    - instance_id：测试已保存实例（用其落库密钥）；
    - base_url/model/api_key/timeout：临时测试（不落库；api_key 留空回落已保存/环境密钥）。
    """
    svc = LlmConfigService(db)
    if payload is not None and payload.instance_id is not None:
        result: LlmConfigTestResult = await svc.test_instance(payload.instance_id)
    elif payload is not None:
        result = await svc.test_connection(
            LlmConfigPayload(
                name="probe",
                provider="custom",
                base_url=payload.base_url,
                model=payload.model,
                api_key=payload.api_key,
                timeout=payload.timeout,
                enabled=False,
            )
        )
    else:
        result = await svc.test_connection(None)
    return ok(data=result.model_dump(), trace_id=trace_id)
