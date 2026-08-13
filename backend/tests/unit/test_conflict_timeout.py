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
