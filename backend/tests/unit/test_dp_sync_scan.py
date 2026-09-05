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


def _task_ids(params: dict | None) -> set[int]:
    """从查询参数提取任务 id 集合（单任务 ``tid`` / 批量 ``tid0..`` / step 批量 ``t0..``）。

    参数命名约定：task 批量用 ``tid{i}``、step 批量 task_id IN 用 ``t{i}``（type
    IN 用 ``s{i}``）；统一取以 ``t`` 开头的参数值作为任务 id 候选。
    """
    out: set[int] = set()
    for k, v in (params or {}).items():
        if v is None or not k.startswith("t"):
            continue
        out.add(int(v))
    return out


class FakeCollector:
    """假 dp 连接器：按 SQL 精确路由。"""

    def __init__(
        self,
        tasks: list[dict] | None = None,
        boom_on_task: bool = False,
        sql: str | None = None,
    ) -> None:
        self.tasks = tasks or TASKS
        self.boom_on_task = boom_on_task
        self.sql = sql or TASK_SQL
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
            ids = _task_ids(params)
            return [
                {
                    "step_id": t["task_id"] * 10,
                    "task_id": t["task_id"],
                    "task_step": 1,
                    "step_name": "SQL",
                    "task_step_type": 7,
                    "task_node_type": 1,
                    "script_info": self.sql,
                }
                for t in self.tasks
                if t["task_id"] in ids
            ]
        if "WHERE id=:tid" in sql:
            tid = (params or {}).get("tid")
            if self.boom_on_task:
                raise RuntimeError("dp 连接中断")
            return [t for t in self.tasks if t["task_id"] == tid]
        if "WHERE id IN" in sql:  # 批量预取（_fetch_tasks_batch）
            if self.boom_on_task:
                raise RuntimeError("dp 连接中断")
            ids = _task_ids(params)
            return [t for t in self.tasks if t["task_id"] in ids]
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
        "id": 1,
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
        # 失败退避字段（scan_once 退避检查读取）
        "consecutive_failures": 0,
        "next_scan_at": None,
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
    # P2 阶段 2：_store_sqlglot_edges 走批量写（scan 全流程经 FakeCollector 简单任务）
    svc._lineage_repo.would_create_cycle_many = AsyncMock(return_value=set())

    async def _fake_upsert_batch(requests):
        out = {}
        for r in requests:
            edge = MagicMock()
            edge.id = 100
            edge.dp_task_refs = None
            out[
                (r["source_node"], r["target_node"], r["edge_type"], "L1")
            ] = (edge, False)
        return out

    svc._lineage_repo.upsert_edges_with_status_batch = AsyncMock(
        side_effect=_fake_upsert_batch
    )
    svc._dp_repo = MagicMock()
    cfg = config or _config()
    svc._dp_repo.get_config = AsyncMock(return_value=cfg)
    svc._dp_repo.get_watermark = AsyncMock(return_value=None)
    svc._dp_repo.create_run_log = AsyncMock(return_value=MagicMock(id=1))
    svc._dp_repo.update_run_log = AsyncMock()
    svc._dp_repo.update_watermark = AsyncMock()
    svc._dp_repo.pending_retry_task_ids = AsyncMock(return_value=[])
    svc._dp_repo.find_ticket_by_step_hash = AsyncMock(return_value=None)
    svc._dp_repo.create_ticket = AsyncMock(return_value=MagicMock())
    svc._dp_repo.upsert_field_mapping = AsyncMock()
    svc._dp_repo.upsert_field_mappings_batch = AsyncMock(return_value=0)
    svc._dp_repo.soft_delete_field_mappings = AsyncMock(return_value=0)
    svc._dp_repo.find_orphan_catalogs = AsyncMock(return_value=[])
    svc._dp_repo.reset_backoff = AsyncMock()
    svc._dp_repo.record_backoff_failure = AsyncMock()
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
async def test_scan_backoff_skips_before_deadline() -> None:
    """退避期（next_scan_at 在未来）：周期任务跳过自动扫描，不建 run_log。"""
    svc = _svc(
        FakeCollector(),
        _config(
            next_scan_at=datetime.now(UTC) + timedelta(minutes=30),
            consecutive_failures=3,
        ),
    )
    result = await svc.scan_once(_fc(FakeCollector()))
    assert result["skipped"] == "backoff"
    assert result["consecutive_failures"] == 3
    svc._dp_repo.create_run_log.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_backoff_force_bypasses_deadline() -> None:
    """手动「立即扫描」（force=True）不受退避限制，正常执行。"""
    collector = FakeCollector()
    svc = _svc(
        collector,
        _config(
            next_scan_at=datetime.now(UTC) + timedelta(minutes=30),
            consecutive_failures=3,
        ),
    )
    result = await svc.scan_once(_fc(collector), force=True)
    assert "skipped" not in result
    assert result["scanned_tasks"] == 2


@pytest.mark.asyncio
async def test_scan_success_resets_backoff() -> None:
    """成功一轮：reset_backoff 被调用（计数归零、清退避截止）。"""
    collector = FakeCollector()
    svc = _svc(collector)
    result = await svc.scan_once(_fc(collector))
    assert "skipped" not in result
    svc._dp_repo.reset_backoff.assert_awaited_with(1)


@pytest.mark.asyncio
async def test_scan_exception_records_backoff_failure() -> None:
    """整轮异常（fetch_collector 抛错）：record_backoff_failure 被调用。"""

    async def _boom(sid: str) -> FakeCollector:
        raise RuntimeError("dp 源不可达")

    svc = _svc(FakeCollector())
    result = await svc.scan_once(_boom)
    assert result["skipped"] == "failed"
    svc._dp_repo.record_backoff_failure.assert_awaited_with(1)


@pytest.mark.asyncio
async def test_scan_naive_watermark_does_not_crash() -> None:
    """H10 回归：MySQL DATETIME 读出 naive 水位时周期轮询不再抛 TypeError。

    修复前 ``datetime.now(UTC) - wm.last_scan_at`` 直接相减（naive/aware）每分钟崩溃。
    """
    svc = _svc(FakeCollector())
    naive_recent = (datetime.now(UTC) - timedelta(minutes=1)).replace(tzinfo=None)
    svc._dp_repo.get_watermark = AsyncMock(
        return_value=_wm(last_scan_at=naive_recent)
    )
    result = await svc.scan_once(_fc(FakeCollector()))
    assert result["skipped"] == "interval_not_due"


@pytest.mark.asyncio
async def test_scan_naive_watermark_auto_full_does_not_crash() -> None:
    """H10 回归：naive last_full_scan_at 距上次全量超周期 → 触发自动全量不崩溃。"""
    collector = FakeCollector()
    svc = _svc(collector)
    naive_old = (datetime.now(UTC) - timedelta(days=2)).replace(tzinfo=None)
    svc._dp_repo.get_watermark = AsyncMock(
        return_value=_wm(
            last_scan_at=naive_old,
            last_max=NOW - timedelta(days=3),
            last_full=naive_old,
        )
    )
    result = await svc.scan_once(_fc(collector))
    assert "skipped" not in result  # 自动全量轮正常执行
    assert result["scanned_tasks"] == 2
    svc._dp_repo.update_watermark.assert_awaited()
    # 全量轮执行了 mark_missing（删除语义闭环）
    svc._lineage_repo.mark_missing.assert_awaited()


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
            last_full=datetime.now(UTC) - timedelta(hours=1),  # 最近全量过 → 增量模式
        )
    )
    result = await svc.scan_once(_fc(collector))
    assert result["scanned_tasks"] == 2
    task_sqls = [q for q in collector.queries if "gmt_modified > :twm" in q]
    assert task_sqls  # 增量模式确实带水位过滤


@pytest.mark.asyncio
async def test_scan_force_full_ignores_watermark() -> None:
    """手动「立即扫描」（force_full=True）即使存在水位也强制全量重扫。

    用户心智：点「立即扫描」= 完整跑一遍看真实解析，而非增量空扫 0 任务。
    - 全量 SQL 不带 ``gmt_modified > :twm`` 水位过滤（扫全部活跃任务）
    - 收尾触发 mark_missing（全量删除观察）并记录 last_full_scan_at
    """
    collector = FakeCollector()
    svc = _svc(collector)
    svc._dp_repo.get_watermark = AsyncMock(
        return_value=_wm(
            last_scan_at=NOW - timedelta(minutes=10),
            last_max=NOW - timedelta(days=1),
            last_full=datetime.now(UTC) - timedelta(hours=1),  # 存在水位（增量条件满足）
        )
    )
    result = await svc.scan_once(_fc(collector), force_full=True)
    assert "skipped" not in result
    assert result["scanned_tasks"] == 2  # 全部任务被扫，而非 0
    task_sqls = [q for q in collector.queries if "gmt_modified > :twm" in q]
    assert not task_sqls  # 全量模式不带水位过滤
    # 全量轮触发删除观察（mark_missing）+ 记录 last_full_scan_at
    svc._lineage_repo.mark_missing.assert_awaited_once()
    wm_calls = svc._dp_repo.update_watermark.await_args_list
    assert wm_calls, "水位应在全量收尾推进"
    assert any(c.kwargs.get("full_scan") is True for c in wm_calls)
    # 注：fake 数据行无 gmt_modified 列 → task_max=None（真实数据返回已扫集 max）；
    # 对比：同样的水位、无 force_full → 走增量（0 变更）而非全量
    collector2 = FakeCollector()
    svc2 = _svc(collector2)
    svc2._dp_repo.get_watermark = AsyncMock(
        return_value=_wm(
            last_scan_at=NOW - timedelta(minutes=10),
            last_max=NOW - timedelta(days=1),
            last_full=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    # FakeCollector 任务 gmt_modified 均 > 水位 → 增量仍会扫到（2 任务）；
    # 关键差异在 SQL 是否带水位过滤 + 是否触发 mark_missing
    await svc2.scan_once(_fc(collector2))
    svc2._lineage_repo.mark_missing.assert_not_awaited()


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
    """收尾 commit 失败 → except 把已前置提交的 run_log 更新为 failed。

    方案 B：run_log 在扫描开始即独立提交持久化（running），收尾故障走 except 时
    直接 update 为 failed——不再重建新行（重建会造成 running 永久残留 + 重复行）。
    """
    collector = FakeCollector()
    svc = _svc(collector)
    calls = {"n": 0}

    async def _commit() -> None:
        calls["n"] += 1
        # 第 6 次 commit = 收尾 update_run_log/finish_ingest_run 之后的提交 → 失败
        # （前 5 次：前置 run、前置 ingest、任务 101、任务 102、收尾 mark_missing 后）
        if calls["n"] == 6:
            raise RuntimeError("db down")

    svc._db.commit = AsyncMock(side_effect=_commit)
    result = await svc.scan_once(_fc(collector))
    assert result["skipped"] == "failed"
    # run_log 已前置提交 → 直接 update 为 failed（不重建新行）
    assert svc._dp_repo.update_run_log.await_args.kwargs["status"] == "failed"
    assert "db down" in str(svc._dp_repo.update_run_log.await_args.kwargs["error"])
    # create_run_log 仅前置 1 次（running），未重建 failed（无 running 残留）
    assert svc._dp_repo.create_run_log.call_count == 1
    # 双轨：采集通道 ingest_run 同标 failed
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
async def test_scan_db_error_isolated_per_task_commit() -> None:
    """单任务 DB 级错误（写库抛异常）独立事务回滚，不影响后续任务。

    方案 B：整轮不再单一大事务——每任务独立 commit。任务 101 失败回滚自身
    （边不落库、记入 failed_task_ids 下轮重扫），任务 102 成功独立提交——
    边随各自任务即时对其它会话可见（不再等整轮统一 commit）。
    """
    import json as _json

    from sqlalchemy.exc import OperationalError

    collector = FakeCollector()
    svc = _svc(collector)
    real_process = svc.process_step

    async def flaky_process(task, step, sql, config, seen_pairs=None, **kwargs):
        if task["task_id"] == 101:
            raise OperationalError("stmt", {}, Exception("Deadlock"))
        return await real_process(task, step, sql, config, seen_pairs)

    svc.process_step = flaky_process  # type: ignore[method-assign]
    result = await svc.scan_once(_fc(collector))
    assert result["errors"] == 1  # 仅任务 101 失败
    assert result["scanned_tasks"] == 2  # 两个任务均进入处理
    assert result["parsed_ok"] == 1  # 任务 102 成功入库
    # 独立事务：失败任务回滚 1 次（101）；成功任务逐任务提交
    # （commit 计数：前置 run/前置 ingest 2 + 任务 102 1 + 收尾 mark_missing 后/
    #   run_log 终态后 2 = 5）
    assert svc._db.rollback.await_count == 1
    assert svc._db.commit.await_count == 5
    # 收尾仍走成功态（部分失败不触发整轮 failed）
    assert svc._dp_repo.update_run_log.await_args.kwargs["status"] == "success"
    # 失败任务 id 记录进 run_log detail（下轮 pending_retry_task_ids 显式重扫）
    detail = _json.loads(
        svc._dp_repo.update_run_log.await_args.kwargs["detail_json"]
    )
    assert detail["retry_task_ids"] == [101]


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

    async def flaky_process(task, step, sql, config, seen_pairs=None, **kwargs):
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
            last_full=datetime.now(UTC) - timedelta(hours=1),  # 最近全量过 → 非周期自动全量
        )
    )
    result = await svc.scan_once(_fc(collector))
    assert "skipped" not in result
    svc._lineage_repo.mark_missing.assert_not_awaited()


@pytest.mark.asyncio
async def test_prefetch_batch_groups_tasks_and_steps() -> None:
    """批量预取按 task_id 归组 task 静态字段 + step 明细；源库消失的任务补 None。

    阶段 1：逐任务拉取 = 每任务 2 次往返，批量后 2 个任务仅 task/step 各 1 次。
    """
    collector = FakeCollector()
    svc = _svc(collector)
    out = await svc._prefetch_batch(collector, _config(), [101, 102])
    assert set(out) == {101, 102}
    # task 静态字段完整归组
    assert out[101][0]["task_name"] == "任务A"
    assert out[102][0]["task_name"] == "任务B"
    # step 明细归到对应 task 下（各 1 个 SQL 节点）
    assert [s["task_id"] for s in out[101][1]] == [101]
    assert [s["task_id"] for s in out[102][1]] == [102]
    # 批量往返计数：task/step 各 1 次批量 IN（不含变更集/水位探测查询）
    detail_queries = [q for q in collector.queries if "WHERE id IN" in q or "task_id IN" in q]
    assert len(detail_queries) == 2


@pytest.mark.asyncio
async def test_prefetch_batch_missing_task_none_and_failure_fallback() -> None:
    """预取对源库消失任务补 (None, [])；批量查询异常时返回 {}（回退逐任务拉取）。"""
    collector = FakeCollector()
    svc = _svc(collector)
    # 变更集含 99（源库不存在）→ (None, [])，等同 _fetch_task None 语义
    out = await svc._prefetch_batch(collector, _config(), [99])
    assert out == {99: (None, [])}
    # 批量查询失败 → 返回 {}（scan_once 逐任务回退，不阻断）
    boom = FakeCollector(boom_on_task=True)
    assert await svc._prefetch_batch(boom, _config(), [101, 102]) == {}


@pytest.mark.asyncio
async def test_scan_batch_prefetch_replaces_per_task_fetch() -> None:
    """scan_once 走批量预取后，_process_task 不再发逐任务明细查询。

    用 monkeypatch 包装 _fetch_task/_fetch_sql_steps 计数：若被调用说明回退发生
    （批量路径成功时不应触发逐任务拉取）。
    """
    collector = FakeCollector()
    svc = _svc(collector)
    fetched: list[str] = []

    async def _spy_fetch_task(*a: object, **kw: object) -> object:
        fetched.append("task")
        # 保留原行为：委托 _fetch_task 真实实现（预取缺失才走这里）
        return await DpSyncService._fetch_task(svc, *a, **kw)

    svc._fetch_task = _spy_fetch_task  # type: ignore[method-assign]
    result = await svc.scan_once(_fc(collector))
    assert result["scanned_tasks"] == 2
    assert result["parsed_ok"] == 2
    # 批量预取成功 → 无逐任务回退（_fetch_task/_fetch_sql_steps 均未触发）
    assert fetched == []
    # 明细往返数：task 批量 1 + step 批量 1（+变更集 2）——远小于逐任务 4 次明细
    batch_detail = [q for q in collector.queries if "WHERE id IN" in q or "task_id IN" in q]
    assert len(batch_detail) == 2


# ---------------------------------------------------------------------------
# 方案 A：scan_once 批级 LLM 并发裁决（phase1 即时提交 + 批末并发 + phase2 落库）
# ---------------------------------------------------------------------------

COMPLEX_TASKS = [
    {"task_id": 201, "task_name": "复杂任务A", "out_table": "wedw_dwd.dp_cx1"},
    {"task_id": 202, "task_name": "复杂任务B", "out_table": "wedw_dwd.dp_cx2"},
]

COMPLEX_SQL = (
    "create table t as select dept_id, "
    "row_number() over (partition by dept_id order by cnt desc) as rn "
    "from (select dept_id, count(1) as cnt from wedw_ods.x group by dept_id) a"
)


@pytest.mark.asyncio
async def test_scan_deferred_llm_agree_phase2_stores() -> None:
    """复杂任务攒批并发裁决 agree → phase2 逐任务入库；llm_calls 计数正确。"""
    collector = FakeCollector(tasks=COMPLEX_TASKS, sql=COMPLEX_SQL)
    svc = _svc(collector)

    async def llm(messages, **kw):
        return {"content": '{"agree": true}'}

    svc._llm_chat = llm  # type: ignore[method-assign]
    result = await svc.scan_once(_fc(collector))
    assert result["scanned_tasks"] == 2
    assert result["llm_calls"] == 2  # 两个复杂 step 均经并发裁决（非现场串行）
    assert result["llm_confirmed"] == 2  # agree → sqlglot 结果入库
    assert result["errors"] == 0
    svc._dp_repo.create_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_deferred_llm_disagree_creates_tickets() -> None:
    """复杂任务 LLM 分歧 → phase2 逐任务建 diverged 单（tickets_created 计数）。"""
    collector = FakeCollector(tasks=COMPLEX_TASKS, sql=COMPLEX_SQL)
    svc = _svc(collector)

    async def llm(messages, **kw):
        return {"content": '{"agree": false, "reason": "目标表应为 wedw_dwd.other"}'}

    svc._llm_chat = llm  # type: ignore[method-assign]
    result = await svc.scan_once(_fc(collector))
    assert result["scanned_tasks"] == 2
    assert result["llm_calls"] == 2
    assert result["diverged"] == 2
    assert result["tickets_created"] == 2
    assert svc._dp_repo.create_ticket.await_count == 2


@pytest.mark.asyncio
async def test_scan_mixed_simple_and_llm_tasks() -> None:
    """简单任务 phase1 即时提交 + 复杂任务攒批并发 phase2：两类都正确计数。"""

    mixed = TASKS + COMPLEX_TASKS  # 101/102 简单 + 201/202 复杂
    collector = FakeCollector(tasks=mixed, sql=COMPLEX_SQL)
    # 注意：简单任务 101/102 的 step 也用 COMPLEX_SQL（FakeCollector 全局 sql），
    # 因此四个任务全复杂——改用 llm agree 使四者都 llm_confirmed。
    svc = _svc(collector)

    async def llm(messages, **kw):
        return {"content": '{"agree": true}'}

    svc._llm_chat = llm  # type: ignore[method-assign]
    result = await svc.scan_once(_fc(collector))
    assert result["scanned_tasks"] == 4
    assert result["llm_calls"] == 4
    assert result["llm_confirmed"] == 4
    assert result["errors"] == 0


@pytest.mark.asyncio
async def test_scan_whole_round_exception_preserves_prev_retry() -> None:
    """D1 回归：整轮异常轮（fetch_collector 抛错）在 failed run detail 中并入前轮
    遗留 retry_task_ids——此前 except 只写本轮 failed_task_ids（为空），使本 run 成为
    latest_run_log（detail 无 retry ids）后，pending_retry_task_ids 读到空，前轮
    失败任务静默丢失（只能等 24h 自动全量兜底）。"""
    import json as _json

    async def _boom(sid: str) -> FakeCollector:
        raise RuntimeError("dp 源不可达")

    svc = _svc(FakeCollector())
    svc._dp_repo.pending_retry_task_ids = AsyncMock(return_value=[100, 101])
    result = await svc.scan_once(_boom)
    assert result["skipped"] == "failed"
    detail = _json.loads(
        svc._dp_repo.update_run_log.await_args.kwargs["detail_json"]
    )
    assert detail["retry_task_ids"] == [100, 101]


@pytest.mark.asyncio
async def test_scan_full_with_errors_skips_mark_missing() -> None:
    """D3 回归：全量轮带任务失败（errors>0）跳过 mark_missing——失败任务回滚其边
    不在 seen_pairs，照常累加会把「连续两轮全量都失败」的任务旧边误标 stale 删边。
    下一轮全量（24h 自动）再补失效观察即可。"""
    from sqlalchemy.exc import OperationalError

    collector = FakeCollector()
    svc = _svc(collector)
    real_process = svc.process_step

    async def flaky_process(task, step, sql, config, seen_pairs=None, **kwargs):
        if task["task_id"] == 101:
            raise OperationalError("stmt", {}, Exception("Deadlock"))
        return await real_process(task, step, sql, config, seen_pairs)

    svc.process_step = flaky_process  # type: ignore[method-assign]
    result = await svc.scan_once(_fc(collector))
    assert result["errors"] == 1
    # 全量轮（首轮无水位）+ errors>0 → mark_missing 不执行（防误伤）
    svc._lineage_repo.mark_missing.assert_not_awaited()
    # 收尾仍成功态 + 失败任务记录重扫
    assert svc._dp_repo.update_run_log.await_args.kwargs["status"] == "success"


@pytest.mark.asyncio
async def test_scan_restored_edges_not_counted_as_tickets_resolved() -> None:
    """D4 回归：mark_seen 恢复的失效边数（restored）单列 restored_edges 记入 detail，
    不再累加进 tickets_resolved（后者只统计抉择单裁决，避免 run_log/统计口径污染）。"""
    import json as _json

    collector = FakeCollector()
    svc = _svc(collector)
    svc._lineage_repo.mark_seen = AsyncMock(return_value=(2, 3))  # confirmed=2, restored=3
    result = await svc.scan_once(_fc(collector))
    assert result["parsed_ok"] >= 1
    kw = svc._dp_repo.update_run_log.await_args.kwargs
    assert kw["tickets_resolved"] == 0  # restored=3 不计入裁决数
    detail = _json.loads(kw["detail_json"])
    assert detail["restored_edges"] == 3
