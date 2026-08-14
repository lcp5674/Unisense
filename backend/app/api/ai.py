"""AI 问数 API（TD §12.7 / FR-14）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
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
from app.services.llm.config_service import LlmConfigService
from app.services.llm.schemas import LlmConfigPayload, LlmConfigResponse, LlmConfigTestResult

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
    # 此前开关已在 main.py 注册但端点未接线，关闭配置形同虚设。
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
    """读取 LLM 生效配置（脱敏：不含明文 API Key）。

    任意登录用户可读（用于展示"是否已配置 LLM"），can_edit 标识当前用户
    是否可写（platform_admin / domain_admin）。
    """
    svc = LlmConfigService(db)
    effective = await svc.get_effective()
    row = await svc.get_config()
    resp = LlmConfigResponse.build(
        provider=effective["provider"],
        base_url=effective["base_url"],
        model=effective["model"],
        has_api_key=bool(effective["api_key"]),
        timeout=effective["timeout"],
        enabled=bool(row is not None and row.enabled) if row is not None else False,
        source=effective["source"],
        can_edit=user.role in ("platform_admin", "domain_admin"),
        updated_by=effective["updated_by"],
        updated_at=str(effective["updated_at"]) if effective["updated_at"] else None,
    )
    return ok(data=resp.model_dump(), trace_id=trace_id)


@router.put("/config", dependencies=_CONFIG_ADMIN_DEPS)
async def save_llm_config(
    payload: LlmConfigPayload,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """保存 LLM 配置（单例行 upsert）。api_key 留空表示保持原密钥不变。"""
    svc = LlmConfigService(db)
    row = await svc.save(payload, updated_by=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="ai.config.update",
        entity_type="llm_config",
        entity_id=str(row.id),
        detail={"provider": payload.provider, "enabled": payload.enabled},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"id": row.id}, trace_id=trace_id)


@router.post("/config/test", dependencies=_CONFIG_ADMIN_DEPS)
async def test_llm_config(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    trace_id: Annotated[str, Depends(get_trace_id)],
    payload: LlmConfigPayload | None = None,
) -> Any:
    """测试 LLM 连通性（payload 为空时用已保存配置）。

    兼容 OpenAI 协议：POST {base_url}/v1/chat/completions 探针。
    """
    svc = LlmConfigService(db)
    result: LlmConfigTestResult = await svc.test_connection(payload)
    return ok(data=result.model_dump(), trace_id=trace_id)
