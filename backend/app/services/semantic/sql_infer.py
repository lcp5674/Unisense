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
import re
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
# 聚合函数 → 规范大写（含窗口/首末值函数；实际解析名见 _agg_display_name）。
# 注意 sqlglot 的 key 无下划线（FIRST_VALUE → FIRSTVALUE），此处保留注册
# schema 的带下划线规范名作为「推断产物 → 注册枚举」对齐的权威清单。
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

# 工业场景常用 SQL 方言（默认方言失败时依次尝试解析；Hadoop/Doris 系在前，
# 与血缘解析器方言策略一致）。sqlglot 25.x 内置：clickhouse/oracle/trino/
# presto/postgres/mysql/tsql/snowflake/bigquery/redshift/databricks/impala/
# teradata/athena/duckdb/materialize/risingwave 等。
_INDUSTRIAL_DIALECTS = (
    "hive",
    "spark",
    "starrocks",
    "doris",
    "clickhouse",
    "oracle",
    "trino",
    "presto",
    "postgres",
    "mysql",
    "tsql",
    "snowflake",
    "bigquery",
    "redshift",
    "databricks",
    "impala",
    "teradata",
    "athena",
    "duckdb",
    "materialize",
    "risingwave",
)

# ClickHouse 合并/条件聚合函数 → 规范聚合（sumMerge/sumIf → SUM 等）
_COMBINED_AGG_MAP = {
    "summerge": "SUM",
    "sumif": "SUM",
    "sumdistinct": "SUM",
    "avgmerge": "AVG",
    "avgif": "AVG",
    "countmerge": "COUNT",
    "countif": "COUNT",
    "maxmerge": "MAX",
    "minmerge": "MIN",
}

# 方言聚合函数名（AnonymousAggFunc/ParameterizedAgg 的 this 字符串）→ 注册枚举聚合。
# 崩溃修复（P0-A）后这些函数能正常产出候选；不归一的话聚合值（QUANTILE/ARGMAX/
# GROUPCONCAT）不在 MetricCreateRequest.aggregation Literal → 批量创建 pydantic
# 整批失败（P1-4 一致性缺陷）。映射到语义最接近的注册枚举：
# - uniqExact/uniq → COUNT_DISTINCT（UV 语义）
# - quantile/quantileExact/percentile_approx → PERCENTILE（分位数）
# - argMax/argMin → MAX/MIN（参数化首/末值）
# - topK/groupArray/stringAgg/groupConcat → COUNT（数组/串聚合，近似计数语义，
#   候选 source=dialect 前端提示人工复核）
_DIALECT_AGG_MAP = {
    "uniqexact": "COUNT_DISTINCT",
    "uniq": "COUNT_DISTINCT",
    "quantile": "PERCENTILE",
    "quantileexact": "PERCENTILE",
    "percentileapprox": "PERCENTILE",
    "approxquantile": "PERCENTILE",
    "argmax": "MAX",
    "argmin": "MIN",
    "topk": "COUNT",
    "topkweighted": "COUNT",
    "grouparray": "COUNT",
    "groupconcat": "COUNT",
    "stringagg": "COUNT",
    "listagg": "COUNT",
    "anyif": "COUNT",
}
# 无注册枚举可归一的统计聚合（相关性/协方差/回归/标准差/方差）→ 返回 None 跳过
# 该度量（诚实不产出非法候选，避免注册失败/口径错误），不崩溃不炸整批。
_DIALECT_AGG_SKIP = {
    "corr", "covar_pop", "covar_samp", "regr_slope", "regr_r2", "regr_intercept",
    "stddev", "stddevpop", "stddevsamp", "stddev_samp", "varpop", "varsamp",
    "var_pop", "var_samp", "variance",
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
    """GROUP BY 维度列（含位置序号 ``GROUP BY 1, 2`` → 映射回 SELECT 投影列名）。"""
    group = select.args.get("group")
    if not group:
        return []
    cols: list[str] = []
    projections = select.expressions
    for expr in group.expressions:
        # 位置序号 GROUP BY 1 → SELECT 投影第 1 列（Postgres/Trino/Oracle 惯用写法；
        # sqlglot 解析为 Literal 数字，非 Column，需按投影下标回映列名）
        if isinstance(expr, exp.Literal) and not expr.is_string:
            with contextlib.suppress(ValueError):
                idx = int(expr.this) - 1
                if 0 <= idx < len(projections):
                    proj = projections[idx]
                    if isinstance(proj, exp.Alias):
                        cols.append(proj.alias_or_name.lower())
                    elif isinstance(proj, exp.Column):
                        cols.append(proj.name.lower())
                    else:
                        # 表达式投影（如 date_trunc(...) as month_id）→ 取内部列名
                        for col in proj.walk():
                            if isinstance(col, exp.Column):
                                cols.append(col.name.lower())
                                break
                    continue
            continue
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


def _table_alias_map(select: exp.Select) -> dict[str, str]:
    """SELECT FROM/JOIN 的别名 → 物理表映射（A-3 源表归属用）。

    ``FROM dwd.orders a LEFT JOIN dwd.ref b ON ...`` → ``{a: dwd.orders, b: dwd.ref}``；
    无别名裸表 ``FROM orders`` → ``{orders: orders}``。sqlglot 把 join 右侧表也放入
    ``find_all(exp.Table)``，故能完整收集两侧别名。
    """
    mapping: dict[str, str] = {}
    for node in select.find_all(exp.Table):
        parent = node.parent
        if isinstance(parent, exp.Insert) and parent.this is node:
            continue
        alias = (node.alias or node.name or "").lower()
        if alias:
            mapping[alias] = _norm_table_name(node)
    return mapping


def _measure_table(select: exp.Select, agg: exp.AggFunc) -> str | None:
    """聚合投影的度量来源物理表：按列前缀归属（A-3 join 同名列区分）。

    ``sum(a.amount), sum(b.amount)`` 的候选此前都取 ``_from_table`` 的第一个物理表
    ——b 侧口径错挂 a 表。此处从聚合参数列的表前缀（``a.amount`` → 别名 a）解析
    对应物理表；无前缀/未命中返回 None（上层回退通用来源表）。
    """
    col = agg.this
    if isinstance(col, exp.Distinct):
        col = col.expressions[0] if col.expressions else None
    if isinstance(col, exp.Column) and col.table:
        return _table_alias_map(select).get(col.table.lower())
    if isinstance(col, exp.Case):
        for c in col.walk():
            if isinstance(c, exp.Column) and c.table:
                return _table_alias_map(select).get(c.table.lower())
    return None


def _agg_display_name(agg: exp.AggFunc) -> str | None:
    """聚合节点 → 规范聚合名（含工业方言聚合归一）；不支持的方言聚合返回 None。

    ClickHouse ``sumMerge/sumIf`` 解析为 ``CombinedAggFunc``（``this`` 是函数名
    字符串）→ 按映射归一为 ``SUM`` 等；``countIf`` → ``COUNT``；Trino/Presto
    ``approx_distinct`` → ``COUNT_DISTINCT``（key 无下划线，与既有映射对齐）；
    ``uniqExact/quantile/topK`` 等解析为 ``AnonymousAggFunc``/``ParameterizedAgg``
    （``this`` 是函数名）→ 按 ``_DIALECT_AGG_MAP`` 归一；无枚举可归一的统计聚合
    （corr/stddev/var 等）→ 返回 ``None``（上层跳过该度量，不产出非法候选）。
    """
    key = agg.key.upper()
    if key == "COMBINEDAGGFUNC":
        fn = str(agg.this).lower() if isinstance(agg.this, str) else ""
        return _COMBINED_AGG_MAP.get(fn, "SUM")
    if key == "COUNTIF":
        return "COUNT"
    if key == "APPROXDISTINCT":
        return "COUNT_DISTINCT"
    # 方言聚合：this 是函数名字符串 → 按函数名归一（corr/stddev/var 等无枚举可归
    # 一 → None 跳过该度量，避免产出非法候选导致批量创建整批失败）
    if isinstance(agg.this, str):
        fn = agg.this.lower()
        if fn in _DIALECT_AGG_SKIP:
            return None
        return _DIALECT_AGG_MAP.get(fn, fn.upper())
    # 首/末值窗口函数：sqlglot key 无下划线（FIRSTVALUE），归一为注册 schema 的
    # 带下划线枚举（MetricCreateRequest.aggregation Literal）——否则批量创建时
    # 候选聚合 FIRSTVALUE 不匹配枚举 → pydantic 校验整批失败（P1-4 一致性缺陷）。
    if key in ("FIRSTVALUE", "FIRST_VALUE"):
        return "FIRST_VALUE"
    if key in ("LASTVALUE", "LAST_VALUE"):
        return "LAST_VALUE"
    if key.startswith("PERCENTILE"):
        return "PERCENTILE"
    # 方言统计聚合专用类（Quantile/Corr/Stddev 等，this 是 Column 非字符串）：
    # 统一按函数名归一/跳过——quantile → PERCENTILE，corr/stddev/var → None 跳过
    # （无注册枚举可归一的统计聚合，诚实不产出非法候选）
    low = key.lower()
    if low in _DIALECT_AGG_SKIP:
        return None
    return _DIALECT_AGG_MAP.get(low, key)


def _extract_col_name(node: exp.Expr | None) -> str:
    """从聚合参数表达式提取度量列名。

    裸列 → 列名；``*`` → ``*``；``DISTINCT x`` → x；``CASE WHEN`` → then 分支列；
    复杂表达式（``COALESCE``/``nvl``/算术/条件/方言函数）→ 取内部第一个
    Column（如 Oracle ``sum(nvl(amount,0))`` → ``amount``）；无列可提取 → ``*``。

    **P0-A 兜底**：方言聚合（ClickHouse ``uniqExact``/``topK``、Trino
    ``approx_distinct`` 等）解析为 ``AnonymousAggFunc``/``ParameterizedAgg`` 时
    ``agg.this`` 是**函数名字符串**——直接对 str 调用 ``.walk()`` 会抛
    ``AttributeError``，使 ``infer_sql_batch`` 整批 500（工业 DAU/UV 常用函数必炸）。
    """
    if isinstance(node, str):
        return "*"
    if node is None:
        return "*"
    if isinstance(node, exp.Star):
        return "*"
    if isinstance(node, exp.Column):
        return node.name.lower()
    if isinstance(node, exp.Distinct):
        inner = node.expressions[0] if node.expressions else None
        return _extract_col_name(inner)
    if isinstance(node, exp.Case):
        return next(
            (c.name.lower() for c in node.walk() if isinstance(c, exp.Column)), "*"
        )
    first = next(
        (c.name.lower() for c in node.walk() if isinstance(c, exp.Column)), None
    )
    return first or "*"


def _projection_measures(
    select: exp.Select,
    enrich: bool = False,
    table: str | None = None,
    sunk: bool = False,
) -> list[dict[str, Any]]:
    """SELECT 投影中的度量：聚合函数包裹的列 → ``{"column", "agg"}``。

    处理 COUNT(DISTINCT x)（DISTINCT 修饰符）、COUNT(*)（星号）与
    ``count(distinct case when ... then col end)``（Case 包裹时取 then 分支列）。

    **P0-B**：支持**无别名裸聚合投影**（``SELECT sum(amount) FROM t`` 的投影是
    ``exp.Sum``，非 ``Alias``/``Column``）——ETL 最普遍写法此前被直接跳过导致
    measures=0；裸聚合用 sqlglot 生成投影别名，与下沉/LLM 兜底候选结构对齐。

    **P0-A**：方言聚合（``AnonymousAggFunc``/``ParameterizedAgg``）的 ``this`` 是
    函数名字符串，列参数在 ``expressions``——取其中第一个 Column 作为度量列
    （``uniqExact(user_id)`` → user_id）；``_agg_display_name`` 返回 ``None`` 的
    统计聚合（corr/stddev/var 等）→ 跳过该投影（不产出非法候选）。

    ``enrich=True`` 时（下沉场景）附加 ``alias``（投影别名）、``table``（来源表）、
    ``expression``（原始聚合投影 SQL）——区分同列不同语义的度量并还原口径。
    """
    measures: list[dict[str, Any]] = []
    for projection in select.expressions:
        target = projection.this if isinstance(projection, exp.Alias) else projection
        # 裸聚合投影（P0-B）：非 Alias/Column 但本身是聚合函数（sum/count 等）
        if not isinstance(projection, exp.Alias) and not isinstance(projection, exp.Column):
            if not isinstance(target, exp.AggFunc):
                continue
            projection = exp.alias_(target, f"_col_{len(measures) + 1}")
        agg = target.find(exp.AggFunc) if target else None
        if agg is None:
            continue
        agg_name = _agg_display_name(agg)
        if agg_name is None:
            # 无注册枚举可归一的统计聚合 → 跳过该投影（诚实不产出非法候选）
            continue
        # DISTINCT 修饰符：sqlglot 将 COUNT(DISTINCT x) 解析为 Count(this=Distinct(...))
        col_expr = agg.this
        if isinstance(col_expr, exp.Distinct):
            agg_name = "COUNT_DISTINCT"
            col_expr = col_expr.expressions[0] if col_expr.expressions else None
        # 方言聚合（P0-A）：this 是函数名字符串，真正列参数在 expressions——
        # 取第一个 Column（uniqExact(user_id) → user_id；topK(10)(product) 跳过
        # Literal 取 product；sumMerge(amount_state) → amount_state）
        if isinstance(agg.this, str):
            col_expr = next(
                (
                    e for e in (agg.expressions or [])
                    if isinstance(e, (exp.Column, exp.Alias))
                ),
                (agg.expressions[0] if agg.expressions else None),
            )
        # ClickHouse 合并/条件聚合：this 是函数名字符串，真正参数在 expressions[0]
        if isinstance(agg, exp.CombinedAggFunc):
            col_expr = agg.expressions[0] if agg.expressions else None
        col_name = _extract_col_name(col_expr)
        measure: dict[str, Any] = {"column": col_name, "agg": agg_name}
        if enrich:
            measure["alias"] = (
                projection.alias_or_name if isinstance(projection, exp.Alias) else None
            )
            # A-3：join 同名列按列前缀归属物理表（sum(a.amount)/sum(b.amount) 分别
            # 归属 a/b 表）；无前缀/未命中回退传入的 table
            measure["table"] = _measure_table(select, agg) or table
            # A-4 下沉标记：候选构建据此区分「下沉子查询度量」（同列多语义用 alias
            # 作编码锚点）vs「顶层投影度量」（编码用真实列，alias 仅为投影别名）
            measure["sunk"] = sunk
            # 原始投影 SQL（A-1/2：CASE/窗口/表达式口径完整保留，替代简化 SUM(col)）。
            # 方言聚合（ClickHouse sumMerge 的 CombinedAggFunc 等）在默认方言下
            # ``target.sql()`` 序列化会抛异常（sqlglot 方言序列化 bug）→ 降级简化式，
            # 绝不因序列化失败让整段解析崩溃（对齐生产降级哲学）。
            try:
                measure["expression"] = target.sql()
            except Exception:  # noqa: BLE001 - 序列化失败仅降级简化式
                measure["expression"] = f"{agg_name}({col_name})"
        measures.append(measure)
    return measures


def _extract_measures(select: exp.Select) -> list[dict[str, Any]]:
    """SELECT 投影度量；外层无聚合时下沉 FROM 子树找聚合投影。

    ETL 落宽表常见 ``insert overwrite ... select a.col1, a.cnt ... from (聚合子查询) a``
    透传形态——最外层投影只是改名/join 字典，聚合在子查询内。此时下沉收集聚合
    投影的度量，按 ``(alias, agg)`` 去重（UNION 多支同指标合并），并附带
    ``alias/table/expression`` 供候选构建区分同列不同语义并还原口径。

    **A-1/2**：顶层投影也走 ``enrich=True`` 携带原始 ``expression``（如
    ``SUM(CASE WHEN status='paid' THEN amount END)``/``SUM(amount) OVER (...``）——
    此前顶层候选由 ``_build_atomic_candidate`` 用简化 ``SUM(col)`` 还原口径，
    CASE 过滤条件/窗口语义被丢弃，注册后指标变全表聚合（数据错误）。
    顶层不传 ``table``（候选的源表由 ``_physical_source_tables`` 过滤 CTE 别名后
    决定，避免顶层 measure 误挂 CTE 名）且 ``sunk=False``（编码锚点用真实列）。
    """
    measures = _projection_measures(select, enrich=True, table=None, sunk=False)
    # A-4：全局子查询投影别名 → 物理列映射（聚合参数是子查询/CTE 投影别名时解析为
    # 底层物理列——``SUM(x) FROM (SELECT amount AS x ...)`` 的 source_fields 若不
    # 解析会落 ``[orders, x]``（物理表+不存在的列），血缘/下游错乱）。仅映射
    # Alias(Column) 简单透传，复杂表达式保留别名。
    alias_map: dict[str, str] = {}
    for sub in select.find_all(exp.Select):
        if sub is select:
            continue
        for proj in sub.expressions:
            if (
                isinstance(proj, exp.Alias)
                and isinstance(proj.this, exp.Column)
                and proj.alias_or_name
            ):
                alias_map[proj.alias_or_name.lower()] = proj.this.name.lower()
    if measures:
        if alias_map:
            for m in measures:
                if m["column"] in alias_map:
                    m["column"] = alias_map[m["column"]]
        return measures
    seen: set[tuple[str, str]] = set()
    for sub in select.find_all(exp.Select):
        if sub is select:
            continue
        for m in _projection_measures(
            sub, enrich=True, table=_from_table(sub), sunk=True
        ):
            if alias_map and m["column"] in alias_map:
                m["column"] = alias_map[m["column"]]
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


# 方言特有聚合函数名（文本命中才遍历方言择优——避免普通 SQL 每条全方言解析的性能损耗）
_DIALECT_AGG_HINT = re.compile(
    r"\b(summerge|sumif|sumdistinct|avgmerge|avgif|countif|countmerge|maxmerge|"
    r"minmerge|approx_distinct|approx_count_distinct|percentile_approx|uniq|uniqexact|"
    r"quantile|grouparray|topk|anyif|corr|covar_pop|regr_slope|stddev|var_pop)\b",
    re.IGNORECASE,
)


def _best_dialect_ast(
    sql: str, baseline: exp.Expr | None
) -> exp.Expr | None:
    """按「聚合识别数最大化」从工业方言中选解析结果（默认方言为基线）。

    方言对同一 SQL 的解析「宽松度」不同：ClickHouse ``sumMerge/sumIf`` 在
    hive/spark 等方言下降级为 ``Anonymous``（非 AggFunc），仅 clickhouse 方言
    识别为 ``CombinedAggFunc``——只取首个可解析方言会丢失度量。故比较各方言
    解析出的 ``AggFunc`` 数量，选识别最充分的 AST；数量未超过基线则保留基线
    （普通 SQL 方言识别度相同 → 行为不变）。

    **多语句脚本**：方言下用 ``parse``（复数）拆分全部语句，选「含 Select 且
    聚合识别最多」的产出语句（ETL 的 ``set`` + ``create table`` DDL +
    ``insert overwrite ... select`` 形态）——``parse_one`` 只取第一条（常是
    ``Set``/``Command``）会丢度量；与 ``_parse_profile_ast`` 的多语句语义对齐，
    并覆盖默认方言 parse 失败的方言 DDL（如 ``comment "中文"`` 列注释）兜底。
    """
    best = baseline
    best_count = len(list(best.find_all(exp.AggFunc))) if best is not None else 0
    for dialect in _INDUSTRIAL_DIALECTS:
        try:
            stmts = sqlglot.parse(
                sql, dialect=dialect, error_level=sqlglot.ErrorLevel.RAISE
            )
        except Exception:
            continue
        if not stmts:
            continue
        for stmt in stmts:
            if stmt is None or stmt.find(exp.Select) is None:
                continue
            count = len(list(stmt.find_all(exp.AggFunc)))
            if count > best_count:
                best, best_count = stmt, count
    return best


def _parse_profile_ast(sql: str) -> exp.Expr | None:
    """解析 SQL 为 AST；多语句脚本拆分 + 默认方言失败/识别不全时用工业方言 + Doris 预处理兜底。

    生产 ETL 常用方言语法（Doris/StarRocks 的 ``CREATE TABLE ... DISTRIBUTED BY
    ... AS SELECT``、ClickHouse ``MergeTree`` 建表 + ``sumMerge`` 聚合、Oracle
    ``trunc(x,'MM')``、Trino ``date_trunc``、T-SQL ``CONVERT`` 等）sqlglot 默认
    方言可能整句降级为 ``Command``（无 Select 子树）或把方言聚合降级为
    ``Anonymous`` → 画像度量缺失 → 批量解析候选减少。解析策略（代价递增）：
    0. 多语句脚本（``set`` 参数 + ``create table`` DDL + ``insert overwrite ... select``）：
       ``parse_one`` 只返回第一条语句（常是 ``Set``/``Command``，无 Select 子树）→
       画像空。改用 ``parse``（复数）拆分全部语句，选「含 Select 且聚合识别最多」
       的产出语句（度量 SELECT 通常在脚本最末的 INSERT 中）——与 ``sql_split``
       批量切分语义对齐；
    1. 默认方言解析，有 Select 子树 → 文本含方言聚合函数名时再遍历方言择优
       （``_best_dialect_ast``，避免 sumMerge 等被宽松方言降级丢度量），否则直接采用；
    2. 默认方言无 Select 子树 → 遍历工业方言选聚合识别最多的 AST；
    3. 复用血缘解析器 ``_preprocess_dialect``（剥离物理分布/副本属性等与口径无关
       的子句）后按默认方言重试——口径语义不变；
    全部失败返回 ``None``（上层降级空画像，不抛异常）。
    """
    # 多语句脚本：parse 拆分后选含 Select + 聚合识别最多的产出语句（先于 parse_one，
    # 否则 set/create DDL 开头的 ETL SQL 直接退化为空画像——真实生产 SQL 常见形态）
    try:
        stmts = sqlglot.parse(sql, error_level=sqlglot.ErrorLevel.RAISE)
        if len(stmts) > 1:
            best_stmt: exp.Expr | None = None
            best_count = 0
            for stmt in stmts:
                if stmt is None or stmt.find(exp.Select) is None:
                    continue
                count = len(list(stmt.find_all(exp.AggFunc)))
                # 聚合数相同取更靠后的语句（ETL 的 insert overwrite ... select 是产出语句）
                if count >= best_count:
                    best_stmt, best_count = stmt, count
            if best_stmt is not None and best_count > 0:
                return best_stmt
    except Exception:
        pass  # 多语句拆分失败 → 走单语句路径
    ast: exp.Expr | None = None
    try:
        ast = sqlglot.parse_one(sql, error_level=sqlglot.ErrorLevel.RAISE)
    except Exception:
        ast = None
    if ast is not None and ast.find(exp.Select) is not None:
        # 默认方言成功：仅当文本疑似含方言特有聚合函数时才遍历方言择优（性能护栏）
        if _DIALECT_AGG_HINT.search(sql.lower()):
            return _best_dialect_ast(sql, baseline=ast)
        return ast
    # 默认方言无 Select 子树（降级 Command / 方言 DDL）→ 工业方言选聚合识别最多的
    best = _best_dialect_ast(sql, baseline=None)
    if best is not None:
        return best
    # Doris/StarRocks 方言语法（CTAS 物理属性等）→ 剥离后重试（懒导入避免模块耦合）
    try:
        from app.services.lineage.parser import _preprocess_dialect

        pre = _preprocess_dialect(sql, "doris")
        if pre != sql:
            return sqlglot.parse_one(pre, error_level=sqlglot.ErrorLevel.RAISE)
    except Exception:
        return None
    return ast if ast is not None else None


def parse_sql_profile(sql: str) -> SqlProfile:
    """解析指标 SQL 为画像。

    解析失败（语法错误/方言不支持）返回空画像，不抛异常。Doris/StarRocks 的
    CTAS 物理属性（``DISTRIBUTED BY``/``PROPERTIES``/``BUCKETS`` 等）经
    ``_parse_profile_ast`` 剥离后按默认方言解析，支持生产 ETL 的宽表落库形态。

    Args:
        sql: 指标定义 SQL。

    Returns:
        SqlProfile
    """
    if not sql or not sql.strip():
        return SqlProfile()
    ast = _parse_profile_ast(sql)
    if ast is None:
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
