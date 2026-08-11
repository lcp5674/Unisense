"""采集领域 API（对齐 TD §12.1 / DEV_GUIDE §8b.1）。

路由：
- 数据源：POST /api/v1/data-sources（写闸门）、GET /api/v1/data-sources（注入守卫）、
  GET/DELETE /api/v1/data-sources/{source_id}
- 采集触发：POST /api/v1/data-sources/{source_id}/collect（写闸门）
- 元数据：POST /api/v1/data-sources/{source_id}/catalogs（写闸门）、
  GET /api/v1/catalogs（注入守卫）、POST /api/v1/catalogs/bulk-deprecate（写闸门，207）

凭据脱敏：响应一律不含 connection_config 明文（见 DataSourceResponse）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.exceptions import BusinessError, NotFoundError
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.collector.schemas import (
    BulkDeprecateRequest,
    BulkDeprecateResult,
    CollectRequest,
    DataSourceCreateRequest,
    DataSourceResponse,
    DBCatalogCreateRequest,
    DBCatalogListParams,
    DBCatalogListResponse,
    DBCatalogResponse,
)
from app.services.collector.service import CollectorService
from app.services.collector.spi import build_collector

source_router = APIRouter(prefix="/data-sources", tags=["collector-source"])
catalog_router = APIRouter(prefix="/catalogs", tags=["collector-catalog"])

_WRITE_ROLES = ("platform_admin", "domain_admin", "metric_owner")
_READ_ROLES = ALL_ROLES
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]


def _svc(db: AsyncSession) -> CollectorService:
    return CollectorService(db)


@source_router.post("", dependencies=[Depends(require_roles(*_WRITE_ROLES))])
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
) -> ApiResponse[list[DataSourceResponse]]:
    svc = _svc(db)
    items, _total = await svc.list_sources(
        domain=domain, source_type=source_type, keyword=keyword, page=page, page_size=page_size
    )
    return ok(data=items, trace_id=trace_id)


@source_router.get("/{source_id}", dependencies=_READ_DEPS)
async def get_data_source(
    source_id: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[DataSourceResponse]:
    svc = _svc(db)
    return ok(data=await svc.get_source(source_id), trace_id=trace_id)


@source_router.delete("/{source_id}", dependencies=[Depends(require_roles(*_WRITE_ROLES))])
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


@source_router.post("/{source_id}/catalogs", dependencies=[Depends(require_roles(*_WRITE_ROLES))])
async def register_catalog(
    source_id: str,
    body: DBCatalogCreateRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[DBCatalogResponse]:
    if body.source_id != source_id:
        raise BusinessError("body.source_id 与路径不一致", error_code="BAD_REQUEST")
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


@source_router.post("/{source_id}/collect", dependencies=[Depends(require_roles(*_WRITE_ROLES))])
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
    collector = build_collector(body.collector_type, src.connection_config)
    try:
        result = await svc.collect_and_register(source_id, collector, user.id)
    finally:
        await collector.dispose()
    pii_registered = result.get("pii_registered", 0)
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
        },
        ip=client_ip(request),
        trace_id=trace_id,
        pii_access=pii_registered > 0,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@source_router.post(
    "/{source_id}/schedule", dependencies=[Depends(require_roles(*_WRITE_ROLES))]
)
async def schedule_collection(
    source_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """全量采集接入异步队列：立即返回 job_id，采集在后台 worker 执行（TD §12.1）。"""
    svc = _svc(db)
    job_id = await svc.schedule_collection(source_id, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="COLLECT_SCHEDULE",
        entity_type="data_source",
        entity_id=source_id,
        detail={"job_id": job_id},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"job_id": job_id, "status": "QUEUED"}, trace_id=trace_id)


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


@catalog_router.get("", dependencies=_READ_DEPS)
async def list_catalogs(
    params: Annotated[DBCatalogListParams, Depends()],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[DBCatalogListResponse]:
    svc = _svc(db)
    return ok(data=await svc.list_catalogs(params), trace_id=trace_id)


@catalog_router.post("/bulk-deprecate", dependencies=[Depends(require_roles(*_WRITE_ROLES))])
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
