"""跨表批量 LLM 推断任务「执行调度」单测（方案 A：任务内并发）。

覆盖 ``run_batch_llm_infer_task`` 的调度语义：
- 有界 worker 池：并发峰值受 ``task.concurrency`` 约束且多表真实并行；
- 全表成功后任务终态 completed、进度全 done；
- 协作取消：``cancel_requested`` 置位后停止派发剩余表，收敛为 cancelled。

不依赖真实 DB/LLM：注入伪 ``async_session_factory``（内存持有任务行，模拟持久化）
与伪 ``_run_single_table``（记录并发峰值、模拟 LLM 耗时）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.models.collector_models import BatchLlmInferTask
from app.services.collector.batch_infer_tasks import run_batch_llm_infer_task


class _FakeSession:
    """伪 AsyncSession：execute 恒返回持有的任务行（内存共享 = 模拟 DB 持久化）。"""

    def __init__(self, task: BatchLlmInferTask) -> None:
        self._task = task

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def commit(self) -> None:
        return None

    def add(self, _obj: object) -> None:
        return None

    async def execute(self, _stmt: object) -> MagicMock:
        res = MagicMock()
        res.scalar_one_or_none.return_value = self._task
        return res


class _FakeSessionFactory:
    """可调用对象：每次调用返回一个共享同一任务行的伪会话。"""

    def __init__(self, task: BatchLlmInferTask) -> None:
        self._task = task

    def __call__(self) -> _FakeSession:
        return _FakeSession(self._task)


def _make_task(n: int, concurrency: int) -> BatchLlmInferTask:
    tables = [
        {
            "catalog_id": 100 + i,
            "entity_name": f"t{i}",
            "missing_fields": 0,
            "needs_table_desc": False,
        }
        for i in range(n)
    ]
    progress = [
        {
            "catalog_id": 100 + i,
            "entity_name": f"t{i}",
            "status": "pending",
            "summary": "",
            "detail": "",
            "error_category": None,
            "added": 0,
            "skipped": 0,
            "inferred": [],
        }
        for i in range(n)
    ]
    return BatchLlmInferTask(
        id=1,
        actor_id=1,
        actor_name="tester",
        tasks_json=tables,
        progress_json=progress,
        status="pending",
        total=n,
        concurrency=concurrency,
        done=0,
        failed=0,
        cancelled=0,
        added_total=0,
        cancel_requested=False,
    )


async def _run_with_patch(
    task: BatchLlmInferTask,
    fake_run,
) -> MagicMock:
    factory = _FakeSessionFactory(task)
    with (
        patch(
            "app.services.collector.batch_infer_tasks.async_session_factory", factory
        ),
        patch(
            "app.services.collector.batch_infer_tasks._run_single_table",
            new=fake_run,
        ),
        patch(
            "app.services.collector.batch_infer_tasks._write_infer_history"
        ) as mock_hist,
    ):
        await run_batch_llm_infer_task({}, task.id)
    return mock_hist


@pytest.mark.asyncio
async def test_concurrent_workers_respect_limit_and_complete_all():
    """6 表 concurrency=3：并发峰值 2~3（真实并行且受控），全 done、终态 completed。"""
    task = _make_task(n=6, concurrency=3)
    active = 0
    peak = 0

    async def fake_run(_task_id: int, item: dict) -> dict:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {
            **item,
            "status": "done",
            "summary": "ok",
            "detail": "",
            "error_category": None,
            "added": 0,
            "skipped": 0,
            "inferred": [],
        }

    mock_hist = await _run_with_patch(task, fake_run)

    assert peak >= 2, f"预期真实并行（峰值>=2），实际峰值 {peak}"
    assert peak <= 3, f"并发峰值 {peak} 超过 concurrency=3"
    assert task.status == "completed"
    assert task.done == 6
    assert all(p["status"] == "done" for p in task.progress_json)
    mock_hist.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrency_one_runs_serially():
    """concurrency=1 时串行：并发峰值恒 1，仍全部完成。"""
    task = _make_task(n=4, concurrency=1)
    active = 0
    peak = 0

    async def fake_run(_task_id: int, item: dict) -> dict:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        return {**item, "status": "done", "summary": "ok"}

    await _run_with_patch(task, fake_run)

    assert peak == 1
    assert task.status == "completed"
    assert task.done == 4


@pytest.mark.asyncio
async def test_cancel_requested_stops_pending_tables():
    """执行中 cancel_requested 置位：已完成的表保留 done，未启动表标 cancelled。"""
    task = _make_task(n=6, concurrency=3)
    first = {"called": False}

    async def fake_run(_task_id: int, item: dict) -> dict:
        await asyncio.sleep(0.01)
        if not first["called"]:
            first["called"] = True
            task.cancel_requested = True  # 模拟 API 取消（首个表完成后）
        return {**item, "status": "done", "summary": "ok"}

    await _run_with_patch(task, fake_run)

    assert task.status == "cancelled"
    statuses = [p["status"] for p in task.progress_json]
    assert task.done + task.cancelled == 6
    assert task.cancelled >= 1, f"应有未启动表被标 cancelled，实际 {statuses}"
    assert all(s in ("done", "cancelled") for s in statuses)
