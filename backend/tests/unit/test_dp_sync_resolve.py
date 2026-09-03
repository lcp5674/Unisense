"""dp 调度血缘待抉择裁决（resolve_ticket）单元测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.lineage.dp_sync_service import DpSyncService

TASK = {"task_id": 1386, "task_name": "任务", "out_table": "wedw_dwd.dp_out"}
STEP = {"step_id": 5012}


def _ticket(**overrides) -> MagicMock:
    t = MagicMock()
    t.id = 1
    t.task_id = 1386
    t.step_id = 5012
    t.task_name = "任务"
    t.out_table = "wedw_dwd.dp_out"
    t.sql_hash = "abc123"
    t.status = "diverged"
    t.sqlglot_result = {
        "table_edges": [{"source": "wedw_ods.a", "target": "wedw_dwd.dp_out"}],
        "field_edges": [
            {
                "source_table": "wedw_ods.a",
                "source_column": "id",
                "target_table": "wedw_dwd.dp_out",
                "target_column": "id",
            }
        ],
    }
    t.llm_opinion = None
    t.manual_edges_json = None
    for k, v in overrides.items():
        setattr(t, k, v)
    return t


def _svc(ticket: MagicMock) -> DpSyncService:
    svc = DpSyncService.__new__(DpSyncService)
    svc._db = MagicMock()
    svc._lineage_repo = MagicMock()
    svc._lineage_repo.would_create_cycle = AsyncMock(return_value=False)

    async def _fake_upsert(**kw):
        edge = MagicMock()
        edge.id = 100
        edge.dp_task_refs = None
        return edge, False

    svc._lineage_repo.upsert_edge_with_status = AsyncMock(side_effect=_fake_upsert)
    svc._dp_repo = MagicMock()
    svc._dp_repo.get_ticket = AsyncMock(return_value=ticket)
    svc._dp_repo.resolve_ticket = AsyncMock(return_value=ticket)
    svc._dp_repo.upsert_field_mapping = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_resolve_accept_sqlglot_stores_edges() -> None:
    svc = _svc(_ticket())
    result = await svc.resolve_ticket(
        1, resolution="accept_sqlglot", resolved_by=3
    )
    assert result["resolution"] == "accept_sqlglot"
    svc._lineage_repo.upsert_edge_with_status.assert_awaited_once()
    edge = svc._lineage_repo.upsert_edge_with_status.await_args.kwargs
    assert edge["target_node"] == "table:wedw_dwd.dp_out"
    assert edge["provenance"] == "dp_sql"
    svc._dp_repo.resolve_ticket.assert_awaited_once()
    svc._dp_repo.upsert_field_mapping.assert_awaited()


@pytest.mark.asyncio
async def test_resolve_accept_llm_diverged_applies_missing() -> None:
    t = _ticket(
        llm_opinion={
            "agree": False,
            "missing_edges": [
                {"source": "wedw_ods.b", "target": "wedw_dwd.dp_out"}
            ],
            "wrong_edges": [],
        }
    )
    svc = _svc(t)
    result = await svc.resolve_ticket(1, resolution="accept_llm", resolved_by=3)
    assert result["resolution"] == "accept_llm"
    # sqlglot 边 + missing 补边 = 2 次 upsert
    assert svc._lineage_repo.upsert_edge_with_status.await_count == 2


@pytest.mark.asyncio
async def test_resolve_accept_llm_fallback_applies_flow() -> None:
    t = _ticket(
        status="llm_fallback",
        llm_opinion={
            "target_tables": ["wedw_dwd.dp_out"],
            "source_tables": ["wedw_ods.a"],
            "field_mappings": [["wedw_ods.a.id", "wedw_dwd.dp_out.id"]],
            "note": "ok",
        },
    )
    svc = _svc(t)
    result = await svc.resolve_ticket(1, resolution="accept_llm", resolved_by=3)
    assert result["resolution"] == "accept_llm"
    assert svc._lineage_repo.upsert_edge_with_status.await_count == 1
    mapping = svc._dp_repo.upsert_field_mapping.await_args.kwargs
    assert mapping["source_column"] == "id"
    assert mapping["provenance"] == "llm"


@pytest.mark.asyncio
async def test_resolve_manual_applies_hand_edges() -> None:
    t = _ticket()
    manual = {
        "table_edges": [{"source": "wedw_ods.c", "target": "wedw_dwd.dp_out"}],
        "field_mappings": [
            {
                "source_table": "wedw_ods.c",
                "source_column": "code",
                "target_table": "wedw_dwd.dp_out",
                "target_column": "code",
            }
        ],
    }
    svc = _svc(t)
    result = await svc.resolve_ticket(
        1, resolution="manual", resolved_by=3, manual_edges=manual
    )
    assert result["resolution"] == "manual"
    assert svc._lineage_repo.upsert_edge_with_status.await_count == 1
    mapping = svc._dp_repo.upsert_field_mapping.await_args.kwargs
    assert mapping["source_column"] == "code"
    assert mapping["provenance"] == "manual"
    # manual_edges 回传 repo 留痕
    resolve_kwargs = svc._dp_repo.resolve_ticket.await_args.kwargs
    assert resolve_kwargs["manual_edges"] == manual


@pytest.mark.asyncio
async def test_resolve_ignore_no_store() -> None:
    svc = _svc(_ticket())
    result = await svc.resolve_ticket(1, resolution="ignore", resolved_by=3)
    assert result["resolution"] == "ignore"
    svc._lineage_repo.upsert_edge_with_status.assert_not_awaited()
    svc._dp_repo.resolve_ticket.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_unknown_raises() -> None:
    svc = _svc(_ticket())
    with pytest.raises(ValueError):
        await svc.resolve_ticket(1, resolution="unknown", resolved_by=3)


@pytest.mark.asyncio
async def test_resolve_missing_ticket_raises() -> None:
    svc = _svc(_ticket())
    svc._dp_repo.get_ticket = AsyncMock(return_value=None)
    with pytest.raises(LookupError):
        await svc.resolve_ticket(999, resolution="ignore", resolved_by=3)


@pytest.mark.asyncio
async def test_resolve_accept_llm_excludes_wrong_edges() -> None:
    """diverged 采纳 LLM：wrong_edges 声明的 sqlglot 边不再入库（P1-4 回归）。

    此前 wrong_edges 是死字段，「采纳 LLM」与「采纳 sqlglot」等价，LLM 判定
    错误的边仍留在库里。
    """
    t = _ticket(
        sqlglot_result={
            "table_edges": [
                {"source": "wedw_ods.a", "target": "wedw_dwd.dp_out"},
                {"source": "wedw_ods.legacy", "target": "wedw_dwd.dp_out"},
            ],
            "field_edges": [
                {
                    "source_table": "wedw_ods.legacy",
                    "source_column": "id",
                    "target_table": "wedw_dwd.dp_out",
                    "target_column": "id",
                }
            ],
        },
        llm_opinion={
            "agree": False,
            "missing_edges": [{"source": "wedw_ods.b", "target": "wedw_dwd.dp_out"}],
            "wrong_edges": [{"source": "wedw_ods.legacy", "target": "wedw_dwd.dp_out"}],
            "reason": "legacy 表未在本 SQL 出现，判定错误",
        },
    )
    svc = _svc(t)
    await svc.resolve_ticket(1, resolution="accept_llm", resolved_by=3)
    # wrong 边剔除 + sqlglot 保留边 + missing 补边 = 2 次 upsert（legacy 不再出现）
    upserts = svc._lineage_repo.upsert_edge_with_status.await_args_list
    assert len(upserts) == 2
    targets = {a.kwargs["source_node"] for a in upserts}
    assert "table:wedw_ods.legacy" not in targets
    # legacy 的字段映射也不落库
    for call in svc._dp_repo.upsert_field_mapping.await_args_list:
        assert call.kwargs["source_table"] != "wedw_ods.legacy"


@pytest.mark.asyncio
async def test_resolve_accept_llm_no_wrong_keeps_all_sqlglot() -> None:
    """无 wrong_edges 时采纳 LLM 保留全部 sqlglot 边（不误伤）。"""
    t = _ticket(
        sqlglot_result={
            "table_edges": [{"source": "wedw_ods.a", "target": "wedw_dwd.dp_out"}],
            "field_edges": [],
        },
        llm_opinion={"agree": False, "missing_edges": [], "wrong_edges": []},
    )
    svc = _svc(t)
    await svc.resolve_ticket(1, resolution="accept_llm", resolved_by=3)
    upserts = svc._lineage_repo.upsert_edge_with_status.await_args_list
    assert len(upserts) == 1
    assert upserts[0].kwargs["source_node"] == "table:wedw_ods.a"


@pytest.mark.asyncio
async def test_resolve_restores_full_task_refs_from_snapshot() -> None:
    """裁决时从 task_refs_json 快照还原完整任务元数据（director 等入 dp_task_refs）。

    回归（P2-9 #12）：此前 resolve 只用 id/name/out_table 构造 task/step，
    build_task_ref 产出的 ref 缺 director/cycle——责任人快照在裁决入库时丢失。
    """
    t = _ticket(
        task_refs_json={
            "task_id": 1386,
            "task_no": "T1386",
            "task_name": "任务",
            "out_table": "wedw_dwd.dp_out",
            "director": "shifeng",
            "cycle": "day",
            "step_id": 5012,
            "step_name": "SQL节点",
            "task_step": 1,
            "task_node_type": 2,
        }
    )
    svc = _svc(t)
    # 捕获实际写入边的 dp_task_refs（共享 edge 对象回写）
    captured: dict[str, int] = {"calls": 0}
    edge = MagicMock()
    edge.id = 100
    edge.dp_task_refs = None

    async def _fake_upsert(**kw):
        captured["calls"] += 1
        return edge, False

    svc._lineage_repo.upsert_edge_with_status = AsyncMock(side_effect=_fake_upsert)
    await svc.resolve_ticket(1, resolution="accept_sqlglot", resolved_by=3)
    assert captured["calls"] == 1
    assert '"director": "shifeng"' in edge.dp_task_refs
    assert '"task_no": "T1386"' in edge.dp_task_refs
    assert '"cycle": "day"' in edge.dp_task_refs
