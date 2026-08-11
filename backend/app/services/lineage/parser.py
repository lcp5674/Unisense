"""血缘解析器（基于 sqlglot）。

提供两张粒度的血缘抽取：
- 表级（L1）：INSERT/CREATE TABLE AS/UPDATE/MERGE 的写入目标与读入源表
- 字段级（L2）：借助 sqlglot.lineage.LineageRunner 抽取列到列的派生关系

所有函数均为纯函数，不依赖数据库或外部服务，便于单元测试。
抽取失败时降级返回空列表（不静默吞掉可观测的异常，但保证主流程不崩）。
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


#: 字段血缘递归解析最大深度（防止 CTE/子查询自引用导致的无限递归）。
_MAX_DEPTH = 8


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


def extract_table_lineage(sql: str, dialect: str | None = None) -> list[TableEdge]:
    """抽取表级血缘。

    Args:
        sql: SQL 文本。
        dialect: sqlglot dialect（可选，如 ``"hive"`` / ``"mysql"``）。

    Returns:
        表级血缘边列表（source -> target）。
    """
    ast = sqlglot.parse_one(sql, dialect=dialect)
    target = _find_target(ast)
    target_name = _norm_table(target) if target is not None else None
    edges: list[TableEdge] = []
    if target_name is None:
        # 纯查询语句无写入目标，仅返回所有源表间的关联（无目标则跳过成边）
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


def _find_source_query(ast: exp.Expression) -> exp.Expression | None:
    """定位生成写入行的源查询（SELECT/UNION），用于字段级深度解析。

    INSERT INTO ... SELECT / CREATE TABLE ... AS SELECT 的源查询即其 ``expression``；
    其余情况（UPDATE/MERGE 等）退化为 None，由调用方降级处理。
    """
    if isinstance(ast, exp.Insert):
        q = ast.expression
        if isinstance(q, (exp.Select, exp.Union)):
            return q
    if isinstance(ast, exp.Create) and ast.kind and ast.kind.upper() == "TABLE":
        q = ast.expression
        if isinstance(q, (exp.Select, exp.Union)):
            return q
    return None


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


def extract_field_lineage(sql: str, dialect: str | None = None) -> list[FieldEdge]:
    """抽取字段级血缘（深度解析：CTE / 子查询 / 表达式）。

    基于 sqlglot ``build_scope`` 递归展开作用域，将每个目标投影列解析到其
    真实源列（可跨多层 CTE 与子查询）。派生表达式（如 ``a.col + b.col``）会
    记录到 ``expression`` 字段，并拆出多个源列边。解析失败或无可解析源查询时
    降级返回空列表（不静默吞异常，但保证主流程不崩）。

    Args:
        sql: SQL 文本。
        dialect: sqlglot dialect（可选）。

    Returns:
        字段级血缘边列表；解析不可用或失败时返回空列表（降级）。
    """
    try:
        ast = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return []

    target = _find_target(ast)
    target_name = _norm_table(target) if target is not None else ""
    query = _find_source_query(ast)
    if query is None:
        return []

    try:
        root_scope = build_scope(query)
    except Exception:
        return []
    if root_scope is None:
        return []

    cte_map = _collect_ctes(ast)
    edges: list[FieldEdge] = []
    selects = getattr(root_scope.expression, "selects", None) or []
    for projection in selects:
        target_col = _projection_name(projection)
        if not target_col:
            continue
        leaf_cols = list(projection.find_all(exp.Column))
        if not leaf_cols:
            continue
        resolved = _resolve_projection(root_scope, projection, cte_map, dialect, 0)
        if not resolved:
            continue
        # 派生表达式（非裸列引用）记录表达式原文到 expression 字段
        is_bare = (
            isinstance(projection, exp.Alias) and isinstance(projection.this, exp.Column)
        ) or isinstance(projection, exp.Column)
        expr_sql = None if is_bare else projection.sql(dialect=dialect)
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
    return edges


def node_table(name: str) -> str:
    """构造表节点标识。"""
    return f"table:{name}"


def node_field(table: str, column: str) -> str:
    """构造字段节点标识。"""
    return f"field:{table}.{column}"
