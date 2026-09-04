"""dp 手动「立即扫描」后台任务管理（dp_sync_manual）单元测试。

聚焦 registry 语义：提交去重 / 状态读取 / 取消受理；后台任务本身依赖
async_session_factory（真实 DB 连接），以 monkeypatch 替换避免跑真实扫描。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.lineage import dp_sync_manual as manual


def _fake_state(task_id: int, status: str = "running"):
    import asyncio

    return {
        "task_id": task_id,
        "status": status,
        "started_at": None,
        "finished_at": None,
        "error": None,
        "message": None,
        "result": None,
        "progress": {"stage": "queued", "total": 0, "processed": 0, "current_task_id": None},
        "cancel_event": asyncio.Event(),
        "force_event": asyncio.Event(),
        "cancel_requested_at": None,
        "force_stop": False,
    }


@pytest.mark.asyncio
async def test_submit_scan_starts_new_task(monkeypatch) -> None:
    monkeypatch.setattr(manual, "_SCANS", {})
    monkeypatch.setattr(manual, "_run_scan_job", AsyncMock())

    task_id, already = await manual.submit_scan()
    assert already is False
    assert task_id in manual._SCANS
    assert manual._SCANS[task_id]["status"] == "running"
    manual._run_scan_job.assert_called_once()  # create_task 已调度后台任务


@pytest.mark.asyncio
async def test_submit_scan_returns_existing_when_running(monkeypatch) -> None:
    st = _fake_state(7)
    monkeypatch.setattr(manual, "_SCANS", {7: st})
    monkeypatch.setattr(manual, "_run_scan_job", AsyncMock())

    task_id, already = await manual.submit_scan()
    assert already is True
    assert task_id == 7
    manual._run_scan_job.assert_not_called()  # 不重复启动


@pytest.mark.asyncio
async def test_scan_status_unknown_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(manual, "_SCANS", {})
    assert manual.scan_status(1) is None


@pytest.mark.asyncio
async def test_scan_status_exposes_progress_and_error(monkeypatch) -> None:
    st = _fake_state(3, status="failed")
    st["error"] = "boom"
    st["progress"] = {"stage": "parsing", "total": 5, "processed": 2, "current_task_id": 9}
    st["result"] = {"skipped": "failed", "error": "boom"}
    monkeypatch.setattr(manual, "_SCANS", {3: st})

    data = manual.scan_status(3)
    assert data is not None
    assert data["status"] == "failed"
    assert data["error"] == "boom"
    assert data["progress"]["processed"] == 2
    assert data["cancel_requested"] is False
    assert data["force_stop"] is False
    assert "cancel_event" not in data  # 事件对象不外泄
    assert "force_event" not in data


@pytest.mark.asyncio
async def test_current_running_status_returns_running_task(monkeypatch) -> None:
    """切页自动恢复：仅当存在运行中任务时返回其状态（供前端接上轮询）。"""
    running = _fake_state(8, status="running")
    finished = _fake_state(9, status="success")
    monkeypatch.setattr(manual, "_SCANS", {8: running, 9: finished})

    data = manual.current_running_status()
    assert data is not None
    assert data["task_id"] == 8
    assert data["status"] == "running"
    assert "cancel_event" not in data  # 事件对象不外泄
    assert "force_event" not in data


@pytest.mark.asyncio
async def test_current_running_status_none_when_idle(monkeypatch) -> None:
    """无运行中任务（仅终态/空 registry）返回 None——前端不接轮询。"""
    finished = _fake_state(10, status="cancelled")
    monkeypatch.setattr(manual, "_SCANS", {10: finished})
    assert manual.current_running_status() is None

    monkeypatch.setattr(manual, "_SCANS", {})
    assert manual.current_running_status() is None


@pytest.mark.asyncio
async def test_cancel_scan_accepts_running_only(monkeypatch) -> None:
    running = _fake_state(1, status="running")
    finished = _fake_state(2, status="success")
    monkeypatch.setattr(manual, "_SCANS", {1: running, 2: finished})

    assert await manual.cancel_scan(1) is True
    assert running["cancel_event"].is_set()
    assert running["cancel_requested_at"] is not None
    assert running["force_stop"] is False  # 协作取消不置 force
    assert running["message"] is not None

    assert await manual.cancel_scan(2) is False  # 已结束不可取消
    assert await manual.cancel_scan(99) is False  # 不存在


@pytest.mark.asyncio
async def test_force_cancel_sets_both_signals(monkeypatch) -> None:
    """强制终止：同时置位 force_event 与 cancel_event，state 标记 force_stop。"""
    running = _fake_state(5, status="running")
    finished = _fake_state(6, status="cancelled")
    monkeypatch.setattr(manual, "_SCANS", {5: running, 6: finished})

    assert await manual.force_cancel_scan(5) is True
    assert running["cancel_event"].is_set()
    assert running["force_event"].is_set()
    assert running["force_stop"] is True
    assert running["cancel_requested_at"] is not None
    assert "强制终止" in (running["message"] or "")

    assert await manual.force_cancel_scan(6) is False  # 已结束不可强制
    assert await manual.force_cancel_scan(99) is False


def test_prune_registry_keeps_latest_finished(monkeypatch) -> None:
    """registry 终态超量时清理最旧、保留运行中 + 最新 N 个终态（P2-8）。

    回归：此前 _SCANS 只增不减，手动扫描低频但进程内无限增长。
    """
    from datetime import UTC, datetime, timedelta

    states: dict[int, dict] = {}
    now = datetime.now(UTC)
    # 25 个终态（finished_at 递增）+ 1 个运行中
    for i in range(1, 26):
        states[i] = {
            "task_id": i,
            "status": "success",
            "finished_at": now - timedelta(seconds=100 - i),
        }
    states[99] = {"task_id": 99, "status": "running", "finished_at": None}
    monkeypatch.setattr(manual, "_SCANS", states)

    manual._prune_registry()

    remaining = manual._SCANS
    assert 99 in remaining  # 运行中保留
    # 终态保留最新 20 个（即 id 6..25 被清掉的是 1..5）
    assert len(remaining) == 21
    assert 1 not in remaining
    assert 2 not in remaining
    assert 25 in remaining


@pytest.mark.asyncio
async def test_submit_scan_throttled_within_interval(monkeypatch) -> None:
    """L1：距上次触发 < 30s 时 submit_scan 返回 (0, False)（节流拒绝）。"""
    import asyncio
    from datetime import UTC, datetime, timedelta

    import app.services.lineage.dp_sync_manual as manual

    now = datetime.now(UTC)
    # 最近一次触发在 10s 前
    states = {5: {"task_id": 5, "status": "success", "started_at": now - timedelta(seconds=10)}}
    monkeypatch.setattr(manual, "_SCANS", states)
    monkeypatch.setattr(manual, "_GUARD", asyncio.Lock())
    monkeypatch.setattr(manual, "asyncio", asyncio)  # create_task 不会被真实调度到
    task_id, already = await manual.submit_scan(force=True)
    assert task_id == 0
    assert already is False


@pytest.mark.asyncio
async def test_submit_scan_allowed_after_interval(monkeypatch) -> None:
    """L1：距上次触发 ≥ 30s 时 submit_scan 正常提交（返回真实 task_id）。"""
    import asyncio
    from datetime import UTC, datetime, timedelta

    import app.services.lineage.dp_sync_manual as manual

    now = datetime.now(UTC)
    states = {5: {"task_id": 5, "status": "success", "started_at": now - timedelta(seconds=60)}}
    monkeypatch.setattr(manual, "_SCANS", states)
    monkeypatch.setattr(manual, "_GUARD", asyncio.Lock())

    async def _fake_run_scan_job(task_id: int, *, force: bool) -> None:
        return None

    monkeypatch.setattr(manual, "_run_scan_job", _fake_run_scan_job)
    task_id, already = await manual.submit_scan(force=True)
    assert task_id != 0
    assert already is False
