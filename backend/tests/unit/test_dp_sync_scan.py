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
        "schema_name": "dp_stable",
        "task_table": "dispatch_task",
        "step_table": "dispatch_task_step",
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
    svc._lineage_repo.mark_missing = AsyncMock(return_value=(0, 0))
    svc._lineage_repo.begin_ingest_run = AsyncMock(return_value=MagicMock(id=900))
    svc._lineage_repo.finish_ingest_run = AsyncMock()

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
    svc._dp_repo.soft_delete_field_mappings = AsyncMock(return_value=0)
    svc._dp_repo.find_orphan_catalogs = AsyncMock(return_value=[])
    svc._llm_chat = None
    return svc


def _wm(
    last_scan_at: datetime | None = None,
    last_max: datetime | None = None,
    last_full: datetime | None = None,
) -> DpSyncWatermark:
    wm = DpSyncWatermark(table_name="task")
    wm.last_scan_at = last_scan_at
    wm.last_max_update = last_max
    wm.last_full_scan_at = last_full
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
    # 双轨：采集通道运行摘要（ingest_run source=dp_sql）
    svc._lineage_repo.begin_ingest_run.assert_awaited_once()
    fin = svc._lineage_repo.finish_ingest_run.await_args.kwargs
    assert fin["status"] == "success"
    assert fin["total_edges"] > 0
    assert collector.disposed is True


@pytest.mark.asyncio
async def test_scan_incremental_passes_watermark() -> None:
    collector = FakeCollector()
    svc = _svc(collector)
    svc._dp_repo.get_watermark = AsyncMock(
        return_value=_wm(
            last_scan_at=NOW - timedelta(minutes=10),
            last_max=NOW - timedelta(days=1),
            last_full=NOW - timedelta(hours=1),  # 最近全量过 → 增量模式
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
    # 失败可见：rollback 撤销了 running run_log，except 重建一条 failed 记录
    # （不再用 update_run_log 更新已回滚行——那是 0 行静默失败）。
    last = svc._dp_repo.create_run_log.call_args.kwargs
    assert last["status"] == "failed"
    assert "db down" in str(last["error"])
    # 双轨：采集通道同样写一条 failed ingest_run
    svc._lineage_repo.begin_ingest_run.assert_awaited()
    fin = svc._lineage_repo.finish_ingest_run.await_args.kwargs
    assert fin["status"] == "failed"


@pytest.mark.asyncio
async def test_scan_force_bypasses_interval_gate() -> None:
    """手动「立即扫描」（force=True）绕过轮询间隔；周期任务保持节流。"""
    collector = FakeCollector()
    svc = _svc(collector)
    svc._dp_repo.get_watermark = AsyncMock(
        return_value=_wm(last_scan_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    # 默认（force=False）：interval 未到 → 跳过
    skipped = await svc.scan_once(_fc(FakeCollector()))
    assert skipped["skipped"] == "interval_not_due"
    # force=True：即使 enabled=False 也执行（手动验证场景）
    svc2 = _svc(collector, _config(enabled=False))
    svc2._dp_repo.get_watermark = AsyncMock(return_value=None)
    result = await svc2.scan_once(_fc(collector), force=True)
    assert "skipped" not in result
    assert result["scanned_tasks"] == 2
    svc2._dp_repo.update_run_log.assert_awaited()
    assert svc2._dp_repo.update_run_log.await_args.kwargs["status"] == "success"


@pytest.mark.asyncio
async def test_scan_cancel_before_start_keeps_watermark() -> None:
    """预置取消：不处理任何任务、run_log 标 cancelled、水位不推进（下轮重扫）。"""
    import asyncio

    collector = FakeCollector()
    svc = _svc(collector)
    cancel_event = asyncio.Event()
    cancel_event.set()
    progress: dict[str, object] = {}
    result = await svc.scan_once(
        _fc(collector), progress=progress, cancel_event=cancel_event
    )
    assert result["skipped"] == "cancelled"
    assert result["scanned_tasks"] == 0
    assert svc._dp_repo.update_run_log.await_args.kwargs["status"] == "cancelled"
    # 水位：只更新 last_scan_at，不传 last_max_update（保留原值下轮重扫）
    wm_calls = svc._dp_repo.update_watermark.await_args_list
    assert wm_calls, "取消时仍应记录 last_scan_at"
    for call in wm_calls:
        assert "last_max_update" not in call.kwargs
    assert progress.get("stage") == "cancelled"


@pytest.mark.asyncio
async def test_scan_cancel_mid_round_keeps_processed() -> None:
    """处理中取消：已处理结果保留（scanned_tasks=1），取消后停止后续任务。"""
    import asyncio

    collector = FakeCollector()
    svc = _svc(collector)
    cancel_event = asyncio.Event()

    async def _proc_one_then_cancel(*_a, **_kw):
        cancel_event.set()  # 首个任务处理完成后置位

    svc._process_task = AsyncMock(side_effect=_proc_one_then_cancel)
    progress: dict[str, object] = {}
    result = await svc.scan_once(
        _fc(collector), progress=progress, cancel_event=cancel_event
    )
    assert result["skipped"] == "cancelled"
    assert svc._process_task.await_count == 1  # 只处理了第一个任务
    assert svc._dp_repo.update_run_log.await_args.kwargs["status"] == "cancelled"
    assert progress.get("stage") == "cancelled"


@pytest.mark.asyncio
async def test_scan_cancel_inside_task_stops_at_step_boundary() -> None:
    """协作取消检查点下沉：任务内 set 取消后，该任务剩余 steps 不再处理。

    之前是「当前任务完整处理完才停」；A 方案下 _process_task 在 step 循环内
    检查 cancel_event → break（剩余 steps 丢弃、回填跳过），外层任务循环同样
    感知取消停止后续任务。
    """
    import asyncio

    collector = FakeCollector()
    svc = _svc(collector)
    cancel_event = asyncio.Event()

    async def _proc_with_checkpoint(*_a, **_kw):
        # 模拟处理完第一个 step 后置位取消 → 后续 steps/任务不应再处理
        cancel_event.set()

    svc._process_task = AsyncMock(side_effect=_proc_with_checkpoint)
    progress: dict[str, object] = {}
    result = await svc.scan_once(
        _fc(collector), progress=progress, cancel_event=cancel_event
    )
    assert result["skipped"] == "cancelled"
    assert svc._process_task.await_count == 1
    fin = svc._lineage_repo.finish_ingest_run.await_args.kwargs
    assert fin["status"] == "cancelled"


@pytest.mark.asyncio
async def test_scan_force_stop_raises_cancelled() -> None:
    """强制终止：force_event 置位后 _process_task 在检查点抛 _ScanCancelled，
    scan_once 捕获并整体按 cancelled 收尾（水位不推进、run_log/ingest 标 cancelled）。
    """
    import asyncio

    from app.services.lineage.dp_sync_service import _ScanCancelledError

    collector = FakeCollector()
    svc = _svc(collector)
    cancel_event = asyncio.Event()
    force_event = asyncio.Event()

    async def _proc_force(*_a, **_kw):
        cancel_event.set()
        force_event.set()
        raise _ScanCancelledError("force-stop")

    svc._process_task = AsyncMock(side_effect=_proc_force)
    progress: dict[str, object] = {}
    result = await svc.scan_once(
        _fc(collector),
        progress=progress,
        cancel_event=cancel_event,
        force_event=force_event,
    )
    assert result["skipped"] == "cancelled"
    assert svc._dp_repo.update_run_log.await_args.kwargs["status"] == "cancelled"
    fin = svc._lineage_repo.finish_ingest_run.await_args.kwargs
    assert fin["status"] == "cancelled"
    assert progress.get("stage") == "cancelled"


@pytest.mark.asyncio
async def test_scan_db_error_isolated_per_task_via_savepoint() -> None:
    """单任务 DB 级错误（写库抛异常）由 savepoint 隔离，不影响后续任务。

    回归（P1-3）：此前整轮单事务 + per-task except 不回滚，第一个任务写库
    抛 DB 错误后事务进入 failed 态，后续任务全部 PendingRollbackError，
    整轮白做只落一条 failed。
    """
    from sqlalchemy.exc import OperationalError

    collector = FakeCollector()
    svc = _svc(collector)
    real_process = svc.process_step

    async def flaky_process(task, step, sql, config, seen_pairs=None):
        if task["task_id"] == 101:
            raise OperationalError("stmt", {}, Exception("Deadlock"))
        return await real_process(task, step, sql, config, seen_pairs)

    svc.process_step = flaky_process  # type: ignore[method-assign]
    result = await svc.scan_once(_fc(collector))
    assert result["errors"] == 1  # 仅任务 101 失败
    assert result["scanned_tasks"] == 2  # 两个任务均进入处理
    assert result["parsed_ok"] == 1  # 任务 102 成功入库
    # savepoint 被使用（per-task 隔离）
    assert svc._db.begin_nested.call_count == 2
    # 收尾仍走成功态（未触发整轮 rollback）
    assert svc._db.rollback.await_count == 0
    assert svc._dp_repo.update_run_log.await_args.kwargs["status"] == "success"


@pytest.mark.asyncio
async def test_scan_partial_errors_still_advance_watermark() -> None:
    """部分任务失败（errors>0）仍推进 max 水位并记录 last_full_scan_at。

    回归（H8）：此前 ``cancelled or errors>0`` 都不推水位——只要有 1 个顽固
    失败任务（gmt_modified ≤ max），水位永远停在初始态，每轮周期任务都全量
    重扫上千任务空转。失败任务由周期自动全量观察兜底重扫，不应阻塞水位推进。
    """
    from sqlalchemy.exc import OperationalError

    collector = FakeCollector()
    svc = _svc(collector)
    real_process = svc.process_step

    async def flaky_process(task, step, sql, config, seen_pairs=None):
        if task["task_id"] == 101:
            raise OperationalError("stmt", {}, Exception("Deadlock"))
        return await real_process(task, step, sql, config, seen_pairs)

    svc.process_step = flaky_process  # type: ignore[method-assign]
    result = await svc.scan_once(_fc(collector))
    assert result["errors"] == 1
    assert result["parsed_ok"] == 1
    # 收尾推进水位（带 last_max_update + full_scan=True），而非只刷 last_scan_at
    # （cancelled 分支不传 last_max_update/full_scan 键）
    wm_calls = svc._dp_repo.update_watermark.await_args_list
    assert len(wm_calls) == 2
    for call in wm_calls:
        assert call.kwargs.get("full_scan") is True
        assert "last_max_update" in call.kwargs
    assert svc._dp_repo.update_run_log.await_args.kwargs["status"] == "success"


@pytest.mark.asyncio
async def test_scan_uses_configured_table_names() -> None:
    """扫描 SQL 使用配置的 schema/task_table/step_table（不再硬编码 dp_stable）。

    回归（P1-5）：此前 schema/task_table/step_table 配置只被 /meta 使用，
    scan 始终扫写死的 dp_stable.dispatch_task。
    """
    collector = FakeCollector()
    svc = _svc(
        collector,
        _config(
            schema_name="other_db",
            task_table="my_task",
            step_table="my_step",
        ),
    )
    await svc.scan_once(_fc(collector))
    assert any("other_db.my_task" in q for q in collector.queries)
    assert any("other_db.my_step" in q for q in collector.queries)
    # 不再出现硬编码默认表名
    assert not any("dp_stable.dispatch_task" in q for q in collector.queries)


@pytest.mark.asyncio
async def test_scan_invalid_table_name_fails_visible() -> None:
    """非法表名标识符（注入面）→ 整轮 fail fast 记 failed，不静默扫错表。

    回归（P2-9 注入面 + P1-5）：配置表名经 f-string 拼 SQL，必须白名单校验。
    """
    collector = FakeCollector()
    svc = _svc(collector, _config(task_table="dispatch_task; DROP TABLE x"))
    result = await svc.scan_once(_fc(collector))
    assert result["skipped"] == "failed"
    assert "合法标识符" in result["error"]


@pytest.mark.asyncio
async def test_scan_full_round_runs_mark_missing() -> None:
    """全量轮（无水位）对未再出现边执行失效观察 mark_missing。

    回归（P1-6）：此前 scan_once 只 mark_seen 从不 mark_missing，任务/节点
    删除后其边永不进入失效队列（stale 保留历史）。
    """
    collector = FakeCollector()
    svc = _svc(collector)  # watermark None → 全量
    result = await svc.scan_once(_fc(collector))
    assert "skipped" not in result
    svc._lineage_repo.mark_missing.assert_awaited_once()
    args = svc._lineage_repo.mark_missing.await_args
    assert args.args[0] == "dp_sql"
    assert args.kwargs["threshold"] == 2


@pytest.mark.asyncio
async def test_scan_incremental_skips_mark_missing() -> None:
    """增量轮（有水位）不执行 mark_missing——未变更任务边不在 seen_pairs，
    若每轮累加会误伤大量正常边（P1-6 防误伤）。
    """
    collector = FakeCollector()
    svc = _svc(collector)
    svc._dp_repo.get_watermark = AsyncMock(
        return_value=_wm(
            last_scan_at=NOW - timedelta(minutes=10),
            last_max=NOW - timedelta(days=1),
            last_full=NOW - timedelta(hours=1),  # 最近全量过 → 非周期自动全量
        )
    )
    result = await svc.scan_once(_fc(collector))
    assert "skipped" not in result
    svc._lineage_repo.mark_missing.assert_not_awaited()
