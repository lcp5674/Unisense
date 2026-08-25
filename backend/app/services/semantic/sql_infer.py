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
# 截断表达式长度 → 统计粒度（substr(x,1,7)=YYYY-MM 月；1,6=YYYYMM 月；1,4=YYYY 年）
_SUBSTR_LENGTH_GRAIN = {7: "month", 6: "month", 5: "month", 4: "year"}
# date_format 格式化模式 → 统计粒度（含 Hive 小写 yyyy/MM/dd 变体）
_DATE_FORMAT_GRAIN = (
    ("%y-%m-%d", "day"), ("%y%m%d", "day"), ("yyyy-mm-dd", "day"), ("yyyymmdd", "day"),
    ("%y-%m", "month"), ("%y%m", "month"), ("yyyy-mm", "month"), ("yyyymm", "month"),
    ("%y", "year"), ("yyyy", "year"),
    ("%y-%w", "week"), ("%y-%u", "week"),
)
# 投影别名/列名 → 统计粒度（比 _TIME_COLUMN_HINTS 更明确，如 month_id 优先于 create_date）
_TIME_GRAIN_ALIAS = (
    ("month_id", "month"), ("stat_month", "month"), ("biz_month", "month"), ("mon", "month"),
    ("week_id", "week"), ("stat_week", "week"), ("wk", "week"),
    ("quarter_id", "quarter"), ("stat_quarter", "quarter"), ("qtr", "quarter"),
    ("year_id", "year"), ("stat_year", "year"), ("yr", "year"),
    ("hour_id", "hour"), ("stat_hour", "hour"),
    ("day_id", "day"), ("stat_date", "day"),
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
    # 截断/别名表达的明确时间粒度（substr(x,1,7) as month_id → "month"），
    # 比 time_column 的模糊 token 更可靠——period 推断优先用它
    time_granularity: str | None = None
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


# 已知统计粒度（date_trunc unit / LLM 结果白名单）
_KNOWN_GRAINS = {"day", "week", "month", "quarter", "year", "hour"}


def _expr_time_grain(node: exp.Expr, alias: str | None = None) -> str | None:
    """单个投影/分组表达式 → 明确时间粒度（截断/格式化/别名）；无信号返回 None。"""
    if alias:
        low = alias.lower()
        for token, grain in _TIME_GRAIN_ALIAS:
            if token in low:
                return grain
    # 裸列（如 group by month_id）→ 按列名匹配粒度别名
    if isinstance(node, exp.Column):
        low = node.name.lower()
        for token, grain in _TIME_GRAIN_ALIAS:
            if token in low:
                return grain
        return None
    # substr(x,1,7)/substring(x,1,7)：按截断长度（7=YYYY-MM 月、6=YYYYMM 月、4=YYYY 年）
    if isinstance(node, exp.Substring):
        length = node.args.get("length")
        if isinstance(length, exp.Literal):
            with contextlib.suppress(ValueError):
                return _SUBSTR_LENGTH_GRAIN.get(int(length.this))
    # date_trunc('month', x)：按 unit
    if isinstance(node, exp.DateTrunc):
        unit = node.args.get("unit")
        if isinstance(unit, exp.Literal):
            grain = str(unit.this).lower()
            return grain if grain in _KNOWN_GRAINS else None
    # date_format(x, '%Y-%m')（默认方言解析为 Anonymous）：按 format 模式
    if isinstance(node, exp.Anonymous) and str(node.this).upper() == "DATE_FORMAT":
        args = node.expressions
        if len(args) >= 2 and isinstance(args[1], exp.Literal):
            fmt = str(args[1].this).lower()
            for pattern, grain in _DATE_FORMAT_GRAIN:
                if pattern in fmt:
                    return grain
    # date(x) 截断到日
    if isinstance(node, exp.Date):
        return "day"
    return None


def _detect_time_granularity(select: exp.Select) -> str | None:
    """从投影别名 / 投影表达式 / GROUP BY 表达式识别明确时间粒度。

    ETL 常见 ``substr(create_date,1,7) as month_id`` / ``date_trunc('month', x)``
    等——表达式把时间列截断到固定粒度，直接决定指标统计周期，比
    ``_detect_time_column`` 的列名 token 更可靠。优先级：投影别名 > 投影表达式
    > GROUP BY 表达式；最外层透传无信号时下沉 FROM 子树（ETL 透传 INSERT 的
    聚合子查询，对齐 ``_extract_measures`` 下沉逻辑）。无信号返回 None。
    """

    def _scan(s: exp.Select) -> str | None:
        for proj in s.expressions:
            if isinstance(proj, exp.Alias):
                grain = _expr_time_grain(proj.this, alias=proj.alias_or_name)
            else:
                grain = _expr_time_grain(proj)
            if grain:
                return grain
        group = s.args.get("group")
        if group:
            for expr in group.expressions:
                grain = _expr_time_grain(expr)
                if grain:
                    return grain
        return None

    grain = _scan(select)
    if grain:
        return grain
    for sub in select.find_all(exp.Select):
        if sub is select:
            continue
        grain = _scan(sub)
        if grain:
            return grain
    return None


def _detect_time_column(
    select: exp.Select | None,
    group_by: list[str],
    filters: list[str],
) -> str | None:
    """从 GROUP BY / 投影别名 / 时间谓词中识别时间列。

    优先返回明确的粒度列（month_id/week_id 等，比 create_date 更能确定周期）；
    其次投影别名里的时间列（``substr(create_date,1,7) as month_id`` 在 group_by
    中只体现底层列 create_date，别名承载真实时间语义）；最后回退 GROUP BY +
    谓词的 hint 匹配（原逻辑）。
    """
    # 1) GROUP BY 中明确的粒度列（month_id/week_id/stat_date 等，比 create_date 更能确定周期）
    for g in group_by:
        gl = g.lower()
        if any(token in gl for token, _ in _TIME_GRAIN_ALIAS):
            return g
    # 2) 投影别名中的时间列
    if select is not None:
        for proj in select.expressions:
            if isinstance(proj, exp.Alias):
                alias = proj.alias_or_name
                if alias and any(h in alias.lower() for h in _TIME_COLUMN_HINTS):
                    return alias
    # 3) GROUP BY + 谓词 hint（原逻辑）
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
    time_column = _detect_time_column(select, group_by, filters)
    time_granularity = _detect_time_granularity(select)
    return SqlProfile(
        source_tables=source_tables,
        group_by=group_by,
        measures=measures,
        filters=filters,
        time_column=time_column,
        time_granularity=time_granularity,
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
        "time_granularity": profile.time_granularity,
        "sql": profile.sql,
    }
