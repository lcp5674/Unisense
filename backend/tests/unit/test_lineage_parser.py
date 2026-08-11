"""lineage 解析器单测（纯函数，依赖 sqlglot）。"""

from __future__ import annotations

from app.services.lineage.parser import (
    extract_field_lineage,
    extract_table_lineage,
    node_field,
    node_table,
)


def test_insert_select_table_lineage() -> None:
    sql = "INSERT INTO dwd_orders SELECT id, user_id FROM ods_orders"
    edges = extract_table_lineage(sql)
    assert len(edges) == 1
    assert edges[0].source == "ods_orders"
    assert edges[0].target == "dwd_orders"


def test_insert_select_multi_source() -> None:
    sql = "INSERT INTO t SELECT a.x, b.y FROM a JOIN b ON a.id = b.id"
    edges = extract_table_lineage(sql)
    sources = {e.source for e in edges}
    assert sources == {"a", "b"}
    assert all(e.target == "t" for e in edges)


def test_create_table_as_select() -> None:
    sql = "CREATE TABLE t AS SELECT * FROM s"
    edges = extract_table_lineage(sql)
    assert len(edges) == 1
    assert edges[0].source == "s"
    assert edges[0].target == "t"


def test_pure_select_has_no_target() -> None:
    sql = "SELECT * FROM a JOIN b"
    assert extract_table_lineage(sql) == []


def test_field_lineage_maps_columns() -> None:
    sql = "INSERT INTO t SELECT a.id AS x, b.name AS y FROM a JOIN b ON a.id = b.id"
    edges = extract_field_lineage(sql)
    mapping = {(e.target_table, e.target_column): (e.source_table, e.source_column) for e in edges}
    assert mapping.get(("t", "x")) == ("a", "id")
    assert mapping.get(("t", "y")) == ("b", "name")


def test_field_lineage_resolves_alias_to_real_table() -> None:
    """§6.3 M1：字段血缘的列引用用别名时，须解析为规范化真实表名，避免字段图与表图断裂。"""
    sql = "INSERT INTO dwd.t SELECT o.id AS x FROM ods.orders o"
    edges = extract_field_lineage(sql)
    assert len(edges) == 1
    # source_table 必须是真实表名 ods.orders，而非别名 o
    assert edges[0].source_table == "ods.orders"
    assert edges[0].source_column == "id"
    assert edges[0].target_table == "dwd.t"


def test_node_helpers() -> None:
    assert node_table("db.tbl") == "table:db.tbl"
    assert node_field("db.tbl", "col") == "field:db.tbl.col"


def test_field_lineage_cte_deep() -> None:
    """深度列血缘：CTE 链须解析到真实源表列。"""
    sql = (
        "WITH cte AS (SELECT id, name FROM src) "
        "INSERT INTO dest SELECT cte.id AS x, cte.name AS y FROM cte"
    )
    edges = extract_field_lineage(sql)
    mapping = {
        (e.target_table, e.target_column): (e.source_table, e.source_column)
        for e in edges
    }
    assert mapping.get(("dest", "x")) == ("src", "id")
    assert mapping.get(("dest", "y")) == ("src", "name")


def test_field_lineage_nested_cte_chain() -> None:
    """多层 CTE 链式引用须逐层解析到真实源表。"""
    sql = (
        "WITH a AS (SELECT id FROM s1), "
        "b AS (SELECT a.id AS id2 FROM a) "
        "INSERT INTO dest SELECT b.id2 AS x FROM b"
    )
    edges = extract_field_lineage(sql)
    assert len(edges) == 1
    assert (edges[0].source_table, edges[0].source_column) == ("s1", "id")
    assert (edges[0].target_table, edges[0].target_column) == ("dest", "x")


def test_field_lineage_subquery_derived_table() -> None:
    """FROM 子查询（派生表）须解析到内部真实表列。"""
    sql = (
        "INSERT INTO dest SELECT sq.v AS x FROM "
        "(SELECT v FROM t) sq"
    )
    edges = extract_field_lineage(sql)
    assert len(edges) == 1
    assert (edges[0].source_table, edges[0].source_column) == ("t", "v")
    assert (edges[0].target_table, edges[0].target_column) == ("dest", "x")


def test_field_lineage_expression_populates_expression_field() -> None:
    """派生表达式（多源）须记录 expression 并拆出多源列边。"""
    sql = (
        "INSERT INTO dest SELECT a.col + b.col AS sum_col "
        "FROM t a JOIN u b ON a.id = b.id"
    )
    edges = extract_field_lineage(sql)
    # 多源：一条边对应一个源列，但同属一个 target_column
    targets = {e.target_column for e in edges}
    assert "sum_col" in targets
    sources = {(e.source_table, e.source_column) for e in edges}
    assert ("t", "col") in sources
    assert ("u", "col") in sources
    # 至少一条边携带派生表达式文本
    assert any(e.expression and "sum_col" in e.expression for e in edges)


def test_field_lineage_update_degrades_gracefully() -> None:
    """UPDATE 无干净源查询时降级为空（不崩）。"""
    sql = "UPDATE dest SET x = 1 WHERE id = 2"
    assert extract_field_lineage(sql) == []
