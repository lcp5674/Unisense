"""dp 调度血缘待抉择裁决（resolve_ticket）单元测试。"""

from __future__ import annotations

from types import SimpleNamespace
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
