"""InferInflightGuard 单测（描述推断 in-flight 去重，TD §12.1 / FR-023）。

覆盖：Redis SETNX 成功/占用/异常降级、进程内 acquire/release/owner 校验、
TTL 自动过期、is_locked。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.collector.infer_guard as guard_mod
from app.services.collector.infer_guard import InferInflightGuard


async def test_redis_acquire_success_and_release() -> None:
    """Redis 可用：acquire 返回 True（SET NX EX），release 用 Lua 仅 owner 可释放。"""
    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)
    redis.eval = AsyncMock(return_value=1)
    guard = InferInflightGuard(redis)
    assert await guard.acquire("column", 1, "id", owner="a") is True
    redis.set.assert_awaited_once_with("infer_inflight:column:1:id", "a", nx=True, ex=120)
    assert await guard.release("column", 1, "id", owner="a") is True
    redis.eval.assert_awaited_once()


async def test_redis_acquire_busy() -> None:
    """Redis 已占用（SET 返回 None）：acquire 返回 False。"""
    redis = MagicMock()
    redis.set = AsyncMock(return_value=None)
    guard = InferInflightGuard(redis)
    assert await guard.acquire("table", 2, owner="a") is False


async def test_redis_error_falls_back_to_local() -> None:
    """Redis 异常：降级为进程内去重（不阻断推断主流程）。"""
    redis = MagicMock()
    redis.set = AsyncMock(side_effect=RuntimeError("redis down"))
    guard = InferInflightGuard(redis)
    assert await guard.acquire("table", 3, owner="a") is True
    # 同一 key 未释放前再 acquire 被拦截
    assert await guard.acquire("table", 3, owner="b") is False


async def test_local_acquire_release_owner_check() -> None:
    """进程内降级：非 owner 不能释放，owner 释放后可重新 acquire。"""
    guard = InferInflightGuard(None)
    assert await guard.acquire("column", 4, "name", owner="a") is True
    assert await guard.is_locked("column", 4, "name") is True
    # 非 owner 释放失败，锁仍在
    assert await guard.release("column", 4, "name", owner="b") is False
    assert await guard.is_locked("column", 4, "name") is True
    # owner 释放成功
    assert await guard.release("column", 4, "name", owner="a") is True
    assert await guard.is_locked("column", 4, "name") is False
    # 释放后可重新 acquire
    assert await guard.acquire("column", 4, "name", owner="a") is True
    await guard.release("column", 4, "name", owner="a")


async def test_local_ttl_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    """进程内 TTL 过期后自动放行（time.monotonic 前移超过 TTL）。"""
    guard = InferInflightGuard(None)
    real_monotonic = guard_mod.time.monotonic
    calls = 0

    def fake_monotonic() -> float:
        nonlocal calls
        calls += 1
        if calls >= 2:
            return real_monotonic() + 200
        return real_monotonic()

    monkeypatch.setattr(guard_mod.time, "monotonic", fake_monotonic)
    assert await guard.acquire("table", 5, owner="a") is True
    # 第二次 acquire（时间已过 TTL）应覆盖成功
    assert await guard.acquire("table", 5, owner="b") is True
    await guard.release("table", 5, owner="b")


async def test_column_and_table_scope_isolated() -> None:
    """不同粒度（column 与 table）锁相互独立，互不阻塞。"""
    guard = InferInflightGuard(None)
    assert await guard.acquire("column", 6, "id", owner="a") is True
    # 同 catalog 的批量/表级推断不受列级锁影响
    assert await guard.acquire("batch", 6, owner="a") is True
    assert await guard.acquire("table", 6, owner="a") is True
    # 同粒度同范围仍被拦截
    assert await guard.acquire("column", 6, "id", owner="b") is False
    await guard.release("column", 6, "id", owner="a")
    await guard.release("batch", 6, owner="a")
    await guard.release("table", 6, owner="a")
