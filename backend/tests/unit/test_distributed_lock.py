"""分布式锁（CollectionLock）测试——重点覆盖续期与崩溃恢复语义。

背景：旧设计 acquire 用 1800s 长 TTL 且不续期，worker 被 SIGKILL 后
finally 的 release 不执行，锁残留 30 分钟阻塞同源全部采集（SKIPPED_CONCURRENT）。
修复后：TTL 120s + 任务内心跳续期（每 60s），崩溃后最多 2 分钟自然过期。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.services.collector.distributed_lock import CollectionLock
from app.services.collector.tasks import _lock_heartbeat


@pytest.mark.asyncio
async def test_refresh_extends_ttl_only_for_owner():
    """refresh 仅 owner 可续期（Lua 判等），续期成功后返回 True。"""
    mock_redis = AsyncMock()
    mock_redis.eval = AsyncMock(return_value=1)
    lock = CollectionLock(mock_redis)

    ok = await lock.refresh("src1", "job1", ttl=120)

    assert ok is True
    script, nkeys, key, owner, ms = mock_redis.eval.call_args.args
    assert key == "collect_lock:src1"
    assert owner == "job1"
    assert ms == 120_000


@pytest.mark.asyncio
async def test_refresh_returns_false_when_not_owner():
    """锁不属于该 owner 时续期失败（返回 False，不误续他人锁）。"""
    mock_redis = AsyncMock()
    mock_redis.eval = AsyncMock(return_value=0)
    lock = CollectionLock(mock_redis)

    assert await lock.refresh("src1", "job1", ttl=120) is False


@pytest.mark.asyncio
async def test_lock_heartbeat_refreshes_periodically():
    """心跳循环定期续期锁；任务取消后不再续期。"""
    mock_lock = AsyncMock()
    mock_lock.refresh = AsyncMock(return_value=True)

    task = asyncio.create_task(_lock_heartbeat(mock_lock, "src1", "job1", interval=0.01))
    await asyncio.sleep(0.05)  # 让心跳跑几轮
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert mock_lock.refresh.await_count >= 1  # 心跳已执行续期


@pytest.mark.asyncio
async def test_lock_heartbeat_refresh_failure_does_not_raise():
    """续期失败仅告警不抛异常（下次心跳再试），心跳不被中断。"""
    mock_lock = AsyncMock()
    mock_lock.refresh = AsyncMock(side_effect=RuntimeError("redis down"))

    task = asyncio.create_task(_lock_heartbeat(mock_lock, "src1", "job1", interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert mock_lock.refresh.await_count >= 1  # 失败未中断心跳
