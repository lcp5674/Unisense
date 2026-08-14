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


def _find_target(ast: exp.Expression) -> exp.Table | None:
    """定位写入目标表（INSERT / CREATE TABLE AS / UPDATE / MERGE）。"""
    for node in ast.walk():
        is_target_node = isinstance(node, (exp.Insert, exp.Update, exp.Merge)) or (
            isinstance(node, exp.Create) and node.kind and node.kind.upper() == "TABLE"
        )
        if not is_target_node:
            continue
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
    seen: set[tuple[str, str]] = set()
    for tbl in ast.find_all(exp.Table):
        src = _norm_table(tbl)
        if src == target_name:
            continue
        key = (src, target_name)
        if key in seen:
            continue
        seen.add(key)
        edges.append(TableEdge(source=src, target=target_name))
    return edges


def extract_table_lineage(sql: str, dialect: str | None = None) -> list[TableEdge]:
    """抽取表级血缘。

    Args:
        sql: SQL 文本（支持注释/多语句，自动净化）。
        dialect: sqlglot dialect（可选，如 ``"hive"`` / ``"mysql"`` / ``"doris"``）。

    Returns:
        表级血缘边列表（source -> target）；解析失败或非法 SQL 时返回空列表（降级）。
    """
    edges: list[TableEdge] = []
    seen: set[tuple[str, str]] = set()
    for stmt in _split_statements(sql):
        try:
            ast = sqlglot.parse_one(stmt, dialect=dialect)
        except Exception:
            # 非 SQL / 语法错误：跳过该语句，整体降级（不抛异常）
            continue
        for edge in _table_edges(ast):
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
        q = ast.expression
        if isinstance(q, (exp.Select, exp.Union)):
            return q
    if isinstance(ast, exp.Create) and ast.kind and ast.kind.upper() == "TABLE":
        q = ast.expression
        if isinstance(q, (exp.Select, exp.Union)):
            return q
    if isinstance(ast, exp.Merge):
        return ast.args.get("using")
    return None


def _branch_queries(query: exp.Expression) -> list[exp.Select]:
    """将源查询展开为多个 SELECT 分支（UNION 拆开，普通 SELECT 单个）。"""
    if isinstance(query, exp.Union):
        # sqlglot 的 Expression.flatten 泛化成生成器后过滤 SELECT 分支
        branches: list[Any] = list(query.flatten())
        return [b for b in branches if isinstance(b, exp.Select)]
    if isinstance(query, exp.Select):
        return [query]
    return []


def _collect_ctes(query: exp.Expression) -> dict[str, exp.CTE]:
    """收集整棵查询树中的 CTE 定义（cte 别名 -> CTE 节点），支持链式 CTE。"""
    ctes: dict[str, exp.CTE] = {}
    for node in query.walk():
        if hasattr(node, "ctes"):
            for cte in node.ctes:
                ctes[cte.alias] = cte
    return ctes


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
        # 未限定列：优先匹配真实表来源，其次匹配输出该列的子查询来源
        for _name, s in sources.items():
            if isinstance(s, exp.Table):
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


def _extract_branch_edges(
    branch: exp.Select,
    scope: Any,
    ast: exp.Expression,
    target_name: str,
    cte_map: dict[str, exp.CTE],
    dialect: str | None,
    edges: list[FieldEdge],
) -> None:
    """展开单个 SELECT 分支的投影：星号降级标记 + 常规列边 + 表达式边。"""
    for projection in getattr(branch, "selects", None) or []:
        if _projection_has_star(projection):
            edges.append(_star_edge(projection, scope, ast, target_name))
            continue
        target_col = _projection_name(projection)
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
    for when in ast.expressions:
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
        elif isinstance(then, exp.Insert) and isinstance(then.this, exp.Tuple):
            keys = getattr(then.this, "expressions", None) or []
            values = getattr(then.expression, "expressions", None) or []
            for col_expr, val_expr in zip(keys, values, strict=False):
                target_col = _column_name(col_expr)
                if not target_col:
                    continue
                is_bare = isinstance(val_expr, exp.Column)
                _emit_leaf_edges(
                    scope, val_expr, cte_map, dialect, target_name, target_col, is_bare, edges
                )


def _extract_field_edges(ast: Any, dialect: str | None) -> list[FieldEdge]:
    """单条已解析语句的字段级血缘边（含 MERGE 专用路径与星号降级标记）。"""
    target = _find_target(ast)
    target_name = _norm_table(target) if target is not None else ""
    cte_map = _collect_ctes(ast)
    edges: list[FieldEdge] = []
    if isinstance(ast, exp.Merge):
        _extract_merge_edges(ast, target_name, cte_map, dialect, edges)
        return edges
    query = _find_source_query(ast)
    if query is None:
        return edges
    for branch in _branch_queries(query):
        scope = _try_build_scope(branch)
        if scope is None:
            continue
        _extract_branch_edges(branch, scope, ast, target_name, cte_map, dialect, edges)
    return edges


def extract_field_lineage(sql: str, dialect: str | None = None) -> list[FieldEdge]:
    """抽取字段级血缘（深度解析：CTE / 子查询 / 表达式 / MERGE / UNION）。

    基于 sqlglot ``build_scope`` 递归展开作用域，将每个目标投影列解析到其真实源列
    （可跨多层）CTE 与子查询；UNION 逐分支解析。派生表达式（如 ``a.col + b.col``）
    记录到 ``expression`` 字段，并拆出多个源列边。``SELECT *`` 投影以
    ``degraded=True`` 标记降级而不产出伪字段边。

    Args:
        sql: SQL 文本（支持注释/多语句，自动净化）。
        dialect: sqlglot dialect（可选，如 ``"hive"`` / ``"doris"`` / ``"starrocks"``）。

    Returns:
        字段级血缘边列表（含降级标记）；解析不可用或失败时返回空列表（降级）。
    """
    edges: list[FieldEdge] = []
    seen: set[tuple[object, ...]] = set()
    for stmt in _split_statements(sql):
        try:
            ast = sqlglot.parse_one(stmt, dialect=dialect)
        except Exception:
            continue
        for edge in _extract_field_edges(ast, dialect):
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


def node_table(name: str) -> str:
    """构造表节点标识。"""
    return f"table:{name}"


def node_metric(code: str) -> str:
    """构造指标节点标识（L3 指标级血缘边两端统一用 ``metric:{code}``）。"""
    return f"metric:{code}"


def node_field(table: str, column: str) -> str:
    """构造字段节点标识。"""
    return f"field:{table}.{column}"
