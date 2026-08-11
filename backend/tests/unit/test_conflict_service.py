"""冲突服务编排单元测试（FakeRepo + FakeEvents，无真实 DB）。"""

from __future__ import annotations

from datetime import datetime

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

    async def create_ruling(self, ruling: RulingRecord) -> RulingRecord:
        self._seq += 1
        ruling.id = self._seq
        self.rulings.append(ruling)
        return ruling

    async def get_rulings(self, conflict_id: str) -> list[RulingRecord]:
        return [r for r in self.rulings if r.conflict_id == conflict_id]


def _svc() -> tuple[ConflictService, FakeRepo, FakeEvents]:
    svc = ConflictService(db=object())  # db 不会被 FakeRepo 使用
    repo = FakeRepo()
    events = FakeEvents()
    svc._repo = repo
    svc._events = events
    return svc, repo, events


async def test_check_creates_open_conflict_and_blocks_on_hard() -> None:
    svc, repo, events = _svc()
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
    svc, repo, events = _svc()
    req = ConflictCheckRequest(
        candidate=MetricInput(metric_code="user_pii", domain="sales", definition="x", has_pii=True),
        existing=[MetricInput(metric_code="other", domain="sales", definition="y")],
    )
    result = await svc.check(req.candidate, req.existing)
    assert result.blocked is True
    assert len(repo.conflicts) == 0  # PII 不入普通冲突表
    assert any(e["event_type"] == "pii_conflict" for e in events.published)


async def test_arbitrate_transitions_to_ruled_and_records() -> None:
    svc, repo, events = _svc()
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
    svc, repo, events = _svc()
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
