"""冲突超时升级测试（T058 部分）。

验证：
1. conflict_escalation_task 正确扫描超时冲突
2. 超时冲突状态更新为 ESCALATED
3. 发布冲突升级事件
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_conflict_escalation_task_exists():
    """T058: conflict_escalation_task 模块可导入。"""
    from app.tasks.conflict_tasks import conflict_escalation_task

    assert callable(conflict_escalation_task)


@pytest.mark.asyncio
async def test_conflict_escalation_threshold():
    """T058: 冲突超时阈值为 48 小时。"""
    from app.tasks.conflict_tasks import _CONFLICT_ESCALATION_HOURS

    assert _CONFLICT_ESCALATION_HOURS == 48


@pytest.mark.asyncio
async def test_escalation_no_rows():
    """T058: 无超时冲突时返回 escalated=0。"""
    from app.tasks.conflict_tasks import conflict_escalation_task

    # Mock DB session
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_db.execute.return_value = mock_result

    with patch("app.tasks.conflict_tasks.async_session_factory") as mock_factory:
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await conflict_escalation_task({})

    assert result["status"] == "SUCCESS"
    assert result["escalated"] == 0


@pytest.mark.asyncio
async def test_escalation_publishes_conflict_escalated_event():
    """T058: 超时冲突升级时发布 conflict_escalated（下划线）事件。

    对齐订阅清单（main.py: _BUSINESS_EVENT_TYPES 用 conflict_escalated）与
    conflict/service.py:261 的 escalate 发布名，保证升级任务的通知闭环不因
    命名分裂（conflict.escalated 点号）而丢失。
    """
    from app.tasks.conflict_tasks import conflict_escalation_task

    # Mock DB session：一条超时冲突
    mock_row = MagicMock()
    mock_row.id = 42
    mock_row.conflict_type = "same_name_diff_def"
    mock_row.metric_code = "GMV"
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_row]
    mock_db.execute.return_value = mock_result
    mock_db.commit = AsyncMock()

    # Mock eventbus：捕获发布的事件
    bus = MagicMock()
    bus.publish = AsyncMock()

    with (
        patch("app.tasks.conflict_tasks.async_session_factory") as mock_factory,
        patch("app.core.eventbus.get_eventbus", return_value=bus),
    ):
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await conflict_escalation_task({})

    assert result["status"] == "SUCCESS"
    assert result["escalated"] == 1
    mock_db.commit.assert_awaited_once()
    # 关键断言：事件名必须与订阅清单一致（下划线，非点号）
    bus.publish.assert_awaited_once()
    event_type = bus.publish.await_args.args[0]
    assert event_type == "conflict_escalated"
    payload = bus.publish.await_args.args[1]
    assert payload["conflict_id"] == 42
    assert payload["metric_code"] == "GMV"


@pytest.mark.asyncio
async def test_escalation_publish_failure_is_best_effort():
    """T058: 事件发布失败不阻断升级主流程（best-effort）。"""
    from app.tasks.conflict_tasks import conflict_escalation_task

    mock_row = MagicMock()
    mock_row.id = 7
    mock_row.conflict_type = "unknown"
    mock_row.metric_code = ""
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_row]
    mock_db.execute.return_value = mock_result
    mock_db.commit = AsyncMock()

    bus = MagicMock()
    bus.publish = AsyncMock(side_effect=RuntimeError("bus down"))

    with (
        patch("app.tasks.conflict_tasks.async_session_factory") as mock_factory,
        patch("app.core.eventbus.get_eventbus", return_value=bus),
    ):
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await conflict_escalation_task({})

    # 发布失败不影响状态更新与返回
    assert result["status"] == "SUCCESS"
    assert result["escalated"] == 1
    mock_db.commit.assert_awaited_once()
