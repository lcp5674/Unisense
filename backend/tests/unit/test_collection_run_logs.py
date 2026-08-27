"""采集运行日志链路单测（Redis 实时缓冲 + 终态回写 DB）。

覆盖采集记录详情页「实时日志」的三段链路：
- ``queue`` 工具：append_run_log / read_run_logs / delete_run_logs（Redis List 缓冲）
- ``tasks`` 回调：_make_progress_cb 带 run_id/redis 写日志、_make_run_log_cb（同步路径）、
  _flush_run_logs（终态回写 DB + 补充失败 ERROR 日志）
- ``service``：flush_run_logs（bulk 落库）、get_collection_run_logs（DB 优先 / Redis 兜底 /
  NotFound）

全部 mock 注入（无外部依赖），与 test_collector.py / test_collector_tasks.py 同风格。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.collector.queue import (
    append_run_log,
    delete_run_logs,
    read_run_logs,
    run_log_key,
)
from app.services.collector.tasks import (
    _flush_run_logs,
    _make_progress_cb,
    _make_run_log_cb,
)

# ---------- queue 工具 ----------


def test_run_log_key():
    assert run_log_key(42) == "collect:run_log:42"


async def test_append_run_log_writes_payload_and_first_ttl():
    redis = MagicMock()
    redis.rpush = AsyncMock(return_value=1)
    redis.expire = AsyncMock()

    await append_run_log(redis, 42, {"message": "开始采集 src", "phase": "start"})

    redis.rpush.assert_awaited_once()
    key, payload = redis.rpush.call_args.args
    assert key == "collect:run_log:42"
    assert "开始采集 src" in payload
    assert redis.expire.await_count == 1  # 首次写入设置 TTL（崩溃兜底）


async def test_append_run_log_skips_expire_on_non_first_write():
    redis = MagicMock()
    redis.rpush = AsyncMock(return_value=2)  # 非首次（已有列表）
    redis.expire = AsyncMock()

    await append_run_log(redis, 42, {"message": "注册 1/2", "phase": "registering"})

    assert redis.expire.await_count == 0


async def test_append_run_log_skips_empty_message():
    redis = MagicMock()
    redis.rpush = AsyncMock()

    await append_run_log(redis, 42, {"message": ""})

    redis.rpush.assert_not_awaited()


async def test_read_run_logs_decodes_bytes_and_paginates():
    import json

    redis = MagicMock()
    redis.llen = AsyncMock(return_value=3)
    redis.lrange = AsyncMock(
        return_value=[
            json.dumps({"ts": "t1", "level": "INFO", "message": "a"}).encode(),
            json.dumps({"ts": "t2", "level": "INFO", "message": "b"}).encode(),
        ]
    )

    items, total = await read_run_logs(redis, 42, 0, 2)

    assert total == 3
    assert [i["message"] for i in items] == ["a", "b"]
    redis.lrange.assert_awaited_once_with("collect:run_log:42", 0, 1)


async def test_read_run_logs_empty_when_offset_beyond():
    redis = MagicMock()
    redis.llen = AsyncMock(return_value=1)
    redis.lrange = AsyncMock()

    items, total = await read_run_logs(redis, 42, 5, 10)

    assert items == []
    assert total == 1
    redis.lrange.assert_not_awaited()


async def test_delete_run_logs_deletes_key():
    redis = MagicMock()
    redis.delete = AsyncMock()

    await delete_run_logs(redis, 42)

    redis.delete.assert_awaited_once_with("collect:run_log:42")


async def test_delete_run_logs_swallows_error():
    redis = MagicMock()
    redis.delete = AsyncMock(side_effect=RuntimeError("redis down"))

    # 不应抛出（清理失败仅记录）
    await delete_run_logs(redis, 42)


# ---------- tasks 回调 ----------


async def test_make_progress_cb_with_run_id_writes_run_log():
    store = MagicMock()
    store.set = AsyncMock()
    redis = MagicMock()
    redis.rpush = AsyncMock(return_value=1)

    cb = _make_progress_cb(
        store, "job1", "src1", 1, mode="FULL", run_id=42, redis=redis
    )
    await cb({"phase": "registering", "message": "注册 1/3：users", "entity_name": "users"})

    # JobStore 进度照常
    assert store.set.await_count == 1
    # Redis 运行日志缓冲追加
    redis.rpush.assert_awaited_once()
    payload = redis.rpush.call_args.args[1]
    assert "注册 1/3：users" in payload
    assert '"phase": "registering"' in payload


async def test_make_progress_cb_without_run_id_skips_run_log():
    store = MagicMock()
    store.set = AsyncMock()
    redis = MagicMock()
    redis.rpush = AsyncMock()

    cb = _make_progress_cb(store, "job1", "src1", 1, mode="FULL")
    await cb({"phase": "start", "message": "开始采集"})

    assert store.set.await_count == 1
    redis.rpush.assert_not_awaited()


async def test_make_run_log_cb_returns_none_without_redis_or_run_id():
    assert _make_run_log_cb(None, 42) is None
    assert _make_run_log_cb(MagicMock(), None) is None


async def test_make_run_log_cb_writes_redis_log():
    redis = MagicMock()
    redis.rpush = AsyncMock(return_value=1)

    cb = _make_run_log_cb(redis, 42)
    assert cb is not None
    await cb({"phase": "registering", "message": "注册 1/3：users"})

    redis.rpush.assert_awaited_once()
    assert "注册 1/3：users" in redis.rpush.call_args.args[1]


async def test_make_run_log_cb_skips_empty_message():
    redis = MagicMock()
    redis.rpush = AsyncMock()

    cb = _make_run_log_cb(redis, 42)
    await cb({"phase": "start", "message": ""})

    redis.rpush.assert_not_awaited()


async def test_flush_run_logs_writes_db_and_deletes_buffer():
    redis = MagicMock()
    redis.delete = AsyncMock()
    svc = MagicMock()
    svc.flush_run_logs = AsyncMock()
    redis_entries = [
        {"ts": "t1", "level": "INFO", "phase": "start", "message": "开始采集"},
        {"ts": "t2", "level": "INFO", "phase": "registering", "entity_name": "users", "message": "注册 1/3：users"},
    ]
    with patch(
        "app.services.collector.tasks.read_run_logs",
        AsyncMock(return_value=(redis_entries, 2)),
    ):
        await _flush_run_logs(redis, svc, 42, result={"failed_specs": []})

    # 缓冲全部回写 DB（含无失败明细时不追加 ERROR）
    entries = svc.flush_run_logs.call_args.args[1]
    assert len(entries) == 2
    redis.delete.assert_awaited_once_with("collect:run_log:42")


async def test_flush_run_logs_appends_failed_specs_as_error():
    redis = MagicMock()
    svc = MagicMock()
    svc.flush_run_logs = AsyncMock()
    with patch(
        "app.services.collector.tasks.read_run_logs",
        AsyncMock(return_value=([], 0)),
    ):
        await _flush_run_logs(
            redis,
            svc,
            42,
            result={
                "failed_specs": [
                    {"entity_name": "broken_tbl", "error": "权限不足"},
                ]
            },
        )

    entries = svc.flush_run_logs.call_args.args[1]
    assert len(entries) == 1
    assert entries[0]["level"] == "ERROR"
    assert entries[0]["entity_name"] == "broken_tbl"
    assert "权限不足" in entries[0]["message"]


async def test_flush_run_logs_appends_failure_error():
    redis = MagicMock()
    svc = MagicMock()
    svc.flush_run_logs = AsyncMock()
    with patch(
        "app.services.collector.tasks.read_run_logs",
        AsyncMock(return_value=([], 0)),
    ):
        await _flush_run_logs(redis, svc, 42, error="连接被拒绝")

    entries = svc.flush_run_logs.call_args.args[1]
    assert len(entries) == 1
    assert entries[0]["level"] == "ERROR"
    assert "连接被拒绝" in entries[0]["message"]


async def test_flush_run_logs_noop_without_redis_or_run_id():
    svc = MagicMock()
    svc.flush_run_logs = AsyncMock()

    await _flush_run_logs(None, svc, 42, error="x")
    await _flush_run_logs(MagicMock(), svc, None, error="x")
    await _flush_run_logs(MagicMock(), None, 42, error="x")

    svc.flush_run_logs.assert_not_awaited()


# ---------- service ----------


def _svc() -> tuple[object, MagicMock]:
    """构造服务并替换其仓库为 mock，返回 (service, mock_repo_instance)。"""
    from app.services.collector.service import CollectorService

    with patch("app.services.collector.service.CollectorRepository") as mock_repo:
        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.flush = AsyncMock()
        svc = CollectorService(db=db)
        return svc, mock_repo.return_value


async def test_service_flush_run_logs_bulk_writes_and_commits():
    svc, repo = _svc()
    repo.append_run_logs = AsyncMock()

    await svc.flush_run_logs(42, [{"ts": "t1", "message": "开始采集"}])

    repo.append_run_logs.assert_awaited_once_with(
        42, [{"ts": "t1", "message": "开始采集"}]
    )
    svc._db.commit.assert_awaited_once()


async def test_service_flush_run_logs_swallows_repo_error():
    svc, repo = _svc()
    repo.append_run_logs = AsyncMock(side_effect=RuntimeError("db down"))

    # 日志回写失败不应上抛（辅助能力）
    await svc.flush_run_logs(42, [{"message": "x"}])


async def test_service_get_run_logs_not_found():
    svc, repo = _svc()
    repo.get_collection_run = AsyncMock(return_value=None)

    with pytest.raises(Exception) as exc:
        await svc.get_collection_run_logs(999)
    assert "不存在" in str(exc.value)


async def test_service_get_run_logs_prefers_db_when_flushed():
    svc, repo = _svc()
    run = MagicMock(status="COMPLETED")
    repo.get_collection_run = AsyncMock(return_value=run)
    repo.has_run_logs = AsyncMock(return_value=True)
    row = MagicMock(ts=datetime(2026, 8, 27, tzinfo=UTC), level="INFO", phase="start", entity_name=None, message="开始采集")
    repo.list_run_logs = AsyncMock(return_value=([row], 1))

    result = await svc.get_collection_run_logs(42)

    assert result["source"] == "db"
    assert result["status"] == "COMPLETED"
    assert result["total"] == 1
    assert result["items"][0]["message"] == "开始采集"


async def test_service_get_run_logs_falls_back_to_redis_when_not_flushed():
    svc, repo = _svc()
    run = MagicMock(status="RUNNING")
    repo.get_collection_run = AsyncMock(return_value=run)
    repo.has_run_logs = AsyncMock(return_value=False)
    redis = MagicMock()
    with (
        patch(
            "app.services.collector.service._redis_available", return_value=True
        ),
        patch("app.services.collector.service.get_redis", return_value=redis),
        patch(
            "app.services.collector.queue.read_run_logs",
            AsyncMock(return_value=([{"ts": "t1", "message": "注册 1/3"}], 1)),
        ),
    ):
        result = await svc.get_collection_run_logs(42)

    assert result["source"] == "redis"
    assert result["status"] == "RUNNING"
    assert result["items"][0]["message"] == "注册 1/3"


async def test_service_get_run_logs_empty_without_redis():
    svc, repo = _svc()
    run = MagicMock(status="RUNNING")
    repo.get_collection_run = AsyncMock(return_value=run)
    repo.has_run_logs = AsyncMock(return_value=False)
    with patch("app.services.collector.service._redis_available", return_value=False):
        result = await svc.get_collection_run_logs(42)

    assert result["source"] == "none"
    assert result["items"] == []
    assert result["total"] == 0
