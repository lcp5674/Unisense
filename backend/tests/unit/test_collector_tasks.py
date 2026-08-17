"""采集 worker 任务体（app/services/collector/tasks.py）单测。

覆盖 ``run_collection_task`` 的全部关键分支：
- US4 幂等检查：Redis SET NX 首次执行 / 已完成 / 异常三种结果
- 幂等跳过：store 回读已有 detail（dict/非 dict）/ 无 detail / 无 store
- 成功路径：ctx 注入 svc / 注入 db+collector / 生产默认自建会话与采集器
- 失败路径：健康状态回写（成功 / 异常告警）/ FAILED 状态回写 / 上抛重试
- finally：own_session 关闭会话与 ``collector.dispose``
"""

# ruff: noqa: SIM117  # 测试中嵌套 with（patch 外层 + pytest.raises 内层）语义更清晰

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.collector.tasks import (
    _check_idempotency,
    _make_progress_cb,
    _mark_idempotent_completed,
    run_collection_task,
)

# ---------- US4: 幂等检查 ----------


async def test_make_progress_cb_detail_keeps_mode():
    """M4: RUNNING 进度 detail 保留真实执行模式（不被进度字段覆盖）。"""
    store = MagicMock()
    store.set = AsyncMock()

    cb = _make_progress_cb(store, "job1", "src1", 1, mode="INCREMENTAL")
    await cb({"phase": "registering", "message": "注册 1/3", "index": 1, "total": 3})

    detail = store.set.call_args.args[2]
    assert detail["mode"] == "INCREMENTAL"
    assert detail["source_id"] == "src1"
    assert detail["progress"]["phase"] == "registering"


async def test_check_idempotency_no_redis_returns_true():
    assert await _check_idempotency(None, "job1") is True


async def test_check_idempotency_first_run_returns_true():
    redis = MagicMock()
    redis.exists = AsyncMock(return_value=0)  # 幂等键不存在 → 首次可执行

    assert await _check_idempotency(redis, "job1") is True
    redis.exists.assert_awaited_once_with("collect_job_idempotent:job1")


async def test_check_idempotency_already_completed_returns_false():
    redis = MagicMock()
    redis.exists = AsyncMock(return_value=1)  # 幂等键存在 → 已成功完成，跳过

    assert await _check_idempotency(redis, "job1") is False


async def test_check_idempotency_redis_error_allows_execution():
    redis = MagicMock()
    redis.exists = AsyncMock(side_effect=RuntimeError("redis down"))

    assert await _check_idempotency(redis, "job1") is True


async def test_mark_idempotent_completed_sets_7d_ttl():
    """m2: 成功后标记幂等键，TTL 与终态（7 天）对齐而非 24h。"""
    redis = MagicMock()
    redis.set = AsyncMock()

    await _mark_idempotent_completed(redis, "job1")

    redis.set.assert_awaited_once_with(
        "collect_job_idempotent:job1", "COMPLETED", ex=7 * 24 * 60 * 60
    )


async def test_mark_idempotent_completed_no_redis_is_noop():
    assert await _mark_idempotent_completed(None, "job1") is None


# ---------- 幂等跳过 ----------


async def test_run_idempotent_skip_returns_existing_detail():
    store = MagicMock()
    store.get = AsyncMock(return_value={"detail": {"status": "COMPLETED", "scanned": 4}})
    redis = MagicMock()
    redis.exists = AsyncMock(return_value=1)  # 已成功完成 → 跳过执行

    result = await run_collection_task({"job_store": store, "redis": redis}, "src1", 1, "job1")

    assert result == {"status": "COMPLETED", "scanned": 4}
    store.get.assert_awaited_once_with("job1")


async def test_run_idempotent_skip_non_dict_detail_returns_empty():
    store = MagicMock()
    store.get = AsyncMock(return_value={"detail": "legacy"})
    redis = MagicMock()
    redis.exists = AsyncMock(return_value=1)  # 已成功完成 → 跳过执行

    result = await run_collection_task({"job_store": store, "redis": redis}, "src1", 1, "job1")

    assert result == {}


async def test_run_idempotent_skip_without_existing_returns_flag():
    store = MagicMock()
    store.get = AsyncMock(return_value=None)
    redis = MagicMock()
    redis.exists = AsyncMock(return_value=1)  # 已成功完成 → 跳过执行

    result = await run_collection_task({"job_store": store, "redis": redis}, "src1", 1, "job1")

    assert result == {"status": "IDEMPOTENT_SKIP"}


async def test_run_idempotent_skip_without_store_returns_flag():
    redis = MagicMock()
    redis.exists = AsyncMock(return_value=1)  # 已成功完成 → 跳过执行

    result = await run_collection_task({"redis": redis}, "src1", 1, "job1")

    assert result == {"status": "IDEMPOTENT_SKIP"}


# ---------- 成功路径 ----------


async def test_run_success_with_injected_svc_db_collector():
    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.complete_collection_run = AsyncMock()
    svc.fail_collection_run = AsyncMock()
    svc.collect_and_register = AsyncMock(return_value={"scanned": 2, "source_id": "src1"})
    db = MagicMock()
    db.commit = AsyncMock()
    collector = MagicMock()
    store = MagicMock()
    store.set = AsyncMock()

    result = await run_collection_task(
        {"svc": svc, "db": db, "collector": collector, "job_store": store}, "src1", 1, "job1"
    )

    assert result == {"scanned": 2, "source_id": "src1"}
    svc.collect_and_register.assert_awaited_once()
    call = svc.collect_and_register.await_args
    assert call.args[:3] == ("src1", collector, 1)
    assert call.kwargs["mode"] == "FULL"
    # 注入 job_store 时须传入进度回调（供 SSE 实时推送）
    assert callable(call.kwargs["progress_cb"])
    db.commit.assert_awaited_once()
    store.set.assert_awaited_once_with("job1", "COMPLETED", result)
    # 注入会话非 own_session：不得关闭会话或采集器
    db.close.assert_not_called()
    collector.dispose.assert_not_called()


async def test_run_success_builds_service_from_injected_db():
    db = MagicMock()
    db.commit = AsyncMock()
    collector = MagicMock()
    collector.dispose = AsyncMock()
    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.complete_collection_run = AsyncMock()
    svc.fail_collection_run = AsyncMock()
    svc.collect_and_register = AsyncMock(return_value={"scanned": 3})

    with patch("app.services.collector.tasks.CollectorService", return_value=svc) as m_svc:
        result = await run_collection_task({"db": db, "collector": collector}, "src1", 1, "job1")

    assert result == {"scanned": 3}
    m_svc.assert_called_once_with(db)
    db.close.assert_not_called()


async def test_run_success_without_db_skips_commit():
    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.complete_collection_run = AsyncMock()
    svc.fail_collection_run = AsyncMock()
    svc.collect_and_register = AsyncMock(return_value={"scanned": 1})
    collector = MagicMock()
    store = MagicMock()
    store.set = AsyncMock()

    result = await run_collection_task(
        {"svc": svc, "collector": collector, "job_store": store}, "src1", 1, "job1"
    )

    assert result == {"scanned": 1}
    store.set.assert_awaited_once_with("job1", "COMPLETED", {"scanned": 1})


async def test_run_success_without_store_returns_result():
    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.complete_collection_run = AsyncMock()
    svc.fail_collection_run = AsyncMock()
    svc.collect_and_register = AsyncMock(return_value={"scanned": 1})
    db = MagicMock()
    db.commit = AsyncMock()
    collector = MagicMock()

    result = await run_collection_task(
        {"svc": svc, "db": db, "collector": collector}, "src1", 1, "job1"
    )

    assert result == {"scanned": 1}
    db.commit.assert_awaited_once()


async def test_run_success_production_path_builds_session_and_collector():
    """生产默认路径：ctx 无 db/collector/svc → 自建会话与采集器，own_session 管理生命周期。"""
    src = MagicMock()
    src.source_type = "mysql"
    src.connection_config = "enc:cfg"

    db = MagicMock()
    db.commit = AsyncMock()
    db.close = AsyncMock()

    collector = MagicMock()
    collector.dispose = AsyncMock()

    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.complete_collection_run = AsyncMock()
    svc.fail_collection_run = AsyncMock()
    svc.collect_and_register = AsyncMock(return_value={"scanned": 5})

    repo = MagicMock()
    repo.get_source = AsyncMock(return_value=src)

    store = MagicMock()
    store.set = AsyncMock()

    redis = MagicMock()
    redis.exists = AsyncMock(return_value=0)  # 首次执行（幂等键不存在）
    redis.set = AsyncMock()

    with (
        patch("app.services.collector.tasks.async_session_factory", return_value=db) as m_fac,
        patch("app.services.collector.tasks.CollectorRepository", return_value=repo) as m_repo,
        patch("app.services.collector.tasks.build_collector", return_value=collector) as m_build,
        patch("app.services.collector.tasks.CollectorService", return_value=svc) as m_svc,
    ):
        result = await run_collection_task({"job_store": store, "redis": redis}, "src1", 1, "job1")

    assert result == {"scanned": 5}
    m_fac.assert_called_once_with()
    m_repo.assert_called_once_with(db)
    m_build.assert_called_once_with("mysql", "enc:cfg")
    m_svc.assert_called_once_with(db)
    db.commit.assert_awaited_once()
    store.set.assert_awaited_once_with("job1", "COMPLETED", {"scanned": 5})
    # own_session=True → finally 关闭会话并释放采集器
    db.close.assert_awaited_once()
    collector.dispose.assert_awaited_once()


# ---------- 失败路径 ----------


async def test_run_failure_writes_health_and_failed_status():
    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.complete_collection_run = AsyncMock()
    svc.fail_collection_run = AsyncMock()
    svc.collect_and_register = AsyncMock(side_effect=RuntimeError("boom"))
    db = MagicMock()
    db.commit = AsyncMock()
    repo = MagicMock()
    repo.update_health_status = AsyncMock()
    store = MagicMock()
    store.set = AsyncMock()

    with patch("app.services.collector.tasks.CollectorRepository", return_value=repo):
        with pytest.raises(RuntimeError, match="boom"):
            await run_collection_task(
                {"svc": svc, "db": db, "job_store": store, "collector": MagicMock()},
                "src1",
                1,
                "job1",
            )

    repo.update_health_status.assert_awaited_once_with("src1", "unhealthy", error="boom")
    db.commit.assert_awaited_once()
    store.set.assert_awaited_once_with(
        "job1", "FAILED", {"source_id": "src1", "actor_id": 1, "error": "boom"}
    )


async def test_run_failure_health_update_error_swallowed():
    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.complete_collection_run = AsyncMock()
    svc.fail_collection_run = AsyncMock()
    svc.collect_and_register = AsyncMock(side_effect=RuntimeError("boom"))
    db = MagicMock()
    db.commit = AsyncMock()
    repo = MagicMock()
    repo.update_health_status = AsyncMock(side_effect=RuntimeError("health write failed"))
    store = MagicMock()
    store.set = AsyncMock()

    with patch("app.services.collector.tasks.CollectorRepository", return_value=repo):
        with pytest.raises(RuntimeError, match="boom"):
            await run_collection_task(
                {"svc": svc, "db": db, "job_store": store, "collector": MagicMock()},
                "src1",
                1,
                "job1",
            )

    # 健康状态回写失败仅告警，不影响 FAILED 回写与上抛
    store.set.assert_awaited_once_with(
        "job1", "FAILED", {"source_id": "src1", "actor_id": 1, "error": "boom"}
    )


async def test_run_failure_without_db_skips_health_update():
    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.complete_collection_run = AsyncMock()
    svc.fail_collection_run = AsyncMock()
    svc.collect_and_register = AsyncMock(side_effect=RuntimeError("boom"))
    store = MagicMock()
    store.set = AsyncMock()

    with pytest.raises(RuntimeError, match="boom"):
        await run_collection_task(
            {"svc": svc, "job_store": store, "collector": MagicMock()}, "src1", 1, "job1"
        )

    store.set.assert_awaited_once_with(
        "job1", "FAILED", {"source_id": "src1", "actor_id": 1, "error": "boom"}
    )


async def test_run_failure_without_store_reraises():
    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.complete_collection_run = AsyncMock()
    svc.fail_collection_run = AsyncMock()
    svc.collect_and_register = AsyncMock(side_effect=RuntimeError("boom"))
    db = MagicMock()
    db.commit = AsyncMock()
    repo = MagicMock()
    repo.update_health_status = AsyncMock()

    with patch("app.services.collector.tasks.CollectorRepository", return_value=repo):
        with pytest.raises(RuntimeError, match="boom"):
            await run_collection_task(
                {"svc": svc, "db": db, "collector": MagicMock()}, "src1", 1, "job1"
            )


async def test_run_failure_production_path_source_not_found():
    db = MagicMock()
    db.close = AsyncMock()
    repo = MagicMock()
    repo.get_source = AsyncMock(return_value=None)
    repo.update_health_status = AsyncMock()
    store = MagicMock()
    store.set = AsyncMock()

    with (
        patch("app.services.collector.tasks.async_session_factory", return_value=db),
        patch("app.services.collector.tasks.CollectorRepository", return_value=repo),
    ):
        with pytest.raises(RuntimeError, match="数据源不存在: src1"):
            await run_collection_task({"job_store": store}, "src1", 1, "job1")

    repo.update_health_status.assert_awaited_once()
    store.set.assert_awaited_once_with(
        "job1", "FAILED", {"source_id": "src1", "actor_id": 1, "error": "数据源不存在: src1"}
    )
    # own_session=True 但采集器尚未构建 → 只关闭会话，不 dispose
    db.close.assert_awaited_once()


async def test_run_failure_own_session_closes_and_disposes_collector():
    src = MagicMock()
    src.source_type = "mysql"
    src.connection_config = "cfg"

    db = MagicMock()
    db.close = AsyncMock()

    collector = MagicMock()
    collector.dispose = AsyncMock()

    repo = MagicMock()
    repo.get_source = AsyncMock(return_value=src)
    repo.update_health_status = AsyncMock()

    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.complete_collection_run = AsyncMock()
    svc.fail_collection_run = AsyncMock()
    svc.collect_and_register = AsyncMock(side_effect=RuntimeError("collect failed"))

    store = MagicMock()
    store.set = AsyncMock()

    with (
        patch("app.services.collector.tasks.async_session_factory", return_value=db),
        patch("app.services.collector.tasks.CollectorRepository", return_value=repo),
        patch("app.services.collector.tasks.build_collector", return_value=collector),
        patch("app.services.collector.tasks.CollectorService", return_value=svc),
    ):
        with pytest.raises(RuntimeError, match="collect failed"):
            await run_collection_task({"job_store": store}, "src1", 1, "job1")

    db.close.assert_awaited_once()
    collector.dispose.assert_awaited_once()
    store.set.assert_awaited_once_with(
        "job1", "FAILED", {"source_id": "src1", "actor_id": 1, "error": "collect failed"}
    )


async def test_run_raises_when_collector_unavailable():
    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.complete_collection_run = AsyncMock()
    svc.fail_collection_run = AsyncMock()
    svc.collect_and_register = AsyncMock(return_value={})
    db = MagicMock()
    db.commit = AsyncMock()
    repo = MagicMock()
    repo.update_health_status = AsyncMock()
    store = MagicMock()
    store.set = AsyncMock()

    with patch("app.services.collector.tasks.CollectorRepository", return_value=repo):
        with pytest.raises(RuntimeError, match="采集器不可用: src1"):
            await run_collection_task({"svc": svc, "db": db, "job_store": store}, "src1", 1, "job1")

    store.set.assert_awaited_once_with(
        "job1", "FAILED", {"source_id": "src1", "actor_id": 1, "error": "采集器不可用: src1"}
    )


# ---------- m2: 成功后才标记幂等 ----------


async def test_run_success_marks_idempotent_key():
    """m2: 成功路径在 COMPLETED 回写后标记幂等键（7 天 TTL）；崩溃/失败不标记。"""
    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.complete_collection_run = AsyncMock()
    svc.fail_collection_run = AsyncMock()
    svc.collect_and_register = AsyncMock(return_value={"scanned": 2})
    db = MagicMock()
    db.commit = AsyncMock()
    collector = MagicMock()
    store = MagicMock()
    store.set = AsyncMock()
    redis = MagicMock()
    redis.exists = AsyncMock(return_value=0)
    redis.set = AsyncMock()
    redis.eval = AsyncMock(return_value=1)

    await run_collection_task(
        {"svc": svc, "db": db, "collector": collector, "job_store": store, "redis": redis},
        "src1",
        1,
        "job1",
    )

    # 幂等标记为最后一次 redis.set（此前 acquire 同源锁也用 redis.set(nx=True)）
    redis.set.assert_awaited_with(
        "collect_job_idempotent:job1", "COMPLETED", ex=7 * 24 * 60 * 60
    )


async def test_run_failure_does_not_mark_idempotent():
    """m2: 失败路径不标记幂等键——arq 重试同一 job_id 可重新执行。"""
    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.complete_collection_run = AsyncMock()
    svc.fail_collection_run = AsyncMock()
    svc.collect_and_register = AsyncMock(side_effect=RuntimeError("boom"))
    db = MagicMock()
    db.commit = AsyncMock()
    repo = MagicMock()
    repo.update_health_status = AsyncMock()
    store = MagicMock()
    store.set = AsyncMock()
    redis = MagicMock()
    redis.exists = AsyncMock(return_value=0)
    redis.set = AsyncMock()
    redis.eval = AsyncMock(return_value=1)

    with patch("app.services.collector.tasks.CollectorRepository", return_value=repo):
        with pytest.raises(RuntimeError, match="boom"):
            await run_collection_task(
                {
                    "svc": svc,
                    "db": db,
                    "job_store": store,
                    "redis": redis,
                    "collector": MagicMock(),
                },
                "src1",
                1,
                "job1",
            )

    # 失败只回写 FAILED，不写幂等键（acquire 锁的 set 不含 COMPLETED 标记）
    idempotent_calls = [c for c in redis.set.call_args_list if "COMPLETED" in (c.args or ())]
    assert idempotent_calls == []


async def test_run_cancelled_writes_failed_and_reraises():
    """P0-3: arq 超时（CancelledError 是 BaseException）也补写 FAILED 终态并重抛。"""
    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.fail_collection_run = AsyncMock()
    svc.collect_and_register = AsyncMock(side_effect=asyncio.CancelledError())
    db = MagicMock()
    db.commit = AsyncMock()
    repo = MagicMock()
    repo.update_health_status = AsyncMock()
    store = MagicMock()
    store.set = AsyncMock()

    with patch("app.services.collector.tasks.CollectorRepository", return_value=repo):
        with pytest.raises(asyncio.CancelledError):
            await run_collection_task(
                {"svc": svc, "db": db, "job_store": store, "collector": MagicMock()},
                "src1",
                1,
                "job1",
            )

    # 副作用收尾全部执行（运行记录 FAILED + 健康 unhealthy + JobStore 终态）
    svc.fail_collection_run.assert_awaited_once_with(1, "采集超时或任务取消")
    repo.update_health_status.assert_awaited_once_with(
        "src1", "unhealthy", error="采集超时或任务取消"
    )
    store.set.assert_awaited_once_with(
        "job1",
        "FAILED",
        {"source_id": "src1", "actor_id": 1, "error": "采集超时或任务取消"},
    )


async def test_run_concurrent_lock_skips_when_acquire_fails():
    """P1-5: 同源已有采集任务（锁被占用）→ 任务 SKIPPED，不执行采集。"""
    svc = MagicMock()
    svc.collect_and_register = AsyncMock()
    db = MagicMock()
    store = MagicMock()
    store.set = AsyncMock()
    redis = MagicMock()
    # acquire 用 SET NX：返回 None 表示锁被占用（未获取）
    redis.set = AsyncMock(return_value=None)
    redis.eval = AsyncMock(return_value=1)

    result = await run_collection_task(
        {"svc": svc, "db": db, "job_store": store, "redis": redis, "collector": MagicMock()},
        "src1",
        1,
        "job1",
    )

    assert result == {"status": "SKIPPED_CONCURRENT"}
    svc.collect_and_register.assert_not_called()
    store.set.assert_awaited_once_with(
        "job1",
        "SKIPPED",
        {"source_id": "src1", "actor_id": 1, "error": "同源已有采集任务在运行"},
    )


async def test_run_concurrent_lock_released_on_success():
    """P1-5: 成功路径 finally 释放同源锁（仅 owner 可释放）。"""
    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.complete_collection_run = AsyncMock()
    svc.collect_and_register = AsyncMock(return_value={"scanned": 2})
    db = MagicMock()
    db.commit = AsyncMock()
    store = MagicMock()
    store.set = AsyncMock()
    redis = MagicMock()
    redis.exists = AsyncMock(return_value=0)
    redis.set = AsyncMock(return_value=True)  # acquire 成功
    redis.eval = AsyncMock(return_value=1)

    await run_collection_task(
        {"svc": svc, "db": db, "collector": MagicMock(), "job_store": store, "redis": redis},
        "src1",
        1,
        "job1",
    )

    # 锁已释放：eval 释放脚本被调用（key=collect_lock:src1，owner=job1）
    release_calls = [
        c for c in redis.eval.call_args_list if "collect_lock:src1" in (c.args or ())
    ]
    assert release_calls, "同源锁应在 finally 中释放"


async def test_run_retries_transient_error_then_succeeds():
    """P1-7: 瞬时错误（ExternalDependencyError）自动重试后成功。"""
    from app.core.exceptions import ExternalDependencyError

    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.complete_collection_run = AsyncMock()
    svc.fail_collection_run = AsyncMock()
    # 第一次抛瞬时错误，第二次成功
    svc.collect_and_register = AsyncMock(
        side_effect=[ExternalDependencyError("db down"), {"scanned": 2, "registered": 2}]
    )
    db = MagicMock()
    db.commit = AsyncMock()
    store = MagicMock()
    store.set = AsyncMock()

    with patch("app.services.collector.tasks.asyncio.sleep", AsyncMock()):
        result = await run_collection_task(
            {"svc": svc, "db": db, "job_store": store, "collector": MagicMock()},
            "src1",
            1,
            "job1",
        )

    assert result["registered"] == 2
    assert svc.collect_and_register.await_count == 2  # 重试 1 次


async def test_run_retry_exhausted_raises():
    """P1-7: 瞬时错误重试耗尽后上抛，任务最终 FAILED。"""
    from app.core.exceptions import ExternalDependencyError

    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.fail_collection_run = AsyncMock()
    svc.collect_and_register = AsyncMock(
        side_effect=ExternalDependencyError("db down")
    )
    db = MagicMock()
    db.commit = AsyncMock()
    repo = MagicMock()
    repo.update_health_status = AsyncMock()
    store = MagicMock()
    store.set = AsyncMock()

    with patch("app.services.collector.tasks.CollectorRepository", return_value=repo):
        with patch("app.services.collector.tasks.asyncio.sleep", AsyncMock()):
            with pytest.raises(ExternalDependencyError):
                await run_collection_task(
                    {"svc": svc, "db": db, "job_store": store, "collector": MagicMock()},
                    "src1",
                    1,
                    "job1",
                )

    # 首次 + 2 次重试 = 3 次尝试
    assert svc.collect_and_register.await_count == 3
    store.set.assert_awaited_once_with(
        "job1",
        "FAILED",
        {"source_id": "src1", "actor_id": 1, "error": "db down"},
    )


async def test_run_does_not_retry_business_error():
    """P1-7: 业务错误（非瞬时）不重试，直接失败。"""
    svc = MagicMock()
    svc.start_collection_run = AsyncMock(return_value=1)
    svc.fail_collection_run = AsyncMock()
    svc.collect_and_register = AsyncMock(side_effect=ValueError("bad config"))
    db = MagicMock()
    db.commit = AsyncMock()
    repo = MagicMock()
    repo.update_health_status = AsyncMock()
    store = MagicMock()
    store.set = AsyncMock()

    with patch("app.services.collector.tasks.CollectorRepository", return_value=repo):
        with pytest.raises(ValueError, match="bad config"):
            await run_collection_task(
                {"svc": svc, "db": db, "job_store": store, "collector": MagicMock()},
                "src1",
                1,
                "job1",
            )

    assert svc.collect_and_register.await_count == 1  # 不重试
