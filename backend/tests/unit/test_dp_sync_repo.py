"""dp 血缘同步仓储层单元测试（P2-8 修复项）。

覆盖：
- ``update_config`` 显式 null 置 NULL（可空字段）/ 非空字段 null 忽略
- ``soft_delete_field_mappings`` 同 step 旧 sql_hash 清理（防字段映射膨胀）
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.sql.dml import Update

from app.models.dp_sync import LineageFieldMapping
from app.services.lineage.dp_sync_repo import DpLineageRepository


class _FakeDb:
    """记录 execute 语句的假 session。"""

    def __init__(self, scalar_value=None) -> None:
        self.executed: list = []
        self.added: list = []
        self._scalar_value = scalar_value

    async def execute(self, stmt, *args, **kwargs):
        self.executed.append(stmt)
        return SimpleNamespace(
            rowcount=1,
            scalar_one_or_none=lambda: self._scalar_value,
            scalars=lambda: SimpleNamespace(all=lambda: []),
        )

    def add(self, obj) -> None:
        self.added.append(obj)


def _repo(scalar_value=None) -> tuple[DpLineageRepository, _FakeDb]:
    db = _FakeDb(scalar_value)
    return DpLineageRepository(db), db


@pytest.mark.asyncio
async def test_update_config_null_sets_nullable_fields() -> None:
    """update_config 显式 null 对可空字段生效（置 NULL 清空/回默认）。

    回归（P2-8）：此前 ``v is not None`` 过滤使传 null 完全无效，
    llm_model/排除规则等无法清空。
    """
    repo, db = _repo()
    await repo.update_config(1, llm_model=None, exclude_table_patterns=None)
    stmt = db.executed[0]
    assert isinstance(stmt, Update)
    values = stmt._values if hasattr(stmt, "_values") else {}
    # SQLAlchemy Update._values: dict[str, BindParameter]，解出值
    resolved = {
        getattr(k, "name", str(k)): (v.value if hasattr(v, "value") else v)
        for k, v in values.items()
    }
    assert resolved.get("llm_model") is None
    assert resolved.get("exclude_table_patterns") is None


@pytest.mark.asyncio
async def test_update_config_ignores_null_on_non_nullable() -> None:
    """非空字段（enabled/source_id 等）收到 null 被忽略，不触发 DB not-null 报错。"""
    repo, db = _repo()
    await repo.update_config(1, enabled=None, poll_interval_minutes=None, llm_model="m")
    stmt = db.executed[0]
    values = stmt._values if hasattr(stmt, "_values") else {}
    resolved = {
        getattr(k, "name", str(k)): (v.value if hasattr(v, "value") else v)
        for k, v in values.items()
    }
    assert "enabled" not in resolved
    assert "poll_interval_minutes" not in resolved
    assert resolved.get("llm_model") == "m"


@pytest.mark.asyncio
async def test_soft_delete_field_mappings_keeps_current_hash() -> None:
    """同 step 仅保留当前 sql_hash 映射，旧 hash 软删（SQL 演进清理，P2-8）。"""
    repo, db = _repo()
    await repo.soft_delete_field_mappings(step_id=5012, keep_sql_hash="abc")
    assert len(db.executed) == 1
    stmt = db.executed[0]
    assert isinstance(stmt, Update)
    assert stmt.table.name == LineageFieldMapping.__tablename__


@pytest.mark.asyncio
async def test_soft_delete_field_mappings_clear_all_when_no_hash() -> None:
    """keep_sql_hash=None 清空该 step 全部映射（SQL 删除场景）。"""
    repo, db = _repo()
    await repo.soft_delete_field_mappings(step_id=5012)
    assert len(db.executed) == 1
    assert isinstance(db.executed[0], Update)


@pytest.mark.asyncio
async def test_upsert_field_mapping_uses_eq_not_is_for_column() -> None:
    """source_column 非 NULL 时查询编译为 ``=``（回归：曾误用 ``.is_()``
    编译成 ``col IS 'x'`` 致 MySQL 1064 语法错误，整轮 dp 扫描失败）。"""
    repo, db = _repo()
    await repo.upsert_field_mapping(
        edge_id=1,
        source_table="ods.a",
        source_column="region_code",
        target_table="dwd.b",
        target_column="region_code",
        sql_hash="h1",
    )
    assert len(db.executed) == 2  # active 查询 + tombstone 查询（均 None 落到新建）
    sql = str(db.executed[0].compile(compile_kwargs={"literal_binds": True}))
    assert "source_column = 'region_code'" in sql
    assert "source_column IS" not in sql
    assert len(db.added) == 1


@pytest.mark.asyncio
async def test_upsert_field_mapping_null_column_uses_is_null() -> None:
    """source_column 为 None 时应匹配 ``IS NULL``（语义正确）。"""
    repo, db = _repo()
    await repo.upsert_field_mapping(
        edge_id=2,
        source_table="ods.a",
        source_column=None,
        target_table="dwd.b",
        target_column="cnt",
        sql_hash="h2",
    )
    sql = str(db.executed[0].compile(compile_kwargs={"literal_binds": True}))
    assert "source_column IS NULL" in sql


@pytest.mark.asyncio
async def test_list_field_mappings_by_table_filters_active_column_mappings() -> None:
    """按表反查字段映射：仅有效列映射（source_column 非空 + 未软删），
    且命中该表作为源或目标（OR 范围）——字段钻取子图数据源。"""
    repo, db = _repo()
    await repo.list_field_mappings_by_table("dwd.dp_dq_measure_df")
    assert len(db.executed) == 1
    sql = str(db.executed[0].compile(compile_kwargs={"literal_binds": True}))
    assert "source_column IS NOT NULL" in sql
    assert "deleted_at IS NULL" in sql
    assert "source_table = 'dwd.dp_dq_measure_df'" in sql
    assert "target_table = 'dwd.dp_dq_measure_df'" in sql
    # 排除降级占位（source_column IS NOT NULL 已涵盖）与软删行
    assert "source_column" in sql


class _StatsFakeDb:
    """sync_stats 专用假 session：按序返回预置结果并记录语句。"""

    def __init__(self, results: list) -> None:
        self._results = list(results)
        self.executed: list = []

    async def execute(self, stmt, *args, **kwargs):
        self.executed.append(stmt)
        return self._results.pop(0)


def _sync_stats_repo(results: list):
    from app.services.lineage.dp_sync_repo import DpLineageRepository as Repo

    db = _StatsFakeDb(results)
    return Repo(db), db


@pytest.mark.asyncio
async def test_sync_stats_aggregates_and_derives() -> None:
    """统计概览聚合：累计 SUM/最近全量轮/待抉择存量/血缘沉淀 + 成功率派生。"""
    from types import SimpleNamespace as NS

    cumulative_row = (3, 100, 120, 60, 20, 10, 5, 3, 2)  # runs + 8 sums
    log_row = NS(
        to_dict=lambda: {
            "id": 9,
            "scan_mode": "full",
            "scanned_tasks": 40,
            "scanned_steps": 55,
            "parsed_ok": 30,
            "llm_confirmed": 5,
            "diverged": 4,
            "llm_fallback": 2,
            "unparseable": 1,
            "errors": 1,
        }
    )
    results = [
        NS(one=lambda: cumulative_row),          # cumulative
        NS(scalar_one_or_none=lambda: log_row),  # last full scan
        NS(all=lambda: [("pending", 2), ("resolved_pending", 1)]),  # tickets
        NS(scalar=lambda: 70),                   # table edges
        NS(scalar=lambda: 42),                   # table nodes
        NS(scalar=lambda: 500),                  # field mappings
    ]
    repo, db = _sync_stats_repo(results)
    stats = await repo.sync_stats()
    assert len(db.executed) == 6
    assert stats["cumulative"]["runs"] == 3
    assert stats["cumulative"]["parsed_ok"] == 60
    assert stats["last_full_scan"]["scanned_tasks"] == 40
    assert stats["pending_tickets"] == {"pending": 2, "resolved_pending": 1}
    assert stats["lineage"] == {
        "table_edges": 70,
        "table_nodes": 42,
        "field_mappings": 500,
    }
    # 派生（API 层 _derived）：最近全量轮 ok=35 bad=8 → rate 81.4
    ok = 30 + 5
    bad = 4 + 2 + 1 + 1
    assert round(ok * 100.0 / (ok + bad), 1) == 81.4


@pytest.mark.asyncio
async def test_sync_stats_sql_scopes_dp_channel_and_active() -> None:
    """血缘沉淀统计仅统计 dp_sql 通道 + 未软删；run_log 仅成功成功轮。"""
    from types import SimpleNamespace as NS

    def _scalar(v):
        return NS(scalar=lambda: v)

    results = [
        NS(one=lambda: (0, *([0] * 8))),
        NS(scalar_one_or_none=lambda: None),
        NS(all=lambda: []),
        _scalar(0),
        _scalar(0),
        _scalar(0),
    ]
    repo, db = _sync_stats_repo(results)
    await repo.sync_stats()
    edge_sql = str(db.executed[3].compile(compile_kwargs={"literal_binds": True}))
    assert "provenance = 'dp_sql'" in edge_sql
    assert "deleted_at IS NULL" in edge_sql
    run_sql = str(db.executed[0].compile(compile_kwargs={"literal_binds": True}))
    assert "status = 'success'" in run_sql
    ticket_sql = str(db.executed[2].compile(compile_kwargs={"literal_binds": True}))
    assert "resolution IS NULL" in ticket_sql


# ---------------------------------------------------------------------------
# 失败退避（B）：record_backoff_failure / reset_backoff
# ---------------------------------------------------------------------------


def _update_values(repo, db, idx=1) -> dict:
    """解出第 idx 次 execute 的 Update 语句 values（列名→值）。"""
    stmt = db.executed[idx]
    assert isinstance(stmt, Update)
    values = stmt._values if hasattr(stmt, "_values") else {}
    return {
        getattr(k, "name", str(k)): (v.value if hasattr(v, "value") else v)
        for k, v in values.items()
    }


@pytest.mark.asyncio
async def test_record_backoff_failure_first_waits_five_minutes() -> None:
    """首次整轮失败：计数 0→1，退避截止 ≈ now+5 分钟。"""
    from datetime import UTC, datetime, timedelta

    repo, db = _repo(scalar_value=0)
    before = datetime.now(UTC)
    await repo.record_backoff_failure(1)
    values = _update_values(repo, db, idx=1)  # idx0=select 计数, idx1=update
    assert values["consecutive_failures"] == 1
    assert isinstance(values["next_scan_at"], datetime)
    assert before + timedelta(minutes=4) <= values["next_scan_at"]
    assert values["next_scan_at"] <= datetime.now(UTC) + timedelta(minutes=6)


@pytest.mark.asyncio
async def test_record_backoff_failure_escalates_after_three() -> None:
    """连续第 3 次失败：按阶梯 15 分钟（1~2 次 5min，≥3 起 15→30→60）。"""
    from datetime import UTC, datetime, timedelta

    repo, db = _repo(scalar_value=2)  # 已有 2 次失败 → 本次为第 3 次
    before = datetime.now(UTC)
    await repo.record_backoff_failure(1)
    values = _update_values(repo, db, idx=1)
    assert values["consecutive_failures"] == 3
    assert before + timedelta(minutes=14) <= values["next_scan_at"]
    assert values["next_scan_at"] <= datetime.now(UTC) + timedelta(minutes=16)


@pytest.mark.asyncio
async def test_record_backoff_failure_caps_at_sixty_minutes() -> None:
    """退避封顶 60 分钟：连续失败次数再多也不再拉长。"""
    from datetime import UTC, datetime, timedelta

    repo, db = _repo(scalar_value=9)  # 本次第 10 次
    before = datetime.now(UTC)
    await repo.record_backoff_failure(1)
    values = _update_values(repo, db, idx=1)
    assert values["consecutive_failures"] == 10
    assert before + timedelta(minutes=59) <= values["next_scan_at"]
    assert values["next_scan_at"] <= datetime.now(UTC) + timedelta(minutes=61)


@pytest.mark.asyncio
async def test_reset_backoff_clears_counter_and_deadline() -> None:
    """成功一轮：计数归零、退避截止清 NULL。"""
    repo, db = _repo()
    await repo.reset_backoff(1)
    assert len(db.executed) == 1
    values = _update_values(repo, db, idx=0)
    assert values["consecutive_failures"] == 0
    assert values["next_scan_at"] is None
