"""指标 SQL 解析画像（基于 sqlglot，纯函数式）。

输入一段指标定义 SQL（SELECT + 聚合 + GROUP BY + 时间过滤），产出统一画像 ``SqlProfile``，
供 ``auto_fill.infer_metric`` 推断指标各字段：

- ``source_tables``: 读入源表（表级血缘 L1）
- ``group_by``: GROUP BY 维度列
- ``measures``: 度量列及聚合方式 ``{"column", "agg"}``
- ``filters``: WHERE 谓词原文（用于时间语义/新鲜度推断）
- ``time_column``: 命中的时间列（dt/date/time/日期）
- ``sql``: 原始 SQL（进入口径定义）

生产级降级：解析任意环节失败均返回空画像（不抛异常），与 lineage/parser 的降级哲学一致。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

# 时间列关键字（命中即认为是时间维度）
_TIME_COLUMN_HINTS = (
    "dt", "date", "day", "time", "month", "week", "quarter", "year", "period", "stat"
)
# 聚合函数 → 规范大写
_AGG_FUNCS = {
    "SUM",
    "AVG",
    "COUNT",
    "COUNT_DISTINCT",
    "APPROX_DISTINCT",
    "MAX",
    "MIN",
    "MEDIAN",
    "PERCENTILE",
    "PERCENTILE_APPROX",
    "LAST_VALUE",
    "FIRST_VALUE",
}


@dataclass
class SqlProfile:
    """SQL 解析画像。"""

    source_tables: list[str] = field(default_factory=list)
    group_by: list[str] = field(default_factory=list)
    measures: list[dict[str, Any]] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    time_column: str | None = None
    sql: str | None = None


def _norm_table_name(table: exp.Table) -> str:
    """将 sqlglot 表节点规范化为 ``catalog.db.table`` 形式（去引号、小写）。"""
    parts = [p.name for p in table.parts if getattr(p, "name", None)]
    return ".".join(parts).lower()


def _extract_source_tables(ast: exp.Expr) -> list[str]:
    """表级血缘：读入源表（INSERT/CREATE TABLE AS/UPDATE/MERGE 的源 + 普通 SELECT FROM）。"""
    tables: list[str] = []
    for node in ast.walk():
        if isinstance(node, exp.Table):
            # 排除被写为目标的表（INSERT INTO target）
            parent = node.parent
            if isinstance(parent, exp.Insert) and parent.this is node:
                continue
            name = _norm_table_name(node)
            if name and name not in tables:
                tables.append(name)
    return tables


def _extract_group_by(select: exp.Select) -> list[str]:
    """GROUP BY 维度列。"""
    group = select.args.get("group")
    if not group:
        return []
    cols: list[str] = []
    for expr in group.expressions:
        if isinstance(expr, exp.Column):
            cols.append(expr.name.lower())
        else:
            # 表达式分组（如 DATE_TRUNC(dt)）→ 取内部列名
            for col in expr.walk():
                if isinstance(col, exp.Column):
                    cols.append(col.name.lower())
                    break
    return cols


def _from_table(select: exp.Select) -> str | None:
    """SELECT 的 FROM 直接引用的第一个物理表（穿透子查询别名）。

    供"外层透传下沉"场景定位度量真正的来源表：聚合子查询的 FROM 常是
    ``(select ... from dwd_xxx) t1 left join (select ...) t2`` 形态，
    取最左侧来源的第一个物理表（而非 join 右侧的字典/维表）。
    """
    fr = select.args.get("from")
    if fr is None:
        return None
    this = fr.this
    if isinstance(this, exp.Table):
        return _norm_table_name(this)
    for node in this.walk():
        if isinstance(node, exp.Table):
            parent = node.parent
            if isinstance(parent, exp.Insert) and parent.this is node:
                continue
            return _norm_table_name(node)
    return None


def _projection_measures(
    select: exp.Select, enrich: bool = False, table: str | None = None
) -> list[dict[str, Any]]:
    """SELECT 投影中的度量：聚合函数包裹的列 → ``{"column", "agg"}``。

    处理 COUNT(DISTINCT x)（DISTINCT 修饰符）、COUNT(*)（星号）与
    ``count(distinct case when ... then col end)``（Case 包裹时取 then 分支列）。

    ``enrich=True`` 时（下沉场景）附加 ``alias``（投影别名）、``table``（来源表）、
    ``expression``（原始聚合投影 SQL）——区分同列不同语义的度量并还原口径。
    """
    measures: list[dict[str, Any]] = []
    for projection in select.expressions:
        if not isinstance(projection, exp.Alias) and not isinstance(projection, exp.Column):
            continue
        target = projection.this if isinstance(projection, exp.Alias) else projection
        agg = target.find(exp.AggFunc) if target else None
        if agg is None:
            continue
        agg_name = agg.key.upper()
        agg_name = "COUNT_DISTINCT" if agg_name == "APPROX_DISTINCT" else agg_name
        agg_name = "PERCENTILE" if agg_name.startswith("PERCENTILE") else agg_name
        # DISTINCT 修饰符：sqlglot 将 COUNT(DISTINCT x) 解析为 Count(this=Distinct(...))
        col_expr = agg.this
        if isinstance(col_expr, exp.Distinct):
            agg_name = "COUNT_DISTINCT"
            inner = col_expr.expressions[0] if col_expr.expressions else None
            if isinstance(inner, (exp.Column, exp.Case)):
                col_expr = inner
        if isinstance(col_expr, exp.Star):
            col_name = "*"
        elif isinstance(col_expr, exp.Column):
            col_name = col_expr.name.lower()
        elif isinstance(col_expr, exp.Case):
            # count(distinct case when ... then col end) → 取 then 分支列
            col_name = next(
                (c.name.lower() for c in col_expr.walk() if isinstance(c, exp.Column)),
                "*",
            )
        else:
            col_name = "*"
        measure: dict[str, Any] = {"column": col_name, "agg": agg_name}
        if enrich:
            measure["alias"] = (
                projection.alias_or_name if isinstance(projection, exp.Alias) else None
            )
            measure["table"] = table
            measure["expression"] = target.sql()
        measures.append(measure)
    return measures


def _extract_measures(select: exp.Select) -> list[dict[str, Any]]:
    """SELECT 投影度量；外层无聚合时下沉 FROM 子树找聚合投影。

    ETL 落宽表常见 ``insert overwrite ... select a.col1, a.cnt ... from (聚合子查询) a``
    透传形态——最外层投影只是改名/join 字典，聚合在子查询内。此时下沉收集聚合
    投影的度量，按 ``(alias, agg)`` 去重（UNION 多支同指标合并），并附带
    ``alias/table/expression`` 供候选构建区分同列不同语义并还原口径。
    """
    measures = _projection_measures(select)
    if measures:
        return measures
    seen: set[tuple[str, str]] = set()
    for sub in select.find_all(exp.Select):
        if sub is select:
            continue
        for m in _projection_measures(sub, enrich=True, table=_from_table(sub)):
            key = (m["alias"] or m["column"], m["agg"])
            if key in seen:
                continue
            seen.add(key)
            measures.append(m)
    return measures


def _extract_filters(select: exp.Select) -> list[str]:
    """WHERE 谓词原文列表。"""
    where = select.args.get("where")
    if where is None:
        return []
    predicates: list[str] = []
    for node in where.walk():
        if isinstance(node, exp.Paren):
            continue
        if isinstance(
            node, (exp.EQ, exp.GT, exp.LT, exp.GTE, exp.LTE, exp.NEQ, exp.In, exp.Between)
        ):
            with contextlib.suppress(Exception):
                predicates.append(node.sql().strip())
    return predicates


def _detect_time_column(group_by: list[str], filters: list[str]) -> str | None:
    """从 GROUP BY 与时间谓词中识别时间列。"""
    haystack = " ".join(group_by + filters).lower()
    for hint in _TIME_COLUMN_HINTS:
        if hint in haystack:
            # 优先返回 group_by 中的时间列
            for g in group_by:
                if hint in g:
                    return g
            return hint
    return None


def parse_sql_profile(sql: str) -> SqlProfile:
    """解析指标 SQL 为画像。

    解析失败（语法错误/方言不支持）返回空画像，不抛异常。

    Args:
        sql: 指标定义 SQL。

    Returns:
        SqlProfile
    """
    if not sql or not sql.strip():
        return SqlProfile()
    try:
        ast = sqlglot.parse_one(sql, error_level=sqlglot.ErrorLevel.RAISE)
    except Exception:
        return SqlProfile(sql=sql.strip())

    select = ast
    if isinstance(ast, (exp.Insert, exp.Create, exp.Update, exp.Merge)):
        # 取源查询（CTAS / INSERT INTO ... SELECT）
        sub = ast.find(exp.Select)
        if sub is None:
            return SqlProfile(sql=sql.strip())
        select = sub
    if not isinstance(select, exp.Select):
        return SqlProfile(sql=sql.strip())

    source_tables = _extract_source_tables(ast)
    group_by = _extract_group_by(select)
    measures = _extract_measures(select)
    filters = _extract_filters(select)
    time_column = _detect_time_column(group_by, filters)
    return SqlProfile(
        source_tables=source_tables,
        group_by=group_by,
        measures=measures,
        filters=filters,
        time_column=time_column,
        sql=sql.strip(),
    )


def profile_to_dict(profile: SqlProfile) -> dict[str, Any]:
    """转为普通 dict（便于 JSON 序列化 / endpoint 透传）。"""
    return {
        "source_tables": profile.source_tables,
        "group_by": profile.group_by,
        "measures": profile.measures,
        "filters": profile.filters,
        "time_column": profile.time_column,
        "sql": profile.sql,
    }
