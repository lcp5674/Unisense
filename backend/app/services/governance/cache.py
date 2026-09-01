"""governance 角色权限映射缓存（多 worker 一致性版本化缓存，P2 加固）。

``role_permission`` 是低频变更的配置表，但 consume dry-run/execute 等高频 PDP
决策路径每次都全表扫——加进程内短 TTL 缓存（60s）降低 DB 压力。

**多 worker 一致性**：旧实现为纯进程内缓存，角色变更后其余 worker 需等 60s
TTL 自然过期才收敛（``invalidate_role_actions_cache`` 只清本进程）。本实现引入
Redis 全局版本号：写操作 ``invalidate_role_actions_cache`` 在 Redis 递增版本，
各 worker 在版本检查窗口（默认 2s）内感知变更并回源，角色变更**≤2s 全 worker
生效**；版本号检查按窗口限频，避免高频 PDP 决策打满 Redis。

Redis 不可用时降级为纯进程内 60s TTL（与历史行为一致，fail-safe 不阻断）。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

_ROLE_ACTIONS_CACHE_TTL = 60.0  # 秒
#: 版本号检查窗口（秒）：各 worker 每窗口最多查一次 Redis。
_VERSION_CHECK_INTERVAL = 2.0
_VERSION_KEY = "unisense:role_actions:version"

#: 本地缓存：role_actions / ui_role_actions 双份 + 统一时间戳与版本号。
_cache: dict[str, Any] = {"ts": 0.0, "role_actions": None, "ui_role_actions": None, "version": 0}
#: 本地感知的全局版本号（含最近检查时间戳，用于限频）。
_local_version: dict[str, Any] = {"value": 0, "checked": 0.0}


async def _current_version() -> int:
    """读取 Redis 全局角色权限版本号（best-effort，失败保持本地值）。

    限频：窗口内直接返回本地缓存值，不重复打 Redis。
    """
    now = time.monotonic()
    if now - _local_version["checked"] < _VERSION_CHECK_INTERVAL:
        return int(_local_version["value"])
    try:
        from app.db.redis import get_redis

        redis_client = get_redis()
        raw = await redis_client.get(_VERSION_KEY)
        _local_version["value"] = int(raw) if raw else 0
    except Exception:  # noqa: BLE001 - Redis 不可用：保持本地版本号，TTL 兜底收敛
        pass
    _local_version["checked"] = now
    return int(_local_version["value"])


async def _get_cached(
    key: str,
    loader: Callable[[], Awaitable[dict[str, frozenset[str]]]],
) -> dict[str, frozenset[str]]:
    """读取缓存（命中且版本一致即返，否则回源并写缓存）。"""
    now = time.monotonic()
    cached = _cache[key]
    version = await _current_version()
    if (
        cached is not None
        and now - _cache["ts"] < _ROLE_ACTIONS_CACHE_TTL
        and _cache["version"] == version
    ):
        return cached
    data = await loader()
    _cache[key] = data
    _cache["version"] = version
    _cache["ts"] = now
    return data


async def get_role_actions_cached(
    loader: Callable[[], Awaitable[dict[str, frozenset[str]]]],
) -> dict[str, frozenset[str]]:
    """读取资源级角色动作映射（缓存命中即返，未命中回源并写缓存）。"""
    return await _get_cached("role_actions", loader)


async def get_ui_role_actions_cached(
    loader: Callable[[], Awaitable[dict[str, frozenset[str]]]],
) -> dict[str, frozenset[str]]:
    """读取 UI 权限点映射（缓存命中即返，未命中回源并写缓存）。"""
    return await _get_cached("ui_role_actions", loader)


async def invalidate_role_actions_cache() -> None:
    """写操作后主动失效角色权限缓存（多 worker 一致，P2 加固）。

    Redis 可用：递增全局版本号（各 worker ≤2s 感知并回源）+ 清本地；
    Redis 不可用：仅清本地，其余 worker 靠 60s TTL 自然收敛（fail-safe）。
    """
    try:
        from app.db.redis import get_redis

        redis_client = get_redis()
        await redis_client.incr(_VERSION_KEY)
        _local_version["value"] += 1
        _local_version["checked"] = time.monotonic()
    except Exception:  # noqa: BLE001 - Redis 不可用：本地失效 + TTL 兜底
        pass
    _cache["ts"] = 0.0
    _cache["role_actions"] = None
    _cache["ui_role_actions"] = None
