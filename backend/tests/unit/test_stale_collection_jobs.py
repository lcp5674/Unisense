"""worker 崩溃滞留任务清扫单测（H1 生产就绪）。

覆盖 ``stale_collection_jobs_task``：
- 对超时 RUNNING/QUEUED 任务补写 FAILED 终态 + 收尾 collection_run；
- 收尾时 flush 滞留任务的运行日志实时缓冲（worker 中断前已产生的
  进度日志不丢失）——回归：此前只 fail 不 flush，中断任务日志留 Redis。
"""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, patch

from app.tasks.stale_collection_jobs import stale_collection_jobs_task


def _ctx_with_redis() -> dict[str, object]:
    return {"redis": MagicMock()}


async def test_stale_task_flushes_run_logs_on_stale_run():
    """滞留 run 被收尾 FAILED 时，其 Redis 运行日志缓冲回写 DB。"""
    ctx = _ctx_with_redis()
    redis = ctx["redis"]
    store = MagicMock()
    store.stale_jobs = AsyncMock(
        return_value=[("collect:src1:abc", "RUNNING")]
    )
    store.set = AsyncMock()

    run = MagicMock(id=7)
    db = MagicMock()
    db.commit = AsyncMock()

    # async with async_session_factory() as db 上下文
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=db)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    repo = MagicMock()
    repo.find_collection_run_by_job_id = AsyncMock(return_value=run)
    repo.fail_collection_run = AsyncMock()
    repo.list_stale_running_runs = AsyncMock(return_value=[])

    with (
        patch("app.services.collector.queue.RedisJobStore", return_value=store),
        patch(
            "app.db.mysql.async_session_factory",
            return_value=session_cm,
        ),
        patch(
            "app.services.collector.repository.CollectorRepository",
            return_value=repo,
        ),
        patch(
            "app.services.collector.tasks._flush_run_logs",
            new=AsyncMock(),
        ) as flush,
    ):
        result = await stale_collection_jobs_task(ctx)

    assert result["cleaned"] == 1
    store.set.assert_awaited_once()
    repo.fail_collection_run.assert_awaited_once_with(7, ANY)
    # 收尾时 flush 滞留任务的运行日志（含中断原因 ERROR 条目）
    flush.assert_awaited_once_with(redis, ANY, 7, error=ANY)


async def test_stale_task_skips_without_redis():
    """无 Redis 时跳过（不抛错）。"""
    result = await stale_collection_jobs_task({})

    assert result["status"] == "SKIP"


async def test_stale_task_continues_on_mark_failure():
    """单任务标记 FAILED 失败不阻断其余清扫。"""
    ctx = _ctx_with_redis()
    store = MagicMock()
    store.stale_jobs = AsyncMock(
        return_value=[
            ("collect:src1:abc", "RUNNING"),
            ("collect:src2:def", "RUNNING"),
        ]
    )
    store.set = AsyncMock(side_effect=[RuntimeError("redis down"), None])

    db = MagicMock()
    db.commit = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=db)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    repo = MagicMock()
    repo.find_collection_run_by_job_id = AsyncMock(return_value=None)
    repo.list_stale_running_runs = AsyncMock(return_value=[])

    with (
        patch("app.services.collector.queue.RedisJobStore", return_value=store),
        patch(
            "app.db.mysql.async_session_factory",
            return_value=session_cm,
        ),
        patch(
            "app.services.collector.repository.CollectorRepository",
            return_value=repo,
        ),
        patch(
            "app.services.collector.tasks._flush_run_logs",
            new=AsyncMock(),
        ),
    ):
        result = await stale_collection_jobs_task(ctx)

    # 第一个标记失败被跳过（continue），第二个正常处理（无 run 不计数）
    assert result["cleaned"] == 0
    assert store.set.await_count == 2


async def test_stale_task_db_fallback_fails_orphan_running_runs():
    """JobStore key 已被 arq 清理时，DB 侧兜底仍收尾超时 RUNNING 记录。

    回归：worker 崩溃 + arq 清 key 后，JobStore 扫描扫不到，collection_run
    永久卡 RUNNING。DB 兜底按表扫描超时 RUNNING/QUEUED 并收尾 + flush 日志。
    """
    ctx = _ctx_with_redis()
    store = MagicMock()
    store.stale_jobs = AsyncMock(return_value=[])  # JobStore 无滞留

    db = MagicMock()
    db.commit = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=db)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    orphan_run = MagicMock(id=99)
    repo = MagicMock()
    repo.find_collection_run_by_job_id = AsyncMock(return_value=None)
    repo.list_stale_running_runs = AsyncMock(return_value=[orphan_run])
    repo.fail_collection_run = AsyncMock()

    with (
        patch("app.services.collector.queue.RedisJobStore", return_value=store),
        patch(
            "app.db.mysql.async_session_factory",
            return_value=session_cm,
        ),
        patch(
            "app.services.collector.repository.CollectorRepository",
            return_value=repo,
        ),
        patch(
            "app.services.collector.tasks._flush_run_logs",
            new=AsyncMock(),
        ) as flush,
    ):
        result = await stale_collection_jobs_task(ctx)

    assert result["cleaned"] == 1
    repo.list_stale_running_runs.assert_awaited_once()
    repo.fail_collection_run.assert_awaited_once_with(99, ANY)
    flush.assert_awaited_once_with(ctx["redis"], ANY, 99, error=ANY)
