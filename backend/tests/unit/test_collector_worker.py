"""采集 worker / arq 队列测试（P0-7 定时调度、P1-4 arq job 状态查询）。

覆盖：
- collect_scheduler：cron 表达式匹配到点 → 投递 run_collection_task（job_id 含时间戳幂等）
- collect_scheduler：cron 解析失败不阻断其他源
- ArqCollectionQueue.enqueue：写入初始 QUEUED 状态到 RedisJobStore
- ArqCollectionQueue.get：委托 RedisJobStore 查询任务状态
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.collector.queue import ArqCollectionQueue


class _FakeCronIter:
    """croniter 替身：下一次执行在当前时刻 10 秒后（1 分钟触发窗口内）。"""

    def __init__(self, expr, dt) -> None:
        self._expr = expr
        self._dt = dt

    def get_next(self, dt_type):
        return datetime.now(UTC) + timedelta(seconds=10)


class _FarCronIter:
    """croniter 替身：下一次执行在 2 小时后（窗口外，不触发）。"""

    def __init__(self, expr, dt) -> None:
        self._expr = expr
        self._dt = dt

    def get_next(self, dt_type):
        return datetime.now(UTC) + timedelta(hours=2)


async def test_scheduler_triggers_source_in_window():
    """cron 下一次执行在 1 分钟窗口内 → 投递采集任务。"""
    from app.services.collector.worker import collect_scheduler

    src = MagicMock()
    src.source_id = "src1"
    src.schedule_cron = "*/5 * * * *"
    src.created_by = 7
    src.collection_mode = "INCREMENTAL"

    redis = MagicMock()
    redis.enqueue_job = AsyncMock()

    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    repo = MagicMock()
    repo.list_scheduled_sources = AsyncMock(return_value=[src])

    with (
        patch("app.db.mysql.async_session_factory") as m_fac,
        patch("app.services.collector.worker.croniter", _FakeCronIter),
        patch("app.services.collector.worker.CollectorRepository", return_value=repo),
    ):
        m_fac.return_value = db
        await collect_scheduler({"redis": redis})

    redis.enqueue_job.assert_awaited_once()
    args = redis.enqueue_job.call_args
    assert args.args[0] == "run_collection_task"
    assert args.args[1] == "src1"
    # 定时调度 actor 语义为 NULL（collection_run 约定），不归因给源创建人
    assert args.args[2] is None
    # job_id 必须作为第 4 位置参数（幂等键 + 状态回写），且与 _job_id 一致
    assert args.args[3] == args.kwargs["_job_id"]
    assert args.kwargs["_job_id"].startswith("collect:sched:src1:")
    # mode 读取源配置的 collection_mode，定时链路同样尊重 INCREMENTAL（M4）
    assert args.kwargs["mode"] == "INCREMENTAL"
    # arq 0.28 不支持 _max_tries/_timeout，不得透传
    assert "_max_tries" not in args.kwargs
    assert "_timeout" not in args.kwargs


async def test_scheduler_skips_source_outside_window():
    """cron 下一次执行超出 1 分钟窗口 → 不投递。"""
    from app.services.collector.worker import collect_scheduler

    src = MagicMock()
    src.source_id = "src1"
    src.schedule_cron = "0 3 * * *"
    src.created_by = 1

    redis = MagicMock()
    redis.enqueue_job = AsyncMock()

    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    repo = MagicMock()
    repo.list_scheduled_sources = AsyncMock(return_value=[src])

    with (
        patch("app.db.mysql.async_session_factory") as m_fac,
        patch("app.services.collector.worker.croniter", _FarCronIter),
        patch("app.services.collector.worker.CollectorRepository", return_value=repo),
    ):
        m_fac.return_value = db
        await collect_scheduler({"redis": redis})

    redis.enqueue_job.assert_not_awaited()


async def test_scheduler_ignores_bad_cron_expr():
    """非法 cron 表达式只记录告警，不阻断其他源。"""
    from app.services.collector.worker import collect_scheduler

    bad = MagicMock()
    bad.source_id = "bad"
    bad.schedule_cron = "not a cron"
    bad.created_by = 1

    redis = MagicMock()
    redis.enqueue_job = AsyncMock()

    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    repo = MagicMock()
    repo.list_scheduled_sources = AsyncMock(return_value=[bad])

    with (
        patch("app.db.mysql.async_session_factory") as m_fac,
        patch("app.services.collector.worker.croniter", side_effect=ValueError("bad cron")),
        patch("app.services.collector.worker.CollectorRepository", return_value=repo),
    ):
        m_fac.return_value = db
        await collect_scheduler({"redis": redis})  # 不应抛异常

    redis.enqueue_job.assert_not_awaited()


# ---------- P1-4: arq 队列 job 状态查询 ----------


async def test_arq_queue_enqueue_writes_initial_status():
    """arq 入队后把初始 QUEUED 状态写入 RedisJobStore（GET /jobs 可查）。"""
    redis = MagicMock()
    job = MagicMock()
    job.job_id = "j1"
    redis.enqueue_job = AsyncMock(return_value=job)
    redis.hset = AsyncMock()
    redis.hsetnx = AsyncMock()

    q = ArqCollectionQueue(redis=redis)
    job_id = await q.enqueue("src1", 1)

    assert job_id == "j1"
    redis.hset.assert_awaited_once()
    assert redis.hset.call_args.kwargs["mapping"]["status"] == "QUEUED"


async def test_arq_queue_enqueue_passes_mode():
    """M4: arq 入队把 mode 透传给 worker 并写入 QUEUED detail（任务中心可读真实模式）。"""
    redis = MagicMock()
    job = MagicMock()
    job.job_id = "j1"
    redis.enqueue_job = AsyncMock(return_value=job)
    redis.hset = AsyncMock()
    redis.hsetnx = AsyncMock()

    q = ArqCollectionQueue(redis=redis)
    await q.enqueue("src1", 1, mode="INCREMENTAL")

    # mode 作为关键字参数透传给 run_collection_task（跨链路一致性的关键）
    enqueue_kwargs = redis.enqueue_job.call_args.kwargs
    assert enqueue_kwargs["mode"] == "INCREMENTAL"
    # QUEUED detail 含 mode，任务中心 RUNNING 前即可展示真实模式
    detail = redis.hset.call_args.kwargs["mapping"]["detail"]
    assert '"mode": "INCREMENTAL"' in detail


async def test_arq_queue_get_returns_job_status():
    """arq 队列 get() 委托 RedisJobStore 返回任务状态（原实现无 get → 恒 404）。"""
    redis = MagicMock()
    redis.hgetall = AsyncMock(return_value={"status": "COMPLETED", "detail": '{"scanned": 3}'})

    q = ArqCollectionQueue(redis=redis)
    status = await q.get("job1")

    assert status is not None
    assert status["status"] == "COMPLETED"
    assert status["detail"] == {"scanned": 3}


async def test_arq_queue_get_returns_none_when_missing():
    redis = MagicMock()
    redis.hgetall = AsyncMock(return_value={})

    q = ArqCollectionQueue(redis=redis)
    assert await q.get("missing") is None
