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
        mock_count = MagicMock()
        mock_count.scalar.return_value = 7
        mock_items = MagicMock()
        mock_items.scalars.return_value.all.return_value = [Feedback(id=1)]
        repo._session.execute = AsyncMock(side_effect=[mock_count, mock_items])
        items, total = await repo.list_feedback(
            target_type=None, status=None, page=1, page_size=20
        )
        assert len(items) == 1
        assert total == 7

    async def test_list_feedback_with_filter(self, repo: ObservabilityRepository) -> None:
        mock_count = MagicMock()
        mock_count.scalar.return_value = 1
        mock_items = MagicMock()
        mock_items.scalars.return_value.all.return_value = [Feedback(id=1, target_type="metric")]
        repo._session.execute = AsyncMock(side_effect=[mock_count, mock_items])
        items, total = await repo.list_feedback(
            target_type="metric", status="pending", page=2, page_size=10
        )
        assert len(items) == 1
        assert total == 1

    async def test_list_feedback_filters_soft_deleted(
        self, repo: ObservabilityRepository
    ) -> None:
        """列表查询（count + 明细）都携带软删过滤，已删除反馈不展示。"""
        captured: list[Any] = []
        mock_count = MagicMock()
        mock_count.scalar.return_value = 0
        mock_items = MagicMock()
        mock_items.scalars.return_value.all.return_value = []

        async def fake_execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
            captured.append(stmt)
            return mock_count if len(captured) == 1 else mock_items

        repo._session.execute = fake_execute
        items, total = await repo.list_feedback(None, None, 1, 20)
        assert items == []
        assert total == 0
        assert len(captured) == 2
        for stmt in captured:
            assert "deleted_at" in str(stmt)
            assert "IS NULL" in str(stmt)

    async def test_resolve_target_names(self, repo: ObservabilityRepository) -> None:
        """批量解析指标名：存在返回名称，不存在/非 metric/无 target_id 为 None。"""
        items = [
            Feedback(id=1, target_type="metric", target_id="sales_gmv"),
            Feedback(id=2, target_type="metric", target_id="ghost_metric"),
            Feedback(id=3, target_type="dashboard", target_id="d1"),
            Feedback(id=4, target_type="metric", target_id=None),
        ]
        mock_result = MagicMock()
        mock_result.all.return_value = [("sales_gmv", "销售GMV")]
        repo._session.execute = AsyncMock(return_value=mock_result)
        names = await repo.resolve_target_names(items)
        assert names[1] == "销售GMV"
        assert names[2] is None  # 指标不存在 → 前端标记「已失效」
        assert names[3] is None  # 非 metric 类型不解析
        assert names[4] is None

    async def test_resolve_target_names_empty(
        self, repo: ObservabilityRepository
    ) -> None:
        assert await repo.resolve_target_names([]) == {}
        repo._session.execute.assert_not_awaited()

    async def test_resolve_target_names_no_metric(
        self, repo: ObservabilityRepository
    ) -> None:
        """全部非 metric 类反馈不触发 DB 查询。"""
        items = [Feedback(id=1, target_type="dashboard", target_id="d1")]
        assert await repo.resolve_target_names(items) == {1: None}
        repo._session.execute.assert_not_awaited()

    async def test_nps_stats(self, repo: ObservabilityRepository) -> None:
        def scalar(v: int) -> MagicMock:
            m = MagicMock()
            m.scalar.return_value = v
            return m

        repo._session.execute = AsyncMock(
            side_effect=[
                scalar(100),  # total
                scalar(60),  # promoters >=9
                scalar(20),  # passives 7-8
                scalar(20),  # detractors <=6
            ]
        )
        stats = await repo.nps_stats()
        assert stats == {
            "total": 100,
            "promoters": 60,
            "passives": 20,
            "detractors": 20,
            "score": 40.0,
        }

    async def test_nps_stats_empty(self, repo: ObservabilityRepository) -> None:
        def scalar0() -> MagicMock:
            m = MagicMock()
            m.scalar.return_value = 0
            return m

        repo._session.execute = AsyncMock(
            side_effect=[scalar0(), scalar0(), scalar0(), scalar0()]
        )
        stats = await repo.nps_stats()
        assert stats["total"] == 0
        assert stats["score"] == 0.0

    async def test_quality_events(self, repo: ObservabilityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value = iter(
            [MagicMock(id=1, level=MagicMock(value="P0"), status=MagicMock(value="OPEN"),
                       metric_id=5, created_at=None)]
        )
        repo._session.execute = AsyncMock(return_value=mock_result)
        events = await repo.quality_events(20)
        assert events == [
            {
                "id": 1,
                "level": "P0",
                "status": "OPEN",
                "metric_id": 5,
                "created_at": None,
            }
        ]

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
