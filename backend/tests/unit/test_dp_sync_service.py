"""dp 调度血缘编排服务单元测试（三态处理/记忆复用/资产回填）。

以 MagicMock 替换 DpSyncService 内部两个 repository，聚焦编排语义。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.dp_sync import DpSyncConfig
from app.services.lineage.dp_sync_service import DpSyncService

TASK = {
    "task_id": 1386,
    "task_no": "DP1386",
    "task_name": "转诊预约指标",
    "out_table": "wedw_dwd.dp_dq_measure_df",
    "director": "licp",
    "cycle": "day",
}
STEP = {"step_id": 5012, "task_id": 1386, "step_name": "SQL 节点", "task_step": 3}


def _config(**overrides) -> DpSyncConfig:
    defaults = {
        "enabled": True,
        "llm_enabled": True,
        "resolve_memory_enabled": True,
        "owner_backfill": "orphan_only",
        "exclude_table_patterns": None,
        "exclude_task_patterns": None,
        "llm_complexity_rules": None,
    }
    return SimpleNamespace(**{**defaults, **overrides})


def _svc(**kwargs) -> DpSyncService:
    svc = DpSyncService.__new__(DpSyncService)
    svc._db = MagicMock()
    svc._lineage_repo = MagicMock()
    svc._dp_repo = MagicMock()
    svc._llm_chat = kwargs.get("llm_chat")
    # 方案 3：schema provider 默认 None（scan_once 内绑定，单测不启用）
    svc._schema_provider = None
    # 默认 mock：无环、upsert 返回带 id 的边
    svc._lineage_repo.would_create_cycle = AsyncMock(return_value=False)

    async def _fake_upsert(**kw):
        edge = MagicMock()
        edge.id = 100
        edge.dp_task_refs = None
        return edge, False

    svc._lineage_repo.upsert_edge_with_status = AsyncMock(side_effect=_fake_upsert)
    # P2 阶段 2：_store_sqlglot_edges 走批量写——批量环检测默认无环、批量 upsert
    # 返回 ``{(source,target,type,gran): (edge, created)}``、字段映射批量默认成功。
    svc._lineage_repo.would_create_cycle_many = AsyncMock(return_value=set())

    async def _fake_upsert_batch(requests):
        out = {}
        for r in requests:
            edge = MagicMock()
            edge.id = 100
            edge.dp_task_refs = None
            key = (
                r["source_node"],
                r["target_node"],
                r["edge_type"],
                r.get("granularity") or "L3",
            )
            out[key] = (edge, False)
        return out

    svc._lineage_repo.upsert_edges_with_status_batch = AsyncMock(
        side_effect=_fake_upsert_batch
    )
    svc._dp_repo.find_ticket_by_step_hash = AsyncMock(return_value=None)
    svc._dp_repo.create_ticket = AsyncMock(return_value=MagicMock())
    svc._dp_repo.upsert_field_mapping = AsyncMock()
    svc._dp_repo.upsert_field_mappings_batch = AsyncMock(return_value=0)
    svc._dp_repo.soft_delete_field_mappings = AsyncMock(return_value=0)
    svc._dp_repo.find_orphan_catalogs = AsyncMock(return_value=[])
    svc._dp_repo.find_user_by_username = AsyncMock(return_value=None)
    svc._dp_repo.create_shadow_user = AsyncMock(return_value=MagicMock(id=99))
    svc._dp_repo.update_catalog_owner = AsyncMock()
    return svc


SIMPLE_SQL = (
    "create table wedw_dwd.dp_dq_measure_df as "
    "select department_id, count(1) as cnt from wedw_ods.visit_d "
    "where date_id='2026-08-18' group by department_id"
)


@pytest.mark.asyncio
async def test_simple_ok_stored_without_llm() -> None:
    svc = _svc()
    result = await svc.process_step(TASK, STEP, SIMPLE_SQL, _config())
    assert result["status"] == "parsed_ok"
    svc._dp_repo.create_ticket.assert_not_awaited()
    # 表级边批量写入 + dp_task_refs 合并 + 字段映射批量
    assert svc._lineage_repo.upsert_edges_with_status_batch.await_count == 1
    edge = svc._lineage_repo.upsert_edges_with_status_batch.await_args.args[0][0]
    assert edge["target_node"] == "table:wedw_dwd.dp_dq_measure_df"
    assert edge["provenance"] == "dp_sql"
    # 字段映射（department_id/cnt 两条源列）批量写入
    svc._dp_repo.upsert_field_mappings_batch.assert_awaited()


@pytest.mark.asyncio
async def test_complex_llm_agree_stored() -> None:
    async def llm(messages, **kw):
        return {"content": '{"agree": true}'}

    svc = _svc(llm_chat=llm)
    sql = (
        "create table t as select dept_id, "
        "row_number() over (partition by dept_id order by cnt desc) as rn "
        "from (select dept_id, count(1) as cnt from wedw_ods.x group by dept_id) a"
    )
    result = await svc.process_step(TASK, STEP, sql, _config())
    assert result["status"] == "llm_confirmed"
    svc._dp_repo.create_ticket.assert_not_awaited()
    assert svc._lineage_repo.upsert_edges_with_status_batch.await_count == 1


@pytest.mark.asyncio
async def test_complex_llm_disagree_creates_diverged_ticket() -> None:
    async def llm(messages, **kw):
        return {"content": '{"agree": false, "reason": "目标表应为 wedw_dwd.other"}'}

    svc = _svc(llm_chat=llm)
    sql = (
        "create table t as select dept_id, "
        "row_number() over (partition by dept_id order by cnt desc) as rn "
        "from (select dept_id, count(1) as cnt from wedw_ods.x group by dept_id) a"
    )
    result = await svc.process_step(TASK, STEP, sql, _config())
    assert result["status"] == "diverged"
    svc._dp_repo.create_ticket.assert_awaited_once()
    kwargs = svc._dp_repo.create_ticket.await_args.kwargs
    assert kwargs["status"] == "diverged"
    assert "不一致" in kwargs["divergence_reason"] or kwargs["divergence_reason"]


@pytest.mark.asyncio
async def test_failed_llm_fallback_creates_llm_fallback_ticket() -> None:
    async def llm(messages, **kw):
        return {
            "content": (
                '{"target_tables": ["wedw_dwd.t"], "source_tables": ["wedw_ods.s"],'
                ' "field_mappings": [], "note": "ok"}'
            )
        }

    svc = _svc(llm_chat=llm)
    result = await svc.process_step(TASK, STEP, "this is not sql {{{", _config())
    assert result["status"] == "llm_fallback"
    kwargs = svc._dp_repo.create_ticket.await_args.kwargs
    assert kwargs["status"] == "llm_fallback"
    assert kwargs["llm_opinion"]["target_tables"] == ["wedw_dwd.t"]


@pytest.mark.asyncio
async def test_failed_llm_unable_creates_unparseable_ticket() -> None:
    async def llm(messages, **kw):
        return {
            "content": (
                '{"target_tables": [], "source_tables": [], "field_mappings": [],'
                ' "note": "无法理解"}'
            )
        }

    svc = _svc(llm_chat=llm)
    result = await svc.process_step(TASK, STEP, "this is not sql {{{", _config())
    assert result["status"] == "unparseable"
    kwargs = svc._dp_repo.create_ticket.await_args.kwargs
    assert kwargs["status"] == "unparseable"


@pytest.mark.asyncio
async def test_llm_output_garbage_falls_to_ticket() -> None:
    async def llm(messages, **kw):
        return {"content": "不是 JSON 内容"}

    svc = _svc(llm_chat=llm)
    sql = (
        "create table t as select dept_id, "
        "row_number() over (partition by dept_id order by cnt desc) as rn "
        "from (select dept_id, count(1) as cnt from wedw_ods.x group by dept_id) a"
    )
    result = await svc.process_step(TASK, STEP, sql, _config())
    assert result["status"] == "diverged"
    assert svc._lineage_repo.upsert_edges_with_status_batch.await_count == 0  # 未入库


@pytest.mark.asyncio
async def test_no_flow_skipped() -> None:
    svc = _svc()
    result = await svc.process_step(
        TASK, STEP, "create table a (id bigint)", _config()
    )
    assert result["status"] == "no_flow"
    svc._dp_repo.create_ticket.assert_not_awaited()
    svc._lineage_repo.upsert_edge_with_status.assert_not_awaited()
    svc._lineage_repo.upsert_edges_with_status_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_reuse_accept_sqlglot() -> None:
    ticket = MagicMock()
    ticket.status = "resolved"
    ticket.resolution = "accept_sqlglot"
    ticket.llm_opinion = None
    ticket.manual_edges_json = None
    svc = _svc()
    svc._dp_repo.find_ticket_by_step_hash = AsyncMock(return_value=ticket)
    sql = (
        "create table t as select dept_id, "
        "row_number() over (partition by dept_id order by cnt desc) as rn "
        "from (select dept_id, count(1) as cnt from wedw_ods.x group by dept_id) a"
    )
    result = await svc.process_step(TASK, STEP, sql, _config())
    assert result["status"] == "memory_reused"
    svc._dp_repo.create_ticket.assert_not_awaited()
    assert svc._lineage_repo.upsert_edges_with_status_batch.await_count == 1


@pytest.mark.asyncio
async def test_memory_reuse_ignored_skips() -> None:
    ticket = MagicMock()
    ticket.status = "ignored"
    ticket.resolution = "ignore"
    svc = _svc()
    svc._dp_repo.find_ticket_by_step_hash = AsyncMock(return_value=ticket)
    sql = (
        "create table t as select dept_id, "
        "row_number() over (partition by dept_id order by cnt desc) as rn "
        "from (select dept_id, count(1) as cnt from wedw_ods.x group by dept_id) a"
    )
    result = await svc.process_step(TASK, STEP, sql, _config())
    assert result["status"] == "memory_ignored"
    svc._dp_repo.create_ticket.assert_not_awaited()
    svc._lineage_repo.upsert_edge_with_status.assert_not_awaited()
    svc._lineage_repo.upsert_edges_with_status_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_llm_disabled_complex_creates_ticket() -> None:
    svc = _svc()
    sql = (
        "create table t as select dept_id, "
        "row_number() over (partition by dept_id order by cnt desc) as rn "
        "from (select dept_id, count(1) as cnt from wedw_ods.x group by dept_id) a"
    )
    result = await svc.process_step(TASK, STEP, sql, _config(llm_enabled=False))
    assert result["status"] == "diverged"
    kwargs = svc._dp_repo.create_ticket.await_args.kwargs
    assert "LLM 已关闭" in kwargs["divergence_reason"]


@pytest.mark.asyncio
async def test_backfill_owner_orphan_with_shadow_user() -> None:
    svc = _svc()
    svc._dp_repo.find_orphan_catalogs = AsyncMock(return_value=[MagicMock(id=5)])
    svc._dp_repo.find_user_by_username = AsyncMock(return_value=None)
    shadow = MagicMock()
    shadow.id = 99
    svc._dp_repo.create_shadow_user = AsyncMock(return_value=shadow)
    svc._dp_repo.update_catalog_owner = AsyncMock()
    result = await svc.backfill_owner(TASK, _config())
    assert result == {"backfilled": 1, "shadow_created": True}
    svc._dp_repo.create_shadow_user.assert_awaited_once_with("licp")
    svc._dp_repo.update_catalog_owner.assert_awaited_once_with(5, 99)


@pytest.mark.asyncio
async def test_backfill_owner_never_skips() -> None:
    svc = _svc()
    result = await svc.backfill_owner(TASK, _config(owner_backfill="never"))
    assert result["backfilled"] == 0
    svc._dp_repo.find_orphan_catalogs.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_owner_existing_user_no_shadow() -> None:
    svc = _svc()
    svc._dp_repo.find_orphan_catalogs = AsyncMock(return_value=[MagicMock(id=5)])
    existing = MagicMock()
    existing.id = 3
    svc._dp_repo.find_user_by_username = AsyncMock(return_value=existing)
    svc._dp_repo.create_shadow_user = AsyncMock()
    svc._dp_repo.update_catalog_owner = AsyncMock()
    result = await svc.backfill_owner(TASK, _config())
    assert result == {"backfilled": 1, "shadow_created": False}
    svc._dp_repo.create_shadow_user.assert_not_awaited()
    svc._dp_repo.update_catalog_owner.assert_awaited_once_with(5, 3)


@pytest.mark.asyncio
async def test_process_task_writes_current_step_type_to_progress() -> None:
    """_process_task 逐 step 处理时把当前节点类型写入 progress（供前端按类型动态展示）。

    进度轮询端读取 progress["current_step_label"]：DataX(2) 在前、Hive(7) 在后——
    处理后 progress 应保留最近处理的类型 label（Hive/Spark SQL）。
    """
    svc = _svc()
    svc._fetch_task = AsyncMock(
        return_value={"task_id": 1386, "task_name": "T", "out_table": "a.b"}
    )
    svc._fetch_sql_steps = AsyncMock(
        return_value=[
            {"task_step_type": 2, "script_info": '{"job": {"content": []}}'},
            {"task_step_type": 7, "script_info": SIMPLE_SQL},
        ]
    )
    svc.process_step = AsyncMock(return_value={"status": "parsed_ok"})
    svc.backfill_owner = AsyncMock()
    counters = {"scanned_tasks": 0, "scanned_steps": 0}
    progress: dict[str, object] = {"stage": "parsing"}
    await svc._process_task(
        object(), _config(), 1386, counters, set(), progress=progress
    )
    assert counters["scanned_steps"] == 2
    assert progress["current_step_type"] == 7
    assert progress["current_step_label"] == "Hive/Spark SQL"
    svc.backfill_owner.assert_awaited_once()


@pytest.mark.asyncio
async def test_store_sqlglot_edges_skips_cyclic_edge_and_its_fields() -> None:
    """P2 阶段 2：批量环检测命中时，成环表边与其字段映射整体跳过（不写库、不进 seen）。"""
    svc = _svc()
    cyclic = {("table:wedw_ods.visit_d", "table:wedw_dwd.dp_dq_measure_df")}
    svc._lineage_repo.would_create_cycle_many = AsyncMock(return_value=cyclic)
    seen: set[tuple[str, str]] = set()
    result = await svc.process_step(TASK, STEP, SIMPLE_SQL, _config(), seen_pairs=seen)
    assert result["status"] == "parsed_ok"
    assert result["fields_written"] == 0
    svc._lineage_repo.upsert_edges_with_status_batch.assert_not_awaited()
    svc._dp_repo.upsert_field_mappings_batch.assert_not_awaited()
    assert seen == set()  # 成环边不进 seen_pairs → 收尾不 mark_seen 保护


@pytest.mark.asyncio
async def test_store_sqlglot_edges_batches_field_items_with_edge_refs() -> None:
    """P2 阶段 2：字段映射走批量，每条携带 edge_id/sql_hash/step_id/provenance。"""
    svc = _svc()
    seen: set[tuple[str, str]] = set()
    result = await svc.process_step(TASK, STEP, SIMPLE_SQL, _config(), seen_pairs=seen)
    assert result["status"] == "parsed_ok"
    # 批量 upsert 一次 + 字段映射批量一次
    assert svc._lineage_repo.upsert_edges_with_status_batch.await_count == 1
    assert svc._dp_repo.upsert_field_mappings_batch.await_count == 1
    items = svc._dp_repo.upsert_field_mappings_batch.await_args.args[0]
    assert len(items) >= 1
    for it in items:
        assert it["edge_id"] == 100  # fake 批量 upsert 返回的边 id
        assert it["sql_hash"]  # sql_fingerprint 派生，非空
        assert it["step_id"] == 5012
        assert it["provenance"] == "sqlglot"
        assert it["confidence"] == 1.0
        assert it["target_table"] == "wedw_dwd.dp_dq_measure_df"
    # seen_pairs 记录成功写入的表边（visit_d → 产出表）
    assert ("table:wedw_ods.visit_d", "table:wedw_dwd.dp_dq_measure_df") in seen


# ---------------------------------------------------------------------------
# 方案 A：LLM 裁决延迟（_LlmWork）+ 批级并发 phase2
# ---------------------------------------------------------------------------

COMPLEX_SQL = (
    "create table t as select dept_id, "
    "row_number() over (partition by dept_id order by cnt desc) as rn "
    "from (select dept_id, count(1) as cnt from wedw_ods.x group by dept_id) a"
)


@pytest.mark.asyncio
async def test_process_step_defer_llm_returns_work_item() -> None:
    """defer_llm=True + LLM 开启：复杂节点返回 _LlmWork（不现场调 LLM/不建单）。"""
    from app.services.lineage.dp_sync_service import _LlmWork

    svc = _svc(llm_chat=lambda messages, **kw: {"content": '{"agree": true}'})
    result = await svc.process_step(TASK, STEP, COMPLEX_SQL, _config(), defer_llm=True)
    assert isinstance(result, _LlmWork)
    assert result.kind == "confirm"
    svc._dp_repo.create_ticket.assert_not_awaited()
    svc._lineage_repo.upsert_edges_with_status_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_step_defer_llm_disabled_creates_ticket_immediately() -> None:
    """defer_llm=True + LLM 关闭：复杂节点即时建单（与不 defer 的关闭行为一致）。"""
    from app.services.lineage.dp_sync_service import _LlmWork

    svc = _svc()
    result = await svc.process_step(
        TASK, STEP, COMPLEX_SQL, _config(llm_enabled=False), defer_llm=True
    )
    assert not isinstance(result, _LlmWork)
    assert result["status"] == "diverged"
    svc._dp_repo.create_ticket.assert_awaited_once()


@pytest.mark.asyncio
async def test_finish_deferred_task_applies_agree_verdict() -> None:
    """phase2：confirm 裁决 agree → 边入库 + llm_confirmed 计数。"""
    from app.services.lineage.dp_sync_service import _LlmWork

    svc = _svc(llm_chat=lambda messages, **kw: {"content": '{"agree": true}'})
    # 先收集工作项（不现场裁决）
    work = await svc.process_step(TASK, STEP, COMPLEX_SQL, _config(), defer_llm=True)
    assert isinstance(work, _LlmWork)
    # 模拟并发裁决回填 result
    from app.services.lineage.dp_sync_llm import parse_confirm_response

    work.result = parse_confirm_response('{"agree": true}')
    counters: dict[str, int] = dict.fromkeys(
        (
            "parsed_ok", "llm_confirmed", "diverged", "llm_fallback",
            "unparseable", "llm_calls", "tickets_created", "errors",
            "field_mappings_written", "field_edges_degraded",
            "tickets_resolved", "memory_ignored", "memory_reused",
            "scanned_tasks", "scanned_steps", "no_flow", "unknown",
        ),
        0,
    )
    seen: set[tuple[str, str]] = set()
    await svc._finish_deferred_task([work], _config(), counters, seen)
    assert counters.get("llm_confirmed") == 1
    assert svc._lineage_repo.upsert_edges_with_status_batch.await_count == 1
    svc._dp_repo.create_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_finish_deferred_task_error_creates_diverged_ticket() -> None:
    """phase2：LLM 输出异常（error 回填）→ 建 diverged 单，不静默丢失。"""
    from app.services.lineage.dp_sync_service import _LlmWork

    svc = _svc(llm_chat=lambda messages, **kw: {"content": ""})
    work = await svc.process_step(TASK, STEP, COMPLEX_SQL, _config(), defer_llm=True)
    assert isinstance(work, _LlmWork)
    work.error = "无法解析 LLM 输出"
    counters: dict[str, int] = dict.fromkeys(
        (
            "parsed_ok", "llm_confirmed", "diverged", "llm_fallback",
            "unparseable", "llm_calls", "tickets_created", "errors",
            "field_mappings_written", "field_edges_degraded",
            "tickets_resolved", "memory_ignored", "memory_reused",
            "scanned_tasks", "scanned_steps", "no_flow", "unknown",
        ),
        0,
    )
    await svc._finish_deferred_task([work], _config(), counters, set())
    assert counters.get("diverged") == 1
    assert counters.get("tickets_created") == 1
    kwargs = svc._dp_repo.create_ticket.await_args.kwargs
    assert kwargs["status"] == "diverged"
    assert "LLM 确认输出异常" in kwargs["divergence_reason"]


@pytest.mark.asyncio
async def test_resolve_llm_works_concurrent_fills_results() -> None:
    """批级并发裁决：全部工作项 result 回填、llm_calls 计数、异常项 error 标记。"""
    import asyncio

    from app.services.lineage.dp_sync_llm import DpSyncLlmError
    from app.services.lineage.dp_sync_service import _LlmWork

    n = {"calls": 0}

    async def llm(messages, **kw):
        n["calls"] += 1
        if n["calls"] == 4:  # 第 4 个并发项模拟 LLM 输出异常
            raise DpSyncLlmError("LLM 输出不可解析")
        await asyncio.sleep(0)  # 让出事件循环（模拟 IO）
        return {"content": '{"agree": true}'}

    svc = _svc(llm_chat=llm)
    works: list[_LlmWork] = []
    for _ in range(4):
        w = await svc.process_step(TASK, STEP, COMPLEX_SQL, _config(), defer_llm=True)
        assert isinstance(w, _LlmWork)
        works.append(w)

    counters: dict[str, int] = dict.fromkeys(
        (
            "parsed_ok", "llm_confirmed", "diverged", "llm_fallback",
            "unparseable", "llm_calls", "tickets_created", "errors",
            "field_mappings_written", "field_edges_degraded",
            "tickets_resolved", "memory_ignored", "memory_reused",
            "scanned_tasks", "scanned_steps", "no_flow", "unknown",
        ),
        0,
    )
    await svc._resolve_llm_works(works, counters)
    assert counters.get("llm_calls") == 4
    assert all(w.result is not None or w.error for w in works)
    assert works[3].error is not None  # 不可解析项被标记


@pytest.mark.asyncio
async def test_store_sqlglot_edges_written_uses_batch_real_count() -> None:
    """D5 回归：fields_written 取 upsert_field_mappings_batch 的真实写入数（新建+
    复活），而非「尝试条数」——SQL 未变的重扫（活跃已存在项被忽略）不再每轮把
    全量映射虚报为 field_mappings_written。"""
    svc = _svc()
    # 批量写返回真实写入数 3（模拟：5 条尝试、2 条活跃已存在被忽略）
    svc._dp_repo.upsert_field_mappings_batch = AsyncMock(return_value=3)
    seen: set[tuple[str, str]] = set()
    result = await svc.process_step(TASK, STEP, SIMPLE_SQL, _config(), seen_pairs=seen)
    assert result["status"] == "parsed_ok"
    assert result["fields_written"] == 3  # 真实写入数（非尝试条数）
    # 对比：旧实现 written = len(field_items) 会把忽略的活跃项也计入
    items = svc._dp_repo.upsert_field_mappings_batch.await_args.args[0]
    assert len(items) >= 1
