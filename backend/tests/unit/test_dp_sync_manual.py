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
