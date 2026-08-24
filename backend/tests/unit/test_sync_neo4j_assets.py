"""Neo4j 资产同步脚本（指标节点 + 血缘边）单元测试。

覆盖 ``scripts/sync_neo4j_assets.py``：
- ``parse_metric_edges``：从 ``definition_json`` 解析指标血缘边
  （source_tables 上游表 / dependencies 依赖指标 / source_table 落地表）
- ``build_metric_nodes``：指标节点展示属性构造
- ``filter_metric_edges``：仅保留表端已存在的边（不扩散无属性孤立表节点）
"""

from __future__ import annotations

import sys
from pathlib import Path

# 将 backend/ 加入 sys.path，使 scripts.sync_neo4j_assets 可导入
_BACKEND = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from scripts.sync_neo4j_assets import (  # noqa: E402
    build_metric_nodes,
    filter_metric_edges,
    parse_metric_edges,
)

# ---- parse_metric_edges ----

def test_parse_metric_edges_source_tables() -> None:
    """上游源表 -> 表派生指标边（source=表, target=指标）。"""
    edges = parse_metric_edges(
        "outp_e2e_fee_day",
        {"source_tables": ["ods_his_receipt"]},
    )
    assert edges == [
        ("table:ods_his_receipt", "metric:outp_e2e_fee_day", "DERIVED_FROM")
    ]


def test_parse_metric_edges_dependencies() -> None:
    """derived 指标依赖 -> 指标间依赖边（source=依赖指标, target=本指标）。"""
    edges = parse_metric_edges(
        "outp_e2e_avgfee_day",
        {"dependencies": ["outp_e2e_fee_day", "outp_e2e_visit_day"]},
    )
    assert edges == [
        ("metric:outp_e2e_fee_day", "metric:outp_e2e_avgfee_day", "DERIVED_FROM"),
        ("metric:outp_e2e_visit_day", "metric:outp_e2e_avgfee_day", "DERIVED_FROM"),
    ]


def test_parse_metric_edges_source_table() -> None:
    """落地物化表 -> 指标产出表边（source=指标, target=表）。"""
    edges = parse_metric_edges(
        "outp_e2e_fee_day",
        {"source_table": "dws_metric_outp_e2e_fee_day"},
    )
    assert edges == [
        ("metric:outp_e2e_fee_day", "table:dws_metric_outp_e2e_fee_day", "DERIVED_FROM")
    ]


def test_parse_metric_edges_full() -> None:
    """三类血缘边一次性解析（顺序：上游表、依赖指标、落地表）。"""
    edges = parse_metric_edges(
        "outp_e2e_avgfee_day",
        {
            "source_tables": ["ads_outp_e2e_fee_day"],
            "dependencies": ["outp_e2e_fee_day"],
            "source_table": "ads_outp_e2e_fee_day",
        },
    )
    assert edges == [
        ("table:ads_outp_e2e_fee_day", "metric:outp_e2e_avgfee_day", "DERIVED_FROM"),
        ("metric:outp_e2e_fee_day", "metric:outp_e2e_avgfee_day", "DERIVED_FROM"),
        ("metric:outp_e2e_avgfee_day", "table:ads_outp_e2e_fee_day", "DERIVED_FROM"),
    ]


def test_parse_metric_edges_downstream_tables() -> None:
    """下游使用表 -> 指标产出表边（source=指标, target=表，与落地表同向）。"""
    edges = parse_metric_edges(
        "outp_e2e_fee_day",
        {"downstream_tables": ["ads.outp_fee_report", "dws.outp_fee_copy"]},
    )
    assert edges == [
        ("metric:outp_e2e_fee_day", "table:ads.outp_fee_report", "DERIVED_FROM"),
        ("metric:outp_e2e_fee_day", "table:dws.outp_fee_copy", "DERIVED_FROM"),
    ]


def test_parse_metric_edges_none_or_empty() -> None:
    """definition 为 None / 空字典 / 缺键时返回空列表。"""
    assert parse_metric_edges("outp_e2e_fee_day", None) == []
    assert parse_metric_edges("outp_e2e_fee_day", {}) == []
    assert parse_metric_edges("outp_e2e_fee_day", {"measures": []}) == []


def test_parse_metric_edges_ignores_non_string() -> None:
    """非字符串元素（脏数据）被忽略，不产生边。"""
    edges = parse_metric_edges(
        "outp_e2e_fee_day",
        {"source_tables": ["ods_his_receipt", "", None, 42]},
    )
    assert edges == [("table:ods_his_receipt", "metric:outp_e2e_fee_day", "DERIVED_FROM")]


# ---- build_metric_nodes ----

def test_build_metric_nodes_attributes() -> None:
    """指标节点属性：id=metric:{code}，属性与 MySQL 对齐。"""
    nodes = build_metric_nodes(
        {
            "outp_e2e_fee_day": {
                "type": "metric",
                "label": "outp_e2e_fee_day",
                "pii": False,
                "domain": "outpatient",
                "owner": "1",
            },
            "outp_e2e_piipatient_day": {
                "type": "metric",
                "label": "outp_e2e_piipatient_day",
                "pii": True,
                "domain": "patient",
                "owner": None,
            },
        }
    )
    assert nodes == [
        {
            "id": "metric:outp_e2e_fee_day",
            "type": "metric",
            "label": "outp_e2e_fee_day",
            "pii": False,
            "domain": "outpatient",
            "owner": "1",
        },
        {
            "id": "metric:outp_e2e_piipatient_day",
            "type": "metric",
            "label": "outp_e2e_piipatient_day",
            "pii": True,
            "domain": "patient",
            "owner": None,
        },
    ]


def test_build_metric_nodes_sorted_and_empty() -> None:
    """按 code 排序输出；空输入返回空列表。"""
    assert build_metric_nodes({}) == []
    nodes = build_metric_nodes({"b": {"type": "metric"}, "a": {"type": "metric"}})
    assert [n["id"] for n in nodes] == ["metric:a", "metric:b"]


# ---- filter_metric_edges ----

def test_filter_metric_edges_keeps_existing_table_only() -> None:
    """表端在 existing_tables 的边保留，不在的丢弃；metric-metric 边始终保留。"""
    edges = [
        ("table:ods_his_receipt", "metric:outp_e2e_fee_day", "DERIVED_FROM"),
        ("table:ghost_table", "metric:outp_e2e_fee_day", "DERIVED_FROM"),
        ("metric:outp_e2e_fee_day", "table:dws_metric_outp_e2e_fee_day", "DERIVED_FROM"),
        ("metric:outp_e2e_fee_day", "metric:outp_e2e_avgfee_day", "DERIVED_FROM"),
    ]
    existing = {"table:ods_his_receipt", "table:dws_metric_outp_e2e_fee_day"}
    assert filter_metric_edges(edges, existing) == [
        ("table:ods_his_receipt", "metric:outp_e2e_fee_day", "DERIVED_FROM"),
        ("metric:outp_e2e_fee_day", "table:dws_metric_outp_e2e_fee_day", "DERIVED_FROM"),
        ("metric:outp_e2e_fee_day", "metric:outp_e2e_avgfee_day", "DERIVED_FROM"),
    ]


def test_filter_metric_edges_empty() -> None:
    """空边/空表集合边界。"""
    assert filter_metric_edges([], {"table:a"}) == []
    assert filter_metric_edges([("table:a", "metric:m", "DERIVED_FROM")], set()) == []
    assert filter_metric_edges(
        [("metric:m1", "metric:m2", "DERIVED_FROM")], set()
    ) == [("metric:m1", "metric:m2", "DERIVED_FROM")]
