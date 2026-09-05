"""dp 待抉择单 LLM 重试后台任务（dp_retry_task）单元测试。

覆盖（第六轮 T5/T6）：
- ``_finalize_retry_task``：中断收敛 failed（不留 running 僵尸）/ 已请求取消收敛
  cancelled / 正常完成 completed
- ``run_dp_ticket_retry_task``：CancelledError 中断 → 终态 failed + 重抛
- ``_run_single_ticket``：auto_resolved 的 resolved_by 归因系统动作 0（非任务 id）
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.lineage.dp_retry_task import (
    _finalize_retry_task,
    _run_single_ticket,
    run_dp_ticket_retry_task,
)


def _row(**overrides) -> SimpleNamespace:
    d = dict(
        id=1,
        tickets_json=[],
        progress_json=[],
        status="running",
        total=0,
        done=0,
        failed=0,
        cancelled=0,
        counts_json={},
        cancel_requested=False,
        error=None,
        finished_at=None,
        started_at=None,
    )
    d.update(overrides)
    return SimpleNamespace(**d)


def _factory_mock(db: MagicMock):
    """async_session_factory 假实现：每次 ``async with factory()`` 产出同一 db。"""
    factory = MagicMock()
    cm = factory.return_value
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    return factory


def _patch_env(row: SimpleNamespace) -> MagicMock:
    """把 _load_task 指向固定 row、async_session_factory 产出 AsyncMock db。"""
    db = AsyncMock()
    factory = _factory_mock(db)
    return patch(
        "app.services.lineage.dp_retry_task.async_session_factory", factory
    ), patch(
        "app.services.lineage.dp_retry_task._load_task",
        new=AsyncMock(return_value=row),
    ), patch(
        "app.services.lineage.dp_retry_task.flag_modified",
        new=lambda *a, **k: None,
    )


@pytest.mark.asyncio
async def test_finalize_interrupted_marks_failed() -> None:
    """T5：中断收敛——运行中任务被 job 超时/进程关闭中断 → failed + error，不留僵尸。"""
    row = _row(status="running")
    with _patch_env(row)[0], _patch_env(row)[1]:
        await _finalize_retry_task(1, interrupted=True, error="job timeout")
    assert row.status == "failed"
    assert "job timeout" in (row.error or "")
    assert row.finished_at is not None


@pytest.mark.asyncio
async def test_finalize_interrupted_cancel_requested_marks_cancelled() -> None:
    """T5：中断但已被 API 请求取消 → 优先收敛 cancelled（协作取消语义不被覆盖）。"""
    row = _row(
        status="running",
        cancel_requested=True,
        progress_json=[{"status": "pending", "summary": ""}],
    )
    with _patch_env(row)[0], _patch_env(row)[1], _patch_env(row)[2]:
        await _finalize_retry_task(1, interrupted=True, error="boom")
    assert row.status == "cancelled"
    assert row.progress_json[0]["status"] == "cancelled"
    assert row.finished_at is not None


@pytest.mark.asyncio
async def test_finalize_normal_completes() -> None:
    """T5：正常跑完 → completed。"""
    row = _row(status="running")
    with _patch_env(row)[0], _patch_env(row)[1]:
        await _finalize_retry_task(1)
    assert row.status == "completed"
    assert row.finished_at is not None


@pytest.mark.asyncio
async def test_run_interrupted_converges_failed_and_reraises() -> None:
    """T5：run 主流程被 CancelledError 中断 → 终态 failed + 重抛（此前 running 僵尸）。"""
    row = _row(
        status="pending",
        tickets_json=[{"ticket_id": 1}],
        progress_json=[{"ticket_id": 1, "status": "pending"}],
        total=1,
    )
    with _patch_env(row)[0], _patch_env(row)[1], _patch_env(row)[2], patch(
        "app.services.lineage.dp_retry_task._run_single_ticket",
        new=AsyncMock(side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await run_dp_ticket_retry_task({}, 1)
    assert row.status == "failed"
    assert row.finished_at is not None


@pytest.mark.asyncio
async def test_run_no_candidates_completes() -> None:
    """T5：无候选单 → 正常 completed（空任务不留 running）。"""
    row = _row(status="pending", total=0)
    with _patch_env(row)[0], _patch_env(row)[1]:
        await run_dp_ticket_retry_task({}, 1)
    assert row.status == "completed"
    assert row.finished_at is not None


@pytest.mark.asyncio
async def test_run_single_ticket_passes_resolved_by_zero() -> None:
    """T6：异步重试 auto_resolved 的 resolved_by 归因系统动作 0（非任务自增 id）。

    回归：此前传 task_id（dp_ticket_retry_task 自增 id）落在真实用户 id 空间，
    前端按用户渲染/审计归属错乱。
    """
    from app.services.lineage.dp_sync_service import DpSyncService

    tk = MagicMock(id=1, resolution=None, status="diverged")
    item = {"ticket_id": 1, "status": "pending"}
    db = AsyncMock()
    factory = _factory_mock(db)
    retry_mock = AsyncMock(return_value=("auto_resolved", {"reason": "ok"}, None))
    with (
        patch("app.services.lineage.dp_retry_task.async_session_factory", factory),
        patch(
            "app.services.lineage.dp_retry_task._ticket_is_cancellable",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.services.lineage.dp_retry_task._load_ticket",
            new=AsyncMock(return_value=tk),
        ),
        patch.object(DpSyncService, "_retry_one_ticket", new=retry_mock),
    ):
        updated = await _run_single_ticket(9, item)
    assert updated["status"] == "done"
    assert updated["action"] == "auto_resolved"
    assert retry_mock.await_count == 1
    assert retry_mock.await_args.kwargs["resolved_by"] == 0
