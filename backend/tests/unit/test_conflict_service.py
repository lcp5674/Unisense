"""冲突服务编排单元测试（FakeRepo + FakeEvents，无真实 DB）。"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core.exceptions import ConflictError
from app.models.conflict import Conflict, ConflictStatus, ConflictType, RulingRecord
from app.services.conflict.schemas import (
    ArbitrateRequest,
    ConflictCheckRequest,
    EscalateRequest,
    MetricInput,
)
from app.services.conflict.service import ConflictService


class FakeEvents:
    def __init__(self) -> None:
        self.published: list[dict] = []

    async def publish(self, event: dict) -> None:
        self.published.append(event)


class FakeRepo:
    def __init__(self) -> None:
        self.conflicts: list[Conflict] = []
        self.rulings: list[RulingRecord] = []
        self._seq = 0
        self.open_by_metric: dict[str, int] = {}

    async def create(self, conflict: Conflict) -> Conflict:
        self._seq += 1
        conflict.id = self._seq
        self.conflicts.append(conflict)
        return conflict

    async def get_by_conflict_id(self, conflict_id: str) -> Conflict | None:
        for c in self.conflicts:
            if c.conflict_id == conflict_id:
                return c
        return None

    async def list_conflicts(self, status, ctype, domain, page, page_size):
        rows = list(self.conflicts)
        return rows, len(rows)

    async def update_status(
        self, conflict, status, arbitrator_id=None, decision_json=None, resolved=False
    ):
        conflict.status = status
        if arbitrator_id is not None:
            conflict.arbitrator_id = arbitrator_id
        if decision_json is not None:
            conflict.decision_json = decision_json
        if resolved:
            conflict.resolved_at = datetime.utcnow()
        return conflict

    async def reopen(self, conflict):
        conflict.status = ConflictStatus.OPEN
        conflict.resolved_at = None
        return conflict

    async def create_ruling(self, ruling: RulingRecord) -> RulingRecord:
        self._seq += 1
        ruling.id = self._seq
        self.rulings.append(ruling)
        return ruling

    async def get_rulings(self, conflict_id: str) -> list[RulingRecord]:
        return [r for r in self.rulings if r.conflict_id == conflict_id]

    async def count_open_for_metric(self, metric_code: str) -> int:
        return self.open_by_metric.get(metric_code, 0)


class FakeClearer:
    def __init__(self) -> None:
        self.cleared: list[str] = []

    async def __call__(self, metric_code: str) -> None:
        self.cleared.append(metric_code)


class FakeMarker:
    """记录重新打开后回置 pending_conflict 标记的调用（metric_code, conflict）。"""

    def __init__(self) -> None:
        self.marked: list[tuple[str, Conflict]] = []

    async def __call__(self, metric_code: str, conflict: Conflict) -> None:
        self.marked.append((metric_code, conflict))


def _svc(
    clearer: FakeClearer | None = None,
    marker: FakeMarker | None = None,
) -> tuple[ConflictService, FakeRepo, FakeEvents, FakeClearer | None]:
    fake_clearer = clearer if clearer is not None else FakeClearer()
    fake_marker = marker if marker is not None else FakeMarker()
    svc = ConflictService(
        db=object(),
        metric_conflict_clearer=fake_clearer,
        metric_conflict_marker=fake_marker,
    )
    repo = FakeRepo()
    events = FakeEvents()
    svc._repo = repo
    svc._events = events
    return svc, repo, events, fake_clearer


async def test_check_creates_open_conflict_and_blocks_on_hard() -> None:
    svc, repo, events, _ = _svc()
    req = ConflictCheckRequest(
        candidate=MetricInput(metric_code="gmv_total", domain="sales", definition="sum(amount)"),
        existing=[MetricInput(metric_code="gmv_total", domain="finance", definition="sum(price)")],
    )
    result = await svc.check(req.candidate, req.existing)
    assert result.blocked is True
    assert len(repo.conflicts) == 1
    assert repo.conflicts[0].status == ConflictStatus.OPEN
    assert repo.conflicts[0].type == ConflictType.SAME_NAME_DIFF_DEF
    # 非 PII，发 conflict_open 通知
    assert any(e["event_type"] == "conflict_open" for e in events.published)


async def test_check_pii_routes_to_governance_not_stored() -> None:
    svc, repo, events, _ = _svc()
    req = ConflictCheckRequest(
        candidate=MetricInput(metric_code="user_pii", domain="sales", definition="x", has_pii=True),
        existing=[MetricInput(metric_code="other", domain="sales", definition="y")],
    )
    result = await svc.check(req.candidate, req.existing)
    assert result.blocked is True
    assert len(repo.conflicts) == 0  # PII 不入普通冲突表
    assert any(e["event_type"] == "pii_conflict" for e in events.published)


async def test_arbitrate_transitions_to_ruled_and_records() -> None:
    svc, repo, events, clearer = _svc()
    req = ConflictCheckRequest(
        candidate=MetricInput(metric_code="gmv_total", domain="sales", definition="sum(amount)"),
        existing=[MetricInput(metric_code="gmv_total", domain="finance", definition="sum(price)")],
    )
    await svc.check(req.candidate, req.existing)
    assert repo.conflicts
    conflict_id = repo.conflicts[0].conflict_id
    arb = ArbitrateRequest(
        decision="merge",
        canonical_metric_code="gmv_total",
        arbitrator_id=99,
        reason="口径一致建议合并",
    )
    conflict = await svc.arbitrate(conflict_id, arb)
    assert conflict.status == ConflictStatus.RULED
    assert conflict.arbitrator_id == 99
    assert conflict.decision_json["decision"] == "merge"
    assert len(repo.rulings) == 1
    assert repo.rulings[0].arbitrator_id == 99
    assert any(e["event_type"] == "conflict_ruled" for e in events.published)


async def test_escalate_transitions_to_escalated() -> None:
    svc, repo, events, _ = _svc()
    req = ConflictCheckRequest(
        candidate=MetricInput(metric_code="gmv_total", domain="sales", definition="sum(amount)"),
        existing=[MetricInput(metric_code="gmv_total", domain="finance", definition="sum(price)")],
    )
    await svc.check(req.candidate, req.existing)
    assert repo.conflicts
    conflict_id = repo.conflicts[0].conflict_id
    conflict = await svc.escalate(conflict_id, EscalateRequest(note="超时未协商，升级"))
    assert conflict.status == ConflictStatus.ESCALATED
    assert any(e["event_type"] == "conflict_escalated" for e in events.published)


async def _ruled_conflict(svc: ConflictService, repo: FakeRepo) -> Conflict:
    """构造一条已 RULED 的冲突（含 metric_codes 候选/现有），返回仲裁后的 conflict。"""
    req = ConflictCheckRequest(
        candidate=MetricInput(metric_code="gmv_total", domain="sales", definition="sum(amount)"),
        existing=[MetricInput(metric_code="gmv_total", domain="finance", definition="sum(price)")],
    )
    await svc.check(req.candidate, req.existing)
    conflict_id = repo.conflicts[0].conflict_id
    return await svc.arbitrate(
        conflict_id,
        ArbitrateRequest(decision="choose_canonical", canonical_metric_code="gmv_total", reason="选候选为权威"),
        actor_id=1,
    )


async def test_arbitrate_clears_metric_pending_conflict_when_no_remaining() -> None:
    svc, repo, _, clearer = _svc()
    conflict = await _ruled_conflict(svc, repo)
    assert conflict.status == ConflictStatus.RULED
    # 候选指标无其他未决冲突 → 清除标记
    assert clearer.cleared == ["gmv_total"]


async def test_arbitrate_keeps_flag_when_other_open_conflicts() -> None:
    svc, repo, _, clearer = _svc()
    repo.open_by_metric["gmv_total"] = 1  # 该指标还有其他未决冲突
    conflict = await _ruled_conflict(svc, repo)
    assert conflict.status == ConflictStatus.RULED
    assert clearer.cleared == []  # 不清除，避免误清


async def test_close_triggers_clearer_for_historical_ruled() -> None:
    svc, repo, _, clearer = _svc()
    conflict = await _ruled_conflict(svc, repo)
    clearer.cleared.clear()
    await svc.close(conflict.conflict_id)
    assert clearer.cleared == ["gmv_total"]


async def test_arbitrate_without_clearer_is_noop() -> None:
    svc = ConflictService(db=object())  # 未注入 clearer：联动为 no-op
    repo = FakeRepo()
    events = FakeEvents()
    svc._repo = repo
    svc._events = events
    conflict = await _ruled_conflict(svc, repo)
    assert conflict.status == ConflictStatus.RULED


async def test_reopen_closed_marks_metric_and_transitions_to_open() -> None:
    svc, repo, events, _ = _svc()
    conflict = await _ruled_conflict(svc, repo)
    conflict = await svc.close(conflict.conflict_id)
    assert conflict.status == ConflictStatus.CLOSED
    # 重新打开：CLOSED → OPEN、清除 resolved_at、回置指标冲突标记、发事件
    marker = FakeMarker()
    svc._metric_conflict_marker = marker
    reopened = await svc.reopen(conflict.conflict_id)
    assert reopened.status == ConflictStatus.OPEN
    assert reopened.resolved_at is None
    assert marker.marked and marker.marked[0][0] == "gmv_total"
    assert any(e["event_type"] == "conflict_reopened" for e in events.published)


async def test_reopen_non_closed_raises() -> None:
    svc, repo, _, _ = _svc()
    conflict = await _ruled_conflict(svc, repo)  # RULED（未关闭）
    with pytest.raises(ConflictError):
        await svc.reopen(conflict.conflict_id)


async def test_reopen_open_raises() -> None:
    svc, repo, _, _ = _svc()
    req = ConflictCheckRequest(
        candidate=MetricInput(metric_code="gmv_total", domain="sales", definition="sum(amount)"),
        existing=[MetricInput(metric_code="gmv_total", domain="finance", definition="sum(price)")],
    )
    await svc.check(req.candidate, req.existing)  # OPEN（未裁决）
    with pytest.raises(ConflictError):
        await svc.reopen(repo.conflicts[0].conflict_id)


async def test_reopen_without_marker_is_noop() -> None:
    svc = ConflictService(db=object())  # 未注入 marker：联动为 no-op
    repo = FakeRepo()
    events = FakeEvents()
    svc._repo = repo
    svc._events = events
    conflict = await _ruled_conflict(svc, repo)
    conflict = await svc.close(conflict.conflict_id)
    reopened = await svc.reopen(conflict.conflict_id)
    assert reopened.status == ConflictStatus.OPEN
    assert reopened.resolved_at is None
