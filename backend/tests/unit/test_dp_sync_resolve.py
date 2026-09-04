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
    t.resolution = None  # 未裁决（M3 幂等拦截基准）
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


@pytest.mark.asyncio
async def test_resolve_already_resolved_different_rejected() -> None:
    """M3：已裁决单不同 resolution 重复裁决被拒（防 accept 落边后改 ignore 背离）。"""
    t = _ticket(resolution="accept_sqlglot")
    svc = _svc(t)
    with pytest.raises(ValueError, match="已裁决为 accept_sqlglot"):
        await svc.resolve_ticket(1, resolution="ignore", resolved_by=3)
    # 同 resolution 幂等放行（不重复写边）
    svc._lineage_repo.upsert_edge_with_status = AsyncMock(return_value=(MagicMock(), False))
    result = await svc.resolve_ticket(1, resolution="accept_sqlglot", resolved_by=3)
    assert result["resolution"] == "accept_sqlglot"


@pytest.mark.asyncio
async def test_resolve_manual_rejects_dirty_table_name() -> None:
    """M3：manual 边表名含脏字符（空格/分号）被格式校验拒绝。"""
    t = _ticket()
    svc = _svc(t)
    manual = {
        "table_edges": [
            {"source": "wedw_ods.a; drop", "target": "wedw_dwd.dp_out"}
        ]
    }
    with pytest.raises(ValueError, match="手动血缘表名不合法"):
        await svc.resolve_ticket(
            1, resolution="manual", resolved_by=3, manual_edges=manual
        )
    svc._lineage_repo.upsert_edge_with_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_reprocess_unparseable_three_state() -> None:
    """调度宏展开后存量 unparseable 自动重判：宏可解析→入库置 accept_sqlglot、
    纯 DDL→ignore、真失败（垃圾文本）→保留待人工。"""
    ok_ticket = _ticket()
    ok_ticket.id = 1
    ok_ticket.status = "unparseable"
    ok_ticket.resolution = None
    ok_ticket.sql_text = (
        "use wedw_ods;\ncreate table wedw_ods.t_${DATA_DATE} as "
        "select * from wedw_ods.src_${DATA_DATE};\n"
    )
    ok_ticket.task_refs_json = {"task_id": 1386, "step_id": 5012}

    noflow_ticket = _ticket()
    noflow_ticket.id = 2
    noflow_ticket.status = "unparseable"
    noflow_ticket.resolution = None
    noflow_ticket.sql_text = "create table wedw_ods.x_${D} (id string);\n"
    noflow_ticket.task_refs_json = {"task_id": 1386, "step_id": 5013}

    keep_ticket = _ticket()
    keep_ticket.id = 3
    keep_ticket.status = "unparseable"
    keep_ticket.resolution = None
    keep_ticket.sql_text = "this is not sql at all {{{"
    keep_ticket.task_refs_json = {"task_id": 1386, "step_id": 5014}

    svc = _svc(ok_ticket)
    svc._dp_repo.list_tickets = AsyncMock(
        return_value=([ok_ticket, noflow_ticket, keep_ticket], 3)
    )

    counters = await svc.reprocess_unparseable_tickets(limit=100)
    assert counters == {"parsed": 1, "no_flow": 1, "kept": 1}
    # 可解析单：边入库 + 标 accept_sqlglot；纯 DDL 单标 ignore；失败单不动
    assert svc._lineage_repo.upsert_edge_with_status.await_count == 1
    calls = [c.kwargs["resolution"] for c in svc._dp_repo.resolve_ticket.await_args_list]
    assert calls == ["accept_sqlglot", "ignore"]


@pytest.mark.asyncio
async def test_resolve_llm_disabled_batch() -> None:
    """一键处置 LLM 关闭期单：筛选标记 + 批量 accept_sqlglot，含失败容错。"""
    t1 = _ticket()
    t1.divergence_reason = "LLM 已关闭（配置），复杂节点未确认，请人工抉择"
    t2 = _ticket()
    t2.id = 2
    t2.divergence_reason = "sqlglot 与 LLM 意见不一致"  # 真分歧，不应被处置
    t3 = _ticket()
    t3.id = 3
    t3.divergence_reason = "LLM 已关闭（配置），复杂节点未确认，请人工抉择"
    t3.resolution = "ignore"  # 已裁决，应跳过
    svc = _svc(t1)
    svc._dp_repo.list_tickets = AsyncMock(
        return_value=([t1, t2, t3], 3)
    )
    # t1 入库成功，t3 已裁决（resolve_ticket 会因 resolution 非 None 报错——
    # 但这里 resolve_ticket 被 mock 恒成功；改用 resolve_ticket side_effect 模拟失败
    async def _maybe_fail(ticket_id, **kw):
        if ticket_id == 3:
            raise ValueError("该单已裁决为 ignore")
        return {"ticket_id": ticket_id}

    svc.resolve_ticket = AsyncMock(side_effect=_maybe_fail)  # type: ignore[method-assign]
    counters = await svc.resolve_llm_disabled_tickets(resolved_by=3)
    # targets 仅 t1（t2 非 LLM 关闭标记、t3 已裁决被跳过）
    assert counters["resolved"] == 1
    assert counters["failed"] == 0
    assert counters["skipped"] == 2  # t2/t3 被排除（非标记 + 已裁决）
    svc.resolve_ticket.assert_awaited_once_with(
        ticket_id=1, resolution="accept_sqlglot", resolved_by=3
    )


# ==================== LLM 重试（retry_llm_tickets） ====================


def _retry_svc(ticket: MagicMock, responses) -> DpSyncService:
    """构造带注入 llm_chat 的 service（retry 时跳过 _build_llm_chat）。"""
    svc = _svc(ticket)
    svc._dp_repo.list_retryable_llm_tickets = AsyncMock(return_value=[ticket])
    svc._dp_repo.update_ticket_llm = AsyncMock(return_value=ticket)
    if isinstance(responses, list):
        svc._llm_chat = AsyncMock(side_effect=responses)
    else:
        svc._llm_chat = AsyncMock(return_value=responses)
    return svc


@pytest.mark.asyncio
async def test_retry_diverged_agree_auto_resolves() -> None:
    """LLM 关闭期 diverged 单重试：LLM 认可 sqlglot → 自动采纳入库消解。"""
    t = _ticket(status="diverged")
    t.divergence_reason = "LLM 已关闭（配置），复杂节点未确认，请人工抉择"
    svc = _retry_svc(t, {"content": '{"agree": true, "reason": "ok"}'})
    counters = await svc.retry_llm_tickets(resolved_by=3)
    assert counters["auto_resolved"] == 1
    assert counters["failed"] == 0
    svc._dp_repo.resolve_ticket.assert_awaited_once_with(
        1, resolution="accept_sqlglot", resolved_by=3
    )
    svc._dp_repo.update_ticket_llm.assert_not_awaited()
    # 边已写入（采纳 sqlglot）
    svc._lineage_repo.upsert_edge_with_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_diverged_disagree_refreshes_opinion() -> None:
    """LLM 恢复后仍不认可 sqlglot → 刷新意见保留待人工，不自动裁决。"""
    t = _ticket(status="diverged")
    t.divergence_reason = "LLM 确认输出异常：LLM 返回空内容"
    content = (
        '{"agree": false, "wrong_edges": [{"source": "wedw_ods.a", '
        '"target": "wedw_dwd.dp_out"}], "reason": "目标表实为中间层"}'
    )
    svc = _retry_svc(t, {"content": content})
    counters = await svc.retry_llm_tickets(resolved_by=3)
    assert counters["refreshed"] == 1
    assert counters["auto_resolved"] == 0
    svc._dp_repo.update_ticket_llm.assert_awaited_once()
    kwargs = svc._dp_repo.update_ticket_llm.await_args.kwargs
    assert kwargs["llm_opinion"]["agree"] is False
    assert kwargs["divergence_reason"] == "目标表实为中间层"
    svc._dp_repo.resolve_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_llm_fallback_refreshes_flow() -> None:
    """llm_fallback 单重试：LLM 重新提炼成功 → 刷新低置信参考意见。"""
    t = _ticket(status="llm_fallback")
    t.llm_opinion = {"target_tables": [], "note": "旧兜底"}
    content = (
        '{"target_tables": ["wedw_dwd.dp_out"], "source_tables": ["wedw_ods.a"], '
        '"field_mappings": [], "note": "新兜底"}'
    )
    svc = _retry_svc(t, {"content": content})
    counters = await svc.retry_llm_tickets(resolved_by=3)
    assert counters["refreshed"] == 1
    svc._dp_repo.update_ticket_llm.assert_awaited_once()
    kwargs = svc._dp_repo.update_ticket_llm.await_args.kwargs
    assert kwargs["status"] == "llm_fallback"
    assert kwargs["llm_opinion"]["target_tables"] == ["wedw_dwd.dp_out"]
    svc._dp_repo.resolve_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_unparseable_still_fails_kept() -> None:
    """unparseable 单重试后 LLM 仍无法提炼 → 保留待人工。"""
    t = _ticket(status="unparseable")
    t.divergence_reason = "LLM 兜底输出异常：LLM 返回空内容"
    content = '{"target_tables": [], "source_tables": [], "note": "还是不行"}'
    svc = _retry_svc(t, {"content": content})
    counters = await svc.retry_llm_tickets(resolved_by=3)
    assert counters["kept"] == 1
    assert counters["refreshed"] == 0
    kwargs = svc._dp_repo.update_ticket_llm.await_args.kwargs
    assert kwargs["status"] == "unparseable"
    assert kwargs["llm_opinion"]["note"] == "还是不行"


@pytest.mark.asyncio
async def test_retry_llm_error_counts_failed_keeps_open() -> None:
    """LLM 仍返回空内容（协议错误）→ 计 failed、单保持未裁决。"""
    t = _ticket(status="diverged")
    t.divergence_reason = "LLM 已关闭（配置），复杂节点未确认，请人工抉择"
    svc = _retry_svc(t, {"content": ""})  # 空 content → parse 抛 DpSyncLlmError
    counters = await svc.retry_llm_tickets(resolved_by=3)
    assert counters["failed"] == 1
    assert counters["auto_resolved"] == 0
    svc._dp_repo.resolve_ticket.assert_not_awaited()
    svc._dp_repo.update_ticket_llm.assert_not_awaited()
