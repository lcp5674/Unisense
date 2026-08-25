"""backfill_conflict_orphans 自引用防御单元测试。

覆盖三个关键防御：
- _forward_pass：detail 指向自身（existing_code==自己 / existing_metric_id==自己）
  时跳过建冲突记录并清除错误标记（不落「无法裁决」的自我冲突）。
- _reverse_pass：metric_a==metric_b 的自引用冲突不把 detail 回置给指标。
- _cleanup_self_refs：软删自引用记录 + 清除关联指标自引用标记（幂等）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.models.conflict import Conflict, ConflictStatus
from app.models.metric import Metric

# 回填脚本是独立运行脚本（backend/scripts/ 非包），用 importlib 从文件路径加载。
_BACKFILL_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "backfill_conflict_orphans.py"
)
_spec = importlib.util.spec_from_file_location("backfill_conflict_orphans", _BACKFILL_PATH)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class _FakeScalars:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


class FakeSession:
    """按 select 目标实体分发结果的最小假会话。"""

    def __init__(self, metrics: list | None = None, conflicts: list | None = None) -> None:
        self._metrics = metrics or []
        self._conflicts = conflicts or []
        self.committed = False

    async def execute(self, stmt):
        entity = None
        for cd in getattr(stmt, "column_descriptions", []):
            entity = cd.get("entity")
            if entity is not None:
                break
        if entity is Metric:
            return _FakeResult(self._metrics)
        if entity is Conflict:
            return _FakeResult(self._conflicts)
        return _FakeResult([])

    async def commit(self) -> None:
        self.committed = True


def _make_metric(
    metric_id: int,
    code: str,
    *,
    domain: str = "sales",
    pending_conflict: bool = True,
    detail: dict | None = None,
) -> Metric:
    return Metric(
        id=metric_id,
        metric_code=code,
        name="E2E 指标",
        domain=domain,
        status="PUBLISHED",
        pending_conflict=pending_conflict,
        pending_conflict_detail=detail,
    )


def _make_self_ref_conflict(conflict_id: str, metric_id: int, code: str) -> Conflict:
    return Conflict(
        conflict_id=conflict_id,
        type="same_name_diff_def",
        status=ConflictStatus.OPEN,
        metric_codes={"candidate": code, "existing": code},
        metric_a=metric_id,
        metric_b=metric_id,
        severity="hard",
        source="backfill",
        block_publish=True,
    )


class TestForwardSelfReference:
    async def test_self_ref_detail_skipped_and_flag_cleared(self) -> None:
        """existing_code==自己 → 不建冲突记录，清除错误标记（apply 时）。"""
        metric = _make_metric(
            11,
            "e2e_rev_assign_day",
            detail={
                "conflict_type": "same_name_diff_def",
                "score": 1.0,
                "existing_code": "e2e_rev_assign_day",
                "existing_metric_id": 11,
                "severity": "hard",
                "block_publish": True,
            },
        )
        session = FakeSession(metrics=[metric])
        repo = MagicMock()
        repo.count_open_for_metric = AsyncMock(return_value=0)

        stats = await mod._forward_pass(session, repo, dry_run=False, limit=None)

        assert stats["skipped_self_ref"] == 1
        assert stats["created"] == 0
        repo.create.assert_not_called()
        # 错误标记被清除
        assert metric.pending_conflict is False
        assert metric.pending_conflict_detail is None

    async def test_self_ref_metric_id_detail_skipped(self) -> None:
        """existing_metric_id==自己（existing_code 缺省）同样跳过。"""
        metric = _make_metric(
            17,
            "order_domain_gua_hao_entity_value_day",
            detail={
                "conflict_type": "same_name_diff_def",
                "existing_code": "",
                "existing_metric_id": 17,
            },
        )
        session = FakeSession(metrics=[metric])
        repo = MagicMock()
        repo.count_open_for_metric = AsyncMock(return_value=0)

        stats = await mod._forward_pass(session, repo, dry_run=False, limit=None)

        assert stats["skipped_self_ref"] == 1
        assert stats["created"] == 0
        repo.create.assert_not_called()
        assert metric.pending_conflict is False

    async def test_legit_conflict_still_created(self) -> None:
        """非自引用 detail（existing_code 指向其它指标）照常建记录。"""
        metric = _make_metric(
            3,
            "sales_gmv_amount_day",
            detail={
                "conflict_type": "same_name_diff_def",
                "existing_code": "sales_gmv_amount_day_dup",
                "existing_metric_id": 9,
            },
        )
        session = FakeSession(metrics=[metric])
        repo = MagicMock()
        repo.count_open_for_metric = AsyncMock(return_value=0)
        repo.create = AsyncMock(side_effect=lambda c: c)

        stats = await mod._forward_pass(session, repo, dry_run=False, limit=None)

        assert stats["created"] == 1
        assert stats["skipped_self_ref"] == 0
        repo.create.assert_called_once()
        created = repo.create.call_args[0][0]
        assert created.metric_a == 3
        assert created.metric_b == 9

    async def test_dry_run_does_not_clear_flag(self) -> None:
        """dry-run 统计但不改动标记。"""
        metric = _make_metric(
            11,
            "e2e_rev_assign_day",
            detail={"existing_code": "e2e_rev_assign_day", "existing_metric_id": 11},
        )
        session = FakeSession(metrics=[metric])
        repo = MagicMock()
        repo.count_open_for_metric = AsyncMock(return_value=0)

        stats = await mod._forward_pass(session, repo, dry_run=True, limit=None)

        assert stats["skipped_self_ref"] == 1
        assert metric.pending_conflict is True
        assert metric.pending_conflict_detail is not None


class TestReverseSelfReference:
    async def test_self_ref_conflict_not_flagged_back(self) -> None:
        """metric_a==metric_b 的自引用冲突不把 detail 回置给指标。"""
        conflict = _make_self_ref_conflict("CF-SELF01", 11, "e2e_rev_assign_day")
        metric = _make_metric(11, "e2e_rev_assign_day", pending_conflict=False)
        session = FakeSession(metrics=[metric], conflicts=[conflict])
        repo = MagicMock()

        stats = await mod._reverse_pass(session, repo, dry_run=False)

        assert stats["flagged"] == 0
        assert metric.pending_conflict is False


class TestCleanupSelfRefs:
    def _make_cleanup_fixture(self):
        conflict = _make_self_ref_conflict("CF-FC5940F2845E", 11, "e2e_rev_assign_day")
        metric = _make_metric(
            11,
            "e2e_rev_assign_day",
            detail={
                "conflict_type": "same_name_diff_def",
                "existing_code": "e2e_rev_assign_day",
                "existing_metric_id": 11,
                "conflict_id": "CF-FC5940F2845E",
            },
        )
        return conflict, metric

    async def test_cleanup_soft_deletes_self_ref_conflict_and_clears_flag(self) -> None:
        conflict, metric = self._make_cleanup_fixture()
        session = FakeSession(metrics=[metric], conflicts=[conflict])

        stats = await mod._cleanup_self_refs(session, dry_run=False)

        assert stats["scanned"] == 1
        assert stats["conflicts_cleaned"] == 1
        assert stats["metrics_cleared"] == 1
        assert conflict.deleted_at is not None  # 软删保留审计
        assert metric.pending_conflict is False
        assert metric.pending_conflict_detail is None

    async def test_cleanup_dry_run_no_mutation(self) -> None:
        conflict, metric = self._make_cleanup_fixture()
        session = FakeSession(metrics=[metric], conflicts=[conflict])

        stats = await mod._cleanup_self_refs(session, dry_run=True)

        assert stats["conflicts_cleaned"] == 1
        assert conflict.deleted_at is None
        assert metric.pending_conflict is True

    async def test_cleanup_skips_non_self_ref_conflicts(self) -> None:
        """非自引用记录不误清——即使查询层返回未过滤行，二次防御仍跳过。"""
        self_ref = _make_self_ref_conflict("CF-SELF01", 11, "e2e_rev_assign_day")
        legit = Conflict(
            conflict_id="CF-REAL01",
            type="same_name_diff_def",
            status=ConflictStatus.OPEN,
            metric_codes={"candidate": "a_metric_day", "existing": "b_metric_day"},
            metric_a=3,
            metric_b=9,
        )
        session = FakeSession(conflicts=[self_ref, legit])

        stats = await mod._cleanup_self_refs(session, dry_run=False)

        assert stats["scanned"] == 2  # 查询层返回全部（FakeSession 不模拟 where）
        assert stats["conflicts_cleaned"] == 1  # 二次防御仅清理自引用
        assert self_ref.deleted_at is not None
        assert legit.deleted_at is None  # 非自引用未被误清

    async def test_cleanup_idempotent(self) -> None:
        """已软删的自引用记录不被重复清理（二次防御跳过 deleted_at 非空）。"""
        conflict = _make_self_ref_conflict("CF-SELF02", 11, "e2e_rev_assign_day")
        from datetime import UTC, datetime

        conflict.deleted_at = datetime.now(UTC)  # 已软删
        session = FakeSession(conflicts=[conflict])

        stats = await mod._cleanup_self_refs(session, dry_run=False)

        assert stats["scanned"] == 1  # 查询层仍返回（FakeSession 不模拟 where）
        assert stats["conflicts_cleaned"] == 0  # 代码跳过已软删记录
