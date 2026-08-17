"""PendingVersionManager 单元测试（P1-8 双实现合并后仅 create_pending）。

确认/拒绝/延期/超时自动接受规则唯一实现在 MetricService
（confirm_version/reject_version/extend_version/auto_accept_timeout），
覆盖见 test_semantic_service.py；本文件只测管理器专属的创建路径。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.metric import Metric
from app.services.semantic.pending_version_manager import PendingVersionManager


@pytest.fixture
def mock_db() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_repo() -> AsyncMock:
    return AsyncMock()


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

    async def test_create_pending_empty_consumers_raises(
        self,
        manager: PendingVersionManager,
        mock_repo: AsyncMock,
        sample_metric: MagicMock,
        sample_version: MagicMock,
    ) -> None:
        """无消费方 → 拒绝创建（VALIDATION_ERROR）。"""
        with pytest.raises(Exception, match="至少一个消费方"):
            await manager.create_pending(sample_metric, sample_version, [])
