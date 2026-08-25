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
        # code -> 活动指标行 id（默认空 = 候选码尚未落库，属「新提交」形态）
        self.metric_ids: dict[str, int] = {}
        # (candidate, existing) -> 已有未决冲突（重复冲突去重）
        self.open_pairs: set[tuple[str, str]] = set()

    async def create(self, conflict: Conflict) -> Conflict:
        self._seq += 1
        conflict.id = self._seq
        self.conflicts.append(conflict)
        return conflict

    async def resolve_active_metric_id(self, metric_code: str) -> int | None:
        return self.metric_ids.get(metric_code)

    async def count_open_for_pair(self, candidate_code: str, existing_code: str) -> int:
        return 1 if (candidate_code, existing_code) in self.open_pairs else 0

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


class FakeApplier:
    """记录仲裁联动指标回调的调用（conflict, decision, canonical_code, actor_id）。"""

    def __init__(self) -> None:
        self.applied: list[tuple[Conflict, str, str | None, int, str | None]] = []
        self.fail = False

    async def __call__(
        self,
        conflict: Conflict,
        decision: str,
        canonical_code: str | None,
        actor_id: int,
        *,
        rename_code: str | None = None,
    ) -> None:
        if self.fail:
            raise RuntimeError("联动失败（降级测试）")
        self.applied.append((conflict, decision, canonical_code, actor_id, rename_code))


def _svc(
    clearer: FakeClearer | None = None,
    marker: FakeMarker | None = None,
    applier: FakeApplier | None = None,
) -> tuple[ConflictService, FakeRepo, FakeEvents, FakeClearer | None, FakeApplier | None]:
    fake_clearer = clearer if clearer is not None else FakeClearer()
    fake_marker = marker if marker is not None else FakeMarker()
    fake_applier = applier if applier is not None else FakeApplier()
    svc = ConflictService(
        db=object(),
        metric_conflict_clearer=fake_clearer,
        metric_conflict_marker=fake_marker,
        arbitration_applier=fake_applier,
    )
    repo = FakeRepo()
    events = FakeEvents()
    svc._repo = repo
    svc._events = events
    return svc, repo, events, fake_clearer, fake_applier


async def test_check_creates_open_conflict_and_blocks_on_hard() -> None:
    svc, repo, events, _, _ = _svc()
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
    svc, repo, events, _, _ = _svc()
    req = ConflictCheckRequest(
        candidate=MetricInput(metric_code="user_pii", domain="sales", definition="x", has_pii=True),
        existing=[MetricInput(metric_code="other", domain="sales", definition="y")],
    )
    result = await svc.check(req.candidate, req.existing)
    assert result.blocked is True
    assert len(repo.conflicts) == 0  # PII 不入普通冲突表
    assert any(e["event_type"] == "pii_conflict" for e in events.published)


async def test_check_drops_self_reference_by_metric_id() -> None:
    """候选/现有携带同一 metric_id（同一条指标行）→ 自我引用被剔除，不落库。"""
    svc, repo, events, _, _ = _svc()
    repo.metric_ids["gmv_total"] = 7
    req = ConflictCheckRequest(
        candidate=MetricInput(
            metric_code="gmv_total", domain="sales", definition="sum(amount)", metric_id=7
        ),
        existing=[
            MetricInput(
                metric_code="gmv_total", domain="finance", definition="sum(price)", metric_id=7
            )
        ],
    )
    result = await svc.check(req.candidate, req.existing)
    assert len(repo.conflicts) == 0  # 同一行不构成冲突，绝不落库
    assert result.detections == []
    assert result.blocked is False
    assert not any(e["event_type"] == "conflict_open" for e in events.published)


async def test_check_drops_self_reference_by_code_resolution() -> None:
    """候选/现有同码且都解析到同一活动指标行（调用方未传 metric_id）→ 剔除。

    这是真实事故形态：check 时把已存在的候选自身塞进 existing，两码相同、
    均解析到同一行——必须拦截，否则冲突表会出现永远无法正常裁决的自我冲突。
    """
    svc, repo, events, _, _ = _svc()
    repo.metric_ids["sales_conflicta_day"] = 11
    req = ConflictCheckRequest(
        candidate=MetricInput(
            metric_code="sales_conflicta_day", domain="sales", definition="sum(amount)"
        ),
        existing=[
            MetricInput(
                metric_code="sales_conflicta_day", domain="sales", definition="sum(price)"
            ),
            MetricInput(metric_code="sales_conflictb_day", domain="sales", definition="sum(x)"),
        ],
    )
    result = await svc.check(req.candidate, req.existing)
    # 自我引用被剔除：检测结果只针对合法对（conflictb_day），绝不含自我引用
    assert [d.existing_code for d in result.detections] == ["sales_conflictb_day"]
    assert len(repo.conflicts) == 1
    assert repo.conflicts[0].metric_codes == {
        "candidate": "sales_conflicta_day",
        "existing": "sales_conflictb_day",
    }
    # 只为合法对发冲突事件；被剔除的自我引用不产生任何事件
    opens = [e for e in events.published if e["event_type"] == "conflict_open"]
    assert len(opens) == 1


async def test_check_keeps_legit_same_name_diff_def_when_candidate_new() -> None:
    """候选码未落库（新提交）时，与同码已存在行构成合法同名不同义 → 照常落库。"""
    svc, repo, _, _, _ = _svc()
    # metric_ids 为空 → 候选解析为 None（新提交形态），保留同码条目
    req = ConflictCheckRequest(
        candidate=MetricInput(metric_code="gmv_total", domain="sales", definition="sum(amount)"),
        existing=[MetricInput(metric_code="gmv_total", domain="finance", definition="sum(price)")],
    )
    result = await svc.check(req.candidate, req.existing)
    assert result.blocked is True
    assert len(repo.conflicts) == 1
    assert repo.conflicts[0].type == ConflictType.SAME_NAME_DIFF_DEF


async def test_check_skips_duplicate_open_conflict_for_same_pair() -> None:
    """同一（候选, 现有）对已有未决冲突 → 不重复落库，但检测结果仍上报。"""
    svc, repo, _, _, _ = _svc()
    repo.open_pairs.add(("gmv_total", "gmv_total"))
    req = ConflictCheckRequest(
        candidate=MetricInput(metric_code="gmv_total", domain="sales", definition="sum(amount)"),
        existing=[MetricInput(metric_code="gmv_total", domain="finance", definition="sum(price)")],
    )
    result = await svc.check(req.candidate, req.existing)
    # 检测照常上报（blocked 语义不变），但不重复创建 OPEN 冲突
    assert result.blocked is True
    assert len(result.detections) == 1
    assert len(repo.conflicts) == 0


async def test_arbitrate_transitions_to_ruled_and_records() -> None:
    svc, repo, events, clearer, _ = _svc()
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
    svc, repo, events, _, _ = _svc()
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
        ArbitrateRequest(
            decision="choose_canonical",
            canonical_metric_code="gmv_total",
            reason="选候选为权威",
        ),
        actor_id=1,
    )


async def test_arbitrate_clears_metric_pending_conflict_when_no_remaining() -> None:
    svc, repo, _, clearer, _ = _svc()
    conflict = await _ruled_conflict(svc, repo)
    assert conflict.status == ConflictStatus.RULED
    # 候选指标无其他未决冲突 → 清除标记
    assert clearer.cleared == ["gmv_total"]


async def test_arbitrate_keeps_flag_when_other_open_conflicts() -> None:
    svc, repo, _, clearer, _ = _svc()
    repo.open_by_metric["gmv_total"] = 1  # 该指标还有其他未决冲突
    conflict = await _ruled_conflict(svc, repo)
    assert conflict.status == ConflictStatus.RULED
    assert clearer.cleared == []  # 不清除，避免误清


async def test_close_triggers_clearer_for_historical_ruled() -> None:
    svc, repo, _, clearer, _ = _svc()
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
    svc, repo, events, _, _ = _svc()
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
    svc, repo, _, _, _ = _svc()
    conflict = await _ruled_conflict(svc, repo)  # RULED（未关闭）
    with pytest.raises(ConflictError):
        await svc.reopen(conflict.conflict_id)


async def test_reopen_open_raises() -> None:
    svc, repo, _, _, _ = _svc()
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


# ---- 仲裁联动指标（TD §12.4）----


async def test_arbitrate_triggers_arbitration_applier() -> None:
    """仲裁成功后调用 arbitration_applier（含决策/权威方/仲裁人），联动指标。"""
    svc, repo, _, _, applier = _svc()
    conflict = await _ruled_conflict(svc, repo)
    assert applier is not None
    assert len(applier.applied) == 1
    applied_conflict, decision, canonical, actor, _rename = applier.applied[0]
    assert applied_conflict.conflict_id == conflict.conflict_id
    assert decision == "choose_canonical"
    assert canonical == "gmv_total"
    assert actor == 1  # actor_id 透传
    assert _rename is None  # 未指定改名目标


async def test_arbitrate_applier_exception_degrades() -> None:
    """仲裁联动失败仅告警，不阻断仲裁主流程。"""
    applier = FakeApplier()
    applier.fail = True
    svc, repo, events, _, _ = _svc(applier=applier)
    conflict = await _ruled_conflict(svc, repo)
    assert conflict.status == ConflictStatus.RULED
    assert len(repo.rulings) == 1
    assert any(e["event_type"] == "conflict_ruled" for e in events.published)


async def test_arbitrate_without_applier_is_noop() -> None:
    """未注入 arbitration_applier：仲裁正常完成，不联动指标。"""
    svc = ConflictService(db=object())
    repo = FakeRepo()
    events = FakeEvents()
    svc._repo = repo
    svc._events = events
    conflict = await _ruled_conflict(svc, repo)
    assert conflict.status == ConflictStatus.RULED


# ---- reopen 跨服务一致性：关联指标已作废时拒绝重新打开 ----


class _ArchivedChecker:
    """模拟「指标已因仲裁软删作废」校验器：记录被检查的指标，按集合判定是否作废。"""

    def __init__(self, archived: set[str]) -> None:
        self.archived = archived
        self.checked: list[str] = []

    async def __call__(self, metric_code: str) -> bool:
        self.checked.append(metric_code)
        return metric_code in self.archived


async def test_reopen_rejects_when_linked_metric_archived() -> None:
    """跨服务一致性：关联指标已因仲裁软删作废时，reopen 拒绝并保持 CLOSED。"""
    svc, repo, _, _, _ = _svc()
    conflict = await _ruled_conflict(svc, repo)
    conflict = await svc.close(conflict.conflict_id)
    codes = conflict.metric_codes or {}
    checker = _ArchivedChecker({codes["candidate"]})
    svc._metric_archived_checker = checker
    with pytest.raises(ConflictError) as excinfo:
        await svc.reopen(conflict.conflict_id)
    assert "已因仲裁作废" in str(excinfo.value)
    # 冲突状态未被破坏（仍 CLOSED，未产生「OPEN 引用已作废指标」的矛盾状态）
    assert repo.conflicts[-1].status == ConflictStatus.CLOSED
    # 候选指标被校验器检查过
    assert codes["candidate"] in checker.checked


async def test_reopen_allows_when_linked_metrics_active() -> None:
    """关联指标均有效（未作废）时，reopen 正常放行。"""
    svc, repo, _, _, _ = _svc()
    conflict = await _ruled_conflict(svc, repo)
    conflict = await svc.close(conflict.conflict_id)
    checker = _ArchivedChecker(set())
    svc._metric_archived_checker = checker
    reopened = await svc.reopen(conflict.conflict_id)
    assert reopened.status == ConflictStatus.OPEN
    assert checker.checked  # 校验器被调用过（候选/现有均检查）


async def test_reopen_skips_check_when_checker_missing() -> None:
    """未注入校验器时 reopen 行为不变（向后兼容）。"""
    svc, repo, _, _, _ = _svc()  # 不注入 metric_archived_checker
    conflict = await _ruled_conflict(svc, repo)
    conflict = await svc.close(conflict.conflict_id)
    reopened = await svc.reopen(conflict.conflict_id)
    assert reopened.status == ConflictStatus.OPEN
    assert reopened.resolved_at is None


async def test_arbitrate_keep_diff_rename_writes_mark_and_passes_rename_code() -> None:
    """「保留差异+指定一方改名」：rename_code 透传给联动回调，decision_json 记录改名目标。"""
    svc, repo, events, _, applier = _svc()
    req = ConflictCheckRequest(
        candidate=MetricInput(metric_code="gmv_total", domain="sales", definition="sum(amount)"),
        existing=[MetricInput(metric_code="gmv_total", domain="finance", definition="sum(price)")],
    )
    await svc.check(req.candidate, req.existing)
    conflict_id = repo.conflicts[0].conflict_id
    conflict = await svc.arbitrate(
        conflict_id,
        ArbitrateRequest(
            decision="keep_diff",
            rename_metric_code="gmv_total",
            reason="同名不同义，保留差异并让候选改名",
        ),
        actor_id=1,
    )
    assert conflict.status == ConflictStatus.RULED
    assert conflict.decision_json["rename_metric_code"] == "gmv_total"
    # 联动回调收到 rename_code
    assert applier is not None
    assert applier.applied
    _conflict, _decision, _canonical, _actor, rename = applier.applied[-1]
    assert rename == "gmv_total"
    # 事件携带 rename_metric_code
    ruled = [e for e in events.published if e["event_type"] == "conflict_ruled"]
    assert ruled and ruled[-1]["payload"].get("rename_metric_code") == "gmv_total"


async def test_arbitrate_rename_target_must_be_conflict_party() -> None:
    """改名目标不在冲突双方中 → 拒绝仲裁（ConflictError）。"""
    svc, repo, _, _, _ = _svc()
    req = ConflictCheckRequest(
        candidate=MetricInput(metric_code="gmv_total", domain="sales", definition="sum(amount)"),
        existing=[MetricInput(metric_code="gmv_total", domain="finance", definition="sum(price)")],
    )
    await svc.check(req.candidate, req.existing)
    conflict_id = repo.conflicts[0].conflict_id
    with pytest.raises(ConflictError):
        await svc.arbitrate(
            conflict_id,
            ArbitrateRequest(
                decision="keep_diff",
                rename_metric_code="outside_metric",
                reason="非法改名目标",
            ),
            actor_id=1,
        )


async def test_arbitrate_rename_target_role_resolves_same_name_conflict() -> None:
    """同名冲突（candidate/existing 同码）下 rename_target 角色定位改名方。

    检测以 cand_code==ext_code 触发同名冲突，候选/现有 metric_code 天然相同——
    用 code 无法区分改名目标，须以角色（candidate/existing）定位。这是
    「仲裁弹窗指定改名方只能选现有、切不了候选」缺陷的直接回归证据。
    """
    svc, repo, events, _, applier = _svc()
    req = ConflictCheckRequest(
        candidate=MetricInput(
            metric_code="sales_conflict_day", domain="sales", definition="sum(amount)"
        ),
        existing=[
            MetricInput(
                metric_code="sales_conflict_day", domain="finance", definition="sum(price)"
            )
        ],
    )
    await svc.check(req.candidate, req.existing)
    conflict_id = repo.conflicts[0].conflict_id
    # 角色指定候选为改名方：即使 candidate==existing 同码，也能精确区分
    conflict = await svc.arbitrate(
        conflict_id,
        ArbitrateRequest(
            decision="keep_diff",
            rename_target="candidate",
            reason="同名不同义，指定候选指标改名",
        ),
        actor_id=1,
    )
    assert conflict.status == ConflictStatus.RULED
    assert conflict.decision_json["rename_metric_code"] == "sales_conflict_day"
    assert conflict.decision_json["rename_target"] == "candidate"
    # 联动回调收到改名目标 code（按角色解析）
    assert applier is not None and applier.applied
    _conflict, _decision, _canonical, _actor, rename = applier.applied[-1]
    assert rename == "sales_conflict_day"
    # 事件携带 rename_target 角色（供通知/裁决记录溯源）
    ruled = [e for e in events.published if e["event_type"] == "conflict_ruled"]
    assert ruled and ruled[-1]["payload"].get("rename_metric_code") == "sales_conflict_day"


async def test_arbitrate_rename_target_invalid_role_rejected() -> None:
    """rename_target 角色非法（非 candidate/existing）→ schema 层即拒绝（fail-fast）。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ArbitrateRequest(
            decision="keep_diff",
            rename_target="other",
            reason="非法角色",
        )


async def test_arbitrate_rename_requires_keep_diff_decision() -> None:
    """改名目标仅在保留差异决策下有意义：choose_canonical 指定改名 → 拒绝。"""
    svc, repo, _, _, _ = _svc()
    req = ConflictCheckRequest(
        candidate=MetricInput(metric_code="gmv_total", domain="sales", definition="sum(amount)"),
        existing=[MetricInput(metric_code="gmv_total", domain="finance", definition="sum(price)")],
    )
    await svc.check(req.candidate, req.existing)
    conflict_id = repo.conflicts[0].conflict_id
    with pytest.raises(ConflictError):
        await svc.arbitrate(
            conflict_id,
            ArbitrateRequest(
                decision="choose_canonical",
                canonical_metric_code="gmv_total",
                rename_metric_code="gmv_total",
                reason="非法组合",
            ),
            actor_id=1,
        )


class FakeLlm:
    """记录 judge_same_semantics 调用（use_llm 参数行为验证）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def judge_same_semantics(self, a: str, b: str) -> bool:
        self.calls.append((a, b))
        return True


async def test_check_persists_governance_fields_hard() -> None:
    """硬冲突落库：severity/source/block_publish/reason/metric_a/metric_b 齐全。"""
    svc, repo, events, _, _ = _svc()
    req = ConflictCheckRequest(
        candidate=MetricInput(
            metric_code="gmv_total",
            domain="sales",
            definition="sum(amount)",
            metric_id=101,
        ),
        existing=[
            MetricInput(
                metric_code="gmv_total",
                domain="finance",
                definition="sum(price)",
                metric_id=202,
            )
        ],
    )
    await svc.check(req.candidate, req.existing, source="auto")
    assert len(repo.conflicts) == 1
    c = repo.conflicts[0]
    assert c.severity == "hard"
    assert c.source == "auto"
    assert c.block_publish is True
    assert c.reason  # 非空检测原因
    assert c.metric_a == 101
    assert c.metric_b == 202


async def test_check_persists_soft_conflict_fields_default_manual() -> None:
    """软冲突落库：source 缺省为 manual、block_publish=False（不阻断发布）。"""
    svc, repo, _, _, _ = _svc()
    req = ConflictCheckRequest(
        candidate=MetricInput(
            metric_code="sales_gmv_amount_day", domain="sales", definition="sum(order_amount)"
        ),
        existing=[
            MetricInput(
                metric_code="sales_gmv_amt_day", domain="sales", definition="sum(order_amount)"
            )
        ],
    )
    await svc.check(req.candidate, req.existing)
    assert len(repo.conflicts) == 1
    c = repo.conflicts[0]
    assert c.type == ConflictType.SAME_DEF_DIFF_NAME
    assert c.severity == "soft"
    assert c.source == "manual"
    assert c.block_publish is False


async def test_check_use_llm_false_skips_llm_borderline() -> None:
    """创建路径 use_llm=False：补位触发区不调 LLM，不落库（词法无冲突）。"""
    from unittest.mock import patch

    from app.services.conflict.similarity import ConflictDetection

    def _fake_detect(candidate: dict, existing: dict, llm_judge=None):
        # 词法恒无冲突；仅 LLM 判定（llm_judge 非空）时升级为同义软冲突
        if llm_judge is not None:
            return ConflictDetection(
                conflict_type=ConflictType.SAME_DEF_DIFF_NAME,
                score=0.9,
                existing_code=existing.get("metric_code", ""),
                existing_metric_id=None,
                severity="soft",
                block_publish=False,
                reason="LLM 语义判定为同义口径（补位）",
                llm_confirmed=True,
            )
        return None

    llm = FakeLlm()
    svc = ConflictService(db=object(), llm=llm)
    repo = FakeRepo()
    events = FakeEvents()
    svc._repo = repo
    svc._events = events
    req = ConflictCheckRequest(
        candidate=MetricInput(metric_code="a_metric_day", domain="sales", definition="def1"),
        existing=[MetricInput(metric_code="b_metric_day", domain="sales", definition="def2")],
    )
    with (
        patch("app.services.conflict.service.detect_conflict", side_effect=_fake_detect),
        patch("app.services.conflict.service.is_borderline_match", return_value=True),
    ):
        result = await svc.check(req.candidate, req.existing, use_llm=False)
    assert llm.calls == []
    assert result.detections == []
    assert len(repo.conflicts) == 0


async def test_check_use_llm_true_calls_llm_borderline() -> None:
    """人工预检 use_llm=True：补位触发区调 LLM，判定同义则落库软冲突。"""
    from unittest.mock import patch

    from app.services.conflict.similarity import ConflictDetection

    def _fake_detect(candidate: dict, existing: dict, llm_judge=None):
        if llm_judge is not None:
            return ConflictDetection(
                conflict_type=ConflictType.SAME_DEF_DIFF_NAME,
                score=0.9,
                existing_code=existing.get("metric_code", ""),
                existing_metric_id=None,
                severity="soft",
                block_publish=False,
                reason="LLM 语义判定为同义口径（补位）",
                llm_confirmed=True,
            )
        return None

    llm = FakeLlm()
    svc = ConflictService(db=object(), llm=llm)
    repo = FakeRepo()
    events = FakeEvents()
    svc._repo = repo
    svc._events = events
    req = ConflictCheckRequest(
        candidate=MetricInput(metric_code="a_metric_day", domain="sales", definition="def1"),
        existing=[MetricInput(metric_code="b_metric_day", domain="sales", definition="def2")],
    )
    with (
        patch("app.services.conflict.service.detect_conflict", side_effect=_fake_detect),
        patch("app.services.conflict.service.is_borderline_match", return_value=True),
    ):
        result = await svc.check(req.candidate, req.existing, use_llm=True)
    assert len(llm.calls) == 1
    assert len(result.detections) == 1
    assert len(repo.conflicts) == 1
    assert repo.conflicts[0].type == ConflictType.SAME_DEF_DIFF_NAME
    assert repo.conflicts[0].severity == "soft"
