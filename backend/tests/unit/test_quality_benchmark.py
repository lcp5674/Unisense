"""质量外部基准对账（D11 / TD §4.15.7）单元测试。

聚焦 QualityService 的核心逻辑：基准导入幂等、对账差异状态判定（OK/WARN/ALERT）、
Owner 确认守卫。使用内存 FakeRepo + FakePublisher，不依赖真实数据库。
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from app.core.exceptions import ValidationError
from app.models.quality import (
    ExternalBenchmark,
    ReconciliationRecord,
    ReconciliationStatus,
)
from app.services.quality.schemas import (
    BenchmarkBind,
    BenchmarkImport,
    ReconciliationConfirm,
    ReconciliationRun,
)
from app.services.quality.service import QualityService


class FakeRepo:
    """内存版 QualityRepository 替身，仅实现 service 调用到的公开方法。"""

    def __init__(self) -> None:
        self.benchmarks: list[ExternalBenchmark] = []
        self.reconciliations: list[ReconciliationRecord] = []
        self._bid = 0
        self._rid = 0

    async def find_benchmark(self, source_id, metric_code, bench_date, dims):
        for b in self.benchmarks:
            cond = (
                b.source_id != source_id
                or b.metric_code != metric_code
                or b.bench_date != bench_date
            )
            if cond:
                continue
            if dims is None:
                if b.dims is None:
                    return b
            elif b.dims is not None and json.dumps(b.dims, sort_keys=True) == json.dumps(
                dims, sort_keys=True
            ):
                return b
        return None

    async def save_benchmark(self, bench: ExternalBenchmark) -> ExternalBenchmark:
        if bench.id is None:
            self._bid += 1
            bench.id = self._bid
        if bench not in self.benchmarks:
            self.benchmarks.append(bench)
        return bench

    async def get_benchmark(self, benchmark_id: int):
        return next((b for b in self.benchmarks if b.id == benchmark_id), None)

    async def list_benchmarks(self, metric_code, source_id, page, page_size):
        rows = self.benchmarks
        if metric_code is not None:
            rows = [b for b in rows if b.metric_code == metric_code]
        if source_id is not None:
            rows = [b for b in rows if b.source_id == source_id]
        start = (page - 1) * page_size
        return rows[start : start + page_size], len(rows)

    async def save_reconciliation(self, rec: ReconciliationRecord) -> ReconciliationRecord:
        if rec.id is None:
            self._rid += 1
            rec.id = self._rid
        if rec not in self.reconciliations:
            self.reconciliations.append(rec)
        return rec

    async def get_reconciliation(self, record_id: int):
        return next((r for r in self.reconciliations if r.id == record_id), None)

    async def list_reconciliations(self, status, metric_code, page, page_size):
        rows = self.reconciliations
        if status is not None:
            rows = [r for r in rows if r.status.value == status]
        if metric_code is not None:
            rows = [r for r in rows if r.metric_code == metric_code]
        start = (page - 1) * page_size
        return rows[start : start + page_size], len(rows)


class FakePublisher:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, event: dict) -> None:
        self.events.append(event)


def _make_service() -> tuple[QualityService, FakeRepo, FakePublisher]:
    svc = QualityService(None)  # type: ignore[arg-type]
    repo = FakeRepo()
    pub = FakePublisher()
    svc._repo = repo  # noqa: SLF001 - 测试注入
    svc._publisher = pub  # noqa: SLF001
    return svc, repo, pub


def _benchmark(
    bench_value: Decimal = Decimal("100"), tolerance: Decimal | None = None
) -> ExternalBenchmark:
    b = ExternalBenchmark(
        source_id="SRC1",
        metric_code="M1",
        bench_date=date(2024, 1, 1),
        dims={"caliber": "CNY"},
        bench_value=bench_value,
        provider="audit",
        tolerance_pct=tolerance,
        imported_by=1,
    )
    b.id = 1
    return b


async def test_import_benchmark_idempotent_updates_same_row() -> None:
    svc, repo, pub = _make_service()
    payload = BenchmarkImport(
        source_id="SRC1",
        metric_code="M1",
        bench_date=date(2024, 1, 1),
        dims={"caliber": "CNY"},
        bench_value=Decimal("100"),
        provider="audit",
    )
    first = await svc.import_benchmark(payload, user_id=1)
    payload_2 = payload.model_copy(update={"bench_value": Decimal("105")})
    second = await svc.import_benchmark(payload_2, user_id=2)

    # 幂等：同一 key 返回同一行 id（更新而非新建）
    assert first.id == second.id == 1
    assert len(repo.benchmarks) == 1
    assert repo.benchmarks[0].bench_value == Decimal("105")
    assert repo.benchmarks[0].imported_by == 2
    # 导入事件已发布
    assert any(e["event_type"] == "benchmark.imported" for e in pub.events)


async def test_import_benchmark_distinct_dims_create_separate_rows() -> None:
    svc, repo, _ = _make_service()
    base: dict = {
        "source_id": "SRC1",
        "metric_code": "M1",
        "bench_date": date(2024, 1, 1),
        "bench_value": Decimal("100"),
        "provider": "audit",
    }
    await svc.import_benchmark(BenchmarkImport(**base, dims={"caliber": "CNY"}), user_id=1)
    await svc.import_benchmark(BenchmarkImport(**base, dims={"caliber": "USD"}), user_id=1)
    assert len(repo.benchmarks) == 2


async def test_run_reconciliation_status_thresholds() -> None:
    svc, repo, pub = _make_service()
    repo.benchmarks.append(_benchmark(bench_value=Decimal("100"), tolerance=Decimal("1.00")))

    # 完全吻合 → OK
    ok = await svc.run_reconciliation(
        ReconciliationRun(benchmark_id=1, metric_value=Decimal("100")), user_id=1
    )
    assert ok.status == ReconciliationStatus.OK
    assert ok.diff_pct == Decimal("0")

    # 差异 1.00% = 容忍率 → OK（边界）
    warn_ok = await svc.run_reconciliation(
        ReconciliationRun(benchmark_id=1, metric_value=Decimal("101")), user_id=1
    )
    assert warn_ok.status == ReconciliationStatus.OK

    # 差异 2.00% = 2 倍容忍率 → WARN（边界）
    warn = await svc.run_reconciliation(
        ReconciliationRun(benchmark_id=1, metric_value=Decimal("102")), user_id=1
    )
    assert warn.status == ReconciliationStatus.WARN

    # 差异 3.00% > 2 倍容忍率 → ALERT + 告警事件
    alert = await svc.run_reconciliation(
        ReconciliationRun(benchmark_id=1, metric_value=Decimal("103")), user_id=1
    )
    assert alert.status == ReconciliationStatus.ALERT
    assert any(e["event_type"] == "reconciliation.alert" for e in pub.events)


async def test_run_reconciliation_default_tolerance_when_none() -> None:
    svc, repo, _ = _make_service()
    repo.benchmarks.append(_benchmark(bench_value=Decimal("100"), tolerance=None))
    # 容忍率默认 1.00，差异 2% → WARN
    rec = await svc.run_reconciliation(
        ReconciliationRun(benchmark_id=1, metric_value=Decimal("102")), user_id=1
    )
    assert rec.status == ReconciliationStatus.WARN


async def test_run_reconciliation_zero_bench_value_rejected() -> None:
    svc, repo, _ = _make_service()
    repo.benchmarks.append(_benchmark(bench_value=Decimal("0"), tolerance=None))
    with pytest.raises(ValidationError):
        await svc.run_reconciliation(
            ReconciliationRun(benchmark_id=1, metric_value=Decimal("5")), user_id=1
        )


async def test_confirm_reconciliation_sets_decision_and_guards_double_confirm() -> None:
    svc, repo, _ = _make_service()
    repo.benchmarks.append(_benchmark())
    rec = await svc.run_reconciliation(
        ReconciliationRun(benchmark_id=1, metric_value=Decimal("103")), user_id=1
    )
    confirmed = await svc.confirm_reconciliation(
        rec.id,
        ReconciliationConfirm(decision="caliber_error", owner_note="口径有误，走变更"),
        user_id=9,
    )
    assert confirmed.status == ReconciliationStatus.CONFIRMED
    assert confirmed.decision == "caliber_error"
    assert confirmed.confirmed_by == 9
    assert confirmed.checked_at is not None
    # 已确认不可重复确认
    with pytest.raises(ValidationError):
        await svc.confirm_reconciliation(
            rec.id, ReconciliationConfirm(decision="reasonable"), user_id=9
        )


async def test_bind_benchmark_updates_target_and_tolerance() -> None:
    svc, repo, _ = _make_service()
    repo.benchmarks.append(_benchmark())
    resp = await svc.bind_benchmark(
        1,
        BenchmarkBind(metric_code="M2", tolerance_pct=Decimal("2.50"), dims={"caliber": "USD"}),
        user_id=1,
    )
    assert resp.metric_code == "M2"
    assert resp.tolerance_pct == Decimal("2.50")
    assert resp.dims == {"caliber": "USD"}
