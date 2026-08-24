"""数值正确性对账加固单测（P1：自动抽样 + 对账触发任务）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.quality.service import QualityService
from app.services.quality.tasks import run_reconciliation_checks


@pytest.fixture
def db() -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock()
    return session


def _rows(*pairs: tuple[int, str, str]) -> MagicMock:
    mock = MagicMock()
    mock.all.return_value = pairs
    return mock


class TestSampleReconciliationTargets:
    async def test_tier1_full_and_tier2_sampled(self, db: MagicMock) -> None:
        svc = QualityService(db)
        # 2 个 T1（各 1 条基准）+ 4 个 T2 基准（按 0.5 抽样 → 2 条）
        db.execute = AsyncMock(
            return_value=_rows(
                (1, "m_t1a", "T1"),
                (2, "m_t1b", "T1"),
                (3, "m_t2a", "T2"),
                (4, "m_t2b", "T2"),
                (5, "m_t2c", "T2"),
                (6, "m_t2d", "T2"),
            )
        )
        targets = await svc.sample_reconciliation_targets(tier2_ratio=0.5, seed=7)
        codes = [t["metric_code"] for t in targets]
        tiers = {t["metric_code"]: t["metric_tier"] for t in targets}
        # T1 全量
        assert "m_t1a" in codes and "m_t1b" in codes
        assert tiers["m_t1a"] == "T1"
        # T2 按 0.5 概率对指标抽样（seed=7 命中 3/4）
        t2_codes = [c for c in codes if c.startswith("m_t2")]
        assert len(t2_codes) == 3

    async def test_due_benchmark_ids_filters(self, db: MagicMock) -> None:
        svc = QualityService(db)
        db.execute = AsyncMock(
            return_value=_rows(
                (1, "m_t1a", "T1"),
                (2, "m_t1b", "T1"),
            )
        )
        targets = await svc.sample_reconciliation_targets(due_benchmark_ids={2}, seed=1)
        assert [t["benchmark_id"] for t in targets] == [2]

    async def test_tier3_excluded_by_default(self, db: MagicMock) -> None:
        svc = QualityService(db)
        db.execute = AsyncMock(
            return_value=_rows((1, "m_t3a", "T3"), (2, "m_t3b", "T3"))
        )
        targets = await svc.sample_reconciliation_targets(seed=1)
        assert targets == []

    async def test_seed_reproducible(self, db: MagicMock) -> None:
        svc = QualityService(db)
        db.execute = AsyncMock(
            return_value=_rows(
                (1, "m_t2a", "T2"),
                (2, "m_t2b", "T2"),
                (3, "m_t2c", "T2"),
                (4, "m_t2d", "T2"),
            )
        )
        first = await svc.sample_reconciliation_targets(tier2_ratio=0.5, seed=42)
        second = await svc.sample_reconciliation_targets(tier2_ratio=0.5, seed=42)
        assert first == second


class TestRunReconciliationChecks:
    async def test_publishes_due_reminders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = MagicMock()
        session.commit = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        def fake_factory():
            return session

        monkeypatch.setattr("app.db.mysql.async_session_factory", fake_factory)
        monkeypatch.setattr(
            "app.services.quality.repository.QualityRepository.list_due_benchmark_ids",
            AsyncMock(return_value=[10, 11]),
        )

        publisher = MagicMock()
        publisher.publish = AsyncMock()

        class _FakeService:
            def __init__(self, db: object) -> None:
                self._publisher = publisher

            async def sample_reconciliation_targets(self, **kwargs: object) -> list[dict]:
                return [
                    {"benchmark_id": 10, "metric_code": "m_t1a", "metric_tier": "T1"},
                    {"benchmark_id": 11, "metric_code": "m_t2a", "metric_tier": "T2"},
                ]

        monkeypatch.setattr("app.services.quality.service.QualityService", _FakeService)

        result = await run_reconciliation_checks({}, period_days=7)
        assert result == {"due": 2, "sampled": 2, "reminded": 2}
        assert publisher.publish.await_count == 2
        event = publisher.publish.await_args.args[0]
        assert event["event_type"] == "reconciliation.due"
        assert event["metric_code"] == "m_t2a"

    async def test_no_due_targets_reminds_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = MagicMock()
        session.commit = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        def fake_factory():
            return session

        monkeypatch.setattr("app.db.mysql.async_session_factory", fake_factory)
        monkeypatch.setattr(
            "app.services.quality.repository.QualityRepository.list_due_benchmark_ids",
            AsyncMock(return_value=[]),
        )

        publisher = MagicMock()
        publisher.publish = AsyncMock()

        class _FakeService:
            def __init__(self, db: object) -> None:
                self._publisher = publisher

            async def sample_reconciliation_targets(self, **kwargs: object) -> list[dict]:
                return []

        monkeypatch.setattr("app.services.quality.service.QualityService", _FakeService)

        result = await run_reconciliation_checks({})
        assert result == {"due": 0, "sampled": 0, "reminded": 0}
        publisher.publish.assert_not_called()
