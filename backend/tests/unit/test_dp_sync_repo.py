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

    def __init__(self) -> None:
        self.executed: list = []
        self.added: list = []

    async def execute(self, stmt, *args, **kwargs):
        self.executed.append(stmt)
        return SimpleNamespace(rowcount=1, scalar_one_or_none=lambda: None)

    def add(self, obj) -> None:
        self.added.append(obj)


def _repo() -> tuple[DpLineageRepository, _FakeDb]:
    db = _FakeDb()
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
