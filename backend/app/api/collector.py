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
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.exceptions import BusinessError, ConflictError, NotFoundError
from app.core.guard import guard_against_injection
from app.core.logging import get_logger
from app.db.mysql import get_db_session
from app.db.redis import get_redis
from app.models.data_source import DBCatalog
from app.services.collector.distributed_lock import CollectionLock
from app.services.collector.schemas import (
    BulkDeprecateRequest,
    BulkDeprecateResult,
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
    DriftLogListResponse,
    InferBatchResponse,
    InferDescriptionRequest,
    InferDescriptionResponse,
    ScheduleRequest,
    TestConnectionRequest,
    TestConnectionResult,
    UpdateDescriptionRequest,
    UpdateDescriptionResponse,
)
from app.services.collector.service import CollectorService
from app.services.collector.spi import build_collector

logger = get_logger("unisense.collector.api")

source_router = APIRouter(prefix="/data-sources", tags=["collector-source"])
catalog_router = APIRouter(prefix="/catalogs", tags=["collector-catalog"])

_WRITE_ROLES = ("platform_admin", "domain_admin", "metric_owner")
_READ_ROLES = ALL_ROLES
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]
# 写端点统一挂注入守卫（纵深防御：ORM 参数化兜底之外拦截注入 payload）
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]


def _svc(db: AsyncSession) -> CollectorService:
    return CollectorService(db)


@source_router.post("", dependencies=_WRITE_DEPS)
async def create_data_source(
    body: DataSourceCreateRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[DataSourceResponse]:
    svc = _svc(db)
    resp = await svc.create_source(body, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="CREATE",
        entity_type="data_source",
        entity_id=resp.source_id,
        detail={"name": resp.name, "source_type": resp.source_type},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@source_router.get("", dependencies=_READ_DEPS)
async def list_data_sources(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    domain: str | None = None,
    source_type: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> ApiResponse[DataSourceListResponse]:
    svc = _svc(db)
    items, total = await svc.list_sources(
        domain=domain, source_type=source_type, keyword=keyword, page=page, page_size=page_size
    )
    # P1-1: 返回分页结构（含 total），此前 total 被丢弃导致 >20 个源时静默截断
    return ok(
        data=DataSourceListResponse(items=items, total=total, page=page, page_size=page_size),
        trace_id=trace_id,
    )


@source_router.get("/types", dependencies=_READ_DEPS)
async def list_source_types(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[list[DataSourceTypeInfo]]:
    """FR-030: 返回全部已注册采集器类型元信息（前端动态渲染类型选择器）。"""
    svc = _svc(db)
    return ok(data=await svc.list_source_types(), trace_id=trace_id)


@source_router.post("/test-connection", dependencies=_WRITE_DEPS)
async def test_connection(
    body: TestConnectionRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[TestConnectionResult]:
    """FR-030: 创建前连接预检（明文配置轻量探活，不落库、不写审计）。"""
    svc = _svc(db)
    source_type_value = (
        body.source_type.value if hasattr(body.source_type, "value") else str(body.source_type)
    )
    result = await svc.test_connection(source_type_value, body.connection_config)
    await write_audit(
        db,
        actor_id=user.id,
        action="TEST_CONNECTION",
        entity_type="data_source",
        entity_id=f"{source_type_value}:{body.connection_config.get('host', '')}",
        detail={"ok": result.ok, "latency_ms": result.latency_ms},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@source_router.post("/databases", dependencies=_WRITE_DEPS)
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
    svc = _svc(db)
    source_type_value = (
        body.source_type.value if hasattr(body.source_type, "value") else str(body.source_type)
    )
    databases = await svc.list_databases(source_type_value, body.connection_config)
    return ok(data={"databases": databases, "source_type": source_type_value}, trace_id=trace_id)


@source_router.get("/jobs", dependencies=_READ_DEPS)
async def list_collection_jobs(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    limit: int = 50,
    offset: int = 0,
    source_id: str | None = None,
) -> ApiResponse[list[dict[str, Any]]]:
    """列出采集任务（按入队逆序分页，采集任务中心入口）。

    可按 ``source_id`` 过滤（任务中心按数据源筛选）；job 含 ``created_at``（创建时间）
    与 ``kind``（manual 手动 / scheduled 定时）供前端展示。

    注意：本端点须注册在 ``GET /{source_id}`` 之前——FastAPI 按注册顺序匹配，
    单段静态路径 ``/jobs`` 若在 ``/{source_id}`` 之后会被当作 source_id 吞掉。
    """
    svc = _svc(db)
    jobs = await svc.list_jobs(limit=limit, offset=offset, source_id=source_id)
    return ok(data=jobs, trace_id=trace_id)


@source_router.get("/{source_id}", dependencies=_READ_DEPS)
async def get_data_source(
    source_id: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[DataSourceResponse]:
    svc = _svc(db)
    return ok(data=await svc.get_source(source_id), trace_id=trace_id)


@source_router.put("/{source_id}", dependencies=_WRITE_DEPS)
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
    resp = await svc.update_source(source_id, body, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="UPDATE",
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
    result = await svc.check_connection(source_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="CHECK_CONNECTION",
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
    await svc.delete_source(source_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="DELETE",
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
        action="REGISTER",
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
    page: int = 1,
    page_size: int = 20,
) -> ApiResponse[DBCatalogListResponse]:
    """按数据源查询采集目录（采集目录页按源查看表/字段的入口）。"""
    svc = _svc(db)
    params = DBCatalogListParams(
        source_id=source_id,
        entity_type=entity_type,
        sensitivity_level=sensitivity_level,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    return ok(data=await svc.list_catalogs(params), trace_id=trace_id)


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
    src = await svc.get_source_orm(source_id)
    collector = build_collector(src.source_type, src.connection_config)
    try:
        result = await svc.refresh_entity(source_id, entity_name, user.id, collector)
    finally:
        await collector.dispose()
    await write_audit(
        db,
        actor_id=user.id,
        action="REFRESH",
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
    src = await svc.get_source_orm(source_id)
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
        # FR-017: asyncio.timeout(300) 保护
        result = await asyncio.wait_for(
            svc.collect_and_register(source_id, collector, user.id, mode=body.mode),
            timeout=300.0,
        )
    except TimeoutError:
        raise BusinessError(
            f"数据源 {source_id} 采集超时（300秒），请使用异步调度",
            error_code="COLLECTION_TIMEOUT",
        ) from None
    finally:
        await lock.release(source_id, owner_id)
        await collector.dispose()
    pii_registered = result.get("pii_registered", 0)
    failed_count = result.get("failed_count", 0)
    await write_audit(
        db,
        actor_id=user.id,
        action="COLLECT",
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
    await svc.update_schedule(source_id, body.cron, body.mode)
    await write_audit(
        db,
        actor_id=user.id,
        action="COLLECT_SCHEDULE",
        entity_type="data_source",
        entity_id=source_id,
        detail={"cron": body.cron, "mode": body.mode, "scheduled": True},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data={"scheduled": True, "cron": body.cron, "mode": body.mode},
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
    job_id = await svc.schedule_collection(source_id, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="COLLECT_ASYNC",
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
    job_id = await svc.schedule_collection(source_id, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="COLLECT_NOW",
        entity_type="data_source",
        entity_id=source_id,
        detail={"job_id": job_id, "mode": body.mode},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data={"job_id": job_id, "status": "QUEUED", "mode": body.mode},
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
    status = await svc.get_job_status(job_id)
    if status is None:
        raise NotFoundError(f"采集任务不存在: {job_id}", ctx={"job_id": job_id})
    return ok(data=status, trace_id=trace_id)


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
    watermark = await svc.get_watermark(source_id)
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
    health_info = await svc.get_health(source_id)
    return ok(data=health_info, trace_id=trace_id)


@source_router.get("/{source_id}/drift-logs", dependencies=_READ_DEPS)
async def list_drift_logs(
    source_id: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    entity_name: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> ApiResponse[DriftLogListResponse]:
    """P1-4: Schema Drift 变更日志（按检测时间倒序，分页）。

    用于前端展示数据源的 schema 漂移历史。
    """
    svc = _svc(db)
    result = await svc.list_drift_logs(source_id, entity_name, page=page, page_size=page_size)
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
    return ok(data=await svc.list_catalogs(params), trace_id=trace_id)


@catalog_router.get("/databases", dependencies=_READ_DEPS)
async def list_catalog_databases(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    source_id: str | None = None,
) -> ApiResponse[dict[str, list[str]]]:
    """目录去重库名列表（供前端库名筛选下拉，可随 source_id 联动）。"""
    svc = _svc(db)
    return ok(data={"items": await svc.list_catalog_databases(source_id)}, trace_id=trace_id)


@catalog_router.get("/{catalog_id}", dependencies=_READ_DEPS)
async def get_catalog_detail(
    catalog_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[DBCatalogResponse]:
    """按主键取目录实体详情（血缘图谱表节点下钻展示用）。"""
    svc = _svc(db)
    return ok(data=await svc.get_catalog_detail(catalog_id), trace_id=trace_id)


@catalog_router.post("/bulk-deprecate", dependencies=_WRITE_DEPS)
async def bulk_deprecate(
    body: BulkDeprecateRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[BulkDeprecateResult]:
    svc = _svc(db)
    result = await svc.bulk_deprecate(body, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="BULK_DEPRECATE",
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
        action="INFER_DESCRIPTION",
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

        # 跳过已有 comment 的字段（除非 comment 为空）
        comment = col.get("comment")
        if comment and col_name not in existing_map:
            skipped.append(col_name)
            continue

        # 推断
        col_type = col.get("type") or col.get("data_type")
        result = await svc._llm_infer_column_description(
            entity_name=cat.entity_name,
            column_name=col_name,
            column_type=str(col_type) if col_type else None,
        )
        if result is None:
            failed.append(col_name)
            continue

        # upsert
        await svc._repo.upsert_description(
            catalog_id=catalog_id,
            column_name=col_name,
            description=result["description"],
            source="llm",
        )
        inferred.append(
            InferDescriptionResponse(
                column_name=col_name,
                description=result["description"],
                source="llm",
                confidence=result["confidence"],
            )
        )

    if inferred:
        await write_audit(
            db,
            actor_id=user.id,
            action="INFER_DESCRIPTIONS_BATCH",
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
        action="UPDATE_DESCRIPTION",
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
