"""dp 调度血缘 LLM 协议单元测试（共识确认/兜底提炼/JSON 容错）。"""

from __future__ import annotations

import pytest

from app.services.lineage.dp_sync_llm import (
    DpSyncLlmError,
    build_confirm_messages,
    build_fallback_messages,
    edges_to_json,
    extract_json,
    parse_confirm_response,
    parse_fallback_response,
)


def _edge_objs() -> tuple[list, list]:
    class TE:
        source = "wedw_ods.a"
        target = "wedw_dwd.t"

    class FE:
        source_table = "wedw_ods.a"
        source_column = "id"
        target_table = "wedw_dwd.t"
        target_column = "id"
        expression = None
        degraded = False

    return [TE()], [FE()]


def test_build_confirm_messages_contains_sql_and_result() -> None:
    table_edges, field_edges = _edge_objs()
    msgs = build_confirm_messages("select 1", edges_to_json(table_edges, field_edges))
    assert msgs[0]["role"] == "system"
    assert "sqlglot" in msgs[0]["content"]
    assert "select 1" in msgs[1]["content"]
    assert "wedw_dwd.t" in msgs[1]["content"]


def test_build_fallback_messages() -> None:
    msgs = build_fallback_messages("create table t as select * from s")
    assert msgs[1]["role"] == "user"
    assert "create table t as select * from s" in msgs[1]["content"]


def test_extract_json_plain() -> None:
    assert extract_json('{"agree": true}') == {"agree": True}


def test_extract_json_with_fence() -> None:
    text = "好的，结果如下：\n```json\n{\"agree\": false, \"reason\": \"x\"}\n```\n完毕"
    assert extract_json(text) == {"agree": False, "reason": "x"}


def test_extract_json_with_surrounding_noise() -> None:
    text = '说明文字 {"target_tables": ["a.t"]} 结尾说明'
    assert extract_json(text) == {"target_tables": ["a.t"]}


def test_extract_json_empty_raises() -> None:
    with pytest.raises(DpSyncLlmError):
        extract_json("")


def test_extract_json_no_object_raises() -> None:
    with pytest.raises(DpSyncLlmError):
        extract_json("完全不是 JSON")


def test_extract_json_invalid_raises() -> None:
    with pytest.raises(DpSyncLlmError):
        extract_json('{"agree": tru}')


def test_parse_confirm_agree_true() -> None:
    v = parse_confirm_response('{"agree": true}')
    assert v.agree is True
    assert v.missing_edges == []
    assert v.wrong_edges == []


def test_parse_confirm_disagree_with_edges() -> None:
    text = (
        '{"agree": false, "missing_edges": [{"target": "b.t", "source": "a.s"}],'
        ' "wrong_edges": [{"target": "x", "source": "y", "reason": "表不存在"}],'
        ' "reason": "目标表写错"}'
    )
    v = parse_confirm_response(text)
    assert v.agree is False
    assert v.missing_edges[0]["target"] == "b.t"
    assert v.wrong_edges[0]["reason"] == "表不存在"


def test_parse_confirm_string_bool() -> None:
    assert parse_confirm_response('{"agree": "false"}').agree is False


def test_parse_confirm_implicit_disagree_on_missing() -> None:
    # 未显式给 agree 但有缺失边 → 推断不同意
    v = parse_confirm_response('{"missing_edges": [{"target": "b.t", "source": "a.s"}]}')
    assert v.agree is False


def test_parse_confirm_cannot_judge_defaults_disagree() -> None:
    """T1：LLM 规则 5「无法判断」漏发 agree:false → 保守按不同意（建分歧单）。"""
    v = parse_confirm_response('{"reason": "无法判断：SQL 语义不明确"}')
    assert v.agree is False
    assert v.reason == "无法判断：SQL 语义不明确"


def test_parse_confirm_empty_object_defaults_disagree() -> None:
    """T1：LLM 返回空对象（缺 agree 且无边差异）→ 不再默认同意入库。"""
    v = parse_confirm_response("{}")
    assert v.agree is False


def test_parse_confirm_explicit_true_still_agree() -> None:
    """T1 保守缺省不影响显式 agree:true 的放行路径。"""
    v = parse_confirm_response('{"agree": true, "reason": "确认正确"}')
    assert v.agree is True


def test_parse_fallback_normal() -> None:
    text = (
        '{"target_tables": ["wedw_dwd.t"], "source_tables": ["wedw_ods.s"],'
        ' "field_mappings": [["wedw_ods.s.id", "wedw_dwd.t.id"]], "note": "ok"}'
    )
    f = parse_fallback_response(text)
    assert f.ok is True
    assert f.target_tables == ["wedw_dwd.t"]
    assert f.source_tables == ["wedw_ods.s"]
    assert f.field_mappings == [["wedw_ods.s.id", "wedw_dwd.t.id"]]


def test_parse_fallback_unable_to_extract() -> None:
    f = parse_fallback_response(
        '{"target_tables": [], "source_tables": [], "field_mappings": [],'
        ' "note": "无法理解该片段"}'
    )
    assert f.ok is False
    assert f.note == "无法理解该片段"


def test_edges_to_json_shape() -> None:
    table_edges, field_edges = _edge_objs()
    data = edges_to_json(table_edges, field_edges)
    assert data["table_edges"][0] == {"source": "wedw_ods.a", "target": "wedw_dwd.t"}
    assert data["field_edges"][0]["source_column"] == "id"
