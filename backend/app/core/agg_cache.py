"""聚合结果短 TTL 缓存（cache-aside，供大盘/总览等全表聚合路径复用）。

背景（P1 性能审查）：``/semantics/dashboard`` 与 ``/observability/overview`` 每次
请求执行约 20 个全表聚合 SQL（含 WORM 审计表无时间窗口扫描），随数据量增长线性
恶化。assetmap 已有同款 ``_agg_cached``（30s TTL + CircuitBreaker）——此处抽为公共
模块供 semantic/observability 复用，避免每模块复制一套。

设计：
- 读缓存命中即返；未命中回源 loader 并写缓存（TTL 30s）。
- best-effort：Redis 不可用/熔断打开/坏数据一律回源，绝不阻断主链路。
- ``agg_cache_invalidate`` 供写操作后主动失效（避免 30s 内陈旧）。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

import structlog

from app.core.resilience import CircuitBreaker

logger = structlog.get_logger("unisense.agg_cache")

_CACHE_TTL = 30  # 秒
_CACHE_PREFIX = "unisense:agg:"
_CACHE_BREAKER = CircuitBreaker(failure_threshold=5, reset_timeout=30.0)


async def _cache_get(key: str) -> Any | None:
    """聚合结果缓存读取（best-effort：Redis 不可用/熔断打开/坏数据均回源）。"""
    if not _CACHE_BREAKER.allow():
        return None
    try:
        from app.db.redis import get_redis

        raw = await get_redis().get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - 缓存降级，不阻断主链路
        _CACHE_BREAKER.record_failure()
        logger.warning("agg_cache_get_failed", key=key, error=str(exc))
        return None


async def _cache_set(key: str, value: Any) -> None:
    """聚合结果缓存写入（best-effort）。熔断打开时跳过写，防雪崩。"""
    if not _CACHE_BREAKER.allow():
        return
    try:
        from app.db.redis import get_redis

        await get_redis().set(
            key, json.dumps(value, ensure_ascii=False, default=str), ex=_CACHE_TTL
        )
        _CACHE_BREAKER.record_success()
    except Exception as exc:  # noqa: BLE001 - 缓存降级，不阻断主链路
        _CACHE_BREAKER.record_failure()
        logger.warning("agg_cache_set_failed", key=key, error=str(exc))


async def agg_cached(
    name: str, loader: Callable[[], Awaitable[dict[str, Any]]]
) -> dict[str, Any]:
    """cache-aside 通用封装：读缓存命中即返，未命中回源并写缓存。

    Args:
        name: 缓存键后缀（含业务维度区分，如 ``dashboard:outpatient``）。
        loader: 回源加载函数（全表聚合）。
    """
    key = f"{_CACHE_PREFIX}{name}"
    cached = await _cache_get(key)
    if cached is not None:
        return cast("dict[str, Any]", cached)
    data = await loader()
    await _cache_set(key, data)
    return data


async def agg_cache_invalidate() -> None:
    """写操作后主动失效全部聚合缓存（避免 TTL 内陈旧）。

    best-effort：Redis 不可用/熔断打开时静默跳过，下次 TTL 自然过期。
    """
    if not _CACHE_BREAKER.allow():
        return
    try:
        from app.db.redis import get_redis

        redis = get_redis()
        async for key in redis.scan_iter(match=f"{_CACHE_PREFIX}*"):
            await redis.delete(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agg_cache_invalidate_failed", error=str(exc))
