"""可观测性 Repository 单测（补齐覆盖率）。

针对 observability/repository.py 的 50% 覆盖率补充。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.feedback import Feedback
from app.services.observability.repository import ObservabilityRepository


@pytest.fixture
def db() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()
    return session


@pytest.fixture
def repo(db: MagicMock) -> ObservabilityRepository:
    return ObservabilityRepository(db)


class TestObservabilityRepository:
    async def test_save_feedback(self, repo: ObservabilityRepository) -> None:
        fb = Feedback(target_type="metric", rating=5)
        result = await repo.save_feedback(fb)
        assert result is fb

    async def test_list_feedback_no_filter(self, repo: ObservabilityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [Feedback(id=1)]
        repo._session.execute = AsyncMock(return_value=mock_result)
        results = await repo.list_feedback(target_type=None, limit=10)
        assert len(results) == 1

    async def test_list_feedback_with_filter(self, repo: ObservabilityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [Feedback(id=1, target_type="metric")]
        repo._session.execute = AsyncMock(return_value=mock_result)
        results = await repo.list_feedback(target_type="metric", limit=10)
        assert len(results) == 1

    async def test_quality_stats(self, repo: ObservabilityRepository) -> None:
        mock_result = MagicMock()
        # 第一次 execute：by_level；第二次 execute：by_status
        mock_result.all.return_value = [("P0", 2), ("P1", 1)]
        repo._session.execute = AsyncMock(return_value=mock_result)
        stats = await repo.quality_stats()
        assert stats["by_level"] == {"P0": 2, "P1": 1}
        assert stats["by_status"] == {"P0": 2, "P1": 1}
        assert stats["total"] == 3

    async def test_api_stats(self, repo: ObservabilityRepository) -> None:
        mock_result = MagicMock()
        mock_result.all.return_value = [("CREATE", 5), ("UPDATE", 3)]
        repo._session.execute = AsyncMock(return_value=mock_result)
        stats = await repo.api_stats()
        assert stats == {"CREATE": 5, "UPDATE": 3}

    async def test_notification_stats(self, repo: ObservabilityRepository) -> None:
        mock_by_status = MagicMock()
        mock_by_status.all.return_value = [("SENT", 4), ("FAILED", 1)]
        mock_scalar1 = MagicMock()
        mock_scalar1.scalar.return_value = 10
        mock_scalar2 = MagicMock()
        mock_scalar2.scalar.return_value = 3
        repo._session.execute = AsyncMock(side_effect=[mock_by_status, mock_scalar1, mock_scalar2])
        stats = await repo.notification_stats()
        assert stats["by_status"] == {"SENT": 4, "FAILED": 1}
        assert stats["event_total"] == 10
        assert stats["event_notified"] == 3

    async def test_lineage_stats(self, repo: ObservabilityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar.return_value = 42
        repo._session.execute = AsyncMock(return_value=mock_result)
        stats = await repo.lineage_stats()
        assert stats == {"edges": 42}

    async def test_overview_stats(self, repo: ObservabilityRepository) -> None:
        def rows(*items: tuple[Any, int]) -> MagicMock:
            m = MagicMock()
            m.all.return_value = list(items)
            return m

        def scalar(v: int) -> MagicMock:
            m = MagicMock()
            m.scalar.return_value = v
            return m

        # 按 overview_stats 执行顺序：源健康/冲突/质量/指标/升级/指标状态/术语/维度/域/客户端
        repo._session.execute = AsyncMock(
            side_effect=[
                rows(("healthy", 2), ("unknown", 1)),  # sources by health
                scalar(1),  # open conflicts
                scalar(2),  # pending quality events
                scalar(3),  # review metrics
                scalar(0),  # open escalations
                rows(("PUBLISHED", 5), ("DRAFT", 2)),  # metrics by status
                scalar(4),  # terms
                scalar(3),  # dimensions
                scalar(2),  # domains
                scalar(1),  # clients total
                scalar(1),  # clients active
            ]
        )
        stats = await repo.overview_stats()
        assert stats["sources"]["by_health"] == {"healthy": 2, "unknown": 1}
        assert stats["sources"]["total"] == 3
        assert stats["backlog"] == {
            "open_conflicts": 1,
            "pending_quality_events": 2,
            "review_metrics": 3,
            "open_escalations": 0,
        }
        assert stats["assets"]["metrics_by_status"] == {"PUBLISHED": 5, "DRAFT": 2}
        assert stats["assets"]["terms"] == 4
        assert stats["assets"]["dimensions"] == 3
        assert stats["assets"]["domains"] == 2
        assert stats["assets"]["sources"] == 3
        assert stats["clients"] == {"total": 1, "active": 1}

    async def test_commit(self, repo: ObservabilityRepository) -> None:
        await repo.commit()
        repo._session.commit.assert_called_once()
