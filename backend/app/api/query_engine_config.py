"""查询引擎（OLAP/MySQL 降级）DB 配置 API（方案 A：前端可配置化）。

提供「系统配置」页查询引擎卡片的读写与连通性测试端点（仿 LLM 配置范式）：
- GET  /query-engine/config            读取 DB 配置行（脱敏）+ 当前生效状态（任意登录可读）
- PUT  /query-engine/config            保存 DB 配置（仅 platform_admin，密码留空保持原值）
- POST /query-engine/config/test       连通性测试（已保存生效配置 或 临时载荷，仅 platform_admin）

查询引擎连接是全平台 consume 查询共享的平台级凭据（Doris 账号/降级库），
写与测试仅限 platform_admin（对齐 admin_key_rotation 敏感度）；读视图任意登录
可查看配置状态（展示「是否已配置/降级原因」）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.audit import write_audit
from app.core.exceptions import ValidationError
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.query_engine.config_service import QueryEngineConfigService
from app.services.query_engine.schemas import (
    QueryEngineConfigPayload,
    QueryEngineConfigResponse,
    QueryEngineEffectiveResponse,
    QueryEngineTestRequest,
    QueryEngineViewResponse,
)

router = APIRouter(prefix="/query-engine", tags=["查询引擎配置"])

_READ_DEPS = [
    Depends(require_roles(*(
        "platform_admin", "domain_admin", "metric_owner", "reviewer",
        "compliance_officer", "analyst", "viewer",
    ))),
    Depends(guard_against_injection),
]
#: 查询引擎连接为平台级共享凭据，写/测试仅 platform_admin（对齐 admin_key_rotation）
_ADMIN_DEPS = [
    Depends(require_roles("platform_admin")),
    Depends(guard_against_injection),
]


def _effective_note(eff: dict[str, Any]) -> str:
    """生成面向用户的配置状态说明（依据生效来源与各段配置状态）。"""
    if eff["source"] == "db":
        return "数据库配置生效中（保存后无需重启，最长 30s 全量生效）"
    if eff["source"] == "env":
        return "环境变量配置生效中（DB 未启用或未配置对应段）"
    if not eff["olap_configured"] and not eff["mysql_fallback_configured"]:
        return (
            "查询引擎未配置：OLAP 与 MySQL 降级均未启用，指标查询将不可用，"
            "请配置 OLAP（或 MySQL 降级）引擎"
        )
    if not eff["olap_configured"]:
        return "OLAP 引擎未配置：指标查询将走 MySQL 降级引擎"
    return "OLAP 引擎已配置（MySQL 降级未配置）"


@router.get("/config", dependencies=_READ_DEPS)
async def get_query_engine_config(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """读取查询引擎配置（DB 行脱敏 + 生效状态）+ 可编辑标记。任意登录可读。"""
    svc = QueryEngineConfigService(db)
    row = await svc.get_row()
    row_resp = None
    if row is not None:
        row_resp = QueryEngineConfigResponse(
            id=row.id,
            olap_url=row.olap_url,
            doris_host=row.doris_host,
            doris_port=row.doris_port,
            doris_database=row.doris_database,
            doris_user=row.doris_user,
            has_doris_password=bool(row.doris_password_enc),
            has_mysql_fallback=bool(row.mysql_fallback_url_enc),
            enabled=row.enabled,
            updated_by=row.updated_by,
            updated_at=str(row.updated_at) if row.updated_at else None,
        )
    eff = await svc.get_effective()
    eff_resp = QueryEngineEffectiveResponse(
        source=eff["source"],
        olap_url=eff["olap_url"],
        doris_host=eff["doris_host"],
        doris_port=eff["doris_port"],
        doris_database=eff["doris_database"],
        doris_user=eff["doris_user"],
        has_doris_password=bool(eff["doris_password"]),
        has_mysql_fallback=bool(eff["mysql_fallback_url"]),
        olap_configured=eff["olap_configured"],
        mysql_fallback_configured=eff["mysql_fallback_configured"],
        updated_by=eff["updated_by"],
        updated_at=eff["updated_at"],
        note=_effective_note(eff),
    )
    view = QueryEngineViewResponse(
        row=row_resp,
        effective=eff_resp,
        can_edit=user.has_role("platform_admin"),
    )
    return ok(data=view.model_dump(), trace_id=trace_id)


@router.put("/config", dependencies=_ADMIN_DEPS)
async def update_query_engine_config(
    payload: QueryEngineConfigPayload,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """保存查询引擎配置（整行 upsert）；密码/URL 留空保持原值。仅 platform_admin。"""
    svc = QueryEngineConfigService(db)
    existing = await svc.get_row()
    # 首次新建且未提供任何连接段 → 拒绝（避免产生「全空」的无效 DB 行）
    if existing is None and not (
        payload.olap_url.strip()
        or payload.doris_host.strip()
        or payload.mysql_fallback_url.strip()
    ):
        raise ValidationError("至少配置一项查询引擎：OLAP（olap_url/doris_host）或 MySQL 降级 URL")
    row = await svc.save(payload, updated_by=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="query_engine_config.update",
        entity_type="query_engine_config",
        entity_id=str(row.id or 1),
        detail={
            "olap_url": bool(payload.olap_url.strip()),
            "doris_host": payload.doris_host.strip(),
            "doris_user": payload.doris_user.strip(),
            "has_doris_password": bool(payload.doris_password.strip()),
            "has_mysql_fallback": bool(payload.mysql_fallback_url.strip()),
            "enabled": payload.enabled,
        },
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"id": row.id}, trace_id=trace_id)


@router.post("/config/test", dependencies=_ADMIN_DEPS)
async def test_query_engine_config(
    req: QueryEngineTestRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """测试查询引擎连通性（engine=olap/mysql；payload 空用生效配置）。仅 platform_admin。"""
    if req.engine not in ("olap", "mysql"):
        raise ValidationError("engine 仅支持 olap / mysql")
    svc = QueryEngineConfigService(db)
    result = await svc.test_connection(req.engine, req.payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="query_engine_config.test",
        entity_type="query_engine_config",
        entity_id=req.engine,
        detail={"ok": result.ok, "engine": req.engine},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result.model_dump(), trace_id=trace_id)
