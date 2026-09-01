"""governance 角色权限映射进程内短 TTL 缓存（P10 性能审查）。

``role_permission`` 是低频变更的配置表，但 consume dry-run/execute 等高频 PDP
决策路径每次都全表扫——加进程内短 TTL 缓存（60s）。多 worker 各持一份，配置
变更在 TTL 内自然收敛；写操作可调 ``invalidate_role_actions_cache`` 主动失效。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

_ROLE_ACTIONS_CACHE_TTL = 60.0  # 秒

_cache: dict[str, Any] = {"ts": 0.0, "role_actions": None, "ui_role_actions": None}


async def get_role_actions_cached(
    loader: Callable[[], Awaitable[dict[str, frozenset[str]]]],
) -> dict[str, frozenset[str]]:
    """读取资源级角色动作映射（缓存命中即返，未命中回源并写缓存）。"""
    now = time.monotonic()
    cached = _cache["role_actions"]
    if cached is not None and now - _cache["ts"] < _ROLE_ACTIONS_CACHE_TTL:
        return cached
    data = await loader()
    _cache["role_actions"] = data
    _cache["ts"] = now
    return data


async def get_ui_role_actions_cached(
    loader: Callable[[], Awaitable[dict[str, frozenset[str]]]],
) -> dict[str, frozenset[str]]:
    """读取 UI 权限点映射（缓存命中即返，未命中回源并写缓存）。"""
    now = time.monotonic()
    cached = _cache["ui_role_actions"]
    if cached is not None and now - _cache["ts"] < _ROLE_ACTIONS_CACHE_TTL:
        return cached
    data = await loader()
    _cache["ui_role_actions"] = data
    _cache["ts"] = now
    return data


def invalidate_role_actions_cache() -> None:
    """写操作后主动失效角色权限缓存（避免 60s TTL 内新配置不生效）。"""
    _cache["ts"] = 0.0
    _cache["role_actions"] = None
    _cache["ui_role_actions"] = None
