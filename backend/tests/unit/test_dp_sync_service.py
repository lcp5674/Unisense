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
        "llm_complexity_rules": None,
    }
    return SimpleNamespace(**{**defaults, **overrides})


def _svc(**kwargs) -> DpSyncService:
    svc = DpSyncService.__new__(DpSyncService)
    svc._db = MagicMock()
    svc._lineage_repo = MagicMock()
    svc._dp_repo = MagicMock()
    svc._llm_chat = kwargs.get("llm_chat")
    # 默认 mock：无环、upsert 返回带 id 的边
    svc._lineage_repo.would_create_cycle = AsyncMock(return_value=False)

    async def _fake_upsert(**kw):
        edge = MagicMock()
        edge.id = 100
        edge.dp_task_refs = None
        return edge, False

    svc._lineage_repo.upsert_edge_with_status = AsyncMock(side_effect=_fake_upsert)
    svc._dp_repo.find_ticket_by_step_hash = AsyncMock(return_value=None)
    svc._dp_repo.create_ticket = AsyncMock(return_value=MagicMock())
    svc._dp_repo.upsert_field_mapping = AsyncMock()
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
    # 表级边写入 + dp_task_refs 合并 + 字段映射
    assert svc._lineage_repo.upsert_edge_with_status.await_count == 1
    edge = svc._lineage_repo.upsert_edge_with_status.await_args.kwargs
    assert edge["target_node"] == "table:wedw_dwd.dp_dq_measure_df"
    assert edge["provenance"] == "dp_sql"
    # 字段映射（department_id/cnt 两条源列）写入
    svc._dp_repo.upsert_field_mapping.assert_awaited()


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
    assert svc._lineage_repo.upsert_edge_with_status.await_count == 1


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
    assert svc._lineage_repo.upsert_edge_with_status.await_count == 0  # 未入库


@pytest.mark.asyncio
async def test_no_flow_skipped() -> None:
    svc = _svc()
    result = await svc.process_step(
        TASK, STEP, "create table a (id bigint)", _config()
    )
    assert result["status"] == "no_flow"
    svc._dp_repo.create_ticket.assert_not_awaited()
    svc._lineage_repo.upsert_edge_with_status.assert_not_awaited()


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
    assert svc._lineage_repo.upsert_edge_with_status.await_count == 1


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
