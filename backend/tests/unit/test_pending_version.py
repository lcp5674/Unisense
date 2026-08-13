"""PendingVersionManager 单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import BusinessError
from app.models.metric import Metric
from app.services.semantic.pending_version_manager import (
    PendingAction,
    PendingVersionManager,
)


@pytest.fixture
def mock_db() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_repo() -> AsyncMock:
    repo = AsyncMock()
    return repo


@pytest.fixture
def manager(mock_db: AsyncMock, mock_repo: AsyncMock) -> PendingVersionManager:
    mgr = PendingVersionManager(mock_db)
    mgr._repo = mock_repo
    return mgr


@pytest.fixture
def sample_metric() -> MagicMock:
    m = MagicMock(spec=Metric)
    m.id = 1
    m.metric_code = "sales_gmv_day"
    m.version = 2
    return m


@pytest.fixture
def sample_version() -> MagicMock:
    v = MagicMock()
    v.metric_id = 1
    v.version = 3
    v.status = "DRAFT"
    return v


class TestCreatePending:
    async def test_create_pending_sets_deadline(
        self,
        manager: PendingVersionManager,
        mock_repo: AsyncMock,
        sample_metric: MagicMock,
        sample_version: MagicMock,
    ) -> None:
        consumer_ids = [10, 20, 30]
        mock_repo.save_pending_confirmation = AsyncMock()

        await manager.create_pending(sample_metric, sample_version, consumer_ids)

        # 验证为每个消费方创建了确认记录
        assert mock_repo.save_pending_confirmation.call_count == 3
        calls = mock_repo.save_pending_confirmation.call_args_list
        for i, cid in enumerate(consumer_ids):
            confirmation = calls[i][0][0]
            assert confirmation.consumer_id == cid
            assert confirmation.metric_id == sample_metric.id
            assert confirmation.version == sample_version.version
            assert confirmation.status == "PENDING"
            # deadline 应约为 14 天后（实现以 now+14d 计算，不依赖 created_at）
            expected_deadline = datetime.now(UTC) + timedelta(days=14)
            assert abs((confirmation.deadline - expected_deadline).total_seconds()) < 5


class TestConfirm:
    async def test_confirm_waits_when_partial(
        self,
        manager: PendingVersionManager,
        mock_repo: AsyncMock,
    ) -> None:
        # 3个消费方，仅1个确认
        confirmations = [
            MagicMock(consumer_id=10, status="CONFIRMED"),
            MagicMock(consumer_id=20, status="PENDING"),
            MagicMock(consumer_id=30, status="PENDING"),
        ]
        mock_repo.get_pending_confirmations = AsyncMock(return_value=confirmations)
        mock_repo.update_confirmation_status = AsyncMock()

        result = await manager.confirm(metric_id=1, version=3, consumer_id=20)

        assert result == PendingAction.WAITING
        mock_repo.update_confirmation_status.assert_called_once()

    async def test_confirm_switches_current_when_all_confirmed(
        self,
        manager: PendingVersionManager,
        mock_repo: AsyncMock,
    ) -> None:
        confirmations = [
            MagicMock(consumer_id=10, status="CONFIRMED"),
            MagicMock(consumer_id=20, status="CONFIRMED"),
            MagicMock(consumer_id=30, status="PENDING"),
        ]
        mock_repo.get_pending_confirmations = AsyncMock(return_value=confirmations)
        mock_repo.update_confirmation_status = AsyncMock()

        result = await manager.confirm(metric_id=1, version=3, consumer_id=30)

        assert result == PendingAction.SWITCH_CURRENT

    async def test_confirm_no_pending_record(
        self,
        manager: PendingVersionManager,
        mock_repo: AsyncMock,
    ) -> None:
        mock_repo.get_pending_confirmations = AsyncMock(return_value=[])

        with pytest.raises(BusinessError, match="无待确认记录"):
            await manager.confirm(metric_id=1, version=3, consumer_id=99)


class TestReject:
    async def test_reject_cancels_pending(
        self,
        manager: PendingVersionManager,
        mock_repo: AsyncMock,
    ) -> None:
        confirmations = [
            MagicMock(consumer_id=10, status="CONFIRMED"),
            MagicMock(consumer_id=20, status="PENDING"),
        ]
        mock_repo.get_pending_confirmations = AsyncMock(return_value=confirmations)
        mock_repo.update_confirmation_status = AsyncMock()

        result = await manager.reject(metric_id=1, version=3, consumer_id=20, reason="口径不合理")

        assert result == PendingAction.CANCEL

    async def test_reject_no_pending_record(
        self,
        manager: PendingVersionManager,
        mock_repo: AsyncMock,
    ) -> None:
        mock_repo.get_pending_confirmations = AsyncMock(return_value=[])

        with pytest.raises(BusinessError, match="无待确认记录"):
            await manager.reject(metric_id=1, version=3, consumer_id=99, reason="test")


class TestExtend:
    async def test_extend_adds_7_days(
        self,
        manager: PendingVersionManager,
        mock_repo: AsyncMock,
    ) -> None:
        now = datetime.now(UTC)
        original_deadline = now + timedelta(days=14)
        confirmations = [
            MagicMock(
                consumer_id=10,
                status="PENDING",
                deadline=original_deadline,
                extension_count=0,
            ),
        ]
        mock_repo.get_pending_confirmations = AsyncMock(return_value=confirmations)
        mock_repo.extend_confirmation_deadline = AsyncMock()

        await manager.extend(metric_id=1, version=3)

        # 验证延期+7天（实现调用 extend_confirmation_deadline）
        call_args = mock_repo.extend_confirmation_deadline.call_args
        assert call_args is not None
        confirmation_id, new_deadline = call_args[0]
        expected_deadline = original_deadline + timedelta(days=7)
        assert abs((new_deadline - expected_deadline).total_seconds()) < 5

    async def test_extend_rejects_second_extension(
        self,
        manager: PendingVersionManager,
        mock_repo: AsyncMock,
    ) -> None:
        now = datetime.now(UTC)
        confirmations = [
            MagicMock(
                consumer_id=10,
                status="PENDING",
                deadline=now + timedelta(days=21),
                extension_count=1,  # 已延期1次
            ),
        ]
        mock_repo.get_pending_confirmations = AsyncMock(return_value=confirmations)

        with pytest.raises(BusinessError, match="延期满 1 次"):
            await manager.extend(metric_id=1, version=3)

    async def test_extend_no_pending_record(
        self,
        manager: PendingVersionManager,
        mock_repo: AsyncMock,
    ) -> None:
        mock_repo.get_pending_confirmations = AsyncMock(return_value=[])

        with pytest.raises(BusinessError, match="无待确认记录"):
            await manager.extend(metric_id=1, version=3)


class TestCheckTimeouts:
    async def test_check_timeouts_auto_accepts(
        self,
        manager: PendingVersionManager,
        mock_repo: AsyncMock,
    ) -> None:
        # 已过deadline的确认记录（实现读取 get_timeout_pending_confirmations）
        expired = MagicMock(
            id=101,
            metric_id=1,
            version=3,
            consumer_id=10,
            status="PENDING",
            deadline=datetime.now(UTC) - timedelta(days=1),
        )
        mock_repo.get_timeout_pending_confirmations = AsyncMock(return_value=[expired])
        mock_repo.update_confirmation_status = AsyncMock()
        # 超时接受后再次查询，记录状态应为 TIMEOUT_ACCEPTED
        accepted = MagicMock(
            id=101,
            metric_id=1,
            version=3,
            consumer_id=10,
            status="TIMEOUT_ACCEPTED",
        )
        mock_repo.get_pending_confirmations = AsyncMock(return_value=[accepted])

        result = await manager.check_timeouts()

        assert 1 in result

    async def test_check_timeouts_no_expired(
        self,
        manager: PendingVersionManager,
        mock_repo: AsyncMock,
    ) -> None:
        mock_repo.get_timeout_pending_confirmations = AsyncMock(return_value=[])

        result = await manager.check_timeouts()

        assert result == []


class TestPauseOnDrift:
    async def test_pause_on_drift_notifies_owner(
        self,
        manager: PendingVersionManager,
        mock_repo: AsyncMock,
    ) -> None:
        # pause_on_drift 应记录日志并不抛异常
        with patch("app.services.semantic.pending_version_manager.logger") as mock_logger:
            await manager.pause_on_drift(
                metric_id=1,
                version=3,
                drift_detail={"field": "column_x", "change": "type_changed"},
            )
            # 验证告警日志已记录
            mock_logger.warning.assert_called_once()
