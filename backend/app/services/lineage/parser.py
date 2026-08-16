"""血缘解析器（基于 sqlglot）。

提供两张粒度的血缘抽取：
- 表级（L1）：INSERT/CREATE TABLE AS/UPDATE/MERGE 的写入目标与读入源表
- 字段级（L2）：借助 sqlglot.lineage.LineageRunner 抽取列到列的派生关系

所有函数均为纯函数，不依赖数据库或外部服务，便于单元测试。

生产级增强：
- ``SELECT *`` 投影降级：无法枚举具体字段时不产出伪字段边，仅在返回结构中以
  ``FieldEdge.degraded=True`` 标记（表级血缘不受影响）。
- MERGE / 多分支 UNION / UNION ALL：``_find_source_query`` 提取源查询，
  UNION 展开为逐分支 SELECT 分别解析。
- 链式 CTE 跨层解析、派生表达式（如 ``SUM(a.col) + b.col``）叶子列拆分。
- 解析失败降级：sqlparse 预处理器（分号拆分/注释剥离）后再交 sqlglot，
  任意环节失败均返回空列表而不抛异常。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope


@dataclass(frozen=True)
class TableEdge:
    """表级血缘边。"""

    source: str
    target: str


@dataclass(frozen=True)
class FieldEdge:
    """字段级血缘边。"""

    source_table: str
    source_column: str | None
    target_table: str
    target_column: str
    expression: str | None = None
    #: True 表示该边是降级标记（如 ``SELECT *`` 无法枚举字段），不构成真实字段派生。
    degraded: bool = False


@dataclass(frozen=True)
class UpstreamDeps:
    """只读查询（纯 SELECT 无写入目标）读取的上游依赖清单。

    不构成血缘边（无下游落点），仅用于「本次查询读取了哪些表/字段」的只读展示，
    区别于正式血缘（写图谱、可影响分析）。
    """

    tables: tuple[str, ...]
    fields: tuple[str, ...]


#: 字段血缘递归解析最大深度（防止 CTE/子查询自引用导致的无限递归）。
_MAX_DEPTH = 8


class _SourceScope:
    """最小作用域占位：仅提供 ``sources``，供 MERGE 等无 SELECT 语句复用列解析。

    与 sqlglot ``Scope`` 在 ``_resolve_column`` 中仅被消费 ``sources`` 的约定对齐。
    """

    def __init__(self, sources: dict[str, Any]) -> None:
        self.sources = sources


def _norm_table(table: exp.Table) -> str:
    """将 sqlglot 表节点规范化为 ``catalog.db.table`` 形式。"""
    parts = [p.name for p in table.parts if getattr(p, "name", None)]
    return ".".join(parts)


def _update_set_target(update: exp.Update) -> exp.Table | None:
    """多表 UPDATE 的写入目标：SET 中被更新列所属的表。

    单表 UPDATE（无 JOIN）返回其 ``this``；多表 UPDATE（``UPDATE t1 JOIN t2 ...``）
    取 SET 子句中第一个被更新列所属的 JOIN 表（实践中单条 UPDATE 通常只更新一张表；
    若 SET 列分散在多个表，退回首表近似）。
    """
    this = update.this
    if not isinstance(this, exp.Table):
        return None
    joins = this.args.get("joins") or []
    if not joins:
        return this
    tables = [this] + [j.this for j in joins if isinstance(j.this, exp.Table)]
    for eq in getattr(update, "expressions", None) or []:
        col = eq.this if isinstance(eq, exp.EQ) else None
        if not isinstance(col, exp.Column) or not col.table:
            continue
        for t in tables:
            if t.alias_or_name == col.table:
                return t
    return this


def _find_target(ast: exp.Expression) -> exp.Table | None:
    """定位写入目标表（INSERT / CREATE TABLE|VIEW AS / UPDATE / MERGE / SELECT INTO）。"""
    for node in ast.walk():
        is_target_node = isinstance(node, (exp.Insert, exp.Update, exp.Merge)) or (
            isinstance(node, exp.Create) and node.kind and node.kind.upper() in ("TABLE", "VIEW")
        )
        if not is_target_node:
            # SELECT INTO（PG/SQL Server 建表式赋值）：``SELECT ... INTO newtbl FROM ...``
            # 的 ``Into.this`` 为 Table；MySQL ``SELECT ... INTO @var`` 是变量赋值
            # （``Into.this`` 为 Table 但内部 this 是 Parameter），不构成血缘目标。
            if isinstance(node, exp.Select):
                into_this = getattr(node.args.get("into"), "this", None)
                if isinstance(into_this, exp.Table) and not isinstance(
                    into_this.this, exp.Parameter
                ):
                    return into_this
            continue
        if isinstance(node, exp.Update):
            updated = _update_set_target(node)
            if updated is not None:
                return updated
        this = node.this
        if isinstance(this, exp.Table):
            return this
        if isinstance(this, exp.Schema):
            inner = this.this
            if isinstance(inner, exp.Table):
                return inner
    return None


def _build_alias_map(ast: exp.Expression) -> dict[str, str]:
    """构造 FROM/JOIN 中 ``alias_or_name -> 规范化真实表名`` 映射。

    字段血缘的列引用常以别名（如 ``a.col``）出现，而表级血缘使用规范化真名
    （``catalog.db.src``）。若不解析别名，字段节点会与表节点断裂，导致字段级
    影响分析查无结果。
    """
    amap: dict[str, str] = {}
    for tbl in ast.find_all(exp.Table):
        amap[tbl.alias_or_name] = _norm_table(tbl)
    return amap


def _preprocess_dialect(sql: str, dialect: str | None) -> str:
    """方言级语法归一化（解析前预处理，血缘语义不变）。

    sqlglot 30.x 对部分生产高频方言语法不支持（降级为 ``Command`` 或抛解析异常），
    这里在解析前做等价改写：
    - mysql/doris/starrocks：``REPLACE INTO ... SELECT`` → ``INSERT INTO ... SELECT``
      （sqlglot 不支持 REPLACE；血缘语义等价——REPLACE 即覆盖式插入，来源表/列映射
      完全一致，仅写入动作不同）。
    - doris/starrocks：剥离 ``INSERT INTO t WITH LABEL 'xxx' SELECT ...`` 中的
      ``WITH LABEL 'xxx'`` 片段（sqlglot 不支持 Doris 的 LABEL 标记）。
    - doris/starrocks：剥离 ``CREATE TABLE ... AS SELECT`` 的物理分布/副本属性
      （``DISTRIBUTED BY ... [BUCKETS n]``、``PROPERTIES(...)``、``ENGINE=``）——
      sqlglot 25.x 对 ``CREATE TABLE t DISTRIBUTED BY HASH(id) BUCKETS 10 AS SELECT``
      整体降级为 Command 致血缘全丢；这些子句仅描述物理布局，不影响 SELECT 源与
      目标表，剥离后血缘语义不变。
    """
    if not sql or not sql.strip():
        return sql
    d = (dialect or "").lower()
    if d in ("mysql", "doris", "starrocks"):
        sql = re.sub(r"\bREPLACE\s+INTO\b", "INSERT INTO", sql, flags=re.IGNORECASE)
    if d in ("doris", "starrocks"):
        sql = re.sub(r"\bWITH\s+LABEL\s+('[^']*'|\S+)", " ", sql, flags=re.IGNORECASE)
        sql = re.sub(
            r"\bDISTRIBUTED\s+BY\b.*?(?=\bAS\b|$)",
            " ",
            sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        sql = re.sub(r"\bPROPERTIES\s*\(.*?\)", " ", sql, flags=re.IGNORECASE | re.DOTALL)
        sql = re.sub(r"\bENGINE\s*=\s*\w+", " ", sql, flags=re.IGNORECASE)
        sql = re.sub(r"\bBUCKETS\s+\d+", " ", sql, flags=re.IGNORECASE)
    return sql


def _split_statements(sql: str) -> list[str]:
    """净化待解析 SQL：剥注释 + 分号拆分，返回非空语句列表。

    降级路径的一部分：先经 sqlparse 预处理（注释剥离、按分号拆分），再将每条
    语句交给 sqlglot；sqlparse 不可用或处理异常时回退为整段原文。
    """
    if not sql or not sql.strip():
        return []
    try:
        import sqlparse
    except ImportError:
        return [sql]
    try:
        cleaned = sqlparse.format(sql, strip_comments=True, reindent=False)
        parsed = sqlparse.split(cleaned)
        stmts = [str(s).strip() for s in parsed if s is not None]
    except Exception:
        return [sql]
    return [s for s in stmts if s]


def _table_edges(ast: Any) -> list[TableEdge]:
    """单条已解析语句的表级血缘边（source -> target）。"""
    target = _find_target(ast)
    target_name = _norm_table(target) if target is not None else None
    edges: list[TableEdge] = []
    if target_name is None:
        # 纯查询语句无写入目标，无成边条件
        return edges
    if isinstance(ast, exp.Create) and not _is_query_node(ast.expression):
        # CREATE TABLE ... LIKE ...（结构复制，无数据流转）与纯 DDL（无 AS SELECT）：
        # 无 SELECT 数据源，不构成血缘边（LIKE 的源表仅是结构模板）
        return edges
    cte_names = _collect_ctes(ast)
    seen: set[tuple[str, str]] = set()
    for tbl in ast.find_all(exp.Table):
        src = _norm_table(tbl)
        # 排除 CTE 引用（``FROM cte1`` 中的 ``cte1`` 不是真实表，避免伪源表节点）
        if not src or src == target_name or _is_cte_ref(tbl, cte_names):
            continue
        key = (src, target_name)
        if key in seen:
            continue
        seen.add(key)
        edges.append(TableEdge(source=src, target=target_name))
    return edges


def _select_table_edges(ast: Any, target_name: str) -> list[TableEdge]:
    """纯 SELECT 指定落点（方案 A+B）：FROM/JOIN 源表 → target_name 表级边。

    与 ``_table_edges`` 的区别：查询本身无写入目标（``_find_target`` 返回 None），
    但调用方显式指定了落点表，此时把查询读取的全部源表指向该落点，构成正式血缘
    （写图谱、可影响分析）。落点表自身出现在 FROM/JOIN 中时跳过，避免自环。
    """
    edges: list[TableEdge] = []
    seen: set[tuple[str, str]] = set()
    cte_names = _collect_ctes(ast)
    for tbl in ast.find_all(exp.Table):
        src = _norm_table(tbl)
        # 排除 CTE 引用（同 ``_table_edges``，避免伪源表节点）
        if not src or src == target_name or _is_cte_ref(tbl, cte_names):
            continue
        key = (src, target_name)
        if key in seen:
            continue
        seen.add(key)
        edges.append(TableEdge(source=src, target=target_name))
    return edges


def extract_table_lineage(
    sql: str, dialect: str | None = None, target_table: str | None = None
) -> list[TableEdge]:
    """抽取表级血缘。

    Args:
        sql: SQL 文本（支持注释/多语句，自动净化）。
        dialect: sqlglot dialect（可选，如 ``"mysql"`` / ``"hive"`` / ``"doris"`` /
            ``"clickhouse"``）。
        target_table: 可选落点表（方案 A+B）。SQL 自身无写入目标（纯 SELECT）但指定
            了该值时，把 FROM/JOIN 源表 → ``target_table`` 生成表级边；未指定时纯
            SELECT 保持无成边（返回空，由调用方降级展示上游依赖）。

    Returns:
        表级血缘边列表（source -> target）；解析失败或非法 SQL 时返回空列表（降级）。
    """
    edges: list[TableEdge] = []
    seen: set[tuple[str, str]] = set()
    sql = _preprocess_dialect(sql, dialect)
    for stmt in _split_statements(sql):
        try:
            ast = sqlglot.parse_one(stmt, dialect=dialect)
        except Exception:
            # 非 SQL / 语法错误：跳过该语句，整体降级（不抛异常）
            continue
        stmt_edges = _table_edges(ast)
        if not stmt_edges and target_table and _is_query_node(ast):
            # 纯 SELECT/集合运算显式落点：FROM/JOIN 源表 → 目标表（写入语句如
            # INSERT OVERWRITE DIRECTORY 目标非表时不做纯查询落点回退）
            stmt_edges = _select_table_edges(ast, target_table)
        for edge in stmt_edges:
            key = (edge.source, edge.target)
            if key in seen:
                continue
            seen.add(key)
            edges.append(edge)
    return edges


def _find_source_query(ast: exp.Expression) -> exp.Expression | None:
    """定位生成写入行的源查询（SELECT/UNION/MERGE 的 USING 集）。

    INSERT INTO ... SELECT / CREATE TABLE ... AS SELECT 的源查询即其 ``expression``；
    MERGE 的源集为 ``using`` 子句（表/子查询）。多分支 UNION/UNION ALL 返回 Union
    节点，由调用方逐分支解析。其余情况退化为 None，由调用方降级处理。
    """
    if isinstance(ast, exp.Insert):
        q: exp.Expression | None = ast.expression
        if _is_query_node(q):
            return q
    if isinstance(ast, exp.Create) and ast.kind and ast.kind.upper() in ("TABLE", "VIEW"):
        q = ast.expression
        if _is_query_node(q):
            return q
    if isinstance(ast, exp.Merge):
        return ast.args.get("using")
    # SELECT INTO（``SELECT ... INTO newtbl FROM ...``）：查询自身即源查询。
    # MySQL ``SELECT ... INTO @var`` 是变量赋值，不作为血缘源查询。
    if isinstance(ast, exp.Select) and ast.args.get("into"):
        into_this = ast.args["into"].this
        if isinstance(into_this, exp.Table) and not isinstance(into_this.this, exp.Parameter):
            return ast
    return None


def _is_query_node(node: exp.Expression | None) -> bool:
    """节点是否为查询语句/查询表达式（SELECT 或集合运算 UNION/EXCEPT/INTERSECT）。

    ``exp.SetOperation`` 是 Union/Except/Intersect 的公共基类；集合运算（如
    ``SELECT ... EXCEPT SELECT ...``）是生产常见语法，字段级/上游依赖均需展开
    各分支解析，不能当作非查询语句跳过。
    """
    return isinstance(node, (exp.Select, exp.SetOperation))


def _branch_queries(query: exp.Expression) -> list[exp.Select]:
    """将源查询展开为多个 SELECT 分支（UNION/EXCEPT/INTERSECT 拆开，普通 SELECT 单个）。"""
    if isinstance(query, exp.SetOperation):
        # 集合运算：this/expression 是左右分支（可能是嵌套集合运算，递归展开）
        branches: list[exp.Select] = []
        for side in (query.args.get("this"), query.args.get("expression")):
            if isinstance(side, exp.Select):
                branches.append(side)
            elif isinstance(side, exp.SetOperation):
                branches.extend(_branch_queries(side))
        return branches
    if isinstance(query, exp.Select):
        return [query]
    return []


def _collect_ctes(query: exp.Expression) -> dict[str, exp.CTE]:
    """收集整棵查询树中的 CTE 定义（cte 别名 -> CTE 节点），支持链式 CTE。

    sqlglot 双版本兼容：Insert/Select 的 ``ctes`` property 直接暴露 WITH 列表，但
    Update/Merge 无该 property（``WITH ... UPDATE`` / ``WITH ... MERGE`` 是生产高频
    增量回刷语法），需从 ``With.expressions`` 中取——否则 CTE 引用（``FROM s``）会
    被误判为伪源表（``s->dws_tgt``）。
    """
    ctes: dict[str, exp.CTE] = {}
    for node in query.walk():
        if hasattr(node, "ctes"):
            for cte in node.ctes or []:
                ctes[cte.alias] = cte
        if isinstance(node, exp.With):
            for cte in node.expressions or []:
                if isinstance(cte, exp.CTE):
                    ctes[cte.alias] = cte
    return ctes


def _is_cte_ref(tbl: exp.Table, cte_names: dict[str, Any] | set[str]) -> bool:
    """判断表引用是否为 CTE（而非真实表）。

    仅当表名命中 CTE 集合且**无 schema/db/catalog 前缀**时才算 CTE 引用：
    ``FROM cte1`` 的 ``cte1`` 是 CTE，而 ``JOIN ods.cte1``（带 schema）是真实表
    （CTE 不携带库前缀），避免 CTE 名与真实表同名时误排除后者。
    """
    if tbl.name not in cte_names:
        return False
    return not tbl.catalog and not tbl.db


def _projection_name(projection: exp.Expression) -> str | None:
    """取投影的目标列名（Alias 取别名，裸 Column 取列名，其余为 None）。"""
    if isinstance(projection, exp.Alias):
        return projection.alias
    if isinstance(projection, exp.Column):
        return projection.name
    return None


def _scope_outputs_column(scope: Any, col_name: str) -> bool:
    """判断某 scope 的表达式是否输出指定列名。"""
    selects = getattr(scope, "selects", None)
    if not selects:
        return False
    return any(_projection_name(p) == col_name for p in selects)


def _cte_outputs_column(cte: exp.CTE, col_name: str) -> bool:
    """CTE 定义 SELECT 是否输出指定列名（用于未限定列解析时判定某 CTE 是否含该列）。"""
    selects = getattr(cte.this, "selects", None) or []
    return any(_projection_name(p) == col_name for p in selects)


def _resolve_projection(
    scope: Any,
    projection: exp.Expression,
    cte_map: dict[str, exp.CTE],
    dialect: str | None,
    depth: int,
) -> list[tuple[str, str]]:
    """递归解析某个投影表达式，返回其所有叶子列的 (真实表名, 列名)。"""
    if depth > _MAX_DEPTH:
        return []
    out: list[tuple[str, str]] = []
    for leaf in projection.find_all(exp.Column):
        out.extend(_resolve_column(scope, leaf, cte_map, dialect, depth))
    return out


def _resolve_column(
    scope: Any,
    col: exp.Column,
    cte_map: dict[str, exp.CTE],
    dialect: str | None,
    depth: int,
) -> list[tuple[str, str]]:
    """将字段引用解析为 (真实表名, 列名)。

    处理三类来源：
    - 真实表（``exp.Table``）：直接返回规范化表名 + 列名；
    - CTE（源表名命中 ``cte_map``）：递归进入 CTE 定义 SELECT 解析；
    - 子查询/派生表（``Scope`` 来源）：递归进入该子查询解析。
    """
    if depth > _MAX_DEPTH:
        return []
    qualifier = col.table or ""
    src = None
    sources = getattr(scope, "sources", {}) or {}
    if qualifier:
        src = sources.get(qualifier)
    if src is None:
        # 未限定列：优先真实表（非 CTE 引用）。CTE 引用可能不含该列——
        # 如 ``SELECT id FROM c1 JOIN ods.b USING(id)`` 里 ``max(v)`` 的 v 实际
        # 来自 ods.b，而 c1 只输出 id；若取第一个来源（CTE c1）会错误解析为空。
        for _name, s in sources.items():
            if isinstance(s, exp.Table) and s.name not in cte_map:
                src = s
                break
        if src is None:
            # 其次：输出该列的 CTE 引用（穿透定义解析）
            for _name, s in sources.items():
                if (
                    isinstance(s, exp.Table)
                    and s.name in cte_map
                    and _cte_outputs_column(cte_map[s.name], col.name)
                ):
                    src = s
                    break
        if src is None:
            for _name, s in sources.items():
                if isinstance(s, Scope) and _scope_outputs_column(s.expression, col.name):
                    src = s
                    break
    if src is None:
        return []

    if isinstance(src, exp.Table):
        name = _norm_table(src)
        cte = cte_map.get(src.name)
        if cte is not None:
            # 进入 CTE 定义：找同名投影并递归解析
            cte_select = cte.this
            inner = build_scope(cte_select)
            if inner is None:
                return []
            for p in getattr(cte_select, "selects", []):
                if _projection_name(p) == col.name:
                    return _resolve_projection(inner, p, cte_map, dialect, depth + 1)
            return []
        return [(name, col.name)]

    if isinstance(src, Scope):
        for p in getattr(src.expression, "selects", []):
            if _projection_name(p) == col.name:
                return _resolve_projection(src, p, cte_map, dialect, depth + 1)
    return []


def _projection_has_star(projection: exp.Expression) -> bool:
    """顶层投影是否含 ``SELECT *`` / ``alias.*``（仅涉及投影自身，不计函数内星号）。"""
    if isinstance(projection, exp.Star):
        return True
    if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
        return True
    if isinstance(projection, exp.Alias):
        return _projection_has_star(projection.this)
    return False


def _star_descriptor(projection: exp.Expression) -> tuple[str | None, str]:
    """返回 ``SELECT *`` 投影的 (别名, 限定表别名)；无别名时返回 None。"""
    if isinstance(projection, exp.Alias):
        inner = projection.this
        if isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star):
            return projection.alias, inner.table or ""
        if isinstance(inner, exp.Star):
            return projection.alias, ""
        return None, ""
    if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
        return None, projection.table or ""
    if isinstance(projection, exp.Star):
        return None, ""
    return None, ""


def _star_source_table(qualifier: str, scope: Any, ast: exp.Expression) -> str:
    """推断 ``SELECT *`` 的来源表真名（限定别名解析；未限定且单源时取唯一源表）。"""
    if qualifier:
        return _build_alias_map(ast).get(qualifier, qualifier)
    tables = [v for v in (getattr(scope, "sources", {}) or {}).values() if isinstance(v, exp.Table)]
    if len(tables) == 1:
        return _norm_table(tables[0])
    return ""


def _star_edge(
    projection: exp.Expression,
    scope: Any,
    ast: exp.Expression,
    target_name: str,
) -> FieldEdge:
    """为 ``SELECT *`` 投影构造降级标记边（不产出伪字段边）。"""
    alias, qualifier = _star_descriptor(projection)
    target_column = alias or (f"{qualifier}.*" if qualifier else "*")
    return FieldEdge(
        source_table=_star_source_table(qualifier, scope, ast),
        source_column=None,
        target_table=target_name,
        target_column=target_column,
        degraded=True,
    )


def _is_bare_column_projection(projection: exp.Expression) -> bool:
    """投影是否为裸列引用（决定是否记录 expression 原文）。"""
    return (isinstance(projection, exp.Alias) and isinstance(projection.this, exp.Column)) or (
        isinstance(projection, exp.Column)
    )


def _emit_leaf_edges(
    scope: Any,
    source_expr: exp.Expression,
    cte_map: dict[str, exp.CTE],
    dialect: str | None,
    target_name: str,
    target_col: str,
    is_bare: bool,
    edges: list[FieldEdge],
) -> None:
    """解析源表达式叶子列并追加字段边（派生表达式记录 expression 原文）。"""
    resolved = _resolve_projection(scope, source_expr, cte_map, dialect, 0)
    if not resolved:
        return
    expr_sql = None if is_bare else source_expr.sql(dialect=dialect)
    for source_table, source_column in resolved:
        edges.append(
            FieldEdge(
                source_table=source_table,
                source_column=source_column,
                target_table=target_name,
                target_column=target_col,
                expression=expr_sql,
            )
        )


def _insert_column_list(ast: exp.Expression) -> list[str]:
    """写入目标显式列清单（``INSERT INTO t (a, b) SELECT ...`` /
    ``CREATE TABLE t (a, b) AS ...``），无则空。

    目标列名来自列清单而非 SELECT 投影别名——``INSERT INTO t (a,b) SELECT x,y`` 与
    ``CREATE TABLE t (a,b) AS SELECT x,y`` 中字段血缘应均为 ``x→a, y→b``（按位置对应
    投影），否则 target 列名错位。Insert 的 Schema expressions 是 Identifier，
    Create 的 Schema expressions 是 ColumnDef（取 ``.name``）。
    """
    for node in ast.walk():
        if isinstance(node, (exp.Insert, exp.Create)) and isinstance(node.this, exp.Schema):
            return [c.name for c in node.this.expressions if hasattr(c, "name")]
    return []


def _extract_branch_edges(
    branch: exp.Select,
    scope: Any,
    ast: exp.Expression,
    target_name: str,
    cte_map: dict[str, exp.CTE],
    dialect: str | None,
    edges: list[FieldEdge],
    target_cols: list[str] | None = None,
) -> None:
    """展开单个 SELECT 分支的投影：星号降级标记 + 常规列边 + 表达式边。

    ``target_cols`` 为 INSERT 显式列清单（按位置对应投影）；未提供时目标列名取
    投影别名（``INSERT INTO t SELECT x AS a`` → 目标列 ``a``）。
    """
    for i, projection in enumerate(getattr(branch, "selects", None) or []):
        if _projection_has_star(projection):
            edges.append(_star_edge(projection, scope, ast, target_name))
            continue
        target_col = _projection_name(projection)
        if target_cols is not None and i < len(target_cols):
            target_col = target_cols[i]
        if not target_col:
            continue
        leaf_cols = list(projection.find_all(exp.Column))
        if not leaf_cols:
            continue
        is_bare = _is_bare_column_projection(projection)
        _emit_leaf_edges(
            scope, projection, cte_map, dialect, target_name, target_col, is_bare, edges
        )


def _try_build_scope(expr: exp.Expression) -> Any:
    """安全构建 sqlglot 作用域；失败返回 None（降级）。"""
    try:
        return build_scope(expr)
    except Exception:
        return None


def _merge_source_scope(using: exp.Expression) -> _SourceScope | None:
    """构造 MERGE 的源集作用域（``USING`` 的表/子查询 -> 列解析作用域）。"""
    if isinstance(using, exp.Subquery):
        sub = _try_build_scope(using.this)
        if sub is None:
            return None
        alias = using.alias or using.name or ""
        if not alias:
            return None
        return _SourceScope({alias: sub})
    if isinstance(using, exp.Table):
        alias = using.alias or using.name or ""
        return _SourceScope({alias: using})
    return None


def _column_name(node: exp.Expression) -> str | None:
    """提取列节点名（非列节点返回 None）。"""
    if isinstance(node, exp.Column):
        return node.name
    return None


def _extract_merge_edges(
    ast: exp.Merge,
    target_name: str,
    cte_map: dict[str, exp.CTE],
    dialect: str | None,
    edges: list[FieldEdge],
) -> None:
    """解析 MERGE 的 WHEN 动作：UPDATE SET 与 INSERT(...) VALUES 分支的字段边。"""
    using = ast.args.get("using")
    scope = _merge_source_scope(using) if isinstance(using, (exp.Subquery, exp.Table)) else None
    if scope is None:
        return
    # sqlglot 双版本兼容：30.x 的 WHEN 分支存放于 args["whens"]（Whens 容器节点），
    # 25.x 直接存于 ast.expressions（列表）——按版本择一遍历，否则字段级血缘全丢。
    whens = ast.args.get("whens")
    if whens is None:
        when_iter: Any = ast.expressions
    else:
        when_iter = getattr(whens, "expressions", None) or []
    for when in when_iter:
        then = when.args.get("then")
        if isinstance(then, exp.Update):
            for eq in getattr(then, "expressions", None) or []:
                if not isinstance(eq, exp.EQ):
                    continue
                target_col = _column_name(eq.this)
                if not target_col:
                    continue
                is_bare = isinstance(eq.expression, exp.Column)
                _emit_leaf_edges(
                    scope, eq.expression, cte_map, dialect, target_name, target_col, is_bare, edges
                )
        elif isinstance(then, exp.Insert):
            # INSERT 分支：有列清单时 ``this`` 为 Tuple(keys)、``expression`` 为
            # Tuple(values)；无列清单（``INSERT VALUES (s.id, s.name)``）时 ``this``
            # 为 None、``expression`` 为 Tuple(values)——目标列名不可显式获得，
            # 用值中裸列引用的列名近似（``s.id`` → 目标列 ``id``）。
            keys_node = then.this if isinstance(then.this, exp.Tuple) else None
            values_node = then.expression if isinstance(then.expression, exp.Tuple) else None
            keys = getattr(keys_node, "expressions", None) or []
            values = getattr(values_node, "expressions", None) or []
            if not values:
                continue
            if not keys:
                for val_expr in values:
                    if not isinstance(val_expr, exp.Column):
                        continue
                    target_col = val_expr.name
                    _emit_leaf_edges(
                        scope, val_expr, cte_map, dialect, target_name, target_col, True, edges
                    )
                continue
            for col_expr, val_expr in zip(keys, values, strict=False):
                target_col = _column_name(col_expr)
                if not target_col:
                    continue
                is_bare = isinstance(val_expr, exp.Column)
                _emit_leaf_edges(
                    scope, val_expr, cte_map, dialect, target_name, target_col, is_bare, edges
                )


def _extract_update_edges(
    update: exp.Update,
    target_name: str,
    cte_map: dict[str, exp.CTE],
    dialect: str | None,
    edges: list[FieldEdge],
) -> None:
    """Update 的字段级血缘：SET 目标列 ← 值表达式中的来源列。

    来源解析两类生产语法（sqlglot 的 build_scope 不支持 Update，此处手动解析）：
    - PG ``UPDATE tgt SET v = s.v FROM src s``（FROM 子句表为来源）
    - MySQL ``UPDATE tgt JOIN src ON ... SET tgt.v = src.v``（JOIN 表为来源）
    - ``SET v = (SELECT MAX(x) FROM src ...)`` 子查询值：解析子查询内部投影列
    - ``WITH s AS (...) UPDATE tgt SET v = s.v FROM s``（CTE 来源）：穿透 CTE 定义
      解析到真实表（``s.v`` → ``ods_src.v``），不产生伪表 ``s``
    自引用（值列与目标同属目标表 / 无来源表限定的列）不产生跨表字段边。
    """
    amap = _build_alias_map(update)
    # CTE 引用（``FROM s`` 的 ``s``）不是真实表：从别名映射中剔除，避免伪表边
    amap = {k: v for k, v in amap.items() if k not in cte_map}
    target_alias = update.this.alias_or_name if isinstance(update.this, exp.Table) else ""
    for eq in getattr(update, "expressions", None) or []:
        if not isinstance(eq, exp.EQ):
            continue
        target_col = _column_name(eq.this)
        if not target_col:
            continue
        val = eq.expression
        if isinstance(val, exp.Subquery):
            # SET 值为子查询：子查询 SELECT 的投影列 → 目标列（无别名时用目标列名）
            sub = _try_build_scope(val.this)
            if sub is None:
                continue
            for projection in getattr(val.this, "selects", None) or []:
                if _projection_has_star(projection):
                    edges.append(_star_edge(projection, sub, update, target_name))
                    continue
                sub_col = _projection_name(projection) or target_col
                is_bare = _is_bare_column_projection(projection)
                _emit_leaf_edges(
                    sub, projection, cte_map, dialect, target_name, sub_col, is_bare, edges
                )
            continue
        # 普通表达式：逐个解析值中的列引用到来源表
        is_bare = isinstance(val, exp.Column)
        expr_sql = None if is_bare else val.sql(dialect=dialect)
        for leaf in val.find_all(exp.Column):
            if not leaf.table or leaf.table == target_alias:
                continue
            if leaf.table in cte_map:
                # 穿透 CTE：进入定义 SELECT 找同名投影列，解析到真实来源表
                cte_select = cte_map[leaf.table].this
                inner = _try_build_scope(cte_select)
                if inner is None:
                    continue
                for p in getattr(cte_select, "selects", None) or []:
                    if _projection_name(p) != leaf.name:
                        continue
                    for rt, rc in _resolve_projection(inner, p, cte_map, dialect, 0):
                        edges.append(
                            FieldEdge(
                                source_table=rt,
                                source_column=rc,
                                target_table=target_name,
                                target_column=target_col,
                                expression=expr_sql,
                            )
                        )
                    break
                continue
            src_t = amap.get(leaf.table)
            if not src_t or src_t == target_name:
                continue
            edges.append(
                FieldEdge(
                    source_table=src_t,
                    source_column=leaf.name,
                    target_table=target_name,
                    target_column=target_col,
                    expression=expr_sql,
                )
            )


def _extract_field_edges(ast: Any, dialect: str | None) -> list[FieldEdge]:
    """单条已解析语句的字段级血缘边（含 MERGE 专用路径与星号降级标记）。"""
    target = _find_target(ast)
    target_name = _norm_table(target) if target is not None else ""
    edges: list[FieldEdge] = []
    if target is None and isinstance(ast, (exp.Insert, exp.Update, exp.Merge, exp.Create)):
        # 写入语句但目标非表（如 Hive INSERT OVERWRITE DIRECTORY）：无表目标，不产字段边
        return edges
    cte_map = _collect_ctes(ast)
    if isinstance(ast, exp.Merge):
        _extract_merge_edges(ast, target_name, cte_map, dialect, edges)
        return edges
    if isinstance(ast, exp.Update):
        _extract_update_edges(ast, target_name, cte_map, dialect, edges)
        return edges
    query = _find_source_query(ast)
    if query is None:
        return edges
    target_cols = _insert_column_list(ast)
    for branch in _branch_queries(query):
        scope = _try_build_scope(branch)
        if scope is None:
            continue
        _extract_branch_edges(branch, scope, ast, target_name, cte_map, dialect, edges, target_cols)
    return edges


def _select_field_edges(ast: Any, target_name: str, dialect: str | None) -> list[FieldEdge]:
    """纯 SELECT 指定落点（方案 A+B）：SELECT 投影列 → target_name 字段级边。

    复用 ``_extract_branch_edges`` 的分支展开 + 作用域列解析链路，仅把目标表替换为
    显式落点。SELECT 为普通查询时单分支，UNION 多分支逐分支解析。
    """
    cte_map = _collect_ctes(ast)
    edges: list[FieldEdge] = []
    for branch in _branch_queries(ast):
        scope = _try_build_scope(branch)
        if scope is None:
            continue
        _extract_branch_edges(branch, scope, ast, target_name, cte_map, dialect, edges)
    return edges


def extract_field_lineage(
    sql: str, dialect: str | None = None, target_table: str | None = None
) -> list[FieldEdge]:
    """抽取字段级血缘（深度解析：CTE / 子查询 / 表达式 / MERGE / UNION）。

    基于 sqlglot ``build_scope`` 递归展开作用域，将每个目标投影列解析到其真实源列
    （可跨多层）CTE 与子查询；UNION 逐分支解析。派生表达式（如 ``a.col + b.col``）
    记录到 ``expression`` 字段，并拆出多个源列边。``SELECT *`` 投影以
    ``degraded=True`` 标记降级而不产出伪字段边。

    Args:
        sql: SQL 文本（支持注释/多语句，自动净化）。
        dialect: sqlglot dialect（可选，如 ``"mysql"`` / ``"hive"`` / ``"doris"`` /
            ``"clickhouse"`` / ``"starrocks"``）。
        target_table: 可选落点表（方案 A+B）。SQL 自身无写入目标（纯 SELECT）但指定
            了该值时，把 SELECT 投影列 → ``target_table`` 对应列生成字段级边；未指定
            时纯 SELECT 保持无成边（返回空，由调用方降级展示上游依赖）。

    Returns:
        字段级血缘边列表（含降级标记）；解析不可用或失败时返回空列表（降级）。
    """
    edges: list[FieldEdge] = []
    seen: set[tuple[object, ...]] = set()
    sql = _preprocess_dialect(sql, dialect)
    for stmt in _split_statements(sql):
        try:
            ast = sqlglot.parse_one(stmt, dialect=dialect)
        except Exception:
            continue
        stmt_edges = _extract_field_edges(ast, dialect)
        if not stmt_edges and target_table and _is_query_node(ast):
            # 纯 SELECT/集合运算显式落点：投影列 → 目标表列（写入语句目标非表时不回退）
            stmt_edges = _select_field_edges(ast, target_table, dialect)
        for edge in stmt_edges:
            key = (
                edge.source_table,
                edge.source_column,
                edge.target_table,
                edge.target_column,
                edge.expression,
                edge.degraded,
            )
            if key in seen:
                continue
            seen.add(key)
            edges.append(edge)
    return edges


def extract_upstream_deps(sql: str, dialect: str | None = None) -> UpstreamDeps:
    """提取只读查询（纯 SELECT 无落点）读取的上游表与字段清单。

    血缘边要求下游落点；纯 SELECT 不构成边。本函数返回该查询读取的源表
    （FROM/JOIN/CTE 定义中的表）与**投影列**（SELECT 输出的列，经作用域递归解析
    为 ``真实表名.列名``，子查询别名/CTE 均追溯到真实来源表），供「本次查询的
    上游依赖」只读展示，不写图谱、不参与影响分析。

    字段清单仅取投影列，不收集 ON/WHERE/GROUP BY 等条件列：条件列属于查询内部
    的连接/过滤逻辑，且子查询别名列（如 ``t2.hospital_id``）若直接暴露会污染血缘
    （前端将其误判为 ``t2`` 表节点）。

    Args:
        sql: SQL 文本（支持注释/多语句，自动净化）。
        dialect: sqlglot dialect（可选）。

    Returns:
        ``UpstreamDeps``：去重排序的 ``tables`` / ``fields``；解析失败降级为空。
    """
    tables: set[str] = set()
    fields: set[str] = set()
    sql = _preprocess_dialect(sql, dialect)
    for stmt in _split_statements(sql):
        try:
            ast: Any = sqlglot.parse_one(stmt, dialect=dialect)
        except Exception:
            continue
        if _find_target(ast) is not None:
            # 写入语句（INSERT/UPDATE/MERGE/CREATE）不是「纯 SELECT 无落点」场景：
            # 其目标表不属上游依赖，直接跳过（避免把写入目标误收为读取来源）
            continue
        if not _is_query_node(ast):
            # 非查询语句（ALTER/DROP/TRUNCATE/DELETE/COPY/USE 等）不是血缘读取：
            # 不产上游依赖，避免把 DDL/DML 的目标表（如 DELETE FROM t 的 t）误收为来源
            continue
        cte_map = _collect_ctes(ast)
        for tbl in ast.find_all(exp.Table):
            name = _norm_table(tbl)
            # 排除 CTE 引用（``FROM cte1`` 的 ``cte1`` 非真实表，避免伪表节点；
            # 带 schema 前缀的 ``ods.cte1`` 是真实表，不排除）
            if name and not _is_cte_ref(tbl, set(cte_map)):
                tables.add(name)
        for branch in _branch_queries(ast):
            scope = _try_build_scope(branch)
            if scope is None:
                continue
            for projection in getattr(branch, "selects", None) or []:
                # 星号投影无法枚举具体字段，跳过（表级依赖不受影响）
                if _projection_has_star(projection):
                    continue
                for real_table, real_col in _resolve_projection(
                    scope, projection, cte_map, dialect, 0
                ):
                    fields.add(f"{real_table}.{real_col}")
    return UpstreamDeps(
        tables=tuple(sorted(tables)),
        fields=tuple(sorted(fields)),
    )


def node_table(name: str) -> str:
    """构造表节点标识。"""
    return f"table:{name}"


def node_metric(code: str) -> str:
    """构造指标节点标识（L3 指标级血缘边两端统一用 ``metric:{code}``）。"""
    return f"metric:{code}"


def node_dimension(code: str) -> str:
    """构造维度节点标识（指标↔维度血缘，``dimension:{dim_code}``）。"""
    return f"dimension:{code}"


def node_column(table: str, column: str) -> str:
    """构造字段节点标识（指标↔字段血缘，``column:{db}.{tbl}.{col}``）。"""
    return f"column:{table}.{column}"


def node_field(table: str, column: str) -> str:
    """构造字段节点标识。"""
    return f"field:{table}.{column}"
