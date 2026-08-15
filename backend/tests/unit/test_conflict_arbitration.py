"""仲裁联动指标模块单测（apply_arbitration_impact，无真实 DB）。

覆盖：四种裁决（选现有/选候选/合并/保留差异）的胜方标记、落败方废弃/作废、
keep_diff 双方共存标记，以及强韧性保护（胜方未发布不废弃、缺少 metric_svc、
指标不存在/已废弃跳过等）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from sqlalchemy import Select, Update
from sqlalchemy.sql.elements import BindParameter

from app.models.conflict import Conflict, ConflictStatus, ConflictType
from app.models.metric import Metric
from app.services.conflict.arbitration import apply_arbitration_impact


def _unwrap(value: Any) -> Any:
    """解包 SQLAlchemy 绑定参数，还原原始值。"""
    if isinstance(value, BindParameter):
        return value.value
    return value


def _metric(code: str, status: str) -> Metric:
    """构造最小 Metric 对象（仅测试读取字段，不落库）。"""
    return Metric(metric_code=code, status=status)


def _conflict(candidate: str, existing: str, conflict_id: str = "CF-TEST") -> Conflict:
    return Conflict(
        conflict_id=conflict_id,
        type=ConflictType.SAME_NAME_DIFF_DEF,
        status=ConflictStatus.RULED,
        metric_codes={"candidate": candidate, "existing": existing},
    )


class FakeResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class FakeDb:
    """记录 execute 调用的假 DB：select 按调用顺序返回预设指标，update 记录 values。"""

    def __init__(self, winner: Metric | None = None, loser: Metric | None = None) -> None:
        self.select_rows = [r for r in (winner, loser) if r is not None]
        self.updated: list[dict[str, Any]] = []

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(stmt, Select):
            row = self.select_rows.pop(0) if self.select_rows else None
            return FakeResult(row)
        if isinstance(stmt, Update):
            # 记录 values 供断言（key 为 Column 对象、value 为 BindParameter，均解包）
            self.updated.append(
                {
                    (k.key if hasattr(k, "key") else str(k)): _unwrap(v)
                    for k, v in (stmt._values or {}).items()
                }
            )
            return AsyncMock(rowcount=1)
        raise AssertionError(f"未预期的语句类型: {type(stmt)}")


async def _applied(
    winner: Metric | None = None,
    loser: Metric | None = None,
    canonical_code: str | None = "existing",
    decision: str = "choose_canonical",
    metric_svc: Any = None,
) -> tuple[FakeDb, Any]:
    db = FakeDb(winner=winner, loser=loser)
    cand = winner.metric_code if winner else "cand"
    exist = loser.metric_code if loser else "exist"
    conflict = _conflict(candidate=cand, existing=exist)
    await apply_arbitration_impact(db, conflict, decision, canonical_code, 1, metric_svc=metric_svc)
    return db, metric_svc


class TestArbitrationImpact:
    async def test_choose_existing_published_loser_deprecated(self) -> None:
        """选现有为权威：候选落败且 PUBLISHED → deprecate(successor=现有)，胜方标记 canonical。"""
        svc = AsyncMock()
        svc.deprecate_metric = AsyncMock()
        db, _ = await _applied(
            winner=_metric("existing", "PUBLISHED"),
            loser=_metric("candidate", "PUBLISHED"),
            canonical_code="existing",
            metric_svc=svc,
        )
        # 胜方标记权威
        assert db.updated[0]["arbitration_mark"]["status"] == "canonical"
        assert db.updated[0]["arbitration_mark"]["opposite_code"] == "candidate"
        # 落败方 PUBLISHED → deprecate(successor=胜方)
        svc.deprecate_metric.assert_awaited_once_with(
            "candidate", "existing", 1, role="platform_admin"
        )

    async def test_choose_candidate_published_existing_deprecated(self) -> None:
        """选候选为权威：现有落败且 PUBLISHED → deprecate(successor=候选)。"""
        svc = AsyncMock()
        svc.deprecate_metric = AsyncMock()
        db, _ = await _applied(
            winner=_metric("candidate", "PUBLISHED"),
            loser=_metric("existing", "PUBLISHED"),
            canonical_code="candidate",
            metric_svc=svc,
        )
        assert db.updated[0]["arbitration_mark"]["status"] == "canonical"
        assert db.updated[0]["arbitration_mark"]["opposite_code"] == "existing"
        svc.deprecate_metric.assert_awaited_once_with(
            "existing", "candidate", 1, role="platform_admin"
        )

    async def test_merge_unpublished_loser_voided(self) -> None:
        """合并到现有：候选落败且 DRAFT（未生效）→ 软删作废，不调用 deprecate。"""
        svc = AsyncMock()
        svc.deprecate_metric = AsyncMock()
        db, _ = await _applied(
            winner=_metric("existing", "PUBLISHED"),
            loser=_metric("candidate", "DRAFT"),
            canonical_code="existing",
            decision="merge",
            metric_svc=svc,
        )
        assert db.updated[0]["arbitration_mark"]["status"] == "canonical"
        # 软删：第二条 update 带 deleted_at
        assert db.updated[1]["deleted_at"] is not None
        # 落败方补写 successor（指向胜方）与 defeated 标记——保证详情直访可友好引导到胜方
        assert db.updated[1]["successor_code"] == "existing"
        assert db.updated[1]["arbitration_mark"]["status"] == "defeated"
        assert db.updated[1]["arbitration_mark"]["opposite_code"] == "existing"
        assert db.updated[1]["arbitration_mark"]["conflict_id"] == "CF-TEST"
        svc.deprecate_metric.assert_not_awaited()

    async def test_keep_diff_both_coexist(self) -> None:
        """保留差异：双方标记 coexist，不废弃任何一方。"""
        svc = AsyncMock()
        svc.deprecate_metric = AsyncMock()
        db, _ = await _applied(
            winner=_metric("candidate", "PUBLISHED"),
            loser=_metric("existing", "PUBLISHED"),
            canonical_code=None,
            decision="keep_diff",
            metric_svc=svc,
        )
        assert len(db.updated) == 2
        for mark in [u["arbitration_mark"] for u in db.updated]:
            assert mark["status"] == "coexist"
            assert mark["decision"] == "keep_diff"
        svc.deprecate_metric.assert_not_awaited()

    async def test_winner_unpublished_keeps_published_loser(self) -> None:
        """强韧性保护：胜方未发布时不废弃 PUBLISHED 落败方（避免无替代指标）。"""
        svc = AsyncMock()
        svc.deprecate_metric = AsyncMock()
        db, _ = await _applied(
            winner=_metric("candidate", "DRAFT"),
            loser=_metric("existing", "PUBLISHED"),
            canonical_code="candidate",
            metric_svc=svc,
        )
        # 仅胜方标记，落败方未被废弃也未被作废
        assert len(db.updated) == 1
        assert db.updated[0]["arbitration_mark"]["status"] == "canonical"
        svc.deprecate_metric.assert_not_awaited()

    async def test_missing_metric_svc_skips_deprecate(self) -> None:
        """缺少 metric_svc 时跳过 PUBLISHED 落败方废弃（标记仍写入）。"""
        db, svc = await _applied(
            winner=_metric("existing", "PUBLISHED"),
            loser=_metric("candidate", "PUBLISHED"),
            canonical_code="existing",
            metric_svc=None,
        )
        assert db.updated[0]["arbitration_mark"]["status"] == "canonical"
        assert len(db.updated) == 1

    async def test_canonical_not_in_pair_skipped(self) -> None:
        """权威方不在冲突双方中 → 跳过联动。"""
        db = FakeDb(winner=_metric("a", "PUBLISHED"), loser=_metric("b", "PUBLISHED"))
        conflict = _conflict(candidate="a", existing="b")
        await apply_arbitration_impact(db, conflict, "choose_canonical", "unknown", 1)
        assert db.updated == []

    async def test_deprecated_loser_skipped(self) -> None:
        """落败方已 DEPRECATED → 跳过（幂等），仅胜方标记。"""
        svc = AsyncMock()
        svc.deprecate_metric = AsyncMock()
        db, _ = await _applied(
            winner=_metric("existing", "PUBLISHED"),
            loser=_metric("candidate", "DEPRECATED"),
            canonical_code="existing",
            metric_svc=svc,
        )
        assert len(db.updated) == 1
        svc.deprecate_metric.assert_not_awaited()

    async def test_missing_metrics_skipped(self) -> None:
        """指标不存在 → 跳过联动。"""
        db = FakeDb(winner=None, loser=None)
        conflict = _conflict(candidate="ghost", existing="ghost2")
        await apply_arbitration_impact(db, conflict, "choose_canonical", "ghost", 1)
        assert db.updated == []
