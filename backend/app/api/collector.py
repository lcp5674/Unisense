"""采集领域 API（对齐 TD §12.1 / DEV_GUIDE §8b.1）。

路由：
- 数据源：POST /api/v1/data-sources（写闸门）、GET /api/v1/data-sources（注入守卫）、
  GET/DELETE /api/v1/data-sources/{source_id}
- 采集触发：POST /api/v1/data-sources/{source_id}/collect（写闸门）
- 元数据：POST /api/v1/data-sources/{source_id}/catalogs（写闸门）、
  GET /api/v1/catalogs（注入守卫）、POST /api/v1/catalogs/bulk-deprecate（写闸门，207）

凭据脱敏：响应一律不含 connection_config 明文（见 DataSourceResponse）。

增强（工业级修复）：
- FR-017: 采集 API 添加 asyncio.timeout(300) 保护
- FR-018: 分布式锁 CollectionLock.acquire/release 采集并发保护
- US3: 定时调度 cron+mode 参数
- US5: 健康探活端点
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.exceptions import AuthError, BusinessError, ConflictError, NotFoundError
from app.core.guard import guard_against_injection, guard_against_injection_exempt
from app.core.logging import get_logger
from app.core.probe_throttle import check_collect_rate, check_probe_rate
from app.db.mysql import get_db_session
from app.db.redis import get_redis
from app.models.collector_models import BatchInferHistory, BatchLlmInferTask
from app.models.data_source import DBCatalog
from app.services.collector.distributed_lock import CollectionLock
from app.services.collector.infer_guard import InferInflightGuard
from app.services.collector.placeholders import is_effective_comment
from app.services.collector.schemas import (
    BatchDeleteRequest,
    BatchInferHistoryCreate,
    BatchInferHistoryEntry,
    BatchInferHistoryTable,
    BatchLlmInferTaskCreate,
    BatchScheduleRequest,
    BatchSourceResult,
    BatchTestConnectionRequest,
    BatchToggleRequest,
    BulkDeprecateRequest,
    BulkDeprecateResult,
    CollectionRunListResponse,
    CollectionRunResponse,
    CollectRequest,
    DataSourceCreateRequest,
    DataSourceListResponse,
    DataSourceResponse,
    DataSourceTypeInfo,
    DataSourceUpdateRequest,
    DBCatalogCreateRequest,
    DBCatalogListParams,
    DBCatalogListResponse,
    DBCatalogResponse,
    DescriptionCoverageResponse,
    DriftLogListResponse,
    InferBatchResponse,
    InferDescriptionRequest,
    InferDescriptionResponse,
    InferTableDescriptionRequest,
    InferTableDescriptionResponse,
    ListTablesRequest,
    ScheduleRequest,
    SqlQueryRequest,
    SqlQueryResponse,
    TableDescriptionRequest,
    TableDescriptionResponse,
    TestConnectionRequest,
    TestConnectionResult,
    UpdateDescriptionRequest,
    UpdateDescriptionResponse,
)
from app.services.collector.service import CollectorService
from app.services.collector.spi import build_collector
from app.services.collector.tasks import _flush_run_logs, _make_run_log_cb

logger = get_logger("unisense.collector.api")

source_router = APIRouter(prefix="/data-sources", tags=["collector-source"])
catalog_router = APIRouter(prefix="/catalogs", tags=["collector-catalog"])
collection_run_router = APIRouter(prefix="/collection-runs", tags=["collector-run"])

#: 数据源/采集写权限：平台级运维仅管理/域管理员（metric_owner 单域指标负责人不操作平台级数据源）。
_WRITE_ROLES = ("platform_admin", "domain_admin")
#: 读权限：采集运维数据（源概览/采集运行/水位/漂移/任务）为平台级治理数据，仅
#: 管理/域管理员/指标负责人可读——对齐前端 data-sources:view、collection-tasks:view、
#: collection-history:view、catalogs:view 基线；viewer/reviewer/analyst/compliance 无
#: 采集页面入口，不应经 API 直读资产规模/PII 分布/采集运行状态。
_READ_ROLES = ("platform_admin", "domain_admin", "metric_owner")
#: 源浏览（仅列表/类型）：查询工作台/维度映射需「选数据源」，对任意登录用户开放
#: （list 已按 org 收敛 + 凭据脱敏，不泄露连接敏感字段）。
_BROWSE_READ_DEPS = [Depends(require_roles(*ALL_ROLES)), Depends(guard_against_injection)]


def _resolve_org_scope(user) -> int | None:
    """多租户组织作用域：平台管理员跨组织（None=不按组织过滤），其余按用户所属组织过滤。

    S1 多租户隔离下，platform_admin 是平台级管理员，应能跨组织查看/管理全部数据源；
    普通用户/域管理员仅能访问其所属组织（org_id）的资源。
    """
    if "platform_admin" in user.roles_all():
        return None
    # 修复：此前误写成 return _resolve_org_scope(user) 无限递归（非管理员 500）
    return getattr(user, "org_id", None)
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]
# 写端点统一挂注入守卫（纵深防御：ORM 参数化兜底之外拦截注入 payload）
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]
# P2-11: 连接配置豁免注入扫描——connection_config 仅用于构建连接（不进 SQL），
# 密码含 -- / /* 等特殊字符是合法凭据，不应 422（注入守卫纵深防御不削弱）。
_PROBE_DEPS = [
    Depends(require_roles(*_WRITE_ROLES)),
    Depends(guard_against_injection_exempt("connection_config")),
]
# 只读 SQL 查询：任意登录用户可访问，但 handler 内校验「平台管理员/域管理员 或 数据源 Owner」。
# sql 字段整体豁免注入扫描（SQL 本身即合法载荷，含 -- 等是正常语法），其余字段仍全量扫描。
_SQL_QUERY_DEPS = [
    Depends(require_roles(*ALL_ROLES)),
    Depends(guard_against_injection_exempt("sql")),
]


def _svc(db: AsyncSession) -> CollectorService:
    return CollectorService(db)


@contextlib.asynccontextmanager
async def _infer_inflight(
    kind: str, catalog_id: int, column: str | None = None
) -> AsyncIterator[None]:
    """LLM 推断 in-flight 去重：获得推断权才进入，退出释放（异常也释放，TTL 兜底）。

    Redis 可用时 SET NX EX 跨进程去重；Redis 不可用降级为进程内去重。
    已有推断进行中时抛 409（LLM_INFER_IN_PROGRESS），前端据此提示「正在进行中」。
    """
    owner_id = f"infer-{uuid.uuid4().hex[:8]}"
    redis = None
    with contextlib.suppress(RuntimeError):
        redis = get_redis()  # Redis 不可用时降级为进程内去重
    guard = InferInflightGuard(redis)
    acquired = await guard.acquire(kind, catalog_id, column, owner=owner_id)
    if not acquired:
        raise ConflictError(
            "该实体的 LLM 推断正在进行中，请稍后重试",
            error_code="LLM_INFER_IN_PROGRESS",
        )
    try:
        yield
    finally:
        await guard.release(kind, catalog_id, column, owner=owner_id)


@source_router.post("", dependencies=_PROBE_DEPS)
async def create_data_source(
    body: DataSourceCreateRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[DataSourceResponse]:
    svc = _svc(db)
    # 创建归属：仍按用户所属组织（platform_admin 也归属其 org，保持既有可见性语义）
    resp = await svc.create_source(body, user.id, org_id=getattr(user, "org_id", None))
    await write_audit(
        db,
        actor_id=user.id,
        action="data_source.create",
        entity_type="data_source",
        entity_id=resp.source_id,
        detail={"name": resp.name, "source_type": resp.source_type},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@source_router.get("", dependencies=_BROWSE_READ_DEPS)
async def list_data_sources(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    domain: str | None = None,
    source_type: str | None = None,
    keyword: str | None = None,
    health_status: str | None = None,
    owner_id: int | None = Query(None, description="责任人（Owner）ID 过滤"),
    source_status: str | None = Query(
        None, description="源状态过滤：deleted=已软删源，其余默认活跃源"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> ApiResponse[DataSourceListResponse]:
    svc = _svc(db)
    items, total = await svc.list_sources(
        domain=domain,
        source_type=source_type,
        keyword=keyword,
        health_status=health_status,
        owner_id=owner_id,
        source_status=source_status,
        org_id=_resolve_org_scope(user),
        page=page,
        page_size=page_size,
    )
    # P1-1: 返回分页结构（含 total），此前 total 被丢弃导致 >20 个源时静默截断
    return ok(
        data=DataSourceListResponse(items=items, total=total, page=page, page_size=page_size),
        trace_id=trace_id,
    )


@source_router.get("/types", dependencies=_BROWSE_READ_DEPS)
async def list_source_types(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[list[DataSourceTypeInfo]]:
    """FR-030: 返回全部已注册采集器类型元信息（前端动态渲染类型选择器）。"""
    svc = _svc(db)
    return ok(data=await svc.list_source_types(), trace_id=trace_id)


@source_router.post("/test-connection", dependencies=_PROBE_DEPS)
async def test_connection(
    body: TestConnectionRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[TestConnectionResult]:
    """FR-030: 创建前连接预检（明文配置轻量探活，不落库、不写审计）。"""
    # P0-2: 探活限流（防 SSRF 端口扫描滥用）
    await check_probe_rate(f"user:{user.id}")
    svc = _svc(db)
    source_type_value = (
        body.source_type.value if hasattr(body.source_type, "value") else str(body.source_type)
    )
    result = await svc.test_connection(source_type_value, body.connection_config)
    await write_audit(
        db,
        actor_id=user.id,
        action="data_source.test_connection",
        entity_type="data_source",
        entity_id=f"{source_type_value}:{body.connection_config.get('host', '')}",
        detail={"ok": result.ok, "latency_ms": result.latency_ms},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@source_router.post("/databases", dependencies=_PROBE_DEPS)
async def list_databases(
    body: TestConnectionRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """FR-030: 枚举实例下可采集的非系统数据库（创建数据源时选择目标库）。

    与 test-connection 同构（明文配置，不落库）；连接器不支持枚举时返回空列表。
    """
    # P0-2: 探活限流（防 SSRF 端口扫描滥用）
    await check_probe_rate(f"user:{user.id}")
    svc = _svc(db)
    source_type_value = (
        body.source_type.value if hasattr(body.source_type, "value") else str(body.source_type)
    )
    databases = await svc.list_databases(source_type_value, body.connection_config)
    # P2-9: 枚举探活（携带明文密码探测主机）补审计——SSRF 探测路径留痕
    await write_audit(
        db,
        actor_id=user.id,
        action="data_source.list_databases",
        entity_type="data_source",
        entity_id=f"{source_type_value}:{body.connection_config.get('host', '')}",
        detail={"count": len(databases)},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"databases": databases, "source_type": source_type_value}, trace_id=trace_id)


@source_router.post("/tables", dependencies=_PROBE_DEPS)
async def list_tables(
    body: ListTablesRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """FR-030: 枚举指定库下的表（按库分组，创建数据源时级联选表）。

    与 list_databases 同构（明文配置，不落库）；连接器不支持枚举表时
    返回空字典，前端隐藏表级选择区。
    """
    # P0-2: 探活限流（防 SSRF 端口扫描滥用）
    await check_probe_rate(f"user:{user.id}")
    svc = _svc(db)
    source_type_value = (
        body.source_type.value if hasattr(body.source_type, "value") else str(body.source_type)
    )
    tables = await svc.list_tables(
        source_type_value, body.connection_config, body.databases or None
    )
    # P2-9: 表枚举探活补审计——与 list_databases 同构留痕
    await write_audit(
        db,
        actor_id=user.id,
        action="data_source.list_tables",
        entity_type="data_source",
        entity_id=f"{source_type_value}:{body.connection_config.get('host', '')}",
        detail={"table_count": sum(len(v) for v in tables.values())},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"tables": tables, "source_type": source_type_value}, trace_id=trace_id)


@source_router.post("/batch-toggle", dependencies=_WRITE_DEPS)
async def batch_toggle_data_sources(
    body: BatchToggleRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[BatchSourceResult]:
    """批量启用/停用数据源（207 语义：单条失败逐项标注，不影响其余）。

    注意：本端点须注册在 ``GET/PUT/DELETE /{source_id}`` 之前——FastAPI 按注册
    顺序匹配，静态路径 ``/batch-toggle`` 若在 ``/{source_id}`` 之后会被当作
    source_id 吞掉（与 ``/jobs`` 同理）。
    """
    svc = _svc(db)
    result = await svc.batch_toggle_sources(
        body.source_ids, body.enabled, user.id, org_id=_resolve_org_scope(user)
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="data_source.batch_enable" if body.enabled else "data_source.batch_disable",
        entity_type="data_source",
        entity_id=f"items:{len(body.source_ids)}",
        detail={
            "succeeded": len(result.succeeded),
            "failed": len(result.failed),
            "enabled": body.enabled,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@source_router.post("/batch-delete", dependencies=_WRITE_DEPS)
async def batch_delete_data_sources(
    body: BatchDeleteRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[BatchSourceResult]:
    """批量删除数据源（软删，207 语义：单条失败逐项标注，不影响其余）。"""
    svc = _svc(db)
    result = await svc.batch_delete_sources(
        body.source_ids, user.id, org_id=_resolve_org_scope(user)
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="data_source.batch_delete",
        entity_type="data_source",
        entity_id=f"items:{len(body.source_ids)}",
        detail={"succeeded": len(result.succeeded), "failed": len(result.failed)},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@source_router.post("/batch-test", dependencies=_WRITE_DEPS)
async def batch_test_data_sources(
    body: BatchTestConnectionRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[BatchSourceResult]:
    """批量探活（207 语义）：用已存连接配置逐条 probe，健康状态随之更新。"""
    svc = _svc(db)
    result = await svc.batch_test_sources(body.source_ids, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="data_source.batch_probe",
        entity_type="data_source",
        entity_id=f"items:{len(body.source_ids)}",
        detail={"succeeded": len(result.succeeded), "failed": len(result.failed)},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@source_router.post("/batch-schedule", dependencies=_WRITE_DEPS)
async def batch_schedule_data_sources(
    body: BatchScheduleRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[BatchSourceResult]:
    """批量设置调度 cron（207 语义，统一覆盖 schedule_cron）。"""
    svc = _svc(db)
    # 越权审查修复：批量写强制 org 隔离（对齐 batch-toggle/batch-delete）。
    result = await svc.batch_schedule_sources(
        body.source_ids, body.schedule_cron, user.id, org_id=_resolve_org_scope(user)
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="data_source.batch_schedule",
        entity_type="data_source",
        entity_id=f"items:{len(body.source_ids)}",
        detail={
            "succeeded": len(result.succeeded),
            "failed": len(result.failed),
            "schedule_cron": body.schedule_cron,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@source_router.get("/jobs", dependencies=_READ_DEPS)
async def list_collection_jobs(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    limit: int = Query(50, ge=1, le=200),
    offset: int = 0,
    source_id: str | None = None,
    status: str | None = None,
) -> ApiResponse[dict[str, Any]]:
    """列出采集任务（按入队逆序服务端分页，采集任务中心入口）。

    可按 ``source_id`` 过滤（任务中心按数据源筛选）；``status`` 供总览仪表
    「采集任务」资产卡片下钻；job 含 ``created_at``（创建时间）与 ``kind``
    （manual 手动 / scheduled 定时）供前端展示。返回 ``{items, total, page, page_size}``
    分页结构（total 修复前端本地切片导致的 50 条上限问题）。

    注意：本端点须注册在 ``GET /{source_id}`` 之前——FastAPI 按注册顺序匹配，
    单段静态路径 ``/jobs`` 若在 ``/{source_id}`` 之后会被当作 source_id 吞掉。
    """
    svc = _svc(db)
    items, total = await svc.list_jobs_paged(
        limit=limit,
        offset=offset,
        source_id=source_id,
        status=status,
        org_id=_resolve_org_scope(user),
    )
    page = offset // limit + 1 if limit else 1
    return ok(
        data={"items": items, "total": total, "page": page, "page_size": limit},
        trace_id=trace_id,
    )


@source_router.get("/sampling-coverage", dependencies=_READ_DEPS)
async def get_sampling_coverage_all(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """全库采样覆盖率（不按数据源过滤）：采集目录总览的采样健康度。

    注意：本端点须注册在 ``GET /{source_id}`` 之前——FastAPI 按注册顺序匹配，
    两段静态路径 ``/sampling-coverage`` 若在 ``/{source_id}`` 之后会被当作
    source_id 吞掉（404）。与 ``/jobs`` 同一条布局约束。
    """
    svc = _svc(db)
    return ok(
        data=await svc.get_sampling_coverage(org_id=_resolve_org_scope(user)),
        trace_id=trace_id,
    )


@source_router.get("/{source_id}", dependencies=_READ_DEPS)
async def get_data_source(
    source_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[DataSourceResponse]:
    svc = _svc(db)
    # 明文连接配置（含密码）仅平台管理员可读；其余角色脱敏
    # （connection_config=None，仅 connection_config_present 标记，
    # 前端掩码回显）。
    role = user.role.value if hasattr(user.role, "value") else user.role
    include_config = str(role) == "platform_admin"
    resp = await svc.get_source(
        source_id, include_config=include_config, org_id=_resolve_org_scope(user)
    )
    # S13（审查修复）：平台管理员查看明文凭据属敏感操作，须落审计
    # （对照 LLM key 回读已有审计；此前凭据披露零审计）
    if include_config:
        await write_audit(
            db,
            actor_id=user.id,
            action="data_source.secret_viewed",
            entity_type="data_source",
            entity_id=source_id,
            detail={"include_config": True},
            ip=client_ip(request),
            trace_id=trace_id,
        )
        await db.commit()
    return ok(data=resp, trace_id=trace_id)


@source_router.put("/{source_id}", dependencies=_PROBE_DEPS)
async def update_data_source(
    source_id: str,
    body: DataSourceUpdateRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[DataSourceResponse]:
    """更新数据源（PATCH 语义：仅更新传入字段；source_id 不可变更）。"""
    svc = _svc(db)
    resp = await svc.update_source(source_id, body, user.id, org_id=_resolve_org_scope(user))
    await write_audit(
        db,
        actor_id=user.id,
        action="data_source.update",
        entity_type="data_source",
        entity_id=source_id,
        detail={
            "name": resp.name,
            "source_type": resp.source_type,
            "config_changed": body.connection_config is not None,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@source_router.post("/{source_id}/check", dependencies=_WRITE_DEPS)
async def check_source_connection(
    source_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[TestConnectionResult]:
    """FR-030: 存量数据源实时探活（解密配置 → 轻量连接 → 更新健康状态）。"""
    svc = _svc(db)
    result = await svc.check_connection(source_id, org_id=_resolve_org_scope(user))
    await write_audit(
        db,
        actor_id=user.id,
        action="data_source.check_connection",
        entity_type="data_source",
        entity_id=source_id,
        detail={"ok": result.ok, "latency_ms": result.latency_ms},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@source_router.delete("/{source_id}", dependencies=_WRITE_DEPS)
async def delete_data_source(
    source_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[None]:
    svc = _svc(db)
    await svc.delete_source(source_id, org_id=_resolve_org_scope(user))
    await write_audit(
        db,
        actor_id=user.id,
        action="data_source.delete",
        entity_type="data_source",
        entity_id=source_id,
        detail={},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=None, trace_id=trace_id)


@source_router.post("/{source_id}/catalogs", dependencies=_WRITE_DEPS)
async def register_catalog(
    source_id: str,
    body: DBCatalogCreateRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[DBCatalogResponse]:
    # source_id 自动生成：请求体可选——未传时以 URL 路径为准并回填，传了则校验一致
    if body.source_id is not None and body.source_id != source_id:
        raise BusinessError("body.source_id 与路径不一致", error_code="BAD_REQUEST")
    body.source_id = source_id
    svc = _svc(db)
    resp = await svc.register_catalog(body, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="db_catalog.register",
        entity_type="db_catalog",
        entity_id=f"{source_id}/{body.entity_name}",
        detail={
            "data_classification": resp.sensitivity_level,
            "sensitivity": resp.sensitivity_level,
        },
        ip=client_ip(request),
        trace_id=trace_id,
        pii_access=(resp.sensitivity_level == "PII"),
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@source_router.get("/{source_id}/catalogs", dependencies=_READ_DEPS)
async def list_source_catalogs(
    source_id: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    entity_type: str | None = None,
    sensitivity_level: str | None = None,
    keyword: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> ApiResponse[DBCatalogListResponse]:
    """按数据源查询采集目录（采集目录页按源查看表/字段的入口）。"""
    svc = _svc(db)
    from pydantic import ValidationError as _PydanticValidationError

    try:
        params = DBCatalogListParams(
            source_id=source_id,
            entity_type=entity_type,
            sensitivity_level=sensitivity_level,
            keyword=keyword,
            page=page,
            page_size=page_size,
        )
    except _PydanticValidationError as exc:
        # P1-1（第八轮）：手工构造与 Depends 注入版行为对齐——入参非法返回 422
        # 而非 pydantic ValidationError 冒泡成 500。
        from app.core.exceptions import ValidationError as AppValidationError

        first_msg = exc.errors()[0]["msg"] if exc.errors() else "参数校验失败"
        raise AppValidationError(str(first_msg)) from exc
    return ok(
        data=await svc.list_catalogs(params, org_id=_resolve_org_scope(user)),
        trace_id=trace_id,
    )


@source_router.post("/{source_id}/entities/{entity_name}/refresh", dependencies=_WRITE_DEPS)
async def refresh_entity(
    source_id: str,
    entity_name: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """单实体元数据刷新（生产运维：只刷新一张表，不触发全源扫描）。

    连接器支持单实体采集（MySQL/Postgres/ClickHouse）时精确刷新目标表；
    不支持（Hive 等）时回退为全量采集后仅取目标实体。
    """
    svc = _svc(db)
    src = await svc.get_source_orm(source_id, org_id=_resolve_org_scope(user))
    collector = build_collector(src.source_type, src.connection_config)
    try:
        result = await svc.refresh_entity(source_id, entity_name, user.id, collector)
    finally:
        await collector.dispose()
    await write_audit(
        db,
        actor_id=user.id,
        action="db_catalog.refresh",
        entity_type="db_catalog",
        entity_id=f"{source_id}/{entity_name}",
        detail={
            "sensitivity": result["sensitivity_level"],
            "drifted": result["drifted"],
            "columns": result["columns"],
        },
        ip=client_ip(request),
        trace_id=trace_id,
        pii_access=(result["sensitivity_level"] == "PII"),
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@source_router.post("/{source_id}/entities/{entity_name}/sample", dependencies=_WRITE_DEPS)
async def sample_entity(
    source_id: str,
    entity_name: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    sample_rows: int | None = Query(None, ge=1, le=200),
) -> ApiResponse[dict[str, Any]]:
    """单表样本采样（不重跑全量采集，只补采样本值）。

    样本经打码后写入 ``schema_json.columns[].sample``，并据 ``name+sample``
    双重验证重算字段级 PII 命中——用于提升 PII 识别精度、纠正仅靠名称的误判。
    需要数据源已开启采样（``quota.sample_rows > 0``）或本次显式指定 ``sample_rows``。
    """
    svc = _svc(db)
    src = await svc.get_source_orm(source_id, org_id=_resolve_org_scope(user))
    collector = build_collector(src.source_type, src.connection_config)
    try:
        result = await svc.sample_entity(
            source_id, entity_name, user.id, collector, sample_rows
        )
    finally:
        await collector.dispose()
    await write_audit(
        db,
        actor_id=user.id,
        action="db_catalog.sample",
        entity_type="db_catalog",
        entity_id=f"{source_id}/{entity_name}",
        detail={
            "sample_rows": result["sample_rows"],
            "sampled": result["sampled"],
            "pii_hits": result["pii_hits"],
        },
        ip=client_ip(request),
        trace_id=trace_id,
        pii_access=(result["sensitivity_level"] == "PII"),
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@source_router.post("/{source_id}/sql-query", dependencies=_SQL_QUERY_DEPS)
async def query_source_sql(
    source_id: str,
    body: SqlQueryRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[SqlQueryResponse]:
    """对已注册数据源执行只读查询（平台内部运维/分析，写审计）。

    黑名单制校验：任何非 DDL/DML 只读语句（SELECT / SHOW / DESC / EXPLAIN / USE /
    HELP / CHECKSUM / CHECK 等）放行，拒绝多语句/DDL/DML/SELECT INTO/行锁/状态变更/
    危险函数（服务层 sqlglot 校验，语法正确性交给执行端，源端错误映射 422）；
    ``USE <db>`` 写入会话级当前库，后续无库前缀的表名自动补当前库前缀；
    LIMIT 兜底防止大结果集；仅平台管理员/域管理员或该数据源 Owner 可执行。
    """
    svc = _svc(db)
    src = await svc.get_source_orm(source_id, org_id=_resolve_org_scope(user))
    is_admin = any(r in _WRITE_ROLES for r in user.roles_all())
    if not is_admin and src.owner_id != user.id:
        raise AuthError("无权查询该数据源（仅平台管理员/域管理员或数据源负责人可执行 SQL）")
    result = await svc.query_sql(source_id, body.sql, body.limit, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="data_source.sql_query",
        entity_type="data_source",
        entity_id=source_id,
        detail={
            "sql": body.sql[:500],
            "limit": body.limit,
            "rows": result["total"],
            "truncated": result["truncated"],
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=SqlQueryResponse(**result), trace_id=trace_id)


@source_router.post("/{source_id}/collect", dependencies=_WRITE_DEPS)
async def collect_source(
    source_id: str,
    body: CollectRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    svc = _svc(db)
    await check_collect_rate(f"user:{user.id}")
    src = await svc.get_source_orm(source_id, org_id=_resolve_org_scope(user))
    # 根据 source_type 从 CollectorRegistry 构建采集器
    collector = build_collector(src.source_type, src.connection_config)

    # FR-018: 分布式锁采集并发保护
    owner_id = f"api-{user.id}-{uuid.uuid4().hex[:8]}"
    redis = None
    with contextlib.suppress(RuntimeError):
        redis = get_redis()  # Redis 不可用时降级为无锁

    lock = CollectionLock(redis)
    acquired = await lock.acquire(source_id, owner_id)
    if not acquired:
        raise ConflictError(
            f"数据源 {source_id} 采集正在进行中，请稍后重试",
            error_code="COLLECTION_IN_PROGRESS",
        )

    try:
        # 采集运行历史：创建 RUNNING 记录（独立提交，进程崩溃不丢）——同步采集归为 manual 触发
        run_id = await svc.start_collection_run(
            source_id=source_id, trigger="manual", mode=body.mode, actor_id=user.id
        )
    except Exception:  # noqa: BLE001 - 运行记录创建失败不应阻断采集主流程
        logger.warning("collection_run_start_failed: source=%s", source_id, exc_info=True)
        run_id = None

    # 采集运行日志实时缓冲回调（同步路径无 JobStore，仅写 Redis；终态回写 DB）。
    # Redis 不可用或 run_id 缺失时返回 None——日志能力降级为 no-op，不影响采集。
    run_log_cb = _make_run_log_cb(redis, run_id)

    try:
        # FR-017: asyncio.timeout(300) 保护
        result = await asyncio.wait_for(
            svc.collect_and_register(
                source_id, collector, user.id, mode=body.mode, progress_cb=run_log_cb
            ),
            timeout=300.0,
        )
    except TimeoutError:
        if run_id is not None:
            await svc.fail_collection_run(run_id, "采集超时（300秒）")
            await _flush_run_logs(redis, svc, run_id, error="采集超时（300秒）")
        raise BusinessError(
            f"数据源 {source_id} 采集超时（300秒），请使用异步调度",
            error_code="COLLECTION_TIMEOUT",
        ) from None
    except Exception as exc:  # noqa: BLE001 - 采集异常需落 FAILED 记录后上抛
        if run_id is not None:
            await svc.fail_collection_run(run_id, str(exc))
            await _flush_run_logs(redis, svc, run_id, error=str(exc))
        raise
    finally:
        await lock.release(source_id, owner_id)
        await collector.dispose()
    if run_id is not None:
        await svc.complete_collection_run(run_id, result)
        await _flush_run_logs(redis, svc, run_id, result=result)
    pii_registered = result.get("pii_registered", 0)
    failed_count = result.get("failed_count", 0)
    await write_audit(
        db,
        actor_id=user.id,
        action="data_source.collect",
        entity_type="data_source",
        entity_id=source_id,
        detail={
            "scanned": result["scanned"],
            "registered": result["registered"],
            "pii_registered": pii_registered,
            "failed_count": failed_count,
        },
        ip=client_ip(request),
        trace_id=trace_id,
        pii_access=pii_registered > 0,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@source_router.post("/{source_id}/schedule", dependencies=_WRITE_DEPS)
async def schedule_collection(
    source_id: str,
    body: ScheduleRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """设置数据源定时调度（P1-7：仅保存 cron+mode，不触发立即采集）。

    设置定时只负责保存调度配置（由 worker 的 collect_scheduler 每分钟扫描触发），
    与「立即采集」语义分离——立即采集请调用 POST /{source_id}/collect 或 /collect-async。
    """
    svc = _svc(db)
    # P1-7: 仅保存 cron+mode 到 DataSource，不投递采集任务
    # 越权审查修复：写路径强制 org 隔离（此前 update_schedule 无 org 过滤，
    # domain_admin 可跨组织改调度——对齐 batch-toggle/batch-delete 的 org 语义）。
    await svc.update_schedule(
        source_id,
        body.cron,
        body.mode,
        schedule_enabled=body.schedule_enabled,
        org_id=_resolve_org_scope(user),
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="data_source.collect_schedule",
        entity_type="data_source",
        entity_id=source_id,
        detail={
            "cron": body.cron,
            "mode": body.mode,
            "schedule_enabled": body.schedule_enabled,
            "scheduled": True,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data={
            "scheduled": True,
            "cron": body.cron,
            "mode": body.mode,
            "schedule_enabled": body.schedule_enabled,
        },
        trace_id=trace_id,
    )


@source_router.post("/{source_id}/collect-async", dependencies=_WRITE_DEPS)
async def collect_source_async(
    source_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """异步立即采集：立即返回 job_id，采集在后台 worker 执行（TD §12.1）。

    与同步 POST /{source_id}/collect 的区别：本端点不阻塞请求，
    适合大库采集（避免 300s 同步超时）。
    """
    svc = _svc(db)
    await check_collect_rate(f"user:{user.id}")
    job_id = await svc.schedule_collection(source_id, user.id, org_id=_resolve_org_scope(user))
    await write_audit(
        db,
        actor_id=user.id,
        action="data_source.collect_async",
        entity_type="data_source",
        entity_id=source_id,
        detail={"job_id": job_id},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data={"job_id": job_id, "status": "QUEUED"},
        trace_id=trace_id,
    )


@source_router.post("/{source_id}/collect-now", dependencies=_WRITE_DEPS)
async def collect_now(
    source_id: str,
    body: CollectRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """P1-7: 立即触发一次采集（与定时调度解耦）。

    将采集任务投递到异步队列，立即返回 job_id；采集在后台 worker 执行，
    不影响已配置的 cron 调度。mode 经由 CollectRequest 指定（默认 FULL）。
    """
    svc = _svc(db)
    await check_collect_rate(f"user:{user.id}")
    job_id = await svc.schedule_collection(
        source_id,
        user.id,
        org_id=_resolve_org_scope(user),
        mode=body.mode,
        include_patterns=body.include_patterns,
        exclude_patterns=body.exclude_patterns,
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="data_source.collect_now",
        entity_type="data_source",
        entity_id=source_id,
        detail={
            "job_id": job_id,
            "mode": body.mode,
            "include_patterns": body.include_patterns,
            "exclude_patterns": body.exclude_patterns,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data={
            "job_id": job_id,
            "status": "QUEUED",
            "mode": body.mode,
            "include_patterns": body.include_patterns,
            "exclude_patterns": body.exclude_patterns,
        },
        trace_id=trace_id,
    )


@source_router.get("/jobs/{job_id}", dependencies=_READ_DEPS)
async def get_collection_job(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """查询异步采集任务状态（进度 / 结果）。"""
    svc = _svc(db)
    status = await svc.get_job_status(job_id, org_id=_resolve_org_scope(user))
    if status is None:
        raise NotFoundError(f"采集任务不存在: {job_id}", ctx={"job_id": job_id})
    return ok(data=status, trace_id=trace_id)


@source_router.post("/jobs/{job_id}/cancel", dependencies=_WRITE_DEPS)
async def cancel_collection_job(
    job_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """P1-7: 取消异步采集任务（任务中心取消能力）。

    - 已入队未运行：取消投递；
    - 运行中：请求取消（worker 收到 CancelledError 补写 FAILED 终态）；
    - 任务不存在或已终态：幂等返回 ``canceled=False``（不报 404）。

    组织级归属校验（用户级越权修复）：先经 ``get_job_status(org_id=...)`` 确认
    任务属于当前组织（跨组织任务不可见）——此前直接 ``q.cancel(job_id)`` 无归属
    校验，任意写角色可凭 job_id 取消其他组织任务。
    """
    from app.core.config import settings as _settings
    from app.services.collector.queue import create_collection_queue

    svc = _svc(db)
    org_id = _resolve_org_scope(user)
    status = await svc.get_job_status(job_id, org_id=org_id)
    if status is None:
        return ok(data={"job_id": job_id, "canceled": False}, trace_id=trace_id)
    q = create_collection_queue(redis_url=_settings.redis_url)
    cancelled = await q.cancel(job_id)
    if cancelled:
        await write_audit(
            db,
            actor_id=user.id,
            action="data_source.cancel_job",
            entity_type="data_source",
            entity_id=job_id,
            detail={"job_id": job_id},
            ip=client_ip(request),
            trace_id=trace_id,
        )
        await db.commit()
    return ok(data={"job_id": job_id, "canceled": cancelled}, trace_id=trace_id)


def _sse_event(event: str, data: dict[str, Any]) -> str:
    """构造一条 SSE 消息（event + JSON data）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@source_router.get("/jobs/{job_id}/stream", dependencies=_READ_DEPS)
async def stream_collection_job(
    job_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> StreamingResponse:
    """SSE 实时推送采集任务进度（轮询 JobStore，1s 粒度）。

    事件流：
    - ``progress``：RUNNING 中的进度快照（phase / index / total / messages）
    - ``done``：终态快照（COMPLETED 含完整结果 / FAILED 含 error）
    - ``error``：任务不存在或推送超时

    客户端断开即停止；单连接最长 1800s（与 worker job_timeout 对齐）。
    """
    from app.core.config import settings as _settings
    from app.services.collector.queue import create_collection_queue

    # 多租户隔离：SSE 流订阅前先校验任务归属（非平台管理员仅可订阅本组织数据源任务）
    svc = _svc(db)
    if await svc.get_job_status(job_id, org_id=_resolve_org_scope(user)) is None:
        return StreamingResponse(
            iter([_sse_event("error", {"message": f"采集任务不存在: {job_id}"})]),
            media_type="text/event-stream",
        )

    async def event_gen() -> Any:
        q = create_collection_queue(redis_url=_settings.redis_url)
        getter = getattr(q, "get", None)
        start = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            status = await getter(job_id) if getter is not None else None
            if status is None:
                yield _sse_event("error", {"message": f"采集任务不存在: {job_id}"})
                break
            if status.get("status") in ("COMPLETED", "FAILED"):
                # 终态先补发一条"进度拉满"的 progress 事件：1s 轮询快照下，
                # 前端收到的最后一帧 RUNNING 进度可能停在中间值（如 25%），
                # 此处以结果中的 scanned 作为 index=total，把进度条推进到 100%。
                _detail = status.get("detail") or {}
                _scanned = int(_detail.get("scanned") or 0)
                _done_progress = {
                    "phase": "done",
                    "index": _scanned,
                    "total": _scanned,
                    "message": "采集完成" if status.get("status") == "COMPLETED" else "采集失败",
                    "messages": [],
                }
                yield _sse_event(
                    "progress",
                    {
                        "job_id": job_id,
                        "status": "RUNNING",
                        "source_id": status.get("source_id"),
                        "detail": {
                            "source_id": status.get("source_id"),
                            "progress": _done_progress,
                        },
                    },
                )
                yield _sse_event("done", status)
                break
            yield _sse_event("progress", status)
            if time.monotonic() - start > 1800:
                yield _sse_event("error", {"message": "进度推送超时（1800s）"})
                break
            await asyncio.sleep(1)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@source_router.get("/{source_id}/watermark", dependencies=_READ_DEPS)
async def get_watermark(
    source_id: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """US3: 获取数据源采集水位（FR-014）。

    数据源不存在时由 service 抛 404；存在但从未采集时返回空水位。
    """
    svc = _svc(db)
    watermark = await svc.get_watermark(source_id, org_id=_resolve_org_scope(user))
    return ok(data=watermark, trace_id=trace_id)


@source_router.get("/{source_id}/health", dependencies=_READ_DEPS)
async def get_health(
    source_id: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """US5: 数据源健康探活端点（FR-016）。"""
    svc = _svc(db)
    health_info = await svc.get_health(source_id, org_id=_resolve_org_scope(user))
    return ok(data=health_info, trace_id=trace_id)


@source_router.get("/{source_id}/overview", dependencies=_READ_DEPS)
async def get_source_overview(
    source_id: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """资产规模概览：实体类型/PII 分布/字段数/漂移/覆盖率/采集水位。"""
    svc = _svc(db)
    overview = await svc.get_source_overview(source_id, org_id=_resolve_org_scope(user))
    return ok(data=overview, trace_id=trace_id)


@source_router.get("/{source_id}/sampling-coverage", dependencies=_READ_DEPS)
async def get_sampling_coverage(
    source_id: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """采样覆盖率：该源已采样表数/列数与占比（PII 识别精度可观测性）。

    采样是 PII 精度增强的前提——未采样的表只能靠字段名/注释推断。本端点让
    治理端看清「哪些范围已具备 name+sample 双重验证能力」。
    """
    svc = _svc(db)
    return ok(
        data=await svc.get_sampling_coverage(
            source_id, org_id=_resolve_org_scope(user)
        ),
        trace_id=trace_id,
    )


@source_router.get("/{source_id}/drift-logs", dependencies=_READ_DEPS)
async def list_drift_logs(
    source_id: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    entity_name: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> ApiResponse[DriftLogListResponse]:
    """P1-4: Schema Drift 变更日志（按检测时间倒序，分页）。

    用于前端展示数据源的 schema 漂移历史。
    """
    svc = _svc(db)
    result = await svc.list_drift_logs(
        source_id, entity_name, org_id=_resolve_org_scope(user), page=page, page_size=page_size
    )
    return ok(
        data=DriftLogListResponse(**result),
        trace_id=trace_id,
    )


@catalog_router.get("", dependencies=_READ_DEPS)
async def list_catalogs(
    params: Annotated[DBCatalogListParams, Depends()],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[DBCatalogListResponse]:
    svc = _svc(db)
    return ok(
        data=await svc.list_catalogs(params, org_id=_resolve_org_scope(user)),
        trace_id=trace_id,
    )


@catalog_router.get("/databases", dependencies=_READ_DEPS)
async def list_catalog_databases(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    source_id: str | None = None,
    source_status: str | None = Query(
        None,
        pattern=r"^(active|deleted|all)$",
        description="源状态过滤：active=仅活跃源 / deleted=仅已删源 / all=全部",
    ),
) -> ApiResponse[dict[str, list[str]]]:
    """目录去重库名列表（供前端库名筛选下拉，可随 source_id / source_status 联动）。"""
    svc = _svc(db)
    return ok(
        data={
            "items": await svc.list_catalog_databases(
                source_id, source_status, org_id=_resolve_org_scope(user)
            )
        },
        trace_id=trace_id,
    )


@catalog_router.get("/description-coverage", dependencies=_READ_DEPS)
async def get_description_coverage(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=500, description="每页条数；缺省全量"),
    source_id: str | None = Query(None, description="按数据源过滤（治理面板筛选）"),
    keyword: str | None = Query(None, description="按表名模糊过滤（治理面板筛选）"),
    database: str | None = Query(None, description="按库名过滤（治理面板筛选）"),
) -> ApiResponse[DescriptionCoverageResponse]:
    """描述缺失统计：表/字段覆盖率 + 按表列缺失字段数（治理优先级排序依据）。

    供资产地图「描述缺失」tab 与采集目录概览卡使用；source_id/keyword/database
    为采集目录治理面板「按数据源、库、表筛选治理」的服务端过滤（汇总与明细同口径）。
    P1-8: 汇总指标 SQL 端聚合；per_table 支持服务端分页。
    多租户隔离：非平台管理员仅统计本组织数据源目录。
    """
    svc = _svc(db)
    coverage = await svc._repo.get_description_coverage(
        page=page,
        page_size=page_size,
        source_id=source_id,
        keyword=keyword,
        database=database,
        org_id=_resolve_org_scope(user),
    )
    return ok(data=DescriptionCoverageResponse(**coverage), trace_id=trace_id)


#: 批量推断历史保留条数（写入时自动裁剪，防止无限增长）。
_BATCH_HISTORY_LIMIT = 20


def _to_history_entry(r: BatchInferHistory) -> BatchInferHistoryEntry:
    """ORM 行 → 响应 schema（JSON 列解析为表结构）。"""
    return BatchInferHistoryEntry(
        id=r.id,
        actor_id=r.actor_id,
        actor_name=r.actor_name,
        tables=[BatchInferHistoryTable(**t) for t in (r.tables_json or [])],
        done=r.done,
        failed=r.failed,
        cancelled=r.cancelled,
        added=r.added,
        elapsed=r.elapsed,
        failed_tables=[
            BatchInferHistoryTable(**t) for t in (r.failed_tables_json or [])
        ],
        created_at=r.created_at.isoformat() if r.created_at else "",
    )


def _task_to_dict(task: BatchLlmInferTask) -> dict[str, Any]:
    """批量任务行转 JSON 安全字典（进度/任务清单原样透传）。"""
    return {
        "id": task.id,
        "actor_id": task.actor_id,
        "actor_name": task.actor_name,
        "status": task.status,
        "total": task.total,
        "done": task.done,
        "failed": task.failed,
        "cancelled": task.cancelled,
        "added_total": task.added_total,
        "concurrency": task.concurrency,
        "cancel_requested": bool(task.cancel_requested),
        "error": task.error,
        "tasks": task.tasks_json or [],
        "progress": task.progress_json or [],
        "created_at": task.created_at.isoformat() if task.created_at else "",
        "started_at": task.started_at.isoformat() if task.started_at else "",
        "finished_at": task.finished_at.isoformat() if task.finished_at else "",
    }


def _assert_task_owner(task: BatchLlmInferTask, user: CurrentUser) -> None:
    """任务归属校验：platform_admin 可见/操作全部；其余仅本人发起任务。"""
    if "platform_admin" in user.roles_all():
        return
    if task.actor_id is not None and task.actor_id == user.id:
        return
    raise AuthError("无权访问该批量推断任务", error_code="FORBIDDEN")


@catalog_router.post("/batch-llm-infer", dependencies=_WRITE_DEPS)
async def create_batch_llm_infer_task(
    body: BatchLlmInferTaskCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """创建跨表批量 LLM 推断后台任务（方案 B：arq worker 执行，进度落库跨页可见）。

    逐表校验目录存在且未软删；创建 pending 任务记录后入队 arq
    ``run_batch_llm_infer_task``，返回任务初始状态供前端轮询（任意页面/刷新可查）。
    """
    from app.core.config import settings
    from app.services.collector.queue import _get_shared_arq_redis

    # 逐表校验目录存在（防无效 id / 已软删目录）
    task_items: list[dict[str, Any]] = []
    for t in body.tasks:
        cat = (
            await db.execute(
                select(DBCatalog).where(
                    DBCatalog.id == t.catalog_id, DBCatalog.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if cat is None:
            raise NotFoundError(f"目录实体不存在: {t.catalog_id}")
        task_items.append(
            {
                "catalog_id": t.catalog_id,
                "entity_name": t.entity_name or cat.entity_name,
                "missing_fields": t.missing_fields,
                "needs_table_desc": t.needs_table_desc,
            }
        )

    row = BatchLlmInferTask(
        actor_id=user.id,
        actor_name=getattr(user, "username", None),
        org_id=getattr(user, "org_id", None),
        tasks_json=task_items,
        progress_json=[],
        status="pending",
        total=len(task_items),
        concurrency=body.concurrency,
    )
    db.add(row)
    await db.flush()
    await write_audit(
        db,
        actor_id=user.id,
        action="catalog.batch_llm_infer_task",
        entity_type="batch_llm_infer_task",
        entity_id=str(row.id),
        detail={"tables": len(task_items), "concurrency": body.concurrency},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    await db.refresh(row)

    # 入队 arq（失败不阻断创建——任务保持 pending 可查询，worker 不消费属环境问题）
    try:
        redis = _get_shared_arq_redis(settings.redis_url)
        await redis.enqueue_job(
            "run_batch_llm_infer_task",
            row.id,
            _job_id=f"batch-infer:{row.id}",
        )
    except Exception as exc:  # noqa: BLE001 - 入队失败仅告警，不阻断响应
        logger.warning("batch_llm_infer_enqueue_failed", task_id=row.id, error=str(exc)[:200])

    return ok(data=_task_to_dict(row), trace_id=trace_id)


@catalog_router.get("/batch-llm-infer", dependencies=_READ_DEPS)
async def list_batch_llm_infer_tasks(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    limit: int = Query(20, ge=1, le=100, description="返回条数"),
) -> ApiResponse[list[dict[str, Any]]]:
    """批量推断任务列表（含进行中与最近历史，按创建倒序）。

    可见性：platform_admin 全部；其余仅本人发起任务（防跨用户窥探批量推断范围）。
    """
    stmt = (
        select(BatchLlmInferTask)
        .where(BatchLlmInferTask.deleted_at.is_(None))
        .order_by(BatchLlmInferTask.created_at.desc())
        .limit(limit)
    )
    if "platform_admin" not in user.roles_all():
        stmt = stmt.where(BatchLlmInferTask.actor_id == user.id)
    rows = (await db.execute(stmt)).scalars().all()
    return ok(data=[_task_to_dict(r) for r in rows], trace_id=trace_id)


@catalog_router.get("/batch-llm-infer/{task_id}", dependencies=_READ_DEPS)
async def get_batch_llm_infer_task(
    task_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """单任务进度（前端任务中心轮询用）。"""
    row = (
        await db.execute(
            select(BatchLlmInferTask).where(
                BatchLlmInferTask.id == task_id,
                BatchLlmInferTask.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"批量推断任务不存在: {task_id}")
    _assert_task_owner(row, user)
    return ok(data=_task_to_dict(row), trace_id=trace_id)


@catalog_router.post("/batch-llm-infer/{task_id}/cancel", dependencies=_WRITE_DEPS)
async def cancel_batch_llm_infer_task(
    task_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """请求取消批量任务（置 cancel_requested；worker 每表完成后检查并收尾）。"""
    row = (
        await db.execute(
            select(BatchLlmInferTask).where(
                BatchLlmInferTask.id == task_id,
                BatchLlmInferTask.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"批量推断任务不存在: {task_id}")
    _assert_task_owner(row, user)
    if row.status not in ("pending", "running"):
        # 已终态任务无需取消（幂等返回当前状态，避免重复取消告警）
        return ok(data=_task_to_dict(row), trace_id=trace_id)
    row.cancel_requested = True
    await write_audit(
        db,
        actor_id=user.id,
        action="catalog.batch_llm_infer_task_cancel",
        entity_type="batch_llm_infer_task",
        entity_id=str(row.id),
        detail={"status": row.status},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    await db.refresh(row)
    return ok(data=_task_to_dict(row), trace_id=trace_id)


@catalog_router.get("/batch-infer-history", dependencies=_READ_DEPS)
async def list_batch_infer_history(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    limit: int = Query(_BATCH_HISTORY_LIMIT, ge=1, le=100, description="返回条数"),
) -> ApiResponse[list[BatchInferHistoryEntry]]:
    """批量推断历史（服务端持久化，跨设备/团队可见，按时间倒序）。

    多租户可见性：platform_admin 可见全部历史；其余角色仅可见**本人**发起的
    批量推断历史（含表清单/身份信息，避免跨用户窥探他人采集推断范围）。
    """
    stmt = (
        select(BatchInferHistory)
        .where(BatchInferHistory.deleted_at.is_(None))
        .order_by(BatchInferHistory.created_at.desc())
        .limit(limit)
    )
    if "platform_admin" not in user.roles_all():
        stmt = stmt.where(BatchInferHistory.actor_id == user.id)
    rows = (await db.execute(stmt)).scalars().all()
    return ok(data=[_to_history_entry(r) for r in rows], trace_id=trace_id)


async def _prune_batch_infer_history(db: AsyncSession, limit: int) -> None:
    """软删超出最近 limit 条的历史记录。

    MySQL 不支持 NOT IN (SELECT ... ORDER BY ... LIMIT n) 子查询（错误 1235），
    先物化「保留的最近 N 条」id 列表，再以具体值列表裁剪；keep_ids 为空时
    跳过（not_in(空) 会渲染为永真条件导致全表软删）。
    """
    keep_ids = list(
        (
            await db.execute(
                select(BatchInferHistory.id)
                .where(BatchInferHistory.deleted_at.is_(None))
                .order_by(BatchInferHistory.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not keep_ids:
        return
    await db.execute(
        update(BatchInferHistory)
        .where(
            BatchInferHistory.deleted_at.is_(None),
            BatchInferHistory.id.not_in(keep_ids),
        )
        .values(deleted_at=datetime.now(UTC))
    )


@catalog_router.post("/batch-infer-history", dependencies=_WRITE_DEPS)
async def create_batch_infer_history(
    body: BatchInferHistoryCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[BatchInferHistoryEntry]:
    """写入一条批量推断历史（自动裁剪到近 N 条，超出的软删）。"""
    row = BatchInferHistory(
        actor_id=user.id,
        actor_name=getattr(user, "username", None),
        tables_json=[t.model_dump() for t in body.tables],
        done=body.done,
        failed=body.failed,
        cancelled=body.cancelled,
        added=body.added,
        elapsed=body.elapsed,
        failed_tables_json=[t.model_dump() for t in body.failed_tables],
    )
    db.add(row)
    await db.flush()
    await _prune_batch_infer_history(db, _BATCH_HISTORY_LIMIT)
    await write_audit(
        db,
        actor_id=user.id,
        action="catalog.batch_infer_history",
        entity_type="batch_infer_history",
        entity_id=str(row.id),
        detail={"done": body.done, "failed": body.failed, "added": body.added},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    await db.refresh(row)
    return ok(data=_to_history_entry(row), trace_id=trace_id)


@catalog_router.delete("/batch-infer-history", dependencies=_WRITE_DEPS)
async def clear_batch_infer_history(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, int]]:
    """清空当前用户自己的批量推断历史（团队他人记录保留）。"""
    result = await db.execute(
        update(BatchInferHistory)
        .where(
            BatchInferHistory.deleted_at.is_(None),
            BatchInferHistory.actor_id == user.id,
        )
        .values(deleted_at=datetime.now(UTC))
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="catalog.batch_infer_history_clear",
        entity_type="batch_infer_history",
        entity_id="",
        detail={"cleared": result.rowcount},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"cleared": result.rowcount}, trace_id=trace_id)


@catalog_router.get("/{catalog_id}", dependencies=_READ_DEPS)
async def get_catalog_detail(
    catalog_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[DBCatalogResponse]:
    """按主键取目录实体详情（血缘图谱表节点下钻展示用）。

    多租户隔离：非平台管理员跨组织实体视为不存在（404）。
    """
    svc = _svc(db)
    return ok(
        data=await svc.get_catalog_detail(catalog_id, org_id=_resolve_org_scope(user)),
        trace_id=trace_id,
    )


@catalog_router.post("/bulk-deprecate", dependencies=_WRITE_DEPS)
async def bulk_deprecate(
    body: BulkDeprecateRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[BulkDeprecateResult]:
    svc = _svc(db)
    # 越权审查修复：批量废弃强制 org 隔离（此前 bulk_deprecate 无 org 过滤，
    # domain_admin 可跨组织废弃目录表）。
    result = await svc.bulk_deprecate(body, user.id, org_id=_resolve_org_scope(user))
    await write_audit(
        db,
        actor_id=user.id,
        action="db_catalog.bulk_deprecate",
        entity_type="db_catalog",
        entity_id=f"items:{len(body.items)}",
        detail={
            "succeeded": len(result.succeeded),
            "failed": len(result.failed),
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


# ---- 字段描述推断 + 人工编辑端点 ----


@catalog_router.post(
    "/{catalog_id}/columns/{column_name}/infer-description",
    dependencies=_WRITE_DEPS,
)
async def infer_column_description(
    catalog_id: int,
    column_name: str,
    body: InferDescriptionRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[InferDescriptionResponse]:
    """LLM 推断单字段描述，成功后 upsert 到 column_descriptions 表。"""
    # 校验 catalog 存在
    cat = (
        await db.execute(
            select(DBCatalog).where(DBCatalog.id == catalog_id, DBCatalog.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if cat is None:
        raise NotFoundError(f"目录实体不存在: {catalog_id}")

    svc = _svc(db)
    # 幂等短路：已存在 LLM 推断描述且未强制重新生成 → 直接返回现有描述，避免重复调 LLM
    if not body.force:
        existing = await svc._repo.get_description(catalog_id, column_name)
        if existing is not None and existing.source == "llm":
            return ok(
                data=InferDescriptionResponse(
                    column_name=column_name,
                    description=existing.description,
                    source="llm",
                    confidence=1.0,
                ),
                trace_id=trace_id,
            )

    # FR-023: in-flight 去重——同一字段推断进行中时拒绝重复请求（409）
    async with _infer_inflight("column", catalog_id, column_name):
        result = await svc._llm_infer_column_description(
            entity_name=cat.entity_name,
            column_name=column_name,
            column_type=body.column_type,
        )
        if result is None:
            raise BusinessError(
                "LLM 推断暂时不可用，请稍后重试",
                error_code="LLM_INFER_UNAVAILABLE",
            )

        # upsert 到 column_descriptions
        await svc._repo.upsert_description(
            catalog_id=catalog_id,
            column_name=column_name,
            description=result["description"],
            source="llm",
        )
        await write_audit(
            db,
            actor_id=user.id,
            action="column.infer_description",
            entity_type="column_description",
            entity_id=f"{catalog_id}/{column_name}",
            detail={"description": result["description"], "confidence": result["confidence"]},
            ip=client_ip(request),
            trace_id=trace_id,
        )
        await db.commit()
        return ok(
            data=InferDescriptionResponse(
                column_name=column_name,
                description=result["description"],
                source="llm",
                confidence=result["confidence"],
            ),
            trace_id=trace_id,
        )


@catalog_router.post("/{catalog_id}/infer-descriptions", dependencies=_WRITE_DEPS)
async def infer_descriptions_batch(
    catalog_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[InferBatchResponse]:
    """批量推断该 catalog 所有空 comment 字段描述。逐字段推断并 upsert。"""
    cat = (
        await db.execute(
            select(DBCatalog).where(DBCatalog.id == catalog_id, DBCatalog.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if cat is None:
        raise NotFoundError(f"目录实体不存在: {catalog_id}")

    # FR-023: in-flight 去重——整表批量推断进行中时拒绝重复请求（409）
    async with _infer_inflight("batch", catalog_id):
        svc = _svc(db)

        # 获取已有描述（避免覆盖 manual/llm 记录）
        existing_descs = await svc._repo.get_descriptions(catalog_id)
        existing_map = {d.column_name: d for d in existing_descs}

        # 解析 schema_json columns
        schema_json = cat.schema_json or {}
        columns = schema_json.get("columns") or schema_json.get("fields") or []

        inferred: list[InferDescriptionResponse] = []
        skipped: list[str] = []
        failed: list[str] = []

        # 收集待推断字段（跳过已有 manual/llm 描述或已有 comment 的字段）
        targets: list[tuple[str, str | None]] = []
        for col in columns:
            if not isinstance(col, dict):
                continue
            col_name = col.get("name") or col.get("column")
            if not col_name:
                continue
            col_name = str(col_name)

            # 跳过已有 manual/llm 描述的字段
            if col_name in existing_map and existing_map[col_name].source in ("manual", "llm"):
                skipped.append(col_name)
                continue

            # 跳过已有 comment 的字段（除非 comment 为空/采集占位串——如 Spark Thrift
            # 无注释列的 "from deserializer"，视为无注释，允许推断）
            comment = col.get("comment")
            if is_effective_comment(comment) and col_name not in existing_map:
                skipped.append(col_name)
                continue

            col_type = col.get("type") or col.get("data_type")
            targets.append((col_name, str(col_type) if col_type else None))

        # FR-023: 一次 LLM 调用返回全部字段描述（json_schema 数组强约束 + 按 column_name 回填，
        # 不依赖返回顺序）。字段超限时按块多次调用（每块仍为一次请求），写库保持 targets 原始顺序。
        batch_chunk = 60
        parsed_map: dict[str, tuple[str, float]] = {}
        for start in range(0, len(targets), batch_chunk):
            chunk = targets[start : start + batch_chunk]
            parsed_map.update(
                await svc._llm_infer_batch_descriptions(
                    entity_name=cat.entity_name,
                    targets=chunk,
                )
            )

        for col_name, _ctype in targets:
            item = parsed_map.get(col_name)
            if item is None:
                failed.append(col_name)
                continue
            description, confidence = item
            # upsert
            await svc._repo.upsert_description(
                catalog_id=catalog_id,
                column_name=col_name,
                description=description,
                source="llm",
            )
            inferred.append(
                InferDescriptionResponse(
                    column_name=col_name,
                    description=description,
                    source="llm",
                    confidence=confidence,
                )
            )

        if inferred:
            await write_audit(
                db,
                actor_id=user.id,
                action="column.infer_descriptions",
                entity_type="column_description",
                entity_id=str(catalog_id),
                detail={"inferred": len(inferred), "skipped": len(skipped), "failed": len(failed)},
                ip=client_ip(request),
                trace_id=trace_id,
            )
        await db.commit()
        return ok(
            data=InferBatchResponse(inferred=inferred, skipped=skipped, failed=failed),
            trace_id=trace_id,
        )


@catalog_router.put("/{catalog_id}/columns/{column_name}/description", dependencies=_WRITE_DEPS)
async def update_column_description(
    catalog_id: int,
    column_name: str,
    body: UpdateDescriptionRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[UpdateDescriptionResponse]:
    """人工编辑字段描述：upsert 到 column_descriptions 表，source=manual。"""
    cat = (
        await db.execute(
            select(DBCatalog).where(DBCatalog.id == catalog_id, DBCatalog.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if cat is None:
        raise NotFoundError(f"目录实体不存在: {catalog_id}")

    svc = _svc(db)
    desc = await svc._repo.upsert_description(
        catalog_id=catalog_id,
        column_name=column_name,
        description=body.description,
        source="manual",
        updated_by=user.id,
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="column.update_description",
        entity_type="column_description",
        entity_id=f"{catalog_id}/{column_name}",
        detail={"description": body.description, "source": "manual"},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=UpdateDescriptionResponse(
            catalog_id=desc.catalog_id,
            column_name=desc.column_name,
            description=desc.description,
            source=desc.source,
            updated_by=desc.updated_by,
            updated_at=desc.updated_at,
        ),
        trace_id=trace_id,
    )


@catalog_router.put("/{catalog_id}/description", dependencies=_WRITE_DEPS)
async def update_table_description(
    catalog_id: int,
    body: TableDescriptionRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[TableDescriptionResponse]:
    """人工编辑表级业务描述：更新 db_catalog.description，source=manual。"""
    svc = _svc(db)
    cat = await svc._repo.update_table_description(
        catalog_id=catalog_id,
        description=body.description,
        source="manual",
        updated_by=user.id,
    )
    if cat is None:
        raise NotFoundError(f"目录实体不存在: {catalog_id}")
    await write_audit(
        db,
        actor_id=user.id,
        action="catalog.update_description",
        entity_type="catalog",
        entity_id=str(catalog_id),
        detail={"description": body.description, "source": "manual"},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=TableDescriptionResponse(
            catalog_id=cat.id,
            description=cat.description or "",
            source=cat.description_source or "manual",
            updated_by=cat.description_updated_by,
            updated_at=cat.description_updated_at,
        ),
        trace_id=trace_id,
    )


@catalog_router.post("/{catalog_id}/infer-table-description", dependencies=_WRITE_DEPS)
async def infer_table_description(
    catalog_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    body: InferTableDescriptionRequest | None = None,
) -> ApiResponse[InferTableDescriptionResponse]:
    """LLM 推断表级业务描述，成功后落库 db_catalog.description（source=llm）。"""
    cat = (
        await db.execute(
            select(DBCatalog).where(DBCatalog.id == catalog_id, DBCatalog.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if cat is None:
        raise NotFoundError(f"目录实体不存在: {catalog_id}")

    force = bool(body and body.force)
    # 幂等短路：已存在 LLM 推断描述且未强制重新生成 → 直接返回现有描述，避免重复调 LLM
    if not force and cat.description_source == "llm" and cat.description:
        return ok(
            data=InferTableDescriptionResponse(
                catalog_id=catalog_id,
                description=cat.description,
                source="llm",
                confidence=1.0,
            ),
            trace_id=trace_id,
        )

    # FR-023: in-flight 去重——表级推断进行中时拒绝重复请求（409）
    async with _infer_inflight("table", catalog_id):
        svc = _svc(db)
        schema_json = cat.schema_json or {}
        fields = (
            body.fields
            if body and body.fields
            else (schema_json.get("columns") or schema_json.get("fields") or [])
        )
        result = await svc._llm_infer_table_description(
            entity_name=cat.entity_name,
            columns=fields,
        )
        if result is None:
            raise BusinessError(
                "LLM 推断暂时不可用，请稍后重试",
                error_code="LLM_INFER_UNAVAILABLE",
            )

        updated = await svc._repo.update_table_description(
            catalog_id=catalog_id,
            description=result["description"],
            source="llm",
        )
        if updated is None:
            raise NotFoundError(f"目录实体不存在: {catalog_id}")
        await write_audit(
            db,
            actor_id=user.id,
            action="catalog.infer_description",
            entity_type="catalog",
            entity_id=str(catalog_id),
            detail={"description": result["description"], "confidence": result["confidence"]},
            ip=client_ip(request),
            trace_id=trace_id,
        )
        await db.commit()
        return ok(
            data=InferTableDescriptionResponse(
                catalog_id=catalog_id,
                description=result["description"],
                confidence=result["confidence"],
            ),
            trace_id=trace_id,
        )


# ---- 采集运行历史端点（采集记录页主视图，TD §12.1）----


def _parse_run_time_param(value: str | None) -> datetime | None:
    """解析采集运行历史时间区间参数（ISO 8601）。

    空值或非法格式静默忽略（返回 None），不阻断主查询——前端时间筛选为可选增强。
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@collection_run_router.get("", dependencies=_READ_DEPS)
async def list_collection_runs(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    source_id: str | None = None,
    status: str | None = None,
    trigger: str | None = None,
    started_after: str | None = None,
    started_before: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> ApiResponse[CollectionRunListResponse]:
    """采集运行历史分页列表（按开始时间倒序，可按源/状态/触发方式/时间区间过滤）。

    采集记录页主视图数据源：区别于 ephemeral 的 job（7 天 TTL），本表为
    持久化采集历史（含失败与排障明细），满足审计与运维可追溯。
    """
    svc = _svc(db)
    result = await svc.list_collection_runs(
        source_id=source_id,
        status=status,
        trigger=trigger,
        page=page,
        page_size=page_size,
        started_after=_parse_run_time_param(started_after),
        started_before=_parse_run_time_param(started_before),
        org_id=_resolve_org_scope(user),
    )
    return ok(data=CollectionRunListResponse(**result), trace_id=trace_id)


@collection_run_router.get("/summary", dependencies=_READ_DEPS)
async def collection_run_summary(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    source_id: str | None = None,
    status: str | None = None,
    trigger: str | None = None,
    started_after: str | None = None,
    started_before: str | None = None,
) -> ApiResponse[dict[str, int]]:
    """采集运行历史聚合统计（服务端 SQL 聚合，供前端统计摘要）。

    与列表共用过滤条件，一次聚合出 total/completed/failed/scanned/registered，
    前端无需用 ``page_size=200`` 拉全量再在客户端聚合（总数 > 200 时口径矛盾）。
    """
    svc = _svc(db)
    result = await svc.get_collection_run_summary(
        source_id=source_id,
        status=status,
        trigger=trigger,
        started_after=_parse_run_time_param(started_after),
        started_before=_parse_run_time_param(started_before),
        org_id=_resolve_org_scope(user),
    )
    return ok(data=result, trace_id=trace_id)


@collection_run_router.get("/{run_id}", dependencies=_READ_DEPS)
async def get_collection_run(
    run_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[CollectionRunResponse]:
    """采集运行详情（含失败实体 / 漂移事件 / 降级原因明细）。"""
    svc = _svc(db)
    detail = await svc.get_collection_run_detail(run_id, org_id=_resolve_org_scope(user))
    return ok(data=CollectionRunResponse(**detail), trace_id=trace_id)


@collection_run_router.get("/{run_id}/logs", dependencies=_READ_DEPS)
async def get_collection_run_logs(
    run_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
) -> ApiResponse[dict[str, Any]]:
    """采集运行日志分页（采集记录详情页「实时日志」）。

    读取策略：终态（已回写 DB）读 ``collection_run_log`` 表；RUNNING 中或
    崩溃未收尾读 Redis 实时缓冲（``collect:run_log:{run_id}``）。返回
    ``source``（db/redis/none）与 ``status`` 供前端决定是否轮询刷新。
    """
    svc = _svc(db)
    result = await svc.get_collection_run_logs(
        run_id, offset, limit, org_id=_resolve_org_scope(user)
    )
    return ok(data=result, trace_id=trace_id)
