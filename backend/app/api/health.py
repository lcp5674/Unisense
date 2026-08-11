"""健康检查与可观测性路由。

提供 liveness（/health）、readiness（/ready）与 Prometheus 指标（/metrics）端点。
对齐 DEV_GUIDE §16 可观测性 与 全栈开发 skill 的 Production Hardening 要求。
"""

from __future__ import annotations

import time

from fastapi import APIRouter
from sqlalchemy import text
from starlette.responses import Response

from app.api.deps import DBSession, RedisClient
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
    degraded: list[str] = []

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

    # 可选依赖探活（TD §11 韧性）：任一降级不影响核心链路
    optional: dict[str, str] = {}
    for name, alive in optional_dependency_status().items():
        optional[name] = "ok" if alive else "fail"
        if not alive:
            degraded.append(name)

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


@router.get("/metrics", summary="Prometheus 指标", include_in_schema=True)
async def metrics() -> Response:
    """Prometheus exposition 格式的 RED 指标端点（无需鉴权）。"""
    return render_metrics()
