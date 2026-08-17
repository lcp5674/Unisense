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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.collector.tasks import _check_idempotency, _make_progress_cb, run_collection_task

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
    redis.set = AsyncMock(return_value="OK")

    assert await _check_idempotency(redis, "job1") is True
    redis.set.assert_awaited_once_with(
        "collect_job_idempotent:job1", "COMPLETED", nx=True, ex=86400
    )


async def test_check_idempotency_already_completed_returns_false():
    redis = MagicMock()
    redis.set = AsyncMock(return_value=None)  # SET NX 返回 None → 已存在

    assert await _check_idempotency(redis, "job1") is False


async def test_check_idempotency_redis_error_allows_execution():
    redis = MagicMock()
    redis.set = AsyncMock(side_effect=RuntimeError("redis down"))

    assert await _check_idempotency(redis, "job1") is True


# ---------- 幂等跳过 ----------


async def test_run_idempotent_skip_returns_existing_detail():
    store = MagicMock()
    store.get = AsyncMock(return_value={"detail": {"status": "COMPLETED", "scanned": 4}})
    redis = MagicMock()
    redis.set = AsyncMock(return_value=None)

    result = await run_collection_task({"job_store": store, "redis": redis}, "src1", 1, "job1")

    assert result == {"status": "COMPLETED", "scanned": 4}
    store.get.assert_awaited_once_with("job1")


async def test_run_idempotent_skip_non_dict_detail_returns_empty():
    store = MagicMock()
    store.get = AsyncMock(return_value={"detail": "legacy"})
    redis = MagicMock()
    redis.set = AsyncMock(return_value=None)

    result = await run_collection_task({"job_store": store, "redis": redis}, "src1", 1, "job1")

    assert result == {}


async def test_run_idempotent_skip_without_existing_returns_flag():
    store = MagicMock()
    store.get = AsyncMock(return_value=None)
    redis = MagicMock()
    redis.set = AsyncMock(return_value=None)

    result = await run_collection_task({"job_store": store, "redis": redis}, "src1", 1, "job1")

    assert result == {"status": "IDEMPOTENT_SKIP"}


async def test_run_idempotent_skip_without_store_returns_flag():
    redis = MagicMock()
    redis.set = AsyncMock(return_value=None)

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
    redis.set = AsyncMock(return_value="OK")

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
