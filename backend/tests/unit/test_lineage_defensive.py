"""血缘解析器防御分支与降级路径测试。

针对 coverage 报告中 parser.py 的剩余未覆盖行，直接构造 AST / mock 异常，
覆盖：
- sqlparse 缺失/异常降级（_split_statements）
- 空集合/无匹配返回（_branch_queries、_scope_outputs_column、_unnest_outputs_column、
  _matching_unpivot、_projection_name、_star_descriptor、_star_source_table、
  _is_cte_ref 等）
- 异常安全降级（_try_build_scope 兜底）
- MERGE 源集构造的 None/无别名分支（_merge_source_scope）
- 多目标 INSERT 非 ConditionalInsert / 非 Select source 分支
- 各解析入口对 None / 空投影 / 写入语句目标非表的降级
- extract_upstream_deps 对纯 SELECT 星号投影跳过
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlglot
from sqlglot import exp

from app.services.lineage.parser import (
    _branch_queries,
    _build_alias_map,
    _collect_ctes,
    _column_name,
    _emit_update_pair,
    _extract_field_edges,
    _extract_merge_edges,
    _extract_multitable_edges,
    _extract_update_edges,
    _find_source_query,
    _find_target,
    _insert_column_list,
    _is_cte_ref,
    _is_query_node,
    _lateral_outputs_column,
    _matching_unpivot,
    _merge_source_scope,
    _multitable_branches,
    _multitable_table_edges,
    _norm_table,
    _preprocess_dialect,
    _projection_has_star,
    _projection_name,
    _resolve_column,
    _resolve_leaf_scope,
    _resolve_projection,
    _resolve_setop_column,
    _scope_outputs_column,
    _select_field_edges,
    _select_table_edges,
    _SourceScope,
    _split_statements,
    _star_descriptor,
    _star_source_table,
    _try_build_scope,
    _unnest_outputs_column,
    _unpivot_output_sources,
    _update_set_target,
    extract_field_lineage,
    extract_table_lineage,
    extract_upstream_deps,
)

# ---- sqlparse 缺失/异常降级（229-230, 235-236）----


def test_split_statements_without_sqlparse(monkeypatch: pytest.MonkeyPatch) -> None:
    """sqlparse 未安装时回退为整段原文（不抛异常）。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> object:
        if name == "sqlparse":
            raise ImportError("no sqlparse")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # 重新加载以触发 import 路径（_split_statements 内部 import sqlparse）
    assert _split_statements("SELECT 1; SELECT 2") == ["SELECT 1; SELECT 2"]


def test_split_statements_sqlparse_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """sqlparse.split 抛异常时回退整段原文（except 分支）。"""
    import sqlparse

    def boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("sqlparse broken")

    monkeypatch.setattr(sqlparse, "split", boom)
    monkeypatch.setattr(sqlparse, "format", lambda *a, **k: "")
    assert _split_statements("SELECT 1") == ["SELECT 1"]


# ---- 空输入 / 空集合（225, 253, 263, 392, 485, 503, 506, ...）----


def test_split_statements_empty_input() -> None:
    assert _split_statements("") == []
    assert _split_statements("   \n\t  ") == []


def test_branch_queries_non_query() -> None:
    """非 SELECT/SetOperation 节点返回空列表（如 Table 节点）。"""
    tbl = exp.Table(this=exp.Identifier(this="t"))
    assert _branch_queries(tbl) == []


def test_multitable_branches_non_insert_expression(monkeypatch: pytest.MonkeyPatch) -> None:
    """MultitableInserts.expressions 含非 Insert 项时被跳过（253 行）。"""

    class FakeCond:
        this = exp.Table(this=exp.Identifier(this="not_an_insert"))

    class FakeMulti:
        args = {"expressions": [FakeCond()]}

    # _norm_table 会遍历 FakeCond.this 作为 Table，但 not isinstance(ins, Insert) → continue
    assert _multitable_branches(FakeMulti()) == []  # type: ignore[arg-type]


def test_multitable_table_edges_non_select_source() -> None:
    """source 非 Select/SetOperation 时返回空（263 行）。"""

    class FakeMulti:
        args = {"source": exp.Table(this=exp.Identifier(this="t"))}

    assert _multitable_table_edges(FakeMulti()) == []  # type: ignore[arg-type]


def test_find_source_query_merge_using() -> None:
    """MERGE 的 using 子句经 392 行返回（已在矩阵覆盖，此处直接守卫分支）。"""
    sql = "MERGE INTO t USING s ON t.id=s.id WHEN MATCHED THEN UPDATE SET t.v=s.v"
    ast = sqlglot.parse_one(sql)
    using = _find_source_query(ast)
    assert using is not None  # MERGE using 分支


def test_find_source_query_select_into_var() -> None:
    """SELECT INTO 变量（@var）不构成源查询目标（into_this.this 是 Parameter）。

    覆盖 397-398 行的 isinstance 检查通过但内部 this 为 Parameter 的分支。
    """
    # MySQL: SELECT ... INTO @var 会被解析；构造一个 Into 包 Parameter
    into = exp.Into(this=exp.Table(this=exp.Parameter(this=exp.Literal.string("v"))))
    sel = exp.Select()
    sel.set("into", into)
    assert _find_source_query(sel) is None


# ---- _scope_outputs_column 空 selects（484-485）----


def test_scope_outputs_column_empty_scope() -> None:
    """scope 无 selects 属性时返回 False（485 行）。"""

    class FakeScope:
        expression = exp.Select()
        selects = None

    assert _scope_outputs_column(FakeScope(), "x") is False


def test_scope_outputs_column_set_operation() -> None:
    """集合运算子查询 scope：展开分支检查（478 行已部分覆盖，守卫无匹配）。"""
    sql = "SELECT x FROM (SELECT id AS x FROM a UNION ALL SELECT uid AS x FROM b) u"
    ast = sqlglot.parse_one(sql)
    sub = ast.find(exp.Subquery)
    assert sub is not None
    scope = _try_build_scope(sub.this)
    assert scope is not None
    assert _scope_outputs_column(scope, "x") is True
    assert _scope_outputs_column(scope, "nonexistent") is False


# ---- _unnest_outputs_column（503, 506）----


def test_unnest_outputs_column_no_alias() -> None:
    """UNNEST 无 alias 时返回 False（503 行）。"""
    unnest = exp.Unnest(expressions=[exp.Column(this=exp.Identifier(this="items"))])
    assert _unnest_outputs_column(unnest, "v") is False


def test_unnest_outputs_column_with_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    """UNNEST 有列别名列表时匹配列名（506 行）。"""
    # 用真实 SQL 构造 Unnest + TableAlias(columns=[...])
    sql = "SELECT u.v FROM UNNEST(a.items) AS u(v)"
    ast = sqlglot.parse_one(sql, read="postgres")
    unnest = ast.find(exp.Unnest)
    assert unnest is not None
    assert _unnest_outputs_column(unnest, "v") is True
    assert _unnest_outputs_column(unnest, "other") is False


def test_unnest_outputs_column_default_name() -> None:
    """UNNEST 无列别名时展开列名默认等于表别名（507 行）。"""
    sql = "SELECT u FROM UNNEST(a.items) AS u"
    ast = sqlglot.parse_one(sql, read="postgres")
    unnest = ast.find(exp.Unnest)
    assert unnest is not None
    assert _unnest_outputs_column(unnest, "u") is True


# ---- _try_build_scope 异常降级（967-968）----


def test_try_build_scope_returns_none_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_scope 抛异常时 _try_build_scope 返回 None（967-968 行）。"""
    import app.services.lineage.parser as parser_mod

    def boom(_expr: object) -> object:
        raise RuntimeError("scope build failed")

    monkeypatch.setattr(parser_mod, "build_scope", boom)
    sel = sqlglot.parse_one("SELECT id FROM a")
    assert _try_build_scope(sel) is None


# ---- _merge_source_scope 边界（976, 979, 984）----


def test_merge_source_scope_subquery_no_alias() -> None:
    """USING 子查询无别名时返回 None（979 行）。"""
    using = exp.Subquery(this=sqlglot.parse_one("SELECT id FROM a"))
    # 去掉别名
    using.set("alias", None)
    assert _merge_source_scope(using) is None


def test_merge_source_scope_subquery_scope_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """USING 子查询 build_scope 返回 None 时整体 None（976 行）。"""
    import app.services.lineage.parser as parser_mod

    monkeypatch.setattr(parser_mod, "_try_build_scope", lambda _e: None)
    using = exp.Subquery(
        this=sqlglot.parse_one("SELECT id FROM a"),
        alias=exp.TableAlias(this=exp.Identifier(this="s")),
    )
    assert _merge_source_scope(using) is None


def test_merge_source_scope_unknown_type() -> None:
    """USING 既不是 Subquery 也不是 Table 时返回 None（984 行）。"""
    assert _merge_source_scope(exp.Literal.number(1)) is None


# ---- _column_name 非列节点（991）----


def test_column_name_non_column() -> None:
    assert _column_name(exp.Literal.number(1)) is None
    assert _column_name(exp.Star()) is None


# ---- _table_edges / _select_table_edges 的 CTE 与 target 排除（已有，守卫 seen）----


def test_table_edges_dedup() -> None:
    """同 (source, target) 对重复出现只产一条边（seen 去重，229-230 行已有部分覆盖）。"""
    sql = "INSERT INTO t SELECT a.id FROM s a JOIN s b ON a.id=b.id"
    edges = extract_table_lineage(sql)
    assert [(e.source, e.target) for e in edges] == [("s", "t")]


# ---- _projection_name / _star_descriptor 边界 ----


def test_projection_name_non_alias_non_column() -> None:
    """投影既不是 Alias 也不是裸 Column 时返回 None。"""
    assert _projection_name(exp.Literal.number(1)) is None
    assert _projection_name(exp.Star()) is None


def test_star_descriptor_variants() -> None:
    """覆盖 _star_descriptor 的各个分支。"""
    # 裸星号
    star = exp.Star()
    assert _star_descriptor(star) == (None, "")
    # alias.* 列
    col_star = exp.Column(this=exp.Star(), table=exp.Identifier(this="a"))
    assert _star_descriptor(col_star) == (None, "a")
    # Alias(Star)
    aliased_star = exp.Alias(this=exp.Star(), alias=exp.Identifier(this="all_cols"))
    assert _star_descriptor(aliased_star) == ("all_cols", "")
    # Alias(Column(*))
    aliased_col_star = exp.Alias(
        this=exp.Column(this=exp.Star(), table=exp.Identifier(this="a")),
        alias=exp.Identifier(this="x"),
    )
    assert _star_descriptor(aliased_col_star) == ("x", "a")
    # 非星号
    assert _star_descriptor(exp.Literal.number(1)) == (None, "")


def test_star_source_table_qualifier_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """qualifier 不在 alias map 中时回退为 qualifier 自身（652 行）。"""
    sel = sqlglot.parse_one("SELECT missing.* FROM a")
    # 构造 scope（单源），但用不存在的 qualifier
    scope = _try_build_scope(sel)
    # qualifier='missing' 不在 amap → 返回 'missing'
    result = _star_source_table("missing", scope, sel)
    assert result == "missing"


# ---- _is_cte_ref（92, 100, 104）----


def test_is_cte_ref_non_cte_table() -> None:
    """表名不在 CTE 集合中 → False（92 行）。"""
    tbl = exp.Table(this=exp.Identifier(this="real_tbl"))
    assert _is_cte_ref(tbl, {"cte1"}) is False


def test_is_cte_ref_with_schema_prefix() -> None:
    """带 schema 前缀的同名表是真实表而非 CTE（104 行）。"""
    # ods.cte1：有 db，即使 cte1 在 CTE 集合中也不算 CTE 引用
    tbl = exp.Table(
        this=exp.Identifier(this="cte1"),
        db=exp.Identifier(this="ods"),
    )
    assert _is_cte_ref(tbl, {"cte1"}) is False


def test_is_cte_ref_bare_cte() -> None:
    """裸表名命中 CTE 集合且无 schema 前缀 → True（100 行）。"""
    tbl = exp.Table(this=exp.Identifier(this="cte1"))
    assert _is_cte_ref(tbl, {"cte1"}) is True


# ---- _projection_has_star 嵌套 Alias ----


def test_projection_has_star_alias_non_star() -> None:
    """Alias 内部不是 Star 时返回 False（已有星号 true 分支，守卫 false）。"""
    alias = exp.Alias(this=exp.Literal.number(1), alias=exp.Identifier(this="x"))
    assert _projection_has_star(alias) is False


# ---- extract_upstream_deps 纯 SELECT 星号跳过（1353-1354）----


def test_upstream_deps_skips_star_projection() -> None:
    """纯 SELECT 含 SELECT * 时 fields 跳过该投影（但 tables 仍收集，1353-1354 行）。"""
    deps = extract_upstream_deps("SELECT * FROM ods.a")
    assert deps.tables == ("ods.a",)
    # 星号不枚举字段
    assert deps.fields == ()


# ---- _find_target 对非写入/非 SELECT INTO 节点返回 None ----


def test_find_target_none_for_pure_select() -> None:
    sel = sqlglot.parse_one("SELECT id FROM a")
    assert _find_target(sel) is None


# ---- _norm_table 过滤无 name 的 parts（79-80）----


def test_norm_table_filters_nameless_parts() -> None:
    """parts 中无 name 的项被过滤（79 行 getattr 守卫）。"""

    class FakePart:
        name = None

    tbl = exp.Table(this=exp.Identifier(this="t"))
    # 构造含无 name part 的 parts：直接调用 _norm_table，用 monkeypatch parts
    tbl.set("parts", [FakePart(), exp.Identifier(this="t")])
    assert _norm_table(tbl) == "t"


# ---- _build_alias_map 收集（已有，守卫空）----


def test_build_alias_map_empty() -> None:
    assert _build_alias_map(exp.Literal.number(1)) == {}


# ---- _preprocess_dialect 空输入 ----


def test_preprocess_dialect_empty() -> None:
    assert _preprocess_dialect("", "mysql") == ""
    assert _preprocess_dialect("   ", None) == "   "


# ---- _insert_column_list 非 Insert/Create 返回空 ----


def test_insert_column_list_non_insert() -> None:
    assert _insert_column_list(exp.Literal.number(1)) == []


# ---- _resolve_column 深度超限 ----


def test_resolve_column_depth_limit() -> None:
    """depth 超 _MAX_DEPTH 时返回空列表（357-358 行）。"""
    sel = sqlglot.parse_one("SELECT id FROM a")
    scope = _try_build_scope(sel)
    assert scope is not None
    col = exp.Column(this=exp.Identifier(this="id"), table=exp.Identifier(this="a"))
    # 用超大 depth
    from app.services.lineage.parser import _MAX_DEPTH

    assert _resolve_column(scope, col, {}, None, _MAX_DEPTH + 1) == []


# ---- _resolve_projection 深度超限 ----


def test_resolve_projection_depth_limit() -> None:
    sel = sqlglot.parse_one("SELECT id FROM a")
    scope = _try_build_scope(sel)
    assert scope is not None
    proj = sel.selects[0]  # type: ignore[attr-defined]
    from app.services.lineage.parser import _MAX_DEPTH

    assert _resolve_projection(scope, proj, {}, None, _MAX_DEPTH + 1) == []


# ---- _matching_unpivot 无匹配（752-761）----


def test_matching_unpivot_no_pivot() -> None:
    """scope 内无 Pivot 节点时返回 None（761 行）。"""
    sel = sqlglot.parse_one("SELECT id FROM a")
    scope = _try_build_scope(sel)
    assert scope is not None
    tbl = exp.Table(this=exp.Identifier(this="a"))
    assert _matching_unpivot(tbl, "u") is None


# ---- _extract_field_edges 写入语句目标非表（609-610）----


def test_extract_field_edges_insert_directory() -> None:
    """INSERT OVERWRITE DIRECTORY 目标非表时无字段边（609-610 行）。"""
    sql = "INSERT OVERWRITE DIRECTORY '/tmp/x' SELECT id, v FROM ods.s"
    ast = sqlglot.parse_one(sql, read="hive")
    assert _extract_field_edges(ast, "hive") == []


# ---- _select_field_edges / _select_table_edges 空分支 ----


def test_select_table_edges_target_self_loop_skipped() -> None:
    """纯 SELECT 显式落点：源表 == 目标表时跳过（235-236 行的 seen/self-loop）。"""
    sql = "SELECT id FROM dws.t"
    edges = _select_table_edges(sqlglot.parse_one(sql), "dws.t")
    assert edges == []


def test_select_field_edges_no_branches() -> None:
    """query 无 SELECT 分支时返回空（1251 行）。"""
    assert _select_field_edges(exp.Literal.number(1), "t", None) == []


# ---- extract_* 非 SQL 输入降级 ----


def test_extract_on_garbage_input() -> None:
    """非 SQL / 语法错误整体降级为空，不抛异常。"""
    assert extract_table_lineage("THIS IS NOT SQL AT ALL !!!") == []
    assert extract_field_lineage("SELECT FROM WHERE !!!") == []
    deps = extract_upstream_deps("DROP TABLE IF EXISTS x; ALTER TABLE y DROP z")
    assert deps.tables == () and deps.fields == ()


def test_preprocess_replace_into_mysql() -> None:
    """REPLACE INTO 被预处理为 INSERT INTO（已有，守卫）。"""
    out = _preprocess_dialect("REPLACE INTO t SELECT id FROM s", "mysql")
    assert "REPLACE" not in out.upper().split("INTO")[0]


# ---- _SourceScope dataclass ----


def test_source_scope_holds_sources() -> None:
    tbl = exp.Table(this=exp.Identifier(this="s"))
    ss = _SourceScope({"s": tbl})
    assert ss.sources == {"s": tbl}


# ---- _collect_ctes 对无 ctes 的节点 ----


def test_collect_ctes_none() -> None:
    assert _collect_ctes(exp.Literal.number(1)) == {}


# ---- _is_query_node 各种类型 ----


def test_is_query_node_variants() -> None:
    assert _is_query_node(sqlglot.parse_one("SELECT 1")) is True
    union_ast = sqlglot.parse_one("SELECT 1 UNION SELECT 2")
    assert _is_query_node(union_ast) is True
    assert _is_query_node(exp.Table(this=exp.Identifier(this="t"))) is False
    assert _is_query_node(None) is False


# ---- 补盲：_update_set_target 分支（92/100/104）----


def test_update_set_target_this_not_table() -> None:
    """UPDATE this 非 Table 时返回 None（92 行）。"""
    upd = exp.Update(this=exp.Identifier(this="x"), expressions=[])
    assert _update_set_target(upd) is None


def test_update_set_target_unqualified_set_col() -> None:
    """多表 UPDATE 的 SET 列无表限定：跳过该列并回退主表（100/104 行）。"""
    upd = sqlglot.parse_one("UPDATE t1 JOIN t2 ON t1.id = t2.id SET v = 1")
    assert _update_set_target(upd) == upd.this  # type: ignore[arg-type]


# ---- 519：_lateral_outputs_column alias 非 TableAlias ----


def test_lateral_outputs_column_alias_not_table_alias() -> None:
    lat = exp.Lateral(this=exp.Func(this=exp.Identifier(this="explode")))
    lat.set("alias", exp.Identifier(this="e"))
    assert _lateral_outputs_column(lat, "tag") is False


# ---- 541：_resolve_setop_column 空分支 ----


def test_resolve_setop_column_empty_branches() -> None:
    assert _resolve_setop_column(exp.Union(), "id", {}, "hive", 0) == []


# ---- 551：_resolve_setop_column branch scope 构建失败 ----


def test_resolve_setop_column_branch_scope_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.lineage.parser as parser_mod

    monkeypatch.setattr(parser_mod, "_try_build_scope", lambda *a, **k: None)
    union = sqlglot.parse_one("SELECT id FROM a UNION SELECT id FROM b")
    assert _resolve_setop_column(union, "id", {}, "hive", 0) == []  # type: ignore[arg-type]


# ---- 631：_resolve_leaf_scope 内层 scope 构建失败 ----


def test_resolve_leaf_scope_inner_scope_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.lineage.parser as parser_mod

    sel = sqlglot.parse_one("SELECT v FROM (SELECT v FROM ods.d) x")
    scope = _try_build_scope(sel)
    assert scope is not None
    leaf = next(sel.find_all(exp.Column))
    monkeypatch.setattr(parser_mod, "_try_build_scope", lambda *a, **k: None)
    out = _resolve_leaf_scope(scope, leaf, {}, "hive", 0)
    assert isinstance(out, list)


# ---- 652：_matching_unpivot 跳过普通 Pivot ----


def test_matching_unpivot_skips_regular_pivot() -> None:
    tbl = exp.Table(this=exp.Identifier(this="s"))
    tbl.set("pivots", [exp.Pivot()])
    assert _matching_unpivot(tbl, "u") is None


# ---- 675：_unpivot_output_sources field 非 In ----


def test_unpivot_output_sources_field_not_in() -> None:
    piv = exp.Pivot(expressions=[exp.column("v")])
    assert _unpivot_output_sources(None, piv, exp.column("v"), {}, "pg", 0) == []


# ---- 712 + 761：_resolve_column UNPIVOT 循环非 Table 源 / 未知限定符 ----


def test_resolve_column_unpivot_loop_non_table_source() -> None:
    scope = _SourceScope({"x": exp.column("a")})  # 非 Table 也非 Scope → 712 continue
    assert _resolve_column(scope, exp.column("u", "v"), {}, "pg", 0) == []


def test_resolve_column_unknown_qualifier_empty() -> None:
    """空 sources 时限定列无法解析 → 761 返回空（真实表 fallback 兜不住）。"""
    assert _resolve_column(_SourceScope({}), exp.column("u", "v"), {}, "pg", 0) == []


# ---- 776：CTE 定义 build_scope 失败（需手动构造 Table 型 CTE 引用）----


def test_resolve_cte_build_scope_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.lineage.parser as parser_mod

    # 正常 build_scope 会把 CTE 引用展开为 Scope；用 _SourceScope 保留 Table 形态
    # 使 _resolve_column 走「Table + cte_map 命中」分支，patch build_scope → 776
    cte_map = _collect_ctes(sqlglot.parse_one("WITH x AS (SELECT id FROM ods.a) SELECT * FROM x"))
    scope = _SourceScope({"x": exp.Table(this=exp.Identifier(this="x"))})
    monkeypatch.setattr(parser_mod, "build_scope", lambda *a, **k: None)
    assert _resolve_column(scope, exp.column("id", "x"), cte_map, "hive", 0) == []


# ---- 835：_star_descriptor Alias(this=Star) ----


def test_star_descriptor_alias_of_star() -> None:
    alias = exp.Alias(this=exp.Star(), alias=exp.Identifier(this="x"))
    assert _star_descriptor(alias) == ("x", "")


def test_star_descriptor_alias_non_star() -> None:
    """Alias 内层非星号（普通列）→ 835 返回 (None, '')。"""
    alias = exp.Alias(this=exp.column("a"), alias=exp.Identifier(this="x"))
    assert _star_descriptor(alias) == (None, "")


# ---- 1005：_extract_merge_edges source scope 构建失败 ----


def test_extract_merge_edges_scope_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.lineage.parser as parser_mod

    ast = sqlglot.parse_one(
        "MERGE INTO tgt USING src ON tgt.id = src.id WHEN MATCHED THEN UPDATE SET tgt.v = src.v"
    )
    monkeypatch.setattr(parser_mod, "_merge_source_scope", lambda *a, **k: None)
    _extract_merge_edges(ast, "tgt", {}, "hive", [])  # type: ignore[arg-type]


# ---- 1012：whens 容器存在但 expressions 为空 ----


def test_extract_merge_edges_whens_empty_container() -> None:
    ast = sqlglot.parse_one(
        "MERGE INTO tgt USING src ON tgt.id = src.id WHEN MATCHED THEN UPDATE SET tgt.v = src.v"
    )

    class _FakeWhens:
        pass

    ast.set("whens", _FakeWhens())  # 模拟 30.x Whens 容器但无分支
    _extract_merge_edges(ast, "tgt", {}, "hive", [])  # type: ignore[arg-type]


# ---- 1018/1021：WHEN UPDATE SET 非 EQ / LHS 非列 ----


def test_extract_merge_edges_update_non_eq_and_bad_lhs() -> None:
    ast = sqlglot.parse_one(
        "MERGE INTO tgt USING src ON tgt.id = src.id WHEN MATCHED THEN UPDATE SET tgt.v = src.v"
    )
    when = ast.args["expressions"][0]
    then = when.args["then"]
    then.set("expressions", [exp.Literal.number(1)])  # 非 EQ → 1018
    _extract_merge_edges(ast, "tgt", {}, "hive", [])  # type: ignore[arg-type]
    then.set(
        "expressions",
        [exp.EQ(this=exp.Literal.number(1), expression=exp.column("v"))],  # LHS 非列 → 1021
    )
    _extract_merge_edges(ast, "tgt", {}, "hive", [])  # type: ignore[arg-type]


# ---- 1036/1040/1049：MERGE INSERT 分支列清单边界 ----


def test_extract_merge_edges_insert_empty_values() -> None:
    ast = sqlglot.parse_one(
        "MERGE INTO tgt USING src ON tgt.id = src.id WHEN NOT MATCHED THEN INSERT (id) VALUES (1)"
    )
    when = ast.args["expressions"][0]
    then = when.args["then"]
    then.set("expression", exp.Tuple(expressions=[]))  # values 空 → 1036
    _extract_merge_edges(ast, "tgt", {}, "hive", [])  # type: ignore[arg-type]


def test_extract_merge_edges_insert_bare_const_values() -> None:
    ast = sqlglot.parse_one(
        "MERGE INTO tgt USING src ON tgt.id = src.id WHEN NOT MATCHED THEN INSERT (id) VALUES (1)"
    )
    when = ast.args["expressions"][0]
    then = when.args["then"]
    then.set("this", None)  # 无列清单
    then.set("expression", exp.Tuple(expressions=[exp.Literal.number(1)]))  # 值非 Column → 1040
    _extract_merge_edges(ast, "tgt", {}, "hive", [])  # type: ignore[arg-type]


def test_extract_merge_edges_insert_bad_key() -> None:
    ast = sqlglot.parse_one(
        "MERGE INTO tgt USING src ON tgt.id = src.id WHEN NOT MATCHED THEN INSERT (id) VALUES (1)"
    )
    when = ast.args["expressions"][0]
    then = when.args["then"]
    then.set("this", exp.Tuple(expressions=[exp.Literal.number(1)]))  # key 非列 → 1049
    then.set("expression", exp.Tuple(expressions=[exp.column("v")]))
    _extract_merge_edges(ast, "tgt", {}, "hive", [])  # type: ignore[arg-type]


# ---- 1078：_emit_update_pair 目标表缺失 fallback ----


def test_emit_update_pair_missing_item_target() -> None:
    edges: list[Any] = []
    _emit_update_pair(exp.column("v", "x"), exp.column("a", "s"), {}, "tgt", {}, "hive", edges)
    assert edges == []


# ---- 1083：SET 子查询值 scope 构建失败 ----


def test_emit_update_pair_subquery_scope_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.lineage.parser as parser_mod

    val = exp.Subquery(this=sqlglot.parse_one("SELECT v FROM ods.d"))
    monkeypatch.setattr(parser_mod, "_try_build_scope", lambda *a, **k: None)
    _emit_update_pair(exp.column("v", "t"), val, {"t": "dws.t"}, "dws.t", {}, "hive", [])


# ---- 1107：CTE 穿透 scope 构建失败 ----


def test_emit_update_pair_cte_scope_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.lineage.parser as parser_mod

    cte_map = _collect_ctes(sqlglot.parse_one("WITH s AS (SELECT v FROM ods.d) SELECT * FROM s"))
    monkeypatch.setattr(parser_mod, "_try_build_scope", lambda *a, **k: None)
    _emit_update_pair(
        exp.column("v", "t"), exp.column("v", "s"), {"t": "dws.t"}, "dws.t", cte_map, "hive", []
    )


# ---- 1163：_extract_update_edges eq 非 EQ ----


def test_extract_update_edges_non_eq() -> None:
    upd = sqlglot.parse_one("UPDATE t SET v = 1")
    upd.set("expressions", [exp.Literal.number(1)])
    _extract_update_edges(upd, "t", {}, "hive", [])  # type: ignore[arg-type]


# ---- 1188/1191：_extract_multitable_edges source 边界 ----


def test_extract_multitable_source_not_query() -> None:
    ast = exp.MultitableInserts(
        expressions=[],
        source=exp.Table(this=exp.Identifier(this="x")),
    )
    _extract_multitable_edges(ast, {}, "oracle", [])


def test_extract_multitable_source_scope_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.lineage.parser as parser_mod

    ast = exp.MultitableInserts(
        expressions=[],
        source=sqlglot.parse_one("SELECT id FROM s"),
    )
    monkeypatch.setattr(parser_mod, "_try_build_scope", lambda *a, **k: None)
    _extract_multitable_edges(ast, {}, "oracle", [])


# ---- 1196/1202：_extract_multitable_edges VALUES 边界 ----


def test_extract_multitable_branch_values_not_values() -> None:
    """分支 INSERT 的 expression 是 Select 而非 VALUES → 1196。"""
    ast = sqlglot.parse_one(
        "INSERT ALL INTO t1 (id) VALUES (s.id) INTO t2 (id) VALUES (s.id) SELECT id FROM s",
        read="oracle",
    )
    branches = _multitable_branches(ast)  # type: ignore[arg-type]
    assert branches
    _, ins = branches[0]
    ins.set("expression", sqlglot.parse_one("SELECT id FROM s"))
    _extract_multitable_edges(ast, {}, "oracle", [])  # type: ignore[arg-type]


def test_extract_multitable_value_no_target_col() -> None:
    """VALUES 行含无列名值（常量）且无列清单 → 1202。"""
    ast = sqlglot.parse_one(
        "INSERT ALL INTO t1 (id) VALUES (s.id) INTO t2 (id) VALUES (s.id) SELECT id FROM s",
        read="oracle",
    )
    branches = _multitable_branches(ast)  # type: ignore[arg-type]
    assert branches
    _, ins = branches[0]
    ins.set(
        "this",
        exp.Schema(this=exp.Table(this=exp.Identifier(this="t1")), expressions=[]),
    )
    ins.set(
        "expression",
        exp.Values(expressions=[exp.Tuple(expressions=[exp.Literal.number(1)])]),
    )
    _extract_multitable_edges(ast, {}, "oracle", [])  # type: ignore[arg-type]


# ---- 1235/1251/1350：各入口 branch scope 构建失败 ----


def test_extract_field_edges_branch_scope_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.lineage.parser as parser_mod

    monkeypatch.setattr(parser_mod, "_try_build_scope", lambda *a, **k: None)
    assert _extract_field_edges(sqlglot.parse_one("INSERT INTO t SELECT id FROM s"), "hive") == []


def test_select_field_edges_branch_scope_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.lineage.parser as parser_mod

    monkeypatch.setattr(parser_mod, "_try_build_scope", lambda *a, **k: None)
    assert _select_field_edges(sqlglot.parse_one("SELECT id FROM s"), "t", "hive") == []


def test_upstream_deps_branch_scope_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.lineage.parser as parser_mod

    monkeypatch.setattr(parser_mod, "_try_build_scope", lambda *a, **k: None)
    deps = extract_upstream_deps("SELECT id FROM ods.a")
    assert "ods.a" in deps.tables  # 表级依赖在 scope 之前收集，不受影响
    assert deps.fields == ()
