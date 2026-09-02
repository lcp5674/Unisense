"""conflict 服务 Repository 单测（补齐覆盖率至 ≥85%）。

针对 conflict/repository.py 的 31% 覆盖率，覆盖全部 7 个方法：
- create / get_by_conflict_id / list_conflicts（含全部过滤分支）/ update_status / reopen
- create_ruling / get_rulings
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.conflict import (
    Conflict,
    ConflictStatus,
    ConflictType,
    RulingRecord,
)
from app.services.conflict.repository import ConflictRepository
from app.services.conflict.schemas import RulingRecordResponse


@pytest.fixture
def db() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.execute = AsyncMock()
    return session


@pytest.fixture
def repo(db: MagicMock) -> ConflictRepository:
    return ConflictRepository(db)


class TestConflictCreate:
    async def test_create(self, repo: ConflictRepository, db: MagicMock) -> None:
        conflict = Conflict(conflict_id="c1", type=ConflictType.SAME_NAME_DIFF_DEF)
        result = await repo.create(conflict)
        assert result is conflict
        db.add.assert_called_once_with(conflict)
        db.flush.assert_awaited_once()
        db.refresh.assert_awaited_once_with(conflict)


class TestGetByConflictId:
    async def test_found(self, repo: ConflictRepository, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = Conflict(conflict_id="c1")
        db.execute.return_value = mock_result
        result = await repo.get_by_conflict_id("c1")
        assert result is not None
        assert result.conflict_id == "c1"

    async def test_not_found(self, repo: ConflictRepository, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute.return_value = mock_result
        result = await repo.get_by_conflict_id("missing")
        assert result is None


class TestListConflicts:
    async def test_no_filters(self, repo: ConflictRepository, db: MagicMock) -> None:
        mock_count = MagicMock()
        mock_count.scalar.return_value = 3
        mock_rows = MagicMock()
        mock_rows.scalars.return_value.all.return_value = [
            Conflict(conflict_id="c1"),
            Conflict(conflict_id="c2"),
        ]
        db.execute = AsyncMock(side_effect=[mock_count, mock_rows])
        rows, total = await repo.list_conflicts(
            status=None, ctype=None, domain=None, page=1, page_size=10
        )
        assert len(rows) == 2
        assert total == 3

    async def test_with_all_filters(self, repo: ConflictRepository, db: MagicMock) -> None:
        mock_count = MagicMock()
        mock_count.scalar.return_value = 1
        mock_rows = MagicMock()
        mock_rows.scalars.return_value.all.return_value = [Conflict(conflict_id="c1")]
        db.execute = AsyncMock(side_effect=[mock_count, mock_rows])
        rows, total = await repo.list_conflicts(
            status=ConflictStatus.OPEN,
            ctype=ConflictType.SAME_NAME_DIFF_DEF,
            domain="sales",
            page=2,
            page_size=5,
        )
        assert len(rows) == 1
        assert total == 1

    async def test_related_actor_filters_to_mine(
        self, repo: ConflictRepository, db: MagicMock
    ) -> None:
        """个人工作台收敛：related_actor_id 非空时 join 指标表按 Owner/副 Owner 过滤，
        不把全平台 OPEN 冲突（他人指标）混入个人待办。"""
        mock_count = MagicMock()
        mock_count.scalar.return_value = 1
        mock_rows = MagicMock()
        mock_rows.scalars.return_value.all.return_value = [Conflict(conflict_id="c1")]
        db.execute = AsyncMock(side_effect=[mock_count, mock_rows])
        rows, total = await repo.list_conflicts(
            status=ConflictStatus.OPEN,
            ctype=None,
            domain=None,
            page=1,
            page_size=10,
            related_actor_id=7,
        )
        assert len(rows) == 1
        assert total == 1
        # count 语句：join 指标表且带 Owner/副 Owner/仲裁人 归属条件
        compiled = str(
            db.execute.call_args_list[0].args[0].compile(
                compile_kwargs={"literal_binds": True}
            )
        )
        assert "metric" in compiled
        assert "owner_id = 7" in compiled
        assert "backup_owner_id = 7" in compiled
        assert "arbitrator_id = 7" in compiled


class TestUpdateStatus:
    async def test_update_with_all_fields(self, repo: ConflictRepository) -> None:
        conflict = Conflict(conflict_id="c1", status=ConflictStatus.OPEN)
        result = await repo.update_status(
            conflict,
            ConflictStatus.RULED,
            arbitrator_id=7,
            decision_json={"canonical": "m1"},
            resolved=True,
        )
        assert result.status == ConflictStatus.RULED
        assert result.arbitrator_id == 7
        assert result.decision_json == {"canonical": "m1"}
        assert result.resolved_at is not None

    async def test_update_minimal(self, repo: ConflictRepository) -> None:
        conflict = Conflict(conflict_id="c1", status=ConflictStatus.OPEN)
        result = await repo.update_status(conflict, ConflictStatus.CLOSED)
        assert result.status == ConflictStatus.CLOSED
        assert result.arbitrator_id is None
        assert result.resolved_at is None


class TestReopen:
    """重新打开已关闭冲突：状态 CLOSED → OPEN、清除 resolved_at（供重新裁决）。"""

    async def test_reopen_sets_open_and_clears_resolved_at(
        self, repo: ConflictRepository
    ) -> None:
        conflict = Conflict(
            conflict_id="c1",
            status=ConflictStatus.CLOSED,
            resolved_at=datetime(2026, 8, 14, 12, 0, 0),
        )
        result = await repo.reopen(conflict)
        assert result.status == ConflictStatus.OPEN
        assert result.resolved_at is None
        repo._db.flush.assert_awaited_once()


class TestRuling:
    async def test_create_ruling(self, repo: ConflictRepository, db: MagicMock) -> None:
        ruling = RulingRecord(conflict_id="c1")
        result = await repo.create_ruling(ruling)
        assert result is ruling
        db.add.assert_called_once_with(ruling)
        db.flush.assert_awaited_once()
        db.refresh.assert_awaited_once_with(ruling)

    async def test_get_rulings(self, repo: ConflictRepository, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            RulingRecord(conflict_id="c1"),
            RulingRecord(conflict_id="c1"),
        ]
        db.execute.return_value = mock_result
        rows = await repo.get_rulings("c1")
        assert len(rows) == 2


class TestCountOpenForMetric:
    async def test_returns_remaining_open(self, repo: ConflictRepository, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar.return_value = 2
        db.execute.return_value = mock_result
        total = await repo.count_open_for_metric("gmv_total")
        assert total == 2
        db.execute.assert_awaited_once()

    async def test_returns_zero_when_none(self, repo: ConflictRepository, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        db.execute.return_value = mock_result
        total = await repo.count_open_for_metric("gmv_total")
        assert total == 0


class TestCountOpenForPair:
    async def test_counts_unresolved_pair(self, repo: ConflictRepository, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar.return_value = 1
        db.execute.return_value = mock_result
        total = await repo.count_open_for_pair("sales_a", "sales_b")
        assert total == 1
        db.execute.assert_awaited_once()

    async def test_zero_when_no_open_pair(self, repo: ConflictRepository, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        db.execute.return_value = mock_result
        total = await repo.count_open_for_pair("sales_a", "sales_b")
        assert total == 0


class TestResolveActiveMetricId:
    async def test_returns_id_for_active_row(
        self, repo: ConflictRepository, db: MagicMock
    ) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = 42
        db.execute.return_value = mock_result
        rid = await repo.resolve_active_metric_id("gmv_total")
        assert rid == 42
        db.execute.assert_awaited_once()

    async def test_none_for_missing_or_deleted(
        self, repo: ConflictRepository, db: MagicMock
    ) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute.return_value = mock_result
        assert await repo.resolve_active_metric_id("gmv_total") is None

    async def test_none_for_empty_code(self, repo: ConflictRepository) -> None:
        assert await repo.resolve_active_metric_id("") is None


class TestRulingRecordResponseFromOrm:
    """GET /conflicts/{id}/rulings 500 回归：ORM 对象可直接 model_validate（from_attributes）。"""

    def test_model_validate_accepts_orm_object(self) -> None:
        ruling = RulingRecord(
            id=1,
            conflict_id="CF-ABC",
            metric_codes={"candidate": "a", "existing": "b"},
            decision="choose_canonical",
            reason="口径一致",
            arbitrator_id=7,
        )
        resp = RulingRecordResponse.model_validate(ruling)
        assert resp.id == 1
        assert resp.conflict_id == "CF-ABC"
        assert resp.decision == "choose_canonical"
        assert resp.arbitrator_id == 7


class TestListConflictsSeverity:
    """仲裁台软/硬冲突区分：severity 过滤（迁移 0090 治理字段）。"""

    async def test_severity_filter_hard(self, repo: ConflictRepository, db: MagicMock) -> None:
        mock_count = MagicMock()
        mock_count.scalar.return_value = 1
        mock_rows = MagicMock()
        mock_rows.scalars.return_value.all.return_value = [
            Conflict(conflict_id="c1", severity="hard")
        ]
        db.execute = AsyncMock(side_effect=[mock_count, mock_rows])
        rows, total = await repo.list_conflicts(
            status=None, ctype=None, domain=None, page=1, page_size=10, severity="hard"
        )
        assert len(rows) == 1
        assert total == 1
        assert rows[0].severity == "hard"

    async def test_severity_none_no_filter(self, repo: ConflictRepository, db: MagicMock) -> None:
        """severity 缺省不加过滤条件（兼容旧调用）。"""
        mock_count = MagicMock()
        mock_count.scalar.return_value = 2
        mock_rows = MagicMock()
        mock_rows.scalars.return_value.all.return_value = [
            Conflict(conflict_id="c1"),
            Conflict(conflict_id="c2"),
        ]
        db.execute = AsyncMock(side_effect=[mock_count, mock_rows])
        rows, total = await repo.list_conflicts(
            status=None, ctype=None, domain=None, page=1, page_size=10
        )
        assert len(rows) == 2
        assert total == 2


class TestGetFirstOpenForMetric:
    """创建路径取最新未决冲突（决定是否挂 pending_conflict 标记）。"""

    async def test_returns_open_conflict(self, repo: ConflictRepository, db: MagicMock) -> None:
        conflict = Conflict(
            conflict_id="CF-1",
            type=ConflictType.SAME_NAME_DIFF_DEF,
            status=ConflictStatus.OPEN,
            metric_codes={"candidate": "a_metric_day", "existing": "a_metric_week"},
            severity="hard",
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = conflict
        db.execute.return_value = mock_result
        row = await repo.get_first_open_for_metric("a_metric_day")
        assert row is not None
        assert row.conflict_id == "CF-1"
        assert row.severity == "hard"
        db.execute.assert_awaited_once()

    async def test_none_when_no_open(self, repo: ConflictRepository, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute.return_value = mock_result
        assert await repo.get_first_open_for_metric("a_metric_day") is None

    async def test_matches_existing_role_too(
        self, repo: ConflictRepository, db: MagicMock
    ) -> None:
        """指标作为 existing 侧出现也须命中（反向一致性回填用）。"""
        conflict = Conflict(
            conflict_id="CF-2",
            type=ConflictType.SAME_DEF_DIFF_NAME,
            status=ConflictStatus.OPEN,
            metric_codes={"candidate": "b_metric_day", "existing": "a_metric_day"},
            severity="soft",
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = conflict
        db.execute.return_value = mock_result
        row = await repo.get_first_open_for_metric("a_metric_day")
        assert row is not None
        assert row.conflict_id == "CF-2"
