"""口径一致率量化统计单测（P1：consistency_stats 聚合）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.conflict.repository import ConflictRepository


@pytest.fixture
def db() -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock()
    return session


@pytest.fixture
def repo(db: MagicMock) -> ConflictRepository:
    return ConflictRepository(db)


def _count_result(value: int) -> MagicMock:
    mock = MagicMock()
    mock.scalar.return_value = value
    return mock


class TestConsistencyStats:
    async def test_empty_platform_returns_vacuous_consistency(
        self, repo: ConflictRepository, db: MagicMock
    ) -> None:
        db.execute = AsyncMock(
            side_effect=[
                _count_result(0),  # total_definitions
                _count_result(0),  # total_conflicts
                _count_result(0),  # conflicted_metrics
                _count_result(0),  # cross_department_conflicts
                _count_result(None),  # avg_resolve_seconds
            ]
        )
        stats = await repo.consistency_stats()
        assert stats["total_definitions"] == 0
        assert stats["total_conflicts"] == 0
        assert stats["consistency_rate_pct"] == 100.0
        assert stats["cross_department_conflicts"] == 0
        assert stats["avg_resolve_hours"] == 0.0

    async def test_computes_rate_and_resolve_hours(
        self, repo: ConflictRepository, db: MagicMock
    ) -> None:
        db.execute = AsyncMock(
            side_effect=[
                _count_result(100),  # total_definitions
                _count_result(10),  # total_conflicts
                _count_result(10),  # conflicted_metrics（10 个指标卷入冲突）
                _count_result(3),  # cross_department_conflicts
                _count_result(7200),  # 平均解决时长 7200 秒 = 2 小时
            ]
        )
        stats = await repo.consistency_stats()
        assert stats["total_definitions"] == 100
        assert stats["total_conflicts"] == 10
        assert stats["conflicted_metrics"] == 10
        # (100-10)/100 = 90.0%
        assert stats["consistency_rate_pct"] == 90.0
        assert stats["cross_department_conflicts"] == 3
        assert stats["avg_resolve_hours"] == 2.0

    async def test_avg_resolve_hours_rounded_to_one_decimal(
        self, repo: ConflictRepository, db: MagicMock
    ) -> None:
        db.execute = AsyncMock(
            side_effect=[
                _count_result(50),
                _count_result(2),
                _count_result(1),
                _count_result(0),
                _count_result(9000),  # 2.5 小时
            ]
        )
        stats = await repo.consistency_stats()
        assert stats["avg_resolve_hours"] == 2.5
