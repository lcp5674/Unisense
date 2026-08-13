"""健康检查与可观测性路由。

提供 liveness（/health）、readiness（/ready）与 Prometheus 指标（/metrics）端点。
对齐 DEV_GUIDE §16 可观测性 与 全栈开发 skill 的 Production Hardening 要求。
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter
from sqlalchemy import text
from starlette.responses import Response

from app.api.deps import DBSession, RedisClient
from app.core.degradation import read_dependency_health
from app.core.degradation_registry import get_degradation_registry
from app.core.es_client import get_es_client
from app.core.metrics import render_metrics
from app.core.resilience import optional_dependency_status

router = APIRouter(tags=["health"])


@router.get("/health", summary="存活探针")
async def health() -> dict[str, str]:
    """Liveness 探针：进程存活即返回 OK。

    Returns:
        状态字典。
    """
    return {"status": "ok"}


@router.get("/ready", summary="就绪探针")
async def ready(
    db: DBSession,
    redis: RedisClient,
) -> dict[str, object]:
    """Readiness 探针：检查 DB/Redis 与可选依赖（Neo4j/ES/OLAP）连通性。

    核心依赖（DB/Redis）任一失败 -> ``unavailable``；仅可选依赖失败 -> ``degraded``
    （核心链路仍可用，体现 TD §11 韧性降级）。

    Args:
        db: 数据库会话。
        redis: Redis 客户端。

    Returns:
        各依赖检查结果与整体状态。
    """
    checks: dict[str, str] = {"db": "ok", "redis": "ok"}

    # 检查 MySQL（核心依赖）
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        checks["db"] = "fail"

    # 检查 Redis（核心依赖）
    if redis is not None:
        try:
            await redis.ping()
        except Exception:
            checks["redis"] = "fail"
    else:
        checks["redis"] = "skip"

    # 可选依赖探活（TD §11 韧性）：任一降级不影响核心链路。
    # TCP 探活为阻塞调用，放入线程池避免阻塞事件循环（就绪探针也要快）。
    # ES 若已接入客户端（es_breaker 进入降级矩阵），用真实 .ping() 探活替代 TCP。
    optional: dict[str, str] = {}
    es_client = get_es_client()
    es_probed = False
    if es_client.enabled:
        es_alive = await es_client.health()
        optional["elasticsearch"] = "ok" if es_alive else "fail"
        es_probed = True
    for name, alive in (await asyncio.to_thread(optional_dependency_status)).items():
        if name == "elasticsearch" and es_probed:
            continue
        optional[name] = "ok" if alive else "fail"
    degraded = [n for n, s in optional.items() if s == "fail"]

    # 同步降级注册中心（OPS-05 统一降级面板）：降级→注册，恢复→清除
    registry = get_degradation_registry()
    for name, state in optional.items():
        if state == "fail":
            registry.register_degradation(name, f"optional_dependency_probe_failed: {name}")
        else:
            registry.clear_degradation(name)

    if checks["db"] == "fail" or checks["redis"] == "fail":
        status = "unavailable"
    elif degraded:
        status = "degraded"
    else:
        status = "ok"

    return {
        "status": status,
        "checks": checks,
        "optional": optional,
        "degraded": degraded,
        "timestamp": int(time.time()),
    }


@router.get("/health/degraded", summary="降级面板（统一降级状态）")
async def degraded_overview() -> dict[str, object]:
    """返回统一降级面板（OPS-05/TD §4.13）：当前活跃降级组件与状态摘要。

    供运营看板/告警网关查询；进程内注册中心为权威源。
    """
    registry = get_degradation_registry()
    return registry.get_status_summary()


@router.get("/dependencies/health", summary="依赖实时健康态（运营看板）")
async def dependencies_health() -> dict[str, object]:
    """返回各依赖实时健康态快照（dependency_health 表），供运营看板实时查询（TD §4.13）。

    任何 DB 异常均 best-effort 降级为空列表，绝不阻断探针/看板。
    """
    items = await read_dependency_health()
    return {"count": len(items), "items": items}


@router.get("/metrics", summary="Prometheus 指标", include_in_schema=True)
async def metrics() -> Response:
    """Prometheus exposition 格式的 RED 指标端点（无需鉴权）。"""
    return render_metrics()
