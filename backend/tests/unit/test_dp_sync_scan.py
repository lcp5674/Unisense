"""dp 调度血缘扫描（scan_once）单元测试。

以假 collector + mock repository 验证：间隔判断/首轮全量/增量水位/单任务失败
容错/排除规则/收尾 run_log 与 mark_seen/commit 失败记录 failed。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.lineage.dp_sync_repo import DpSyncWatermark
from app.services.lineage.dp_sync_service import DpSyncService

NOW = datetime(2026, 9, 3, 10, 0, 0, tzinfo=UTC)

TASK_SQL = (
    "create table wedw_dwd.dp_out as "
    "select department_id, count(1) as cnt from wedw_ods.visit_d "
    "where date_id='2026-08-18' group by department_id"
)

TASKS = [
    {"task_id": 101, "task_name": "任务A", "out_table": "wedw_dwd.dp_out"},
    {"task_id": 102, "task_name": "任务B", "out_table": "wedw_dwd.dp_out2"},
]


class FakeCollector:
    """假 dp 连接器：按 SQL 精确路由。"""

    def __init__(
        self, tasks: list[dict] | None = None, boom_on_task: bool = False
    ) -> None:
        self.tasks = tasks or TASKS
        self.boom_on_task = boom_on_task
        self.disposed = False
        self.queries: list[str] = []

    async def query(self, sql: str, params: dict | None = None):
        self.queries.append(sql)
        if "MAX(gmt_modified) AS m FROM dp_stable.dispatch_task_step" in sql:
            return [{"m": NOW}]
        if "MAX(gmt_modified) AS m FROM dp_stable.dispatch_task" in sql:
            return [{"m": NOW}]
        if "dispatch_task_step st" in sql:
            return []  # step 独立变更（无）
        if "FROM dp_stable.dispatch_task_step" in sql:
            tid = (params or {}).get("tid")
            return [
                {
                    "step_id": t["task_id"] * 10,
                    "task_id": t["task_id"],
                    "task_step": 1,
                    "step_name": "SQL",
                    "task_step_type": 7,
                    "task_node_type": 1,
                    "script_info": TASK_SQL,
                }
                for t in self.tasks
                if t["task_id"] == tid
            ]
        if "WHERE id=:tid" in sql:
            tid = (params or {}).get("tid")
            if self.boom_on_task:
                raise RuntimeError("dp 连接中断")
            return [t for t in self.tasks if t["task_id"] == tid]
        if "FROM dp_stable.dispatch_task" in sql:
            return [{"id": t["task_id"]} for t in self.tasks]
        return []

    async def dispose(self) -> None:
        self.disposed = True


def _fc(collector: FakeCollector):
    """async fetch_collector 包装。"""

    async def fetch(sid: str) -> FakeCollector:
        return collector

    return fetch


def _config(**overrides) -> SimpleNamespace:
    defaults = {
        "enabled": True,
        "source_id": "mysql_uncategorized",
        "poll_interval_minutes": 5,
        "task_type_filter": [1],
        "step_type_filter": [7],
        "exclude_task_patterns": None,
        "exclude_table_patterns": None,
        "llm_complexity_rules": None,
        "llm_enabled": True,
        "resolve_memory_enabled": True,
        "owner_backfill": "orphan_only",
    }
    return SimpleNamespace(**{**defaults, **overrides})


def _svc(
    collector: FakeCollector, config: SimpleNamespace | None = None
) -> DpSyncService:
    svc = DpSyncService.__new__(DpSyncService)
    svc._db = MagicMock()
    svc._db.commit = AsyncMock()
    svc._db.rollback = AsyncMock()
    svc._lineage_repo = MagicMock()
    svc._lineage_repo.would_create_cycle = AsyncMock(return_value=False)
    svc._lineage_repo.mark_seen = AsyncMock(return_value=(1, 0))

    async def _fake_upsert(**kw):
        edge = MagicMock()
        edge.id = 100
        edge.dp_task_refs = None
        return edge, False

    svc._lineage_repo.upsert_edge_with_status = AsyncMock(side_effect=_fake_upsert)
    svc._dp_repo = MagicMock()
    cfg = config or _config()
    svc._dp_repo.get_config = AsyncMock(return_value=cfg)
    svc._dp_repo.get_watermark = AsyncMock(return_value=None)
    svc._dp_repo.create_run_log = AsyncMock(return_value=MagicMock(id=1))
    svc._dp_repo.update_run_log = AsyncMock()
    svc._dp_repo.update_watermark = AsyncMock()
    svc._dp_repo.find_ticket_by_step_hash = AsyncMock(return_value=None)
    svc._dp_repo.create_ticket = AsyncMock(return_value=MagicMock())
    svc._dp_repo.upsert_field_mapping = AsyncMock()
    svc._dp_repo.find_orphan_catalogs = AsyncMock(return_value=[])
    svc._llm_chat = None
    return svc


def _wm(
    last_scan_at: datetime | None = None, last_max: datetime | None = None
) -> DpSyncWatermark:
    wm = DpSyncWatermark(table_name="task")
    wm.last_scan_at = last_scan_at
    wm.last_max_update = last_max
    return wm


@pytest.mark.asyncio
async def test_scan_not_configured_skipped() -> None:
    svc = _svc(FakeCollector())
    svc._dp_repo.get_config = AsyncMock(return_value=None)
    result = await svc.scan_once(_fc(FakeCollector()))
    assert result == {"skipped": "not_configured_or_disabled"}


@pytest.mark.asyncio
async def test_scan_disabled_skipped() -> None:
    svc = _svc(FakeCollector(), _config(enabled=False))
    result = await svc.scan_once(_fc(FakeCollector()))
    assert result["skipped"] == "not_configured_or_disabled"


@pytest.mark.asyncio
async def test_scan_interval_not_due() -> None:
    svc = _svc(FakeCollector())
    svc._dp_repo.get_watermark = AsyncMock(
        return_value=_wm(last_scan_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    result = await svc.scan_once(_fc(FakeCollector()))
    assert result["skipped"] == "interval_not_due"


@pytest.mark.asyncio
async def test_scan_first_full_round() -> None:
    collector = FakeCollector()
    svc = _svc(collector)
    result = await svc.scan_once(_fc(collector))
    assert "skipped" not in result
    assert result["scanned_tasks"] == 2
    assert result["scanned_steps"] == 2
    assert result["parsed_ok"] == 2
    svc._dp_repo.update_watermark.assert_awaited()
    svc._lineage_repo.mark_seen.assert_awaited_once()
    args = svc._dp_repo.update_run_log.await_args.kwargs
    assert args["status"] == "success"
    assert collector.disposed is True


@pytest.mark.asyncio
async def test_scan_incremental_passes_watermark() -> None:
    collector = FakeCollector()
    svc = _svc(collector)
    svc._dp_repo.get_watermark = AsyncMock(
        return_value=_wm(
            last_scan_at=NOW - timedelta(minutes=10), last_max=NOW - timedelta(days=1)
        )
    )
    result = await svc.scan_once(_fc(collector))
    assert result["scanned_tasks"] == 2
    task_sqls = [q for q in collector.queries if "gmt_modified > :twm" in q]
    assert task_sqls  # 增量模式确实带水位过滤


@pytest.mark.asyncio
async def test_scan_task_failure_does_not_abort_round() -> None:
    collector = FakeCollector(boom_on_task=True)
    svc = _svc(collector)
    result = await svc.scan_once(_fc(collector))
    assert result["errors"] == 2  # 两个 task 拉取失败均容错
    assert "skipped" not in result
    assert result["scanned_tasks"] == 0


@pytest.mark.asyncio
async def test_scan_excludes_tasks_by_pattern() -> None:
    collector = FakeCollector()
    svc = _svc(collector, _config(exclude_task_patterns=[r"^任务A$"]))
    result = await svc.scan_once(_fc(collector))
    assert result["scanned_tasks"] == 1  # 任务A 被排除
    assert result["scanned_steps"] == 1


@pytest.mark.asyncio
async def test_scan_commit_failure_records_failed_run() -> None:
    collector = FakeCollector()
    svc = _svc(collector)
    svc._db.commit = AsyncMock(
        side_effect=[None, RuntimeError("db down"), None]
    )
    result = await svc.scan_once(_fc(collector))
    assert result["skipped"] == "failed"
    svc._dp_repo.update_run_log.assert_awaited()
    assert svc._dp_repo.update_run_log.await_args.kwargs["status"] == "failed"
