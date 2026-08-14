"""DP 血缘导入脚本解析逻辑单元测试。

覆盖 ``scripts/import_dp_lineage.py`` 的节点血缘提取：
- SQL 节点（nodeType=2）：INSERT/CREATE...SELECT 的读入源表 -> 写入目标表
- 同步节点（nodeType=6/8/10）：syncSourceInfo 源表 -> syncTargetInfo 目标表
- parentIds DAG 边：父节点输出表 -> 子节点输入表
- 自环边（source == target）一律跳过
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# 将 backend/ 加入 sys.path，使 scripts.import_dp_lineage 可导入
_BACKEND = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from scripts.import_dp_lineage import (  # noqa: E402
    edges_from_nodes,
    parse_task_definition,
    sync_source_tables,
    sync_target_table,
)


def _node(
    node_type: int,
    command: str = "",
    *,
    node_id: str = "",
    parent_ids: list[str] | None = None,
    sync_source: dict | None = None,
    sync_target: dict | None = None,
) -> dict:
    """构造一个任务节点。"""
    node: dict = {
        "nodeId": node_id or f"n{node_type}",
        "nodeType": node_type,
        "command": command,
    }
    if parent_ids:
        node["parentIds"] = parent_ids
    if sync_source or sync_target:
        node["params"] = {
            "syncSourceInfo": sync_source or {},
            "syncTargetInfo": sync_target or {},
        }
    return node


# ---- parse_task_definition ----

def test_parse_task_definition_valid_json() -> None:
    raw = json.dumps({"nodes": [{"nodeId": "a", "nodeType": 2}]})
    assert parse_task_definition(raw) == [{"nodeId": "a", "nodeType": 2}]


def test_parse_task_definition_bare_list() -> None:
    raw = json.dumps([{"nodeId": "a", "nodeType": 2}])
    assert parse_task_definition(raw) == [{"nodeId": "a", "nodeType": 2}]


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "not json", "{}", '{"foo": 1}'],
)
def test_parse_task_definition_invalid(raw: str) -> None:
    assert parse_task_definition(raw) == []


# ---- sync 表提取 ----

def test_sync_source_tables_from_db_info_list() -> None:
    info = {
        "dbInfoBOList": [
            {
                "databaseName": "trcd_dcare_sign",
                "tableName": ["patient_sign_protocol", "patient_info"],
            },
            {"databaseName": "YHHIS", "tableName": "B_ITEM"},
        ]
    }
    assert sync_source_tables(info) == [
        "trcd_dcare_sign.patient_sign_protocol",
        "trcd_dcare_sign.patient_info",
        "YHHIS.B_ITEM",
    ]


def test_sync_source_tables_with_qualified_name() -> None:
    info = {"dbInfoBOList": [{"databaseName": "db", "tableName": ["a.b"]}]}
    assert sync_source_tables(info) == ["a.b"]


def test_sync_target_table_qualified() -> None:
    info = {"databaseName": "wedw_dwd", "tableName": "wedw_ods.xxx_ful_d"}
    assert sync_target_table(info) == ["wedw_ods.xxx_ful_d"]


def test_sync_target_table_unqualified() -> None:
    info = {"databaseName": "wedw_dwd", "tableName": "xxx_ful_d"}
    assert sync_target_table(info) == ["wedw_dwd.xxx_ful_d"]


def test_sync_tables_empty() -> None:
    assert sync_source_tables(None) == []
    assert sync_source_tables({}) == []
    assert sync_target_table({}) == []


# ---- edges_from_nodes：三类边 + 自环过滤 ----

def test_edges_from_sql_node() -> None:
    nodes = [
        _node(
            2,
            "INSERT OVERWRITE TABLE wedw_dwd.target_df "
            "SELECT a, b FROM wedw_dwd.src_df JOIN wedw_dwd.dim_df d ON d.id = src_df.id",
            node_id="n1",
        )
    ]
    edges = edges_from_nodes(nodes)
    assert ("wedw_dwd.src_df", "wedw_dwd.target_df") in edges
    assert ("wedw_dwd.dim_df", "wedw_dwd.target_df") in edges


def test_edges_from_sync_node() -> None:
    nodes = [
        _node(
            8,
            node_id="n1",
            sync_source={
                "dbInfoBOList": [{"databaseName": "YHHIS", "tableName": ["B_ITEM"]}]
            },
            sync_target={"databaseName": "wedw_dwd", "tableName": "wedw_ods.YHHIS_B_ITEM_ful_d"},
        )
    ]
    edges = edges_from_nodes(nodes)
    assert edges == {("YHHIS.B_ITEM", "wedw_ods.YHHIS_B_ITEM_ful_d")}


def test_edges_from_dag_parent_ids() -> None:
    # 父节点输出 ods 表，子节点读取它写入 dw 表 -> 通过 parentIds 连出 DAG 边
    nodes = [
        _node(
            8,
            node_id="parent",
            sync_source={"dbInfoBOList": [{"databaseName": "YHHIS", "tableName": ["B_ITEM"]}]},
            sync_target={"databaseName": "wedw_dwd", "tableName": "wedw_ods.YHHIS_B_ITEM_ful_d"},
        ),
        _node(
            2,
            "INSERT OVERWRITE TABLE wedw_dw.target_df "
            "SELECT * FROM wedw_ods.YHHIS_B_ITEM_ful_d",
            node_id="child",
            parent_ids=["parent"],
        ),
    ]
    edges = edges_from_nodes(nodes)
    # 子节点自身 SQL 边
    assert ("wedw_ods.YHHIS_B_ITEM_ful_d", "wedw_dw.target_df") in edges
    # 父输出 -> 子输入 DAG 边
    assert ("wedw_ods.YHHIS_B_ITEM_ful_d", "wedw_ods.YHHIS_B_ITEM_ful_d") not in edges


def test_edges_skip_self_loop_sql() -> None:
    # INSERT OVERWRITE 同表自更新（源=目标）不产生边
    nodes = [
        _node(
            2,
            "INSERT OVERWRITE TABLE wedw_tmp.tmp_df "
            "SELECT a, b FROM wedw_tmp.tmp_df WHERE dt = '2024'",
            node_id="n1",
        )
    ]
    assert edges_from_nodes(nodes) == set()


def test_edges_skip_self_loop_dag() -> None:
    # 父输出表 == 子输入表：任务内表流转，不应产生 X -> X 伪边
    nodes = [
        _node(2, "INSERT OVERWRITE TABLE db.x SELECT * FROM db.a", node_id="p"),
        _node(2, "INSERT OVERWRITE TABLE db.y SELECT * FROM db.x", node_id="c", parent_ids=["p"]),
    ]
    edges = edges_from_nodes(nodes)
    assert ("db.x", "db.x") not in edges
    assert ("db.a", "db.x") in edges
    assert ("db.x", "db.y") in edges


def test_edges_skip_drop_and_script_nodes() -> None:
    nodes = [
        _node(4, "DROP TABLE IF EXISTS db.old_df", node_id="n1"),  # drop
        _node(3, "echo 'monitor'", node_id="n2"),  # script
    ]
    assert edges_from_nodes(nodes) == set()
