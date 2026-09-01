"""governance 角色权限缓存（多 worker 版本化）测试。

覆盖：缓存命中复用、失效后回源、Redis 版本号传播（模拟第二 worker 感知变更）、
Redis 不可用降级（本地失效 + TTL 兜底，不阻断）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.governance import cache as cache_mod


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    cache_mod._cache.update(ts=0.0, role_actions=None, ui_role_actions=None, version=0)
    cache_mod._local_version.update(value=0, checked=0.0)
    yield


async def _loader(value: dict[str, frozenset[str]] | None = None) -> dict[str, frozenset[str]]:
    return value or {"viewer": frozenset({"read"})}


async def test_cache_hit_reuses_loader_result() -> None:
    calls = 0

    async def loader() -> dict[str, frozenset[str]]:
        nonlocal calls
        calls += 1
        return {"viewer": frozenset({"read"})}

    first = await cache_mod.get_role_actions_cached(loader)
    second = await cache_mod.get_role_actions_cached(loader)
    assert first == second == {"viewer": frozenset({"read"})}
    assert calls == 1  # 二次命中缓存，loader 只执行一次


async def test_invalidate_forces_reload() -> None:
    calls = 0

    async def loader() -> dict[str, frozenset[str]]:
        nonlocal calls
        calls += 1
        return {"viewer": frozenset({"read"})}

    await cache_mod.get_role_actions_cached(loader)
    await cache_mod.invalidate_role_actions_cache()
    await cache_mod.get_role_actions_cached(loader)
    assert calls == 2  # 失效后回源


async def test_version_change_propagates_to_second_worker() -> None:
    """模拟两个 worker：worker A 写操作 bump 版本号后，worker B 在版本检查窗口内感知并回源。"""
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.incr = AsyncMock(return_value=1)
    with patch("app.db.redis.get_redis", return_value=redis):
        # worker B 首次加载（版本 0）
        b_calls = 0

        async def b_loader() -> dict[str, frozenset[str]]:
            nonlocal b_calls
            b_calls += 1
            return {"viewer": frozenset({"read"})}

        await cache_mod.get_role_actions_cached(b_loader)
        assert b_calls == 1

        # worker A 写操作失效（Redis incr 到 1）
        await cache_mod.invalidate_role_actions_cache()
        redis.incr.assert_awaited_once_with("unisense:role_actions:version")

        # worker B 第二次读取：Redis 版本已变 → 回源（b_calls=2）
        redis.get = AsyncMock(return_value="1")
        await cache_mod.get_role_actions_cached(b_loader)
        assert b_calls == 2


async def test_redis_unavailable_fallback_does_not_block() -> None:
    """Redis 不可用（get_redis 抛 RuntimeError）时：读取/失效均不抛异常，本地缓存仍可用。"""
    async def loader() -> dict[str, frozenset[str]]:
        return {"viewer": frozenset({"read"})}

    with patch("app.db.redis.get_redis", side_effect=RuntimeError("not initialized")):
        data = await cache_mod.get_role_actions_cached(loader)
        assert data == {"viewer": frozenset({"read"})}
        await cache_mod.invalidate_role_actions_cache()  # 不应抛异常
        data2 = await cache_mod.get_role_actions_cached(loader)
        assert data2 == {"viewer": frozenset({"read"})}
