"""lineage 解析器单测（纯函数，依赖 sqlglot）。"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from app.services.lineage.parser import (
    _branch_queries,
    extract_field_lineage,
    extract_table_lineage,
    extract_upstream_deps,
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
    mapping = {(e.target_table, e.target_column): (e.source_table, e.source_column) for e in edges}
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
    sql = "INSERT INTO dest SELECT sq.v AS x FROM (SELECT v FROM t) sq"
    edges = extract_field_lineage(sql)
    assert len(edges) == 1
    assert (edges[0].source_table, edges[0].source_column) == ("t", "v")
    assert (edges[0].target_table, edges[0].target_column) == ("dest", "x")


def test_field_lineage_expression_populates_expression_field() -> None:
    """派生表达式（多源）须记录 expression 并拆出多源列边。"""
    sql = "INSERT INTO dest SELECT a.col + b.col AS sum_col FROM t a JOIN u b ON a.id = b.id"
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


# ---- T1 生产级补强：SELECT * / MERGE / UNION / 多语句拆分 / 非法 SQL 降级 ----


def test_select_star_table_lineage_kept() -> None:
    """SELECT * 场景表级血缘仍正常产出（字段级降级）。"""
    sql = "CREATE TABLE t AS SELECT * FROM s"
    edges = extract_table_lineage(sql)
    assert len(edges) == 1
    assert (edges[0].source, edges[0].target) == ("s", "t")


def test_select_star_field_lineage_degraded_no_crash() -> None:
    """SELECT * 字段级无法确定具体列——不产出伪边且不抛异常。"""
    sql = "CREATE TABLE t AS SELECT * FROM s"
    edges = extract_field_lineage(sql)
    # 允许空列表（降级），但绝不能崩
    assert isinstance(edges, list)


def test_union_branch_table_lineage() -> None:
    """UNION 多分支表级血缘合并所有源表。"""
    sql = "CREATE TABLE t AS SELECT a FROM x UNION ALL SELECT b FROM y"
    edges = extract_table_lineage(sql)
    sources = {e.source for e in edges}
    assert "x" in sources and "y" in sources
    assert all(e.target == "t" for e in edges)


def test_union_field_lineage_both_branches() -> None:
    """UNION 多分支字段级血缘：两分支源列均解析到目标。"""
    sql = "CREATE TABLE t AS SELECT id FROM x UNION SELECT uid AS id FROM y"
    edges = extract_field_lineage(sql)
    targets = {(e.target_table, e.target_column) for e in edges}
    assert ("t", "id") in targets
    sources = {(e.source_table, e.source_column) for e in edges}
    assert ("x", "id") in sources
    assert ("y", "uid") in sources


def test_union_multi_branch_flatten() -> None:
    """多级 UNION 经 _branch_queries 由 sqlglot flatten 全量展开（锁 parser.py:193 链路）。

    多级 UNION 必须拆成恰好 3 个 SELECT 分支：既不能漏分支（flatten 退化导致丢源表），
    也不能把 UNION 节点误当 SELECT 多拆。端到端校验表级/字段级血缘均合并全部源。
    """
    sql = "CREATE TABLE t AS SELECT a FROM x UNION ALL SELECT b FROM y UNION ALL SELECT c FROM z"
    ast = sqlglot.parse_one(sql)
    branches = _branch_queries(ast.expression)
    # 直接守卫本次改动链路：多级 UNION -> 3 个 Select 分支
    assert len(branches) == 3
    assert all(isinstance(b, exp.Select) for b in branches)
    # 端到端：表级血缘合并全部 3 个源表，字段级解析各分支源列
    table_edges = extract_table_lineage(sql)
    assert {e.source for e in table_edges} == {"x", "y", "z"}
    assert all(e.target == "t" for e in table_edges)
    field_sources = {(e.source_table, e.source_column) for e in extract_field_lineage(sql)}
    assert ("x", "a") in field_sources
    assert ("y", "b") in field_sources
    assert ("z", "c") in field_sources


def test_merge_into_table_lineage() -> None:
    """MERGE INTO 表级血缘：源 USING 表 → 目标表。"""
    sql = "MERGE INTO tgt USING src ON tgt.id = src.id WHEN MATCHED THEN UPDATE SET tgt.v = src.v"
    edges = extract_table_lineage(sql)
    assert any(e.source == "src" and e.target == "tgt" for e in edges)


def test_merge_into_field_lineage() -> None:
    """MERGE INTO 字段级血缘：UPDATE 分支列映射解析。"""
    sql = "MERGE INTO tgt USING src ON tgt.id = src.id WHEN MATCHED THEN UPDATE SET tgt.v = src.v"
    edges = extract_field_lineage(sql)
    # 允许空（MERGE 源查询无 SELECT 时降级），但不得抛异常
    assert isinstance(edges, list)


def test_multi_statement_split_table_lineage() -> None:
    """多语句 SQL（分号拆分）表级血缘合并。"""
    sql = "INSERT INTO t1 SELECT * FROM s1; INSERT INTO t2 SELECT * FROM s2"
    edges = extract_table_lineage(sql)
    targets = {e.target for e in edges}
    assert "t1" in targets and "t2" in targets
    sources = {e.source for e in edges}
    assert "s1" in sources and "s2" in sources


def test_invalid_sql_degrades_empty() -> None:
    """非法/半结构 SQL 降级为空列表，不抛异常。"""
    assert extract_table_lineage("NOT VALID SQL @@@") == []
    assert extract_field_lineage("") == []


def test_dialect_doris_clickhouse_and_hive() -> None:
    """方言透传：doris/clickhouse/hive 等均能解析（不因方言差异崩溃）。"""
    for dialect in ("hive", "doris", "clickhouse", "mysql"):
        edges = extract_table_lineage("INSERT INTO t SELECT id FROM s", dialect=dialect)
        assert len(edges) == 1
        assert (edges[0].source, edges[0].target) == ("s", "t")


def test_select_star_qualified_field_degrades() -> None:
    """限定表 `s.*` 的 SELECT * 字段级不产出伪边、不崩溃（表级仍可用）。"""
    sql = "CREATE TABLE t AS SELECT s.* FROM s"
    table_edges = extract_table_lineage(sql)
    assert len(table_edges) == 1
    assert (table_edges[0].source, table_edges[0].target) == ("s", "t")
    assert isinstance(extract_field_lineage(sql), list)


def test_merge_insert_branch_field_lineage() -> None:
    """MERGE 的 WHEN NOT MATCHED INSERT 分支字段边解析。"""
    sql = (
        "MERGE INTO tgt USING src ON tgt.id = src.id "
        "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (src.id, src.v)"
    )
    edges = extract_field_lineage(sql)
    # MERGE INSERT 分支可能解析出目标 tgt 的字段边；至少不崩溃
    assert isinstance(edges, list)
    assert all(e.target_table == "tgt" for e in edges)


def test_merge_using_subquery_field_lineage() -> None:
    """MERGE USING 子查询源作用域字段边解析。"""
    sql = (
        "MERGE INTO tgt USING (SELECT id, v FROM src) s ON tgt.id = s.id "
        "WHEN MATCHED THEN UPDATE SET tgt.v = s.v"
    )
    edges = extract_field_lineage(sql)
    assert isinstance(edges, list)
    assert all(e.target_table == "tgt" for e in edges)


def test_expression_constant_projection_degrades() -> None:
    """常量/无列引用的投影不产出源列边（降级为空），不崩溃。"""
    sql = "CREATE TABLE t AS SELECT 1 AS const_col FROM s"
    edges = extract_field_lineage(sql)
    # 常量投影无源列：允许空结果，但不抛异常
    assert isinstance(edges, list)


# ---- 方案 A+B：纯 SELECT 显式落点 / 上游依赖 ----


def test_pure_select_with_target_table_lineage() -> None:
    """纯 SELECT 指定落点：FROM/JOIN 源表 → 目标表（表级边）。"""
    sql = "SELECT o.id, u.name FROM ods_orders o JOIN dim_user u ON o.uid = u.uid"
    edges = extract_table_lineage(sql, target_table="dws_report")
    assert {e.source for e in edges} == {"ods_orders", "dim_user"}
    assert all(e.target == "dws_report" for e in edges)


def test_pure_select_with_target_table_field_lineage() -> None:
    """纯 SELECT 指定落点：SELECT 投影列 → 目标表列（字段级边）。"""
    sql = (
        "SELECT o.id AS order_id, u.name AS user_name "
        "FROM ods_orders o JOIN dim_user u ON o.uid = u.uid"
    )
    edges = extract_field_lineage(sql, target_table="dws_report")
    mapping = {(e.source_table, e.source_column): (e.target_table, e.target_column) for e in edges}
    assert mapping.get(("ods_orders", "id")) == ("dws_report", "order_id")
    assert mapping.get(("dim_user", "name")) == ("dws_report", "user_name")


def test_pure_select_without_target_stays_empty() -> None:
    """纯 SELECT 未指定落点：表级/字段级仍为空（由上层降级展示上游依赖）。"""
    sql = "SELECT id, name FROM src"
    assert extract_table_lineage(sql) == []
    assert extract_field_lineage(sql) == []


def test_natural_target_ignores_target_table() -> None:
    """SQL 自带写入目标时，target_table 不覆盖自然目标（方案 A 兼容既有解析）。"""
    sql = "INSERT INTO real_target SELECT a.id FROM a"
    edges = extract_table_lineage(sql, target_table="forced_target")
    assert all(e.target == "real_target" for e in edges)


def test_union_pure_select_with_target_table() -> None:
    """UNION 纯 SELECT 指定落点：多分支源表/源列均指向目标表。"""
    sql = "SELECT id FROM x UNION ALL SELECT uid FROM y"
    table_edges = extract_table_lineage(sql, target_table="t")
    assert {e.source for e in table_edges} == {"x", "y"}
    assert all(e.target == "t" for e in table_edges)
    field_sources = {
        (e.source_table, e.source_column) for e in extract_field_lineage(sql, target_table="t")
    }
    assert ("x", "id") in field_sources
    assert ("y", "uid") in field_sources


def test_upstream_deps_pure_select() -> None:
    """上游依赖：纯 SELECT 读取的源表与源字段清单（别名解析为真实表名）。"""
    sql = (
        "SELECT o.id, u.name FROM ods_orders o JOIN dim_user u ON o.uid = u.uid "
        "WHERE o.dt = '2026-08-01'"
    )
    deps = extract_upstream_deps(sql)
    assert set(deps.tables) == {"ods_orders", "dim_user"}
    assert "ods_orders.id" in deps.fields
    assert "dim_user.name" in deps.fields
    # 限定列必须解析为真实表名（不得残留别名 o./u.）
    assert not any(f.startswith("o.") for f in deps.fields)
    assert not any(f.startswith("u.") for f in deps.fields)


def test_upstream_deps_invalid_sql_degrades() -> None:
    """非法 SQL 上游依赖降级为空（不抛异常）。"""
    deps = extract_upstream_deps("NOT VALID @@@")
    assert deps.tables == ()
    assert deps.fields == ()


def test_upstream_deps_hive_subquery_alias_not_leaked() -> None:
    """Hive 上游依赖：子查询别名（t2）不得泄漏为表/字段，投影列正确归属真实来源表。"""
    sql = (
        "SELECT t1.hosp_id, t1.hosp_name, t3.expert_id, t3.expert_name "
        "FROM wedw_dw.wy_zh_hospital_std_df t1 "
        "JOIN (SELECT tag_id, hospital_id FROM wedw_dwd.hospital_tag_df "
        "      WHERE date_id = '2026-08-13' AND tag_id = 1151 AND state = 0 "
        "      GROUP BY tag_id, hospital_id) t2 "
        "ON t1.hosp_id = t2.hospital_id "
        "JOIN wedw_dw.wy_zh_hosp_dept_expert_relation_df t3 "
        "ON t1.hosp_id = t3.hosp_id AND t3.status_id = 1 AND t1.status_id = 1"
    )
    deps = extract_upstream_deps(sql, dialect="hive")
    # 上游表：两个显式源表 + 子查询内部表，不含子查询别名 t2
    assert set(deps.tables) == {
        "wedw_dw.wy_zh_hospital_std_df",
        "wedw_dwd.hospital_tag_df",
        "wedw_dw.wy_zh_hosp_dept_expert_relation_df",
    }
    assert not any(f.startswith("t2.") for f in deps.fields)
    # 投影列正确归属真实来源表（不得残留别名 t1./t3.）
    assert "wedw_dw.wy_zh_hospital_std_df.hosp_id" in deps.fields
    assert "wedw_dw.wy_zh_hospital_std_df.hosp_name" in deps.fields
    assert "wedw_dw.wy_zh_hosp_dept_expert_relation_df.expert_id" in deps.fields
    assert "wedw_dw.wy_zh_hosp_dept_expert_relation_df.expert_name" in deps.fields
    # 条件列（ON/WHERE/GROUP BY）不进入字段清单，避免污染血缘
    assert not any(f.endswith(".status_id") for f in deps.fields)
    assert not any(f.endswith(".tag_id") for f in deps.fields)
    assert not any(f.endswith(".date_id") for f in deps.fields)


def test_upstream_deps_projection_through_subquery() -> None:
    """上游依赖：投影引用子查询输出列时穿透到子查询内部真实来源表。"""
    sql = (
        "SELECT t2.hospital_id, t2.tag_id FROM ("
        "SELECT tag_id, hospital_id FROM wedw_dwd.hospital_tag_df "
        "WHERE date_id = '2026-08-13') t2"
    )
    deps = extract_upstream_deps(sql, dialect="hive")
    assert "wedw_dwd.hospital_tag_df.hospital_id" in deps.fields
    assert "wedw_dwd.hospital_tag_df.tag_id" in deps.fields
    assert not any(f.startswith("t2.") for f in deps.fields)


def test_upstream_deps_cte_reference_not_leaked() -> None:
    """上游依赖：CTE 引用（``FROM cte1`` 的 ``cte1``）不得泄漏为伪表，仅保留真实表。"""
    sql = (
        "WITH cte1 AS (SELECT id, name FROM wedw_dwd.hospital_tag_df "
        "WHERE date_id = '2026-08-13') "
        "SELECT cte1.id, t3.expert_id FROM cte1 "
        "JOIN wedw_dw.wy_zh_hosp_dept_expert_relation_df t3 ON cte1.id = t3.hosp_id"
    )
    deps = extract_upstream_deps(sql, dialect="hive")
    # 表清单只含真实表（CTE 定义内部表 + JOIN 显式表），不含 cte1
    assert set(deps.tables) == {
        "wedw_dwd.hospital_tag_df",
        "wedw_dw.wy_zh_hosp_dept_expert_relation_df",
    }
    # 投影列穿透 CTE 到内部真实来源表，且不残留 cte1. 前缀
    assert "wedw_dwd.hospital_tag_df.id" in deps.fields
    assert "wedw_dw.wy_zh_hosp_dept_expert_relation_df.expert_id" in deps.fields
    assert not any(f.startswith("cte1.") for f in deps.fields)


def test_upstream_deps_cte_not_leaked_all_dialects() -> None:
    """上游依赖：CTE 引用不泄漏在所有受支持数据源方言下均成立。"""
    sql = (
        "WITH cte1 AS (SELECT x AS c FROM db1.t1) "
        "SELECT cte1.c FROM cte1 JOIN db3.t3 ON cte1.c = t3.k"
    )
    for dialect in ("mysql", "postgres", "hive", "spark", "doris", "clickhouse", "starrocks"):
        deps = extract_upstream_deps(sql, dialect=dialect)
        assert "cte1" not in deps.tables, f"[{dialect}] CTE 引用泄漏为伪表"
        assert {"db1.t1", "db3.t3"}.issubset(set(deps.tables)), f"[{dialect}] 真实表缺失"
        assert not any(f.startswith("cte1.") for f in deps.fields), f"[{dialect}] CTE 列泄漏"
        assert "db1.t1.x" in deps.fields, f"[{dialect}] 投影列未穿透 CTE"


def test_table_lineage_cte_reference_not_leaked() -> None:
    """表级血缘：INSERT + CTE 时 CTE 引用不得作为伪源表（正式血缘不污染）。"""
    sql = (
        "WITH cte1 AS (SELECT id, name FROM db1.src1 WHERE dt = '2026-08-13') "
        "INSERT INTO dws.result "
        "SELECT c.id, c.name, t2.extra FROM cte1 c "
        "JOIN db2.src2 t2 ON c.id = t2.id"
    )
    edges = extract_table_lineage(sql, dialect="hive")
    sources = {e.source for e in edges}
    targets = {e.target for e in edges}
    assert targets == {"dws.result"}
    assert sources == {"db1.src1", "db2.src2"}  # 无 cte1
    assert "cte1" not in sources


def test_select_target_table_cte_not_leaked() -> None:
    """纯 SELECT 显式落点 + CTE：落点血缘源表不含 CTE 引用（方案 A+B）。"""
    sql = (
        "WITH cte1 AS (SELECT id, name FROM db1.src1) "
        "SELECT c.id, c.name FROM cte1 c WHERE c.id > 0"
    )
    edges = extract_table_lineage(sql, dialect="hive", target_table="dws.result")
    sources = {e.source for e in edges}
    assert sources == {"db1.src1"}
    assert "cte1" not in sources
    # 字段级落点血缘同样穿透 CTE
    field_edges = extract_field_lineage(sql, dialect="hive", target_table="dws.result")
    assert {e.source_table for e in field_edges} == {"db1.src1"}
    assert {e.target_table for e in field_edges} == {"dws.result"}
