"""SQL 智能推断评测自定义样本服务测试（CRUD + 合并 + 预览）。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.sql_infer_eval_sample import SqlInferEvalSample
from app.services.semantic.sql_infer_eval import service as svc
from app.services.semantic.sql_infer_eval.dataset import GOLDEN


def _row(
    case_id: str = "custom_case",
    dialect: str = "hive",
    sql: str = "select sum(amount) as gmv from ods.orders",
    period: str = "day",
    measures: list | None = None,
    tables: list | None = None,
    note: str = "",
    enabled: bool = True,
    is_builtin: bool = False,
    sid: int = 1,
) -> SqlInferEvalSample:
    return SqlInferEvalSample(
        id=sid,
        case_id=case_id,
        dialect=dialect,
        sql=sql,
        expected_measures=measures
        or [{"column": "amount", "agg": "SUM", "alias": "gmv", "table": None}],
        expected_tables=tables or ["ods.orders"],
        expected_period=period,
        note=note,
        enabled=enabled,
        is_builtin=is_builtin,
        created_by=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        deleted_at=None,
    )


class _FakeResult:
    """模拟 ``db.execute`` 返回：scalars().all() / first() / scalar_one_or_none()。"""

    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


def _db(rows):
    db = MagicMock()
    db.execute = AsyncMock(side_effect=lambda *a, **k: _FakeResult(rows))
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=lambda r: None)
    return db


# ---------------------------------------------------------------- merged_cases


async def test_merged_cases_builtin_plus_custom() -> None:
    db = _db([_row(), _row(case_id="second", sid=2)])
    cases = await svc.merged_cases(db)
    ids = [c.case_id for c in cases]
    assert ids[0] == GOLDEN[0].case_id  # 内置在前
    assert "custom_case" in ids and "second" in ids
    assert len(cases) == len(GOLDEN) + 2


async def test_merged_cases_skips_builtin_conflict_and_disabled() -> None:
    # 与内置 case_id 冲突的自定义应跳过；disabled 行由 DB 查询过滤（mock 只返回 enabled）
    db = _db([_row(case_id=GOLDEN[0].case_id, sid=2)])
    cases = await svc.merged_cases(db)
    # 冲突的自定义被跳过（内置保留一份）
    assert sum(1 for c in cases if c.case_id == GOLDEN[0].case_id) == 1


# ---------------------------------------------------------------- create_sample


async def test_create_sample_success() -> None:
    db = _db([])  # 唯一性查询无结果
    row = await svc.create_sample(
        db,
        case_id="my_sample",
        dialect="spark",
        sql="select count(*) as c from t",
        expected_period="month",
        expected_measures=[{"column": "*", "agg": "COUNT", "alias": "c"}],
        expected_tables=["t"],
        note="示例",
        actor_id=7,
    )
    assert row["case_id"] == "my_sample"
    assert row["is_builtin"] is False
    assert row["enabled"] is True
    assert row["expected_measures"][0]["agg"] == "COUNT"


async def test_create_sample_rejects_builtin_conflict() -> None:
    db = _db([])
    with pytest.raises(ValueError, match="与内置基线冲突"):
        await svc.create_sample(
            db,
            case_id=GOLDEN[0].case_id,
            dialect="hive",
            sql="select 1",
            expected_period="day",
            expected_measures=None,
            expected_tables=None,
            note="",
        )


async def test_create_sample_rejects_illegal_agg() -> None:
    db = _db([])
    with pytest.raises(ValueError, match="不在合法枚举"):
        await svc.create_sample(
            db,
            case_id="bad_agg",
            dialect="hive",
            sql="select sum(x) as s from t",
            expected_period="day",
            expected_measures=[{"column": "x", "agg": "BOGUS"}],
            expected_tables=None,
            note="",
        )


async def test_create_sample_rejects_empty_sql() -> None:
    db = _db([])
    with pytest.raises(ValueError, match="不能为空"):
        await svc.create_sample(
            db,
            case_id="empty",
            dialect="hive",
            sql="   ",
            expected_period="day",
            expected_measures=None,
            expected_tables=None,
            note="",
        )


async def test_create_sample_rejects_dup_case_id() -> None:
    db = _db([_row(case_id="dup")])  # 唯一性查询命中
    with pytest.raises(ValueError, match="已存在"):
        await svc.create_sample(
            db,
            case_id="dup",
            dialect="hive",
            sql="select 1",
            expected_period="day",
            expected_measures=None,
            expected_tables=None,
            note="",
        )


# ---------------------------------------------------------------- update_sample


async def test_update_sample_success() -> None:
    db = _db([_row()])
    row = await svc.update_sample(db, 1, sql="select avg(x) as a from t", expected_period="week")
    assert row["expected_period"] == "week"
    assert row["sql"] == "select avg(x) as a from t"


async def test_update_sample_rejects_builtin() -> None:
    db = _db([_row(is_builtin=True)])
    with pytest.raises(ValueError, match="只读"):
        await svc.update_sample(db, 1, sql="select 1")


async def test_update_sample_not_found() -> None:
    db = _db([])
    with pytest.raises(ValueError, match="不存在或已删除"):
        await svc.update_sample(db, 999, sql="select 1")


# ---------------------------------------------------------------- delete_sample


async def test_delete_sample_soft_delete() -> None:
    db = _db([_row()])
    await svc.delete_sample(db, 1)
    # 提交被调用（软删已执行）；get_sample 查询返回行
    db.commit.assert_awaited_once()


async def test_delete_sample_rejects_builtin() -> None:
    db = _db([_row(is_builtin=True)])
    with pytest.raises(ValueError, match="只读"):
        await svc.delete_sample(db, 1)


# ---------------------------------------------------------------- preview_sample


def test_preview_sample_parses_profile() -> None:
    res = svc.preview_sample("select sum(amount) as gmv from ods.orders group by day_id")
    assert any(m["agg"] == "SUM" for m in res["measures"])
    assert "ods.orders" in res["source_tables"]
    assert res["period"] is not None


def test_preview_sample_empty_sql() -> None:
    res = svc.preview_sample("select 1")
    # 无聚合 → 空 measures，不抛异常
    assert isinstance(res["measures"], list)
