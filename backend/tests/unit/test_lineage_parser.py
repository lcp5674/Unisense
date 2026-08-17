"""lineage 解析器单测（纯函数，依赖 sqlglot）。"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from app.services.lineage.parser import (
    _branch_queries,
    expand_variables,
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


# ---- 生产场景语法覆盖补强（7 方言核查） ----


def test_insert_column_list_field_mapping() -> None:
    """INSERT 带显式列清单：字段级目标列按位置取列清单（x→a, y→b），非投影别名。"""
    sql = "INSERT INTO tgt (a, b) SELECT x, y FROM src"
    edges = extract_field_lineage(sql, dialect="mysql")
    mapping = {(e.source_table, e.source_column): (e.target_table, e.target_column) for e in edges}
    assert mapping.get(("src", "x")) == ("tgt", "a")
    assert mapping.get(("src", "y")) == ("tgt", "b")
    assert not any(e.target_column in ("x", "y") for e in edges)


def test_insert_column_list_field_mapping_all_dialects() -> None:
    """INSERT 列清单字段映射在全部受支持方言下一致（列清单按位置覆盖投影别名）。"""
    sql = "INSERT INTO tgt (a, b) SELECT x, y FROM src"
    for dialect in ("mysql", "postgres", "hive", "spark", "doris", "clickhouse", "starrocks"):
        mapping = {
            (e.source_table, e.source_column): (e.target_table, e.target_column)
            for e in extract_field_lineage(sql, dialect=dialect)
        }
        assert mapping.get(("src", "x")) == ("tgt", "a"), f"[{dialect}] x 映射错位"
        assert mapping.get(("src", "y")) == ("tgt", "b"), f"[{dialect}] y 映射错位"
        got_cols = [e.target_column for e in extract_field_lineage(sql, dialect=dialect)]
        assert not any(c in ("x", "y") for c in got_cols), f"[{dialect}] 目标列未取列清单"


def test_insert_column_list_union_mapping() -> None:
    """INSERT 列清单 + UNION：两分支投影均按列清单映射到目标列。"""
    sql = "INSERT INTO tgt (a, b) SELECT x, y FROM s1 UNION ALL SELECT u, v FROM s2"
    edges = extract_field_lineage(sql, dialect="mysql")
    mapping = {(e.source_table, e.source_column): (e.target_table, e.target_column) for e in edges}
    assert mapping.get(("s1", "x")) == ("tgt", "a")
    assert mapping.get(("s1", "y")) == ("tgt", "b")
    assert mapping.get(("s2", "u")) == ("tgt", "a")
    assert mapping.get(("s2", "v")) == ("tgt", "b")


def test_replace_into_treated_as_insert() -> None:
    """MySQL REPLACE INTO ... SELECT：sqlglot 不支持 REPLACE，预处理为 INSERT 后血缘等价。"""
    sql = "REPLACE INTO tgt SELECT id, name FROM src"
    table_edges = extract_table_lineage(sql, dialect="mysql")
    assert len(table_edges) == 1
    assert (table_edges[0].source, table_edges[0].target) == ("src", "tgt")
    field_sources = {
        (e.source_table, e.source_column): (e.target_table, e.target_column)
        for e in extract_field_lineage(sql, dialect="mysql")
    }
    assert field_sources.get(("src", "id")) == ("tgt", "id")
    assert field_sources.get(("src", "name")) == ("tgt", "name")


def test_doris_insert_with_label() -> None:
    """Doris/StarRocks INSERT ... WITH LABEL 'xxx'：剥离 LABEL 片段后正常解析血缘。"""
    sql = "INSERT INTO tgt WITH LABEL 'lbl1' SELECT id FROM src"
    table_edges = extract_table_lineage(sql, dialect="doris")
    assert len(table_edges) == 1
    assert (table_edges[0].source, table_edges[0].target) == ("src", "tgt")
    field_edges = extract_field_lineage(sql, dialect="doris")
    assert {e.source_table for e in field_edges} == {"src"}
    assert {e.target_table for e in field_edges} == {"tgt"}
    # StarRocks 同样支持
    assert len(extract_table_lineage(sql, dialect="starrocks")) == 1


def test_insert_overwrite_directory_no_dirty_edge() -> None:
    """Hive INSERT OVERWRITE DIRECTORY：目标非表，表级/字段级均不产脏边（无空目标列）。"""
    sql = "INSERT OVERWRITE DIRECTORY '/tmp/out' SELECT id FROM ods.src"
    assert extract_table_lineage(sql, dialect="hive") == []
    field_edges = extract_field_lineage(sql, dialect="hive")
    assert field_edges == []
    # 即使显式传入 target_table，写入语句也不回退为纯查询落点
    assert extract_table_lineage(sql, dialect="hive", target_table="forced") == []
    assert extract_field_lineage(sql, dialect="hive", target_table="forced") == []


def test_create_view_table_lineage() -> None:
    """CREATE VIEW AS SELECT：视图作为逻辑表产血缘（TD§12.2 明确要求）。"""
    sql = "CREATE VIEW v_tgt AS SELECT id FROM src"
    table_edges = extract_table_lineage(sql, dialect="mysql")
    assert len(table_edges) == 1
    assert (table_edges[0].source, table_edges[0].target) == ("src", "v_tgt")
    field_edges = extract_field_lineage(sql, dialect="mysql")
    assert {e.source_table for e in field_edges} == {"src"}
    assert {e.target_table for e in field_edges} == {"v_tgt"}


def test_create_view_variants_table_lineage() -> None:
    """视图变体（OR REPLACE / MATERIALIZED / TEMPORARY）均归一为 VIEW 目标产血缘。"""
    cases = [
        ("CREATE OR REPLACE VIEW v AS SELECT id FROM src", "spark", "v"),
        ("CREATE MATERIALIZED VIEW mv AS SELECT id FROM src", "clickhouse", "mv"),
        ("CREATE TEMPORARY VIEW tv AS SELECT id FROM src", "spark", "tv"),
        ("CREATE VIEW dws.v AS SELECT id, name FROM ods.src", "mysql", "dws.v"),
    ]
    for sql, dialect, expect_target in cases:
        edges = extract_table_lineage(sql, dialect=dialect)
        assert len(edges) == 1, f"[{dialect}] {sql} 未产表级边"
        expect_source = "src" if dialect != "mysql" else "ods.src"
        assert (edges[0].source, edges[0].target) == (expect_source, expect_target)
        # 字段级同样解析
        field_edges = extract_field_lineage(sql, dialect=dialect)
        assert {e.source_table for e in field_edges} == {edges[0].source}
        assert {e.target_table for e in field_edges} == {edges[0].target}


def test_multi_table_update_set_target() -> None:
    """多表 UPDATE：目标取 SET 中被更新列所属表（而非首表）。"""
    sql = "UPDATE t1 JOIN t2 ON t1.id = t2.id SET t2.v = t1.v"
    edges = extract_table_lineage(sql, dialect="mysql")
    assert len(edges) == 1
    assert (edges[0].source, edges[0].target) == ("t1", "t2")
    # SET 首表（常见写法）仍正确
    sql2 = "UPDATE tgt JOIN src ON tgt.id = src.id SET tgt.v = src.v"
    edges2 = extract_table_lineage(sql2, dialect="mysql")
    assert (edges2[0].source, edges2[0].target) == ("src", "tgt")


def test_upstream_deps_excludes_write_target() -> None:
    """上游依赖只服务纯 SELECT：写入语句（含 INSERT VALUES）不把目标表误收为来源。"""
    assert extract_upstream_deps("INSERT INTO tgt SELECT id FROM src").tables == ()
    assert extract_upstream_deps("INSERT INTO tgt (a, b) VALUES (1, 2)").tables == ()
    assert extract_upstream_deps("UPDATE tgt SET x = 1 WHERE id = 2").tables == ()
    # 纯 SELECT 仍正常返回
    deps = extract_upstream_deps("SELECT id FROM src")
    assert deps.tables == ("src",)


def test_merge_insert_values_no_column_list_field_lineage() -> None:
    """MERGE 无列清单 INSERT VALUES（this=None）：值中裸列引用列名近似目标列。"""
    sql = """
    MERGE INTO dws.target t
    USING ods.src s ON t.id = s.id
    WHEN NOT MATCHED THEN INSERT VALUES (s.id, s.name)
    """
    edges = extract_field_lineage(sql, dialect="hive")
    mapped = {(e.source_table, e.source_column, e.target_table, e.target_column) for e in edges}
    assert ("ods.src", "id", "dws.target", "id") in mapped
    assert ("ods.src", "name", "dws.target", "name") in mapped
    assert len(edges) == 2


def test_select_into_table_and_field_lineage() -> None:
    """PG SELECT ... INTO newtbl：等价 CTAS，产表级 + 字段级血缘。"""
    sql = "SELECT id, name INTO dws.newtbl FROM ods.src"
    table_edges = extract_table_lineage(sql, dialect="postgres")
    assert len(table_edges) == 1
    assert (table_edges[0].source, table_edges[0].target) == ("ods.src", "dws.newtbl")
    field_edges = extract_field_lineage(sql, dialect="postgres")
    mapped = {
        (e.source_table, e.source_column, e.target_table, e.target_column) for e in field_edges
    }
    assert ("ods.src", "id", "dws.newtbl", "id") in mapped
    assert ("ods.src", "name", "dws.newtbl", "name") in mapped
    assert len(field_edges) == 2


def test_mysql_select_into_var_not_target() -> None:
    """MySQL SELECT ... INTO @var：变量赋值（Into.this 为 Parameter），不构成血缘目标。"""
    sql = "SELECT id INTO @v FROM ods.src"
    assert extract_table_lineage(sql, dialect="mysql") == []
    assert extract_field_lineage(sql, dialect="mysql") == []


def test_update_from_field_lineage() -> None:
    """PG UPDATE ... FROM：SET 列 ← FROM 来源表列，产字段级血缘。"""
    sql = "UPDATE dws.tgt SET v = s.v, name = s.name FROM ods.src s WHERE tgt.id = s.id"
    edges = extract_field_lineage(sql, dialect="postgres")
    mapped = {(e.source_table, e.source_column, e.target_table, e.target_column) for e in edges}
    assert ("ods.src", "v", "dws.tgt", "v") in mapped
    assert ("ods.src", "name", "dws.tgt", "name") in mapped
    assert len(edges) == 2


def test_update_join_field_lineage() -> None:
    """MySQL UPDATE tgt JOIN src：JOIN 表为来源，SET 目标列产字段血缘。"""
    sql = "UPDATE dws.tgt t JOIN ods.src s ON t.id = s.id SET t.v = s.v"
    edges = extract_field_lineage(sql, dialect="mysql")
    assert len(edges) == 1
    e = edges[0]
    assert (e.source_table, e.source_column, e.target_table, e.target_column) == (
        "ods.src",
        "v",
        "dws.tgt",
        "v",
    )


def test_update_set_subquery_field_lineage() -> None:
    """UPDATE SET 值为子查询：子查询投影列 → 目标列。"""
    sql = "UPDATE ods.tgt t SET t.v = (SELECT MAX(v) FROM ods.src WHERE src.id = t.id)"
    edges = extract_field_lineage(sql, dialect="mysql")
    assert len(edges) == 1
    e = edges[0]
    assert (e.source_table, e.source_column, e.target_table, e.target_column) == (
        "ods.src",
        "v",
        "ods.tgt",
        "v",
    )


def test_update_self_update_no_cross_field_lineage() -> None:
    """UPDATE 自更新（无来源表）：不产跨表字段边；静态值同样无来源。"""
    sql1 = "UPDATE dws.tgt SET v = v * 1.1 WHERE id > 100"
    assert extract_field_lineage(sql1, dialect="mysql") == []
    sql2 = "UPDATE dws.tgt SET status = 'done' WHERE id = 1"
    assert extract_field_lineage(sql2, dialect="hive") == []


def test_select_into_dialects_parametrized() -> None:
    """SELECT INTO 在支持该语法的方言（postgres/tsql）均产血缘。"""
    cases = [
        ("postgres", "SELECT id INTO dws.newtbl FROM ods.src", "ods.src", "dws.newtbl"),
        ("tsql", "SELECT id INTO dws.newtbl FROM ods.src", "ods.src", "dws.newtbl"),
    ]
    for dialect, sql, expect_src, expect_tgt in cases:
        edges = extract_table_lineage(sql, dialect=dialect)
        assert len(edges) == 1, f"[{dialect}] 未产表级边"
        assert (edges[0].source, edges[0].target) == (expect_src, expect_tgt)
        field_edges = extract_field_lineage(sql, dialect=dialect)
        assert {e.source_table for e in field_edges} == {expect_src}
        assert {e.target_table for e in field_edges} == {expect_tgt}


def test_ctas_with_column_list_field_mapping() -> None:
    """CTAS 带列清单：字段映射按位置取列清单（x→a/y→b），与 INSERT 列清单一致。"""
    sql = "CREATE TABLE dws.tgt (a INT, b VARCHAR(20)) AS SELECT x, y FROM ods.src"
    edges = extract_field_lineage(sql, dialect="mysql")
    mapped = {(e.source_column, e.target_column) for e in edges}
    assert ("x", "a") in mapped
    assert ("y", "b") in mapped
    assert len(edges) == 2
    # 无列清单时仍用投影别名
    sql2 = "CREATE TABLE dws.tgt AS SELECT x AS a, y AS b FROM ods.src"
    edges2 = extract_field_lineage(sql2, dialect="mysql")
    mapped2 = {(e.source_column, e.target_column) for e in edges2}
    assert ("x", "a") in mapped2
    assert ("y", "b") in mapped2


def test_update_with_cte_not_leaked_as_table() -> None:
    """PG ``WITH s AS (...) UPDATE ... FROM s``：CTE 引用不被误判为伪源表。"""
    sql = (
        "WITH s AS (SELECT id, v FROM ods_src) "
        "UPDATE dws.tgt SET v = s.v FROM s WHERE dws.tgt.id = s.id"
    )
    table_edges = extract_table_lineage(sql, dialect="postgres")
    assert [(e.source, e.target) for e in table_edges] == [("ods_src", "dws.tgt")]
    field_edges = extract_field_lineage(sql, dialect="postgres")
    assert [(e.source_table, e.source_column, e.target_column) for e in field_edges] == [
        ("ods_src", "v", "v")
    ]
    assert all(e.source_table != "s" for e in field_edges)


def test_create_table_like_no_lineage() -> None:
    """CREATE TABLE ... LIKE ... 是结构复制（无数据流转），不产血缘边。"""
    sql = "CREATE TABLE dws.tgt LIKE ods.src"
    assert extract_table_lineage(sql, dialect="mysql") == []
    assert extract_field_lineage(sql, dialect="mysql") == []


def test_upstream_deps_ignores_non_select_statements() -> None:
    """非血缘读取语句（DDL/DML/COPY/USE 等）不产上游依赖，避免目标表被误收为来源。"""
    cases = [
        "ALTER TABLE dws.tgt ADD COLUMN c INT",
        "DROP TABLE IF EXISTS dws.tgt",
        "TRUNCATE TABLE dws.tgt",
        "DELETE FROM dws.tgt WHERE id = 1",
        "COPY dws.tgt (id, v) FROM '/tmp/data.csv' WITH (FORMAT csv)",
        "USE db1",
        "CREATE INDEX idx ON ods.a (id)",
    ]
    for sql in cases:
        ud = extract_upstream_deps(sql, dialect="mysql")
        assert ud.tables == (), f"[{sql}] tables 应为空: {ud.tables}"
        assert ud.fields == (), f"[{sql}] fields 应为空: {ud.fields}"
    # 对照组：纯 SELECT 仍正常收集
    ud = extract_upstream_deps("SELECT id FROM ods.a", dialect="mysql")
    assert ud.tables == ("ods.a",)
    assert ud.fields == ("ods.a.id",)


def test_doris_ctas_physical_attrs_stripped() -> None:
    """Doris/StarRocks CTAS 带 DISTRIBUTED BY/PROPERTIES/ENGINE：剥离物理属性后正常产血缘。

    sqlglot 25.x 对 ``CREATE TABLE t DISTRIBUTED BY ... AS SELECT`` 整体降级为
    Command 致血缘全丢；这些子句仅描述物理布局，剥离后血缘语义不变。
    """
    sql = (
        "CREATE TABLE dws.t DISTRIBUTED BY HASH(id) BUCKETS 10 "
        'PROPERTIES("replication_num"="1") AS SELECT id, v FROM ods.s'
    )
    table_edges = extract_table_lineage(sql, dialect="doris")
    assert [(e.source, e.target) for e in table_edges] == [("ods.s", "dws.t")]
    field_edges = extract_field_lineage(sql, dialect="doris")
    assert [(e.source_table, e.source_column, e.target_column) for e in field_edges] == [
        ("ods.s", "id", "id"),
        ("ods.s", "v", "v"),
    ]
    # StarRocks 同构
    sql_sr = "CREATE TABLE dws.t ENGINE=OLAP DISTRIBUTED BY HASH(id) AS SELECT id FROM ods.s"
    assert [(e.source, e.target) for e in extract_table_lineage(sql_sr, dialect="starrocks")] == [
        ("ods.s", "dws.t")
    ]


def test_cte_name_shadowing_real_table() -> None:
    """CTE 名与真实表同名：带 schema 前缀的引用（ods.cte1）是真实表，不被 CTE 遮蔽排除。"""
    sql = (
        "WITH cte1 AS (SELECT id FROM ods.a) "
        "INSERT INTO dws.t SELECT cte1.id, c.v FROM cte1 JOIN ods.cte1 c ON cte1.id = c.id"
    )
    table_edges = extract_table_lineage(sql, dialect="hive")
    sources = {e.source for e in table_edges}
    assert "ods.cte1" in sources, "带 schema 前缀的同名真实表应保留为来源"
    assert "ods.a" in sources
    assert "cte1" not in sources, "裸 CTE 引用不应成为伪表"


def test_cte_chain_aggregate_column_resolved() -> None:
    """CTE 链式 + 聚合 + JOIN：未限定列避开不含该列的 CTE，解析到真实来源表。

    ``c2.v = MAX(v)`` 的 v 来自 JOIN 的 ods.b（c1 仅输出 id），应解析到 ods.b.v
    而非因选中不含 v 的 CTE c1 而落空。
    """
    sql = (
        "WITH c1 AS (SELECT id FROM ods.a), "
        "c2 AS (SELECT id, MAX(v) AS v FROM c1 JOIN ods.b USING(id) GROUP BY id) "
        "INSERT INTO dws.t SELECT id, v FROM c2"
    )
    field_edges = extract_field_lineage(sql, dialect="postgres")
    assert [
        (e.source_table, e.source_column, e.target_column)
        for e in field_edges
        if e.target_column == "v"
    ] == [("ods.b", "v", "v")], "MAX(v) 的 v 应穿透 CTE 解析到 ods.b.v"


def test_except_field_lineage() -> None:
    """INSERT 源 EXCEPT：字段级血缘覆盖两个分支（此前 _branch_queries 只认 Union）。"""
    sql = "INSERT INTO dws.t SELECT id, v FROM ods.a EXCEPT SELECT id, v FROM ods.b"
    table_edges = extract_table_lineage(sql, dialect="postgres")
    assert {e.source for e in table_edges} == {"ods.a", "ods.b"}
    field_edges = extract_field_lineage(sql, dialect="postgres")
    assert [(e.source_table, e.source_column, e.target_column) for e in field_edges] == [
        ("ods.a", "id", "id"),
        ("ods.a", "v", "v"),
        ("ods.b", "id", "id"),
        ("ods.b", "v", "v"),
    ]


def test_intersect_field_lineage() -> None:
    """INSERT 源 INTERSECT：字段级血缘覆盖两个分支。"""
    sql = "INSERT INTO dws.t SELECT id FROM ods.a INTERSECT SELECT id FROM ods.b"
    field_edges = extract_field_lineage(sql, dialect="spark")
    assert {("ods.a", "id", "id"), ("ods.b", "id", "id")} == {
        (e.source_table, e.source_column, e.target_column) for e in field_edges
    }


def test_upstream_deps_except() -> None:
    """纯 SELECT EXCEPT（无落点）：上游依赖收集左右两分支的表与字段。"""
    ud = extract_upstream_deps("SELECT id FROM ods.a EXCEPT SELECT id FROM ods.b", dialect="hive")
    assert ud.tables == ("ods.a", "ods.b")
    assert ud.fields == ("ods.a.id", "ods.b.id")


def test_except_with_target_table() -> None:
    """纯 SELECT EXCEPT + 显式落点（方案 A+B）：源表 → 目标表 表级/字段级边。"""
    sql = "SELECT id, v FROM ods.a EXCEPT SELECT id, v FROM ods.b"
    table_edges = extract_table_lineage(sql, dialect="postgres", target_table="dws.t")
    assert {e.source for e in table_edges} == {"ods.a", "ods.b"}
    assert all(e.target == "dws.t" for e in table_edges)
    field_edges = extract_field_lineage(sql, dialect="postgres", target_table="dws.t")
    assert {("ods.a", "id", "id"), ("ods.b", "v", "v")} <= {
        (e.source_table, e.source_column, e.target_column) for e in field_edges
    }


def test_multi_table_update_cross_set_field_bidirectional() -> None:
    """多表 UPDATE 跨 SET 字段级双向血缘：每项 SET 的目标表独立判定为 LHS 所属表。

    ``UPDATE t1 JOIN t2 SET t1.v=t2.v, t2.w=t1.x`` 中 t1 与 t2 均被 SET 更新，字段级
    应产 ``t2.v→t1.v`` 与 ``t1.x→t2.w`` 双向（旧实现全局取首表 t1，把 t2.w=t1.x 的
    t1 当自引用跳过致 t1→t2 方向丢失）。表级受血缘图 DAG 约束取主目标（首个被 SET
    更新表 t1）单方向 ``t2→t1``——互刷方向不入表级图谱（避免循环依赖 409）。
    """
    sql = "UPDATE t1 JOIN t2 ON t1.id = t2.id SET t1.v = t2.v, t2.w = t1.x"
    table_edges = extract_table_lineage(sql, dialect="mysql")
    assert {(e.source, e.target) for e in table_edges} == {("t2", "t1")}
    field_edges = extract_field_lineage(sql, dialect="mysql")
    mapped = {
        (e.source_table, e.source_column, e.target_table, e.target_column) for e in field_edges
    }
    assert mapped == {("t2", "v", "t1", "v"), ("t1", "x", "t2", "w")}


def test_multi_table_update_cross_set_field_three() -> None:
    """三表 UPDATE 跨 SET：字段级各被更新列归属正确（t1.v←t2.v、t3.w←t1.x）。"""
    sql = "UPDATE t1 JOIN t2 ON t1.id = t2.id JOIN t3 ON t1.id = t3.id SET t1.v = t2.v, t3.w = t1.x"
    table_edges = extract_table_lineage(sql, dialect="mysql")
    # 表级主目标 t1（首个被 SET 更新表）：t2/t3 均指向 t1，无自环
    targets = {e.target for e in table_edges}
    assert targets == {"t1"}
    assert ("t2", "t1") in {(e.source, e.target) for e in table_edges}
    assert ("t3", "t1") in {(e.source, e.target) for e in table_edges}
    assert all(e.source != e.target for e in table_edges)
    field_edges = extract_field_lineage(sql, dialect="mysql")
    mapped = {
        (e.source_table, e.source_column, e.target_table, e.target_column) for e in field_edges
    }
    assert ("t2", "v", "t1", "v") in mapped
    assert ("t1", "x", "t3", "w") in mapped


# ---- 第七轮：UNNEST 数组展开 / 无 FROM 常量 / MAX_DEPTH 边界 ----


def test_unnest_field_lineage_resolves_to_array_column() -> None:
    """PG UNNEST 展开列血缘：``UNNEST(a.items) AS u(v)`` 的 ``u.v`` 归属 ``a.items``。

    第七轮核查发现：UNNEST 是 Scope(Unnest) 而非普通子查询，旧实现 Scope 分支按
    ``selects`` 匹配列名（Unnest 的 selects 是 Identifier 非 Alias/Column）导致
    ``u.v`` 无法解析而丢边。修复后展开列来源取 Unnest 表达式的叶子列。
    """
    sql = "INSERT INTO dws.t SELECT u.v, a.id FROM ods.a a CROSS JOIN UNNEST(a.items) AS u(v)"
    edges = extract_field_lineage(sql, dialect="postgres")
    mapped = {(e.source_table, e.source_column, e.target_column) for e in edges}
    # 展开列 v 归属数组来源列 items；普通列 id 不受影响
    assert ("ods.a", "items", "v") in mapped
    assert ("ods.a", "id", "id") in mapped


def test_unnest_no_column_alias_defaults_to_table_alias() -> None:
    """UNNEST 无列别名（``AS u``）：未限定列 ``SELECT u`` 归属展开表达式。

    无列别名时展开列名默认等于表别名 u，未限定列解析应命中 Unnest 显式列名声明
    （而非猜测真实表 a 的列 u）。
    """
    sql = "INSERT INTO dws.t SELECT u, a.id FROM ods.a a CROSS JOIN UNNEST(a.items) AS u"
    edges = extract_field_lineage(sql, dialect="postgres")
    mapped = {(e.source_table, e.source_column, e.target_column) for e in edges}
    assert ("ods.a", "items", "u") in mapped


def test_explode_field_lineage_resolves_to_array_column() -> None:
    """Hive LATERAL VIEW EXPLODE 展开列血缘：``e.tag`` 归属 ``a.tags``。

    第十一轮核查发现：EXPLODE 的别名（TableAlias）挂在 Lateral 节点上而非 Explode，
    Scope 分支只处理 Unnest，导致 ``SELECT e.tag`` 字段级为 0（表级正确）。
    修复后展开列来源取 Lateral 内 EXPLODE 表达式的叶子列。
    """
    sql = "INSERT INTO dws.t SELECT e.tag, a.id FROM ods.a LATERAL VIEW EXPLODE(a.tags) e AS tag"
    edges = extract_field_lineage(sql, dialect="hive")
    mapped = {(e.source_table, e.source_column, e.target_column) for e in edges}
    assert ("ods.a", "tags", "tag") in mapped
    assert ("ods.a", "id", "id") in mapped


def test_explode_no_column_alias_defaults_to_table_alias() -> None:
    """EXPLODE 无列清单（``EXPLODE(a.tags) tag AS tag_name``）：展开列归属表达式叶子列。

    别名列清单缺省时展开列名取列别名（tag_name），未限定列引用也应命中展开表。
    """
    sql = (
        "INSERT INTO dws.t SELECT tag_name FROM ods.a LATERAL VIEW EXPLODE(a.tags) tag AS tag_name"
    )
    edges = extract_field_lineage(sql, dialect="hive")
    mapped = {(e.source_table, e.source_column, e.target_column) for e in edges}
    assert ("ods.a", "tags", "tag_name") in mapped


def test_explode_function_wrapped_array_column() -> None:
    """EXPLODE 表达式被函数包裹（``explode(split(a.tags, ','))``）：叶子列穿透解析。

    生产高频写法：先 split 再 explode，展开列血缘仍应归属原始数组列 a.tags。
    """
    sql = (
        "INSERT INTO dws.t SELECT t.tag FROM ods.a "
        "LATERAL VIEW explode(split(a.tags, ',')) t AS tag"
    )
    edges = extract_field_lineage(sql, dialect="hive")
    mapped = {(e.source_table, e.source_column, e.target_column) for e in edges}
    assert ("ods.a", "tags", "tag") in mapped


def test_constant_projection_no_field_lineage() -> None:
    """无源表常量投影：INSERT 纯常量不产字段边、纯 SELECT 常量上游依赖为空。"""
    assert extract_field_lineage("INSERT INTO dws.t SELECT 1 AS a, 'x' AS b") == []
    assert extract_table_lineage("INSERT INTO dws.t SELECT 1 AS a") == []
    ud = extract_upstream_deps("SELECT 1 AS a, 'x' AS b")
    assert ud.tables == () and ud.fields == ()


def test_deep_cte_chain_beyond_max_depth_table_kept() -> None:
    """超 MAX_DEPTH 的 CTE 链：表级血缘保留，字段级合理降级为空（防无限递归）。"""
    ctes = []
    prev = "ods.a"
    for i in range(1, 10):
        ctes.append(f"c{i} AS (SELECT id, v FROM {prev})")
        prev = f"c{i}"
    sql = "WITH " + ", ".join(ctes) + " INSERT INTO dws.t SELECT c9.id, c9.v FROM c9"
    table_edges = extract_table_lineage(sql)
    assert {(e.source, e.target) for e in table_edges} == {("ods.a", "dws.t")}
    # 字段级受 _MAX_DEPTH 保护降级（不抛异常、不产伪边）
    assert extract_field_lineage(sql) == []


def test_nested_set_operation_derived_column() -> None:
    """嵌套集合运算派生表的列解析：``SELECT x FROM (SELECT ... UNION SELECT ...) u``。

    UNION 子查询的 scope 不聚合分支 sources，列 x 需逐分支解析——合并列同时来自
    多分支（a.id AS x / b.uid AS x），收集所有分支来源。
    """
    sql = (
        "INSERT INTO dws.t SELECT x FROM "
        "(SELECT a.id AS x FROM ods.a UNION ALL SELECT b.uid AS x FROM ods.b) u"
    )
    field_edges = extract_field_lineage(sql, dialect="postgres")
    assert {("ods.a", "id", "x"), ("ods.b", "uid", "x")} == {
        (e.source_table, e.source_column, e.target_column) for e in field_edges
    }
    table_edges = extract_table_lineage(sql, dialect="postgres")
    assert {e.source for e in table_edges} == {"ods.a", "ods.b"}


def test_nested_set_operation_derived_multi_col() -> None:
    """嵌套集合运算派生表多列引用：每个输出列收集全部分支来源。"""
    sql = (
        "INSERT INTO dws.t SELECT u.x, u.y FROM "
        "(SELECT a.id AS x, a.v AS y FROM ods.a "
        "UNION ALL SELECT b.uid AS x, b.w AS y FROM ods.b) u"
    )
    field_edges = extract_field_lineage(sql, dialect="postgres")
    assert {
        ("ods.a", "id", "x"),
        ("ods.b", "uid", "x"),
        ("ods.a", "v", "y"),
        ("ods.b", "w", "y"),
    } == {(e.source_table, e.source_column, e.target_column) for e in field_edges}


def test_nested_except_derived_column() -> None:
    """嵌套 EXCEPT 派生表列解析同样支持（SetOperation 统一处理）。"""
    sql = (
        "INSERT INTO dws.t SELECT x FROM "
        "(SELECT a.id AS x FROM ods.a EXCEPT SELECT b.uid AS x FROM ods.b) u"
    )
    field_edges = extract_field_lineage(sql, dialect="postgres")
    assert {("ods.a", "id", "x"), ("ods.b", "uid", "x")} == {
        (e.source_table, e.source_column, e.target_column) for e in field_edges
    }


def test_cte_union_column_lineage() -> None:
    """CTE 定义为 UNION 时外层列血缘穿透到各分支真实源（第九轮）。

    ``WITH x AS (SELECT id FROM ods.a UNION ALL SELECT id FROM ods.b)
    INSERT INTO dws.t SELECT id FROM x`` —— CTE 引用 x.id 应同时归属两分支。
    """
    sql = (
        "WITH x AS (SELECT id FROM ods.a UNION ALL SELECT id FROM ods.b) "
        "INSERT INTO dws.t SELECT id FROM x"
    )
    field_edges = extract_field_lineage(sql, dialect="hive")
    assert {("ods.a", "id", "id"), ("ods.b", "id", "id")} == {
        (e.source_table, e.source_column, e.target_column) for e in field_edges
    }
    # 表级血缘两分支源表均保留、无 CTE 伪表
    table_edges = extract_table_lineage(sql, dialect="hive")
    assert {e.source for e in table_edges} == {"ods.a", "ods.b"}
    assert all(e.target == "dws.t" for e in table_edges)


def test_cte_union_positional_fallback() -> None:
    """UNION 分支列名不同时按位置回退（输出列名取自首分支）。

    ``WITH x AS (SELECT id, v FROM ods.a UNION ALL SELECT uid, w FROM ods.b)``
    的 ``x.id`` 位置对应第二分支 ``uid``、``x.v`` 对应 ``w``。
    """
    sql = (
        "WITH x AS (SELECT id, v FROM ods.a UNION ALL SELECT uid, w FROM ods.b) "
        "INSERT INTO dws.t SELECT id, v FROM x"
    )
    field_edges = extract_field_lineage(sql, dialect="hive")
    assert {
        ("ods.a", "id", "id"),
        ("ods.b", "uid", "id"),
        ("ods.a", "v", "v"),
        ("ods.b", "w", "v"),
    } == {(e.source_table, e.source_column, e.target_column) for e in field_edges}


def test_derived_union_positional_fallback() -> None:
    """派生表 UNION 分支列名不同同样按位置回退（与 CTE 分支一致）。"""
    sql = (
        "INSERT INTO dws.t SELECT u.x, u.y FROM "
        "(SELECT a.id AS x, a.v AS y FROM ods.a "
        "UNION ALL SELECT b.uid, b.w FROM ods.b) u"
    )
    field_edges = extract_field_lineage(sql, dialect="postgres")
    assert {
        ("ods.a", "id", "x"),
        ("ods.b", "uid", "x"),
        ("ods.a", "v", "y"),
        ("ods.b", "w", "y"),
    } == {(e.source_table, e.source_column, e.target_column) for e in field_edges}


def test_tsql_insert_top_stripped() -> None:
    """tsql INSERT TOP 剥离限行后正常产血缘（sqlglot 25.x 不支持该语法）。"""
    sql = "INSERT TOP (10) INTO dws.t SELECT id, v FROM ods.s"
    table_edges = extract_table_lineage(sql, dialect="tsql")
    assert [(e.source, e.target) for e in table_edges] == [("ods.s", "dws.t")]
    field_edges = extract_field_lineage(sql, dialect="tsql")
    assert {("ods.s", "id", "id"), ("ods.s", "v", "v")} == {
        (e.source_table, e.source_column, e.target_column) for e in field_edges
    }


def test_pg_multi_column_update() -> None:
    """PG 多列合并赋值 ``SET (a, b) = (s.a, s.b)``：按位置 zip 映射列组。"""
    sql = "UPDATE dws.t SET (a, b) = (s.a, s.b) FROM ods.s WHERE t.id = s.id"
    field_edges = extract_field_lineage(sql, dialect="postgres")
    assert {("ods.s", "a", "dws.t", "a"), ("ods.s", "b", "dws.t", "b")} == {
        (e.source_table, e.source_column, e.target_table, e.target_column) for e in field_edges
    }
    table_edges = extract_table_lineage(sql, dialect="postgres")
    assert [(e.source, e.target) for e in table_edges] == [("ods.s", "dws.t")]


def test_insert_self_reference_no_self_loop() -> None:
    """INSERT 同表自引用（``INSERT INTO t SELECT ... FROM t``）不产自环字段边（t.id→t.id）。"""
    sql = "INSERT INTO dws.t SELECT id, v FROM dws.t WHERE dt = '2026'"
    field_edges = extract_field_lineage(sql, dialect="hive")
    assert all(e.source_table != e.target_table for e in field_edges), (
        "不应产出源==目标的自环字段边"
    )
    assert extract_table_lineage(sql, dialect="hive") == []
    # 对照：跨表正常产边
    sql2 = "INSERT INTO dws.t SELECT id, v FROM ods.s"
    assert {("ods.s", "id", "id"), ("ods.s", "v", "v")} == {
        (e.source_table, e.source_column, e.target_column)
        for e in extract_field_lineage(sql2, "hive")
    }


def test_pg_lateral_subquery_column_resolved_to_inner() -> None:
    """PG ``LATERAL (SELECT ...) x`` 相关子查询：``x.v`` 须解析到内层来源表而非外层。

    旧实现把 LATERAL 当 Hive LATERAL VIEW 处理，对 Lateral 整体 find_all 列，
    导致 ``x.v`` 误归属外层表 ``ods.s`` 并多产相关性伪边。
    """
    sql = (
        "INSERT INTO dws.t SELECT s.id, x.v FROM ods.s, "
        "LATERAL (SELECT v FROM ods.d WHERE d.id = s.id) x"
    )
    field_edges = extract_field_lineage(sql, dialect="postgres")
    mapping = {(e.target_column, e.source_table): e.source_column for e in field_edges}
    assert mapping.get(("id", "ods.s")) == "id"
    assert mapping.get(("v", "ods.d")) == "v", "x.v 应归属内层 ods.d.v"
    # 不得把外层表当作 x.v 的来源（无伪边）
    assert all(e.target_column != "v" or e.source_table == "ods.d" for e in field_edges)
    # Hive LATERAL VIEW EXPLODE 回归不受影响
    sql2 = "INSERT INTO dws.t SELECT e.tag FROM ods.a LATERAL VIEW EXPLODE(a.tags) e AS tag"
    assert {("ods.a", "tags", "tag")} == {
        (e.source_table, e.source_column, e.target_column)
        for e in extract_field_lineage(sql2, dialect="hive")
    }


def test_doris_aggregate_key_column_agg_type_stripped() -> None:
    """Doris ``CREATE TABLE ... (v INT SUM) AGGREGATE KEY(...) AS SELECT``：血缘不丢。

    sqlglot 25.x 不支持列级聚合类型（SUM）与 AGGREGATE KEY 子句，剥离后血缘等价。
    """
    sql = (
        "CREATE TABLE dws.t (id INT, v INT SUM) AGGREGATE KEY(id) "
        "DISTRIBUTED BY HASH(id) AS SELECT id, v FROM ods.s"
    )
    table_edges = extract_table_lineage(sql, dialect="doris")
    assert [(e.source, e.target) for e in table_edges] == [("ods.s", "dws.t")]
    field_edges = extract_field_lineage(sql, dialect="doris")
    assert {("ods.s", "id", "id"), ("ods.s", "v", "v")} == {
        (e.source_table, e.source_column, e.target_column) for e in field_edges
    }


def test_doris_unique_key_and_decimal_agg_type() -> None:
    """Doris ``UNIQUE KEY`` + ``DECIMAL(10,2) SUM`` 组合列定义：血缘仍正常。"""
    sql = (
        "CREATE TABLE dws.t (id INT, v DECIMAL(10,2) SUM) UNIQUE KEY(id) AS SELECT id, v FROM ods.s"
    )
    assert [(e.source, e.target) for e in extract_table_lineage(sql, "doris")] == [
        ("ods.s", "dws.t")
    ]
    # SELECT 中的 SUM(v) 不受剥离正则影响（仅剥离「类型 + 聚合类型」组合）
    sql2 = "INSERT INTO dws.t SELECT SUM(v), MAX(x) FROM ods.s"
    assert extract_table_lineage(sql2, "doris")


def test_oracle_insert_all_multi_target() -> None:
    """Oracle ``INSERT ALL`` 多目标：每个目标表均产边，且不得产 t2->t1 伪边。"""
    sql = (
        "INSERT ALL INTO dws.t1 (id, v) VALUES (s.id, s.v) "
        "INTO dws.t2 (id) VALUES (s.id) SELECT id, v FROM ods.s"
    )
    table_edges = extract_table_lineage(sql, dialect="oracle")
    assert {("ods.s", "dws.t1"), ("ods.s", "dws.t2")} == {(e.source, e.target) for e in table_edges}
    assert not any(e.source.startswith("dws.t2") for e in table_edges), "不得产 t2->t1 伪边"
    field_edges = extract_field_lineage(sql, dialect="oracle")
    assert {
        ("ods.s", "id", "dws.t1", "id"),
        ("ods.s", "v", "dws.t1", "v"),
        ("ods.s", "id", "dws.t2", "id"),
    } == {(e.source_table, e.source_column, e.target_table, e.target_column) for e in field_edges}


def test_oracle_insert_first_conditional() -> None:
    """Oracle ``INSERT FIRST`` 条件多目标：逐分支产边。"""
    sql = (
        "INSERT FIRST WHEN s.v > 100 THEN INTO dws.t1 (id,v) VALUES (s.id,s.v) "
        "ELSE INTO dws.t2 (id,v) VALUES (s.id,s.v) SELECT id, v FROM ods.s"
    )
    table_edges = extract_table_lineage(sql, dialect="oracle")
    assert {("ods.s", "dws.t1"), ("ods.s", "dws.t2")} == {(e.source, e.target) for e in table_edges}
    field_edges = extract_field_lineage(sql, dialect="oracle")
    assert {
        ("ods.s", "id", "dws.t1", "id"),
        ("ods.s", "v", "dws.t1", "v"),
        ("ods.s", "id", "dws.t2", "id"),
        ("ods.s", "v", "dws.t2", "v"),
    } == {(e.source_table, e.source_column, e.target_table, e.target_column) for e in field_edges}


# ---- 第十三轮：命名窗口 / 标量子查询 scope / UNPIVOT 输出列 ----


def test_named_window_derivation_columns() -> None:
    """命名窗口引用（``OVER w``）：PARTITION/ORDER 列按 WINDOW 子句解析为派生源。

    对照内联窗口行为——``rn`` 应同时得 ``g`` 与 ``ts`` 两个派生源，而非仅 id 边。
    """
    sql = (
        "INSERT INTO dws.t SELECT id, ROW_NUMBER() OVER w AS rn FROM ods.s "
        "WINDOW w AS (PARTITION BY g ORDER BY ts)"
    )
    edges = extract_field_lineage(sql, dialect="postgres")
    rn_sources = {
        (e.source_table, e.source_column)
        for e in edges
        if e.target_column == "rn" and not e.degraded
    }
    assert rn_sources == {("ods.s", "g"), ("ods.s", "ts")}


def test_scalar_subquery_field_lineage_correct_scope() -> None:
    """CASE 分支标量子查询：子查询内部列在自身 scope 解析——得 ``d.v`` 且无 ``s.v`` 伪边。"""
    sql = (
        "INSERT INTO dws.t SELECT id, "
        "CASE WHEN flag = 1 THEN (SELECT max(v) FROM ods.d WHERE d.id = s.id) "
        "ELSE 0 END AS calc FROM ods.s s"
    )
    edges = extract_field_lineage(sql, dialect="postgres")
    calc_sources = {
        (e.source_table, e.source_column)
        for e in edges
        if e.target_column == "calc" and not e.degraded
    }
    # 子查询自身表的聚合列 d.v 必须出现；不得出现 SQL 中不存在的 s.v 伪边
    assert ("ods.d", "v") in calc_sources
    assert not any(src_table == "ods.s" and src_col == "v" for src_table, src_col in calc_sources)


def test_select_list_scalar_subquery_correct_scope() -> None:
    """SELECT 列表标量子查询的 sv 归属 ``ods.d.v``，而非伪 ``s.v``。"""
    sql = "INSERT INTO dws.t SELECT id, (SELECT v FROM ods.d WHERE d.id = s.id) AS sv FROM ods.s s"
    edges = extract_field_lineage(sql, dialect="postgres")
    sv_sources = {
        (e.source_table, e.source_column)
        for e in edges
        if e.target_column == "sv" and not e.degraded
    }
    assert ("ods.d", "v") in sv_sources
    assert not any(src_table == "ods.s" and src_col == "v" for src_table, src_col in sv_sources)


def test_unpivot_value_column_multi_source() -> None:
    """UNPIVOT 值列：``u.v`` 归 In 列表源列（多源）；名列 ``u.k`` 是列名字面量不产数据边。"""
    sql = (
        "INSERT INTO dws.t (id, k, v) "
        "SELECT s.id, u.k, u.v FROM ods.s s UNPIVOT (v FOR k IN (a, b, c)) u"
    )
    edges = extract_field_lineage(sql, dialect="tsql")
    v_sources = {
        (e.source_table, e.source_column)
        for e in edges
        if e.target_column == "v" and not e.degraded
    }
    # 值列来自被展开的 a/b/c 三列，而非不存在的 s.v/s.k
    assert v_sources == {("ods.s", "a"), ("ods.s", "b"), ("ods.s", "c")}
    assert not any(e.source_column in ("k", "v") and e.source_table == "ods.s" for e in edges)


# ---- P5：Hive 变量/宏展开（${hivevar:..}/${hiveconf:..}/${..}）----


def test_hive_inline_set_var_expansion() -> None:
    """内联 ``set hivevar:x=y;`` 声明 + ``${hivevar:x}`` 占位符：解析前展开，血缘正常。"""
    sql = (
        "set hivevar:date_id=2026-08-13;\n"
        "INSERT INTO dws.t SELECT id, v FROM ods.s WHERE date_id = ${hivevar:date_id}"
    )
    edges = extract_table_lineage(sql, dialect="hive")
    assert [(e.source, e.target) for e in edges] == [("ods.s", "dws.t")]
    field_edges = extract_field_lineage(sql, dialect="hive")
    assert {(e.source_table, e.target_table) for e in field_edges} == {("ods.s", "dws.t")}


def test_hive_explicit_variables_expansion() -> None:
    """显式传入 ``variables``：``${tbl}``/``${hiveconf:dt}`` 均展开为字面值。"""
    sql = "INSERT INTO ${tbl} SELECT id, amount FROM ods.src WHERE dt = ${hiveconf:dt}"
    edges = extract_table_lineage(
        sql, dialect="hive", variables={"tbl": "dws.finance", "dt": "20260816"}
    )
    assert [(e.source, e.target) for e in edges] == [("ods.src", "dws.finance")]
    field_edges = extract_field_lineage(
        sql, dialect="hive", variables={"tbl": "dws.finance", "dt": "20260816"}
    )
    assert {(e.source_column, e.target_column) for e in field_edges} == {
        ("id", "id"),
        ("amount", "amount"),
    }


def test_hive_comment_var_expansion() -> None:
    """注释行 ``--hivevar src=ods.orders``：变量声明行展开后不污染血缘。"""
    sql = "--hivevar src=ods.orders\nINSERT INTO dws.t SELECT id FROM ${src}"
    edges = extract_table_lineage(sql, dialect="hive")
    assert [(e.source, e.target) for e in edges] == [("ods.orders", "dws.t")]


def test_hive_unknown_var_graceful() -> None:
    """未知变量保留占位符：不抛异常（sqlglot 能解析则照常产边，否则降级为空）。"""
    sql = "INSERT INTO ${no_such_table} SELECT id FROM ods.s WHERE dt = ${hivevar:missing}"
    # 未知占位符被 sqlglot 当作合法标识符解析（no_such_table 表）→ 不崩
    edges = extract_table_lineage(sql, dialect="hive")
    assert isinstance(edges, list)
    assert any(e.source == "ods.s" for e in edges)
    # 纯 SELECT 未知表名无法解析 → 降级为空且不崩
    assert isinstance(extract_field_lineage(sql, dialect="hive"), list)


def test_variables_ignored_for_non_hive() -> None:
    """非 Hive/Spark 方言（且未显式传 variables）：不展开占位符（血缘降级为空）。"""
    sql = "INSERT INTO ${tbl} SELECT id FROM ods.s"
    assert extract_table_lineage(sql, dialect="mysql") == []
    assert extract_table_lineage(sql, dialect="clickhouse") == []


def test_expand_variables_hiveconf_and_bare() -> None:
    """``expand_variables`` 纯函数：hiveconf 前缀 + 裸变量名均替换；未知保留原样。"""
    expanded = expand_variables(
        "SELECT * FROM ${hiveconf:db}.src WHERE dt = ${dt} AND a = ${unknown}",
        dialect="hive",
        variables={"db": "ods", "dt": "20260816"},
    )
    assert "ods.src" in expanded
    assert "dt = 20260816" in expanded
    assert "${unknown}" in expanded
    # 非 hive 方言直接原样返回
    assert expand_variables("${x}", dialect="mysql") == "${x}"
