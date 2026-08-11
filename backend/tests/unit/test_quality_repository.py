"""quality 服务 Repository 单测（补齐覆盖率）。

针对 quality/repository.py 的 23% 覆盖率，补充以下场景：
- create_rule / get_rule / list_rules / update_rule / delete_rule
- create_event / get_event / list_events / transition_event / save_event
- record_observation / list_recent_observations
- find_benchmark / save_benchmark / get_benchmark / list_benchmarks
- save_reconciliation / get_reconciliation / list_reconciliations
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.quality import (
    ExternalBenchmark,
    QualityEvent,
    QualityEventStatus,
    QualityObservation,
    QualityRule,
    QualityRuleType,
    QualitySeverity,
    ReconciliationRecord,
)
from app.services.quality.repository import QualityRepository


@pytest.fixture
def db() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.execute = AsyncMock()
    return session


@pytest.fixture
def repo(db: MagicMock) -> QualityRepository:
    return QualityRepository(db)


class TestQualityRuleRepo:
    async def test_create_rule(self, repo: QualityRepository) -> None:
        rule = QualityRule(
            metric_id=1,
            rule_type=QualityRuleType.COMPLETENESS,
            threshold={"value": 100},
            severity=QualitySeverity.P0,
        )
        result = await repo.create_rule(rule)
        assert result is rule
        repo._db.add.assert_called_once_with(rule)

    async def test_get_rule_found(self, repo: QualityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = QualityRule(id=1)
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_rule(1)
        assert result is not None
        assert result.id == 1

    async def test_get_rule_not_found(self, repo: QualityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_rule(999)
        assert result is None

    async def test_list_rules_no_filters(self, repo: QualityRepository) -> None:
        mock_count = MagicMock()
        mock_count.scalar.return_value = 2
        mock_rows = MagicMock()
        mock_rows.scalars.return_value.all.return_value = [QualityRule(id=1), QualityRule(id=2)]
        repo._db.execute = AsyncMock(side_effect=[mock_count, mock_rows])
        results, total = await repo.list_rules(
            metric_id=None, rule_type=None, severity=None, enabled=None, page=1, page_size=10
        )
        assert len(results) == 2
        assert total == 2

    async def test_list_rules_with_filters(self, repo: QualityRepository) -> None:
        mock_count = MagicMock()
        mock_count.scalar.return_value = 1
        mock_rows = MagicMock()
        mock_rows.scalars.return_value.all.return_value = [QualityRule(id=1)]
        repo._db.execute = AsyncMock(side_effect=[mock_count, mock_rows])
        results, total = await repo.list_rules(
            metric_id=5,
            rule_type=QualityRuleType.COMPLETENESS,
            severity=QualitySeverity.P0,
            enabled=True,
            page=1,
            page_size=10,
        )
        assert len(results) == 1

    async def test_update_rule(self, repo: QualityRepository) -> None:
        rule = QualityRule(id=1)
        result = await repo.update_rule(rule, enabled=False, severity=QualitySeverity.P2)
        assert result.enabled is False
        assert result.severity == QualitySeverity.P2

    async def test_delete_rule(self, repo: QualityRepository) -> None:
        rule = QualityRule(id=1)
        await repo.delete_rule(rule)
        assert rule.deleted_at is not None

    async def test_list_enabled_rules_for(self, repo: QualityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [QualityRule(id=1, enabled=True)]
        repo._db.execute = AsyncMock(return_value=mock_result)
        results = await repo.list_enabled_rules_for(
            metric_id=1, rule_type=QualityRuleType.COMPLETENESS
        )
        assert len(results) == 1


class TestQualityEventRepo:
    async def test_create_event(self, repo: QualityRepository) -> None:
        event = QualityEvent(
            metric_id=1,
            level=QualitySeverity.P0,
            rule_type=QualityRuleType.COMPLETENESS,
        )
        result = await repo.create_event(event)
        assert result is event

    async def test_get_event_found(self, repo: QualityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = QualityEvent(id=1)
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_event(1)
        assert result is not None

    async def test_get_event_not_found(self, repo: QualityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_event(999)
        assert result is None

    async def test_list_events_no_filters(self, repo: QualityRepository) -> None:
        mock_count = MagicMock()
        mock_count.scalar.return_value = 3
        mock_rows = MagicMock()
        mock_rows.scalars.return_value.all.return_value = [
            QualityEvent(id=1),
            QualityEvent(id=2),
            QualityEvent(id=3),
        ]
        repo._db.execute = AsyncMock(side_effect=[mock_count, mock_rows])
        results, total = await repo.list_events(
            metric_id=None, status=None, level=None, page=1, page_size=10
        )
        assert len(results) == 3
        assert total == 3

    async def test_transition_event_ack(self, repo: QualityRepository) -> None:
        event = QualityEvent(id=1, status=QualityEventStatus.OPEN)
        result = await repo.transition_event(
            event, QualityEventStatus.ACK, operator_id=5, ack_note="处理中"
        )
        assert result.status == QualityEventStatus.ACK
        assert result.ack_by == 5
        assert result.ack_at is not None
        assert result.ack_note == "处理中"

    async def test_transition_event_resolve(self, repo: QualityRepository) -> None:
        event = QualityEvent(id=1, status=QualityEventStatus.ACK)
        result = await repo.transition_event(event, QualityEventStatus.RESOLVED, operator_id=5)
        assert result.status == QualityEventStatus.RESOLVED
        assert result.resolved_by == 5
        assert result.resolved_at is not None

    async def test_transition_event_close(self, repo: QualityRepository) -> None:
        event = QualityEvent(id=1, status=QualityEventStatus.RESOLVED)
        result = await repo.transition_event(event, QualityEventStatus.CLOSED, operator_id=5)
        assert result.status == QualityEventStatus.CLOSED
        assert result.closed_by == 5
        assert result.closed_at is not None

    async def test_save_event(self, repo: QualityRepository) -> None:
        event = QualityEvent(id=1)
        result = await repo.save_event(event)
        assert result is event


class TestQualityObservationRepo:
    async def test_record_observation(self, repo: QualityRepository) -> None:
        obs = QualityObservation(metric_id=1, metric_code="M1")
        result = await repo.record_observation(obs)
        assert result is obs

    async def test_list_recent_observations_no_since(self, repo: QualityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            QualityObservation(metric_id=1),
        ]
        repo._db.execute = AsyncMock(return_value=mock_result)
        results = await repo.list_recent_observations(metric_id=1)
        assert len(results) == 1

    async def test_list_recent_observations_with_since(self, repo: QualityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        repo._db.execute = AsyncMock(return_value=mock_result)
        results = await repo.list_recent_observations(
            metric_id=1, since=datetime.now(UTC), limit=100
        )
        assert results == []


class TestExternalBenchmarkRepo:
    async def test_save_benchmark(self, repo: QualityRepository) -> None:
        bench = ExternalBenchmark(source_id="S1", metric_code="M1")
        result = await repo.save_benchmark(bench)
        assert result is bench

    async def test_get_benchmark_found(self, repo: QualityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ExternalBenchmark(id=1)
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_benchmark(1)
        assert result is not None

    async def test_get_benchmark_not_found(self, repo: QualityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_benchmark(999)
        assert result is None

    async def test_find_benchmark_no_dims(self, repo: QualityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            ExternalBenchmark(id=1, dims=None),
        ]
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.find_benchmark(
            source_id="S1",
            metric_code="M1",
            bench_date=__import__("datetime").date(2024, 1, 1),
            dims=None,
        )
        assert result is not None

    async def test_find_benchmark_with_dims(self, repo: QualityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            ExternalBenchmark(id=1, dims={"caliber": "CNY"}),
        ]
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.find_benchmark(
            source_id="S1",
            metric_code="M1",
            bench_date=__import__("datetime").date(2024, 1, 1),
            dims={"caliber": "CNY"},
        )
        assert result is not None

    async def test_find_benchmark_not_found(self, repo: QualityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.find_benchmark(
            source_id="S1",
            metric_code="M1",
            bench_date=__import__("datetime").date(2024, 1, 1),
            dims=None,
        )
        assert result is None

    async def test_list_benchmarks_no_filters(self, repo: QualityRepository) -> None:
        mock_count = MagicMock()
        mock_count.scalar.return_value = 2
        mock_rows = MagicMock()
        mock_rows.scalars.return_value.all.return_value = [
            ExternalBenchmark(id=1),
            ExternalBenchmark(id=2),
        ]
        repo._db.execute = AsyncMock(side_effect=[mock_count, mock_rows])
        results, total = await repo.list_benchmarks(
            metric_code=None, source_id=None, page=1, page_size=10
        )
        assert len(results) == 2


class TestReconciliationRepo:
    async def test_save_reconciliation(self, repo: QualityRepository) -> None:
        rec = ReconciliationRecord(metric_code="M1")
        result = await repo.save_reconciliation(rec)
        assert result is rec

    async def test_get_reconciliation_found(self, repo: QualityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ReconciliationRecord(id=1)
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_reconciliation(1)
        assert result is not None

    async def test_get_reconciliation_not_found(self, repo: QualityRepository) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        repo._db.execute = AsyncMock(return_value=mock_result)
        result = await repo.get_reconciliation(999)
        assert result is None

    async def test_list_reconciliations_no_filters(self, repo: QualityRepository) -> None:
        mock_count = MagicMock()
        mock_count.scalar.return_value = 1
        mock_rows = MagicMock()
        mock_rows.scalars.return_value.all.return_value = [ReconciliationRecord(id=1)]
        repo._db.execute = AsyncMock(side_effect=[mock_count, mock_rows])
        results, total = await repo.list_reconciliations(
            status=None, metric_code=None, page=1, page_size=10
        )
        assert len(results) == 1
