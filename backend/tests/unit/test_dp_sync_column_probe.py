"""dp 同步 fetch 列自适应（生产表结构差异）单元测试。

不同环境的调度元库 ``dispatch_task``/``dispatch_task_step`` 结构可能不同——
有的含 settle_project_*/master_task_* 等增强列，有的为精简旧表缺这些列。
``_fetch_task``/``_fetch_sql_steps`` 查询前先经 information_schema 探测真实列，
仅 SELECT 交集，避免 ``Unknown column`` 使单任务失败（生产曾因缺
settle_project_director 报 1054）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.lineage.dp_sync_service import DpSyncService

#: 生产精简 dispatch_task 真实列（对照用户贴的 DDL：无 settle_*/master_task_*）。
_SPARSE_TASK_COLUMNS = [
    "id",
    "task_no",
    "name",
    "type",
    "out_table",
    "director",
    "created_user_id",
    "modified_user_id",
    "checker",
    "project_id",
    "cycle",
    "cron_express",
    "week_day",
    "month_day",
    "specific_time",
    "frequence",
    "remark",
    "task_version_desc",
    "task_version",
    "gmt_modified",
]


#: 生产精简 dispatch_task_step 真实列（无 is_deleted 软删列）。
_SPARSE_STEP_COLUMNS = [
    "id",
    "task_id",
    "task_step",
    "task_step_name",
    "task_step_type",
    "task_node_type",
    "script_info",
    "gmt_modified",
]


class _SparseCollector:
    """模拟生产精简元表：信息 schema 按表返回真实列；对不存在的列 SELECT 视为报错。"""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.disposed = False

    async def query(self, sql: str, params: dict | None = None):
        self.queries.append(sql)
        if "information_schema.columns" in sql:
            table = (params or {}).get("t")
            cols = (
                _SPARSE_STEP_COLUMNS
                if table == "dispatch_task_step"
                else _SPARSE_TASK_COLUMNS
            )
            return [{"column_name": c} for c in cols]
        if "FROM dp_stable.dispatch_task_step" in sql:
            # step 表同样精简：无 is_deleted，其余列齐全
            if "is_deleted" in sql:
                raise RuntimeError("dispatch_task_step has no is_deleted column")
            return []
        if "WHERE id=:tid" in sql:
            # 模拟真实行（key = SELECT 别名；缺增强列）
            if "settle_project_director" in sql or "master_task_id" in sql:
                raise RuntimeError(
                    "Unknown column 'settle_project_director' in 'field list'"
                )
            return [{"task_id": 101, "task_name": "任务A", "out_table": "wedw_dwd.x"}]
        return []

    async def dispose(self) -> None:
        self.disposed = True


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        schema_name="dp_stable",
        task_table="dispatch_task",
        step_table="dispatch_task_step",
        task_type_filter=None,
        step_type_filter=None,
    )


def _svc(collector: _SparseCollector) -> DpSyncService:
    svc = DpSyncService.__new__(DpSyncService)
    svc._db = MagicMock()
    svc._lineage_repo = MagicMock()
    svc._dp_repo = MagicMock()
    svc._schema_provider = None
    return svc


@pytest.mark.asyncio
async def test_fetch_task_sparse_table_selects_existing_columns_only() -> None:
    """生产精简 dispatch_task（缺 settle_*/master_task_*）不再报 Unknown column。

    期望：SELECT 只含真实列（含别名重命名 id AS task_id / name AS task_name），
    不含任何缺失增强列；行正常返回。
    """
    collector = _SparseCollector()
    svc = _svc(collector)
    row = await svc._fetch_task(collector, 101, _config())
    assert row is not None and row["task_id"] == 101
    task_sql = collector.queries[-1]
    assert "settle_project_director" not in task_sql
    assert "settle_project_name" not in task_sql
    assert "settle_department_name" not in task_sql
    assert "budget_unit_name" not in task_sql
    assert "master_task_id" not in task_sql
    assert "is_master_task" not in task_sql
    # 真实列保留 + 别名正确
    assert "id AS task_id" in task_sql
    assert "name AS task_name" in task_sql
    assert "director" in task_sql
    assert "project_id" in task_sql
    assert "task_version" in task_sql


@pytest.mark.asyncio
async def test_fetch_sql_steps_omits_missing_soft_delete() -> None:
    """step 表缺 is_deleted 时 WHERE 省略软删条件（不报 Unknown column）。"""
    collector = _SparseCollector()
    svc = _svc(collector)
    steps = await svc._fetch_sql_steps(collector, 101, _config())
    assert steps == []
    step_sql = collector.queries[-1]
    assert "is_deleted=0" not in step_sql
    assert "task_id=:tid" in step_sql
    assert "script_info" in step_sql


@pytest.mark.asyncio
async def test_column_probe_cached_per_scan() -> None:
    """同一 (schema, table) 只探测一次 information_schema（轮内缓存）。"""
    collector = _SparseCollector()
    svc = _svc(collector)
    await svc._fetch_task(collector, 101, _config())
    await svc._fetch_task(collector, 101, _config())
    probe_sqls = [
        q for q in collector.queries if "information_schema.columns" in q
    ]
    assert len(probe_sqls) == 1


@pytest.mark.asyncio
async def test_column_probe_empty_falls_back_to_full_list() -> None:
    """探测结果为空（如测试 mock 不识别 information_schema）→ 回退完整期望列。

    与既有 FakeCollector 行为一致：不新增探测即按历史完整列 SELECT，保证
    旧测试/降级路径不回退成「零列 SELECT」。
    """

    class _BlankCollector(_SparseCollector):
        async def query(self, sql: str, params: dict | None = None):
            self.queries.append(sql)
            if "WHERE id=:tid" in sql:
                return [
                    {"task_id": 1, "task_name": "A", "out_table": "x"},
                    {"settle_project_director": "p"},  # 增强列在行里也无妨
                ]
            return []  # information_schema 也返回空 → cols=None → 全列回退

    collector = _BlankCollector()
    svc = _svc(collector)
    row = await svc._fetch_task(collector, 1, _config())
    assert row is not None and row["task_id"] == 1
    task_sql = collector.queries[-1]
    # 回退 = 包含历史全部增强列（与旧实现一致，测试/旧环境不被裁剪）
    assert "settle_project_director" in task_sql
