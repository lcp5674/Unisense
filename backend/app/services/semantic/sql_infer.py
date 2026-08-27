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
    "maxif": "MAX",       # 条件极值（maxIf(amount, cond)）——此前未映射被兜底为 SUM
    "minif": "MIN",       # 语义错误（maxIf 是 MAX 不是 SUM），补齐归一到正确枚举
    "argmaxif": "MAX",
    "argminif": "MIN",
}

# 方言聚合函数名（AnonymousAggFunc/ParameterizedAgg 的 this 字符串）→ 注册枚举聚合。
# 崩溃修复（P0-A）后这些函数能正常产出候选；不归一的话聚合值（QUANTILE/ARGMAX/
# GROUPCONCAT）不在 MetricCreateRequest.aggregation Literal → 批量创建 pydantic
# 整批失败（P1-4 一致性缺陷）。映射到语义最接近的注册枚举：
# - uniqExact/uniq → COUNT_DISTINCT（UV 语义）
# - quantile/quantileExact/percentile_approx → PERCENTILE（分位数）
# - argMax/argMin → MAX/MIN（参数化首/末值）
# - U-2：集合/串聚合（collect_set/collect_list/array_agg/group_concat/listagg/
#   string_agg/group_uniq_array）→ COUNT_DISTINCT（**去重集合语义**，不再静默降级
#   为 COUNT——「收集商品集合」≠「统计商品数」；候选带 needs_review 提示人工核对
#   聚合方式），避免产出语义错误的指标被当成对的创建）
_DIALECT_AGG_MAP = {
    "uniqexact": "COUNT_DISTINCT",
    "uniq": "COUNT_DISTINCT",
    "quantile": "PERCENTILE",
    "quantileexact": "PERCENTILE",
    "percentileapprox": "PERCENTILE",
    "approxquantile": "PERCENTILE",
    "approx_percentile": "PERCENTILE",  # Snowflake/Spark/Trino 分位数（函数名形态）
    "argmax": "MAX",
    "argmin": "MIN",
    "topk": "COUNT",
    "topkweighted": "COUNT",
    "grouparray": "COUNT_DISTINCT",  # U-2：数组聚合（去重集合→去重计数）
    "groupconcat": "COUNT_DISTINCT",  # U-2：串聚合（字符串连接无枚举→去重计数 + needs_review）
    "stringagg": "COUNT_DISTINCT",
    "listagg": "COUNT_DISTINCT",
    "anyif": "COUNT",
    "anylast": "COUNT",          # ClickHouse anyLast（匿名聚合函数名形态）
    "groupuniqarray": "COUNT_DISTINCT",  # U-2：ClickHouse groupUniqArray（数组去重）
    "histogram": "COUNT",        # ClickHouse/Trino histogram（分布桶→近似计数）
    "sumwithoverflow": "SUM",    # ClickHouse sumWithOverflow（溢出不换 SUM 语义）
    "avgweighted": "AVG",        # ClickHouse avgWeighted（加权均值）
    "count_big": "COUNT",        # T-SQL COUNT_BIG（Count 类 key 已合法，兜底函数名形态）
    "collect_list": "COUNT_DISTINCT",  # U-2：Spark collect_list（数组聚合→去重计数）
    "collect_set": "COUNT_DISTINCT",   # U-2：Spark collect_set（去重数组→去重计数）
    "first": "FIRST_VALUE",      # Spark/Hive first（方言下可能 AnonymousAggFunc）
    "last": "LAST_VALUE",        # Spark/Hive last
    "arrayagg": "COUNT_DISTINCT",  # U-2：PG/Spark array_agg（数组聚合→去重计数）
}

# U-2：集合/串聚合函数（sqlglot key 或方言函数名）——语义非简单计数（「收集商品
# 集合」≠「统计商品数」），候选映射为 COUNT_DISTINCT（去重集合语义）并标记
# needs_review 强制人工核对聚合方式，避免静默产出语义错误的指标被当成对的创建。
_SET_STRING_AGG_KEYS = {
    "COLLECT_SET",
    "COLLECT_LIST",
    "ARRAYAGG",
    "ARRAYUNIQUEAGG",
    "GROUPCONCAT",
    "LISTAGG",
    "STRINGAGG",
    "GROUPUNIQARRAY",
    "GROUPARRAY",
}

# V-1：Doris/StarRocks 位图/HLL 去重聚合（Anonymous 形态，非 AggFunc）——
# ``bitmap_union(to_bitmap(uid))``/``hll_union(hll_hash(uid))`` 是工业 UV/DAU
# 计算的标准写法，sqlglot 解析为 ``exp.Anonymous``（``this`` 是函数名字符串，
# 真实列藏在内层 ``to_bitmap(uid)``），``target.find(exp.AggFunc)`` 返回 None →
# 此前整段静默 0 候选（用户误以为 SQL 有问题）。按函数名识别，从内层表达式取
# 真实列，映射去重计数语义（union/intersect/count 均是 UV 口径）+ needs_review
# 强制人工核对（位图/HLL 是近似/集合语义，非普通 COUNT_DISTINCT）。
_BITMAP_HLL_AGGS = {
    "bitmap_union",
    "hll_union",
    "bitmap_count",
    "hll_cardinality",
    "bitmap_intersect",
    "group_bitmap_xor",
}
# 无注册枚举可归一的统计聚合（相关性/协方差/回归/标准差/方差）→ 返回 None 跳过
# 该度量（诚实不产出非法候选，避免注册失败/口径错误），不崩溃不炸整批。
# 含函数名形态（stdev/stdevp/std/var/varp）——某些方言下统计聚合解析为
# AnonymousAggFunc（this 是函数名字符串），若不入 skip 会产出非法枚举。
_DIALECT_AGG_SKIP = {
    "corr", "covar_pop", "covar_samp", "regr_slope", "regr_r2", "regr_intercept",
    "stddev", "stddevpop", "stddevsamp", "stddev_samp", "varpop", "varsamp",
    "var_pop", "var_samp", "variance",
    "stdev", "stdevp", "std", "var", "varp",  # T-SQL/MySQL 简写（函数名形态防御）
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


def _is_write_target(node: exp.Table) -> bool:
    """判断 Table 节点是否为写操作（INSERT/CREATE/UPDATE/MERGE）的目标表。

    ``INSERT INTO t (a, b) SELECT ...`` 带列清单时，sqlglot 把目标表解析为
    ``exp.Schema(expressions=[列清单])`` 包裹 ``Table``——``node.parent`` 是
    ``Schema`` 而非 ``Insert``，旧判断 ``parent is exp.Insert`` 失配，导致目标表
    混入 source_tables（P0-2：INSERT 带列清单源表错挂）。逐层上溯识别写入目标：
    Table → Schema → Insert/Create/Update/Merge。
    """
    parent = node.parent
    if parent is None:
        return False
    if isinstance(parent, exp.Schema):
        if parent.this is not node:
            return False
        gp = parent.parent
        return isinstance(gp, (exp.Insert, exp.Create, exp.Update, exp.Merge))
    if isinstance(parent, exp.Insert):
        return parent.this is node
    if isinstance(parent, exp.Create):
        return parent.this is node
    if isinstance(parent, exp.Update):
        return parent.this is node
    if isinstance(parent, exp.Merge):
        return parent.this is node
    return False


def _extract_source_tables(ast: exp.Expr) -> list[str]:
    """表级血缘：读入源表（INSERT/CREATE TABLE AS/UPDATE/MERGE 的源 + 普通 SELECT FROM）。

    U-7：CTE 名不视为物理表——``WITH base AS (...) SELECT ... FROM base`` 中
    ``base`` 被 sqlglot 解析为 ``Table(name='base')``（无 catalog/db），混入
    source_tables 会让血缘挂到不存在的表；收集 CTE 别名集合二次过滤（同时覆盖
    带前缀引用形态 ``catalog.base`` 的尾段匹配）。

    **V-3**：仅收集主查询（顶层 UNION 各分支）的 **FROM 子树**内的表——WHERE/
    JOIN-ON 里的标量/相关子查询（``WHERE d = (SELECT max(d) FROM ods.u)``）是
    维表/查找表，不是指标数据来源，混入 source_tables 会让血缘错挂一张无关表
    （此前 walk 整棵 AST 把 ``ods.u`` 也收进来）。FROM 子树内的嵌套子查询（真实
    事实表/维表 join）仍合法保留。**主 FROM 引用 CTE 时递归收集该 CTE 体的 FROM
    子树**（``WITH u AS (SELECT ... FROM ods.a UNION ALL SELECT ... FROM ods.b)
    SELECT ... FROM u`` 的源表在 CTE 体里，只在主 FROM 收不到）——visited 集合
    防递归 CTE（``WITH RECURSIVE c AS (... FROM c)``）死循环。
    """
    cte_names = {
        c.alias_or_name.lower()
        for c in ast.find_all(exp.CTE)
        if c.alias_or_name
    }
    tables: list[str] = []
    visited_ctes: set[str] = set()

    def _collect_from(from_node: exp.From | None) -> None:
        if from_node is None:
            return
        for node in from_node.walk():
            if not isinstance(node, exp.Table):
                continue
            # 排除被写为目标的表（INSERT INTO target，含带列清单的 Schema 包裹形态）
            if _is_write_target(node):
                continue
            name = _norm_table_name(node)
            if name and name not in tables:
                # U-7：CTE 名（或全名尾段）命中 → 跳过，不视为物理表
                if name in cte_names or name.split(".")[-1] in cte_names:
                    continue
                tables.append(name)

    def _collect_select_from(sel: exp.Select) -> None:
        # FROM + JOIN 子树都是合法源表——sqlglot 把 JOIN（含右侧维表/事实表）存在
        # select.args['joins'] 而非 From 节点，只走 from 会漏掉 LEFT JOIN 维表
        # （doctor_active_month 的 disease_care_sys_org_staff_relation_df）。V-3
        # 排除的是 WHERE/ON 内标量/相关子查询的查找表（不在 FROM/JOIN 子树），
        # JOIN 的维表/事实表仍属指标数据来源。
        sources = [sel.args.get("from")] + list(sel.args.get("joins") or [])
        sources = [s for s in sources if s is not None]
        for src in sources:
            _collect_from(exp.From(this=src) if not isinstance(src, exp.From) else src)
        # 递归收集 FROM/JOIN 引用的 CTE 体（其 FROM/JOIN 内的真实事实表/维表）
        for src in sources:
            for node in src.walk():
                if not isinstance(node, exp.Table):
                    continue
                ref = _norm_table_name(node)
                ref_tail = ref.split(".")[-1]
                if ref not in cte_names and ref_tail not in cte_names:
                    continue
                if ref_tail in visited_ctes:
                    continue
                visited_ctes.add(ref_tail)
                for cte in ast.find_all(exp.CTE):
                    if cte.alias_or_name.lower() != ref_tail:
                        continue
                    body = cte.this
                    if isinstance(body, exp.Union):
                        for b in body.find_all(exp.Select):
                            _collect_select_from(b)
                    elif isinstance(body, exp.Select):
                        _collect_select_from(body)
                    break

    if isinstance(ast, exp.Union):
        # 顶层 UNION 多源合并：各分支 FROM 都是合法源表（U-1 多子公司合并等）
        for branch in ast.find_all(exp.Select):
            _collect_select_from(branch)
        return tables
    main = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
    if main is None:
        return tables
    _collect_select_from(main)
    return tables


def _extract_group_by(select: exp.Select) -> list[str]:
    """GROUP BY 维度列（含位置序号 ``GROUP BY 1, 2`` → 映射回 SELECT 投影列名）。

    U-9：GROUPING SETS / ROLLUP / CUBE 分组——sqlglot 把分组集合放
    ``group.args['grouping_sets']``（``GroupingSets`` 节点，expressions 是
    ``Tuple`` 列表，每个 Tuple 含一或多个维度列），普通维度在
    ``group.expressions``。GROUPING SETS 的维度应进入 group_by 供周期/维度
    推断（此前 group.expressions 为空 → 维度列整体丢失，与 ROLLUP 行为不一致）。
    """
    group = select.args.get("group")
    if not group:
        return []
    cols: list[str] = []

    def _append_column(expr: exp.Expr) -> None:
        if isinstance(expr, exp.Column):
            cols.append(expr.name.lower())
        else:
            for col in expr.walk():
                if isinstance(col, exp.Column):
                    cols.append(col.name.lower())
                    break

    # U-9：GROUPING SETS / ROLLUP / CUBE 的维度列（从 Tuple 分组集合展开）
    gs = group.args.get("grouping_sets")
    if gs:
        for sets_node in gs:
            for tup in sets_node.expressions:
                for item in tup.expressions if isinstance(tup, exp.Tuple) else [tup]:
                    _append_column(item)
    # V-6：GROUP BY CUBE(a, b)——sqlglot 把 CUBE 维度放 ``group.args['cube']``
    # （list of ``Cube`` 节点，各含 expressions；与 grouping_sets 不同节点，此前
    # group.expressions 为空 → 维度整体丢失，被误判为全局聚合无维度）；与
    # GROUPING SETS 对齐展开进 group_by。
    cube = group.args.get("cube")
    if cube:
        for cube_node in cube:
            for item in (
                cube_node.expressions
                if isinstance(cube_node, exp.Cube)
                else [cube_node]
            ):
                _append_column(item)
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
            if _is_write_target(node):
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
        if _is_write_target(node):
            continue
        alias = (node.alias or node.name or "").lower()
        if alias:
            mapping[alias] = _norm_table_name(node)
    return mapping


def _full_alias_map(select: exp.Select) -> dict[str, str]:
    """别名 → 物理表（穿透子查询别名链，P0-3a 多层嵌套源表归属）。

    ``FROM (SELECT ... FROM ods.raw_event) a LEFT JOIN ods.ref_dict b`` →
    ``{a: ods.raw_event, b: ods.ref_dict}``——子查询别名 a 也解析到其 FROM 主表，
    使 ``sum(a.amount)`` 候选 source_table 正确归属 ods.raw_event 而非 join 右侧
    字典表（此前 ``_table_alias_map`` 只映射裸 Table 别名，子查询别名缺失 →
    ``_measure_table`` 返回 None → 候选回退 ``tables[0]``＝join 右表，口径错挂）。
    """
    mapping = _table_alias_map(select)
    for sub in select.find_all(exp.Subquery):
        alias = (sub.alias or "").lower()
        if not alias or alias in mapping:
            continue
        inner = sub.this
        if isinstance(inner, exp.Select):
            phys = _from_table(inner)
            if phys:
                mapping[alias] = phys
    return mapping


def _measure_table(select: exp.Select, agg: exp.AggFunc) -> str | None:
    """聚合投影的度量来源物理表：按列前缀归属（A-3 join 同名列区分）。

    ``sum(a.amount), sum(b.amount)`` 的候选此前都取 ``_from_table`` 的第一个物理表
    ——b 侧口径错挂 a 表。此处从聚合参数列的表前缀（``a.amount`` → 别名 a）解析
    对应物理表（经 ``_full_alias_map`` 穿透子查询别名，P0-3a 多层嵌套也能归属）；
    无前缀/未命中返回 None（上层回退通用来源表）。
    """
    col = agg.this
    if isinstance(col, exp.Distinct):
        col = col.expressions[0] if col.expressions else None
    alias_map = _full_alias_map(select)
    if isinstance(col, exp.Column) and col.table:
        return alias_map.get(col.table.lower())
    if isinstance(col, exp.Case):
        for c in col.walk():
            if isinstance(c, exp.Column) and c.table:
                return alias_map.get(c.table.lower())
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
    # 方言首/末值聚合：Spark/Hive/CH first()/last() 解析为 First/Last 类（key=FIRST/
    # LAST，无下划线）→ 归一到注册枚举 FIRST_VALUE/LAST_VALUE，否则产出非法枚举
    # FIRST/LAST → 批量创建 pydantic 整批失败（P1-4 同类缺陷）。
    if key == "FIRST":
        return "FIRST_VALUE"
    if key == "LAST":
        return "LAST_VALUE"
    if key.startswith("PERCENTILE"):
        return "PERCENTILE"
    # 数组/布尔/任意值聚合类（sqlglot 内置子类，key 无下划线）：array_agg→COUNT_DISTINCT
    # （U-2：数组聚合=去重集合语义，不再静默降级 COUNT）、bool_and/bool_or→COUNT、
    # any()/arbitrary()/ANY_VALUE→COUNT、APPROX_TOP_K→COUNT——均按「近似计数语义」
    # 归一到注册枚举，否则产出非法枚举（ARRAYAGG/LOGICALAND/LOGICALOR/ANYVALUE/
    # APPROXTOPK/ARRAYUNIQUEAGG）导致批量创建整批失败（P1-4 同类缺陷）。
    if key in ("ARRAYAGG", "ARRAYUNIQUEAGG"):
        return "COUNT_DISTINCT"
    if key in ("LOGICALAND", "LOGICALOR"):
        return "COUNT"
    if key == "ANYVALUE":
        return "COUNT"
    if key == "APPROXTOPK":
        return "COUNT"
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


def _is_wrapped_aggregate(target: exp.Expr) -> bool:
    """是否「格式化包裹的单聚合」投影（IFNULL(SUM(x),0)/ROUND(SUM(x),2)/COALESCE(SUM(x),0)）。

    这类投影本质是单个聚合（外层只是格式/默认值包裹，如 MySQL ``IFNULL(SUM(amount),0)``
    仍指「金额合计」），应作为独立聚合度量捕获。

    而 ``ROUND(SUM(a)/NULLIF(COUNT(b),0),2)``（算术组合多个聚合）是**派生比率**，
    由 ``_collect_derived_measures`` 收集——若 ``_projection_measures`` 也提取内嵌聚合，
    会：① 与独立聚合（gmv=SUM(amount)）撞同一编码（``fin_trade_amount_day`` 重复 →
    注册 METRIC_CODE_EXISTS）；② 内嵌聚合列名（is_refund）未命中受控词根 → 注册失败。

    注意：**CASE/IF 不算组合节点**——单聚合内部的 ``CASE WHEN`` 只是过滤条件
    （``count(distinct case when cond then col end)`` = 条件去重计数、
    ``SUM(CASE WHEN status='paid' THEN amount END)`` = 条件金额合计），与
    ``sum(case...)``（target 直接是 AggFunc）语义一致，应作为独立聚合捕获；否则
    包一层 ``COALESCE(COUNT(DISTINCT CASE...),0)`` 默认值就静默丢度量（真实 ETL
    「当月活跃医生数/上月活跃医生数」双度量场景）。真正排除的是**聚合之间的算术
    组合**（Div/Mul/Add/Sub/Mod 连接多个聚合或聚合与非聚合），已被 ``len(aggs)>1``
    或算术节点检查覆盖。
    """
    aggs = [n for n in target.walk() if isinstance(n, exp.AggFunc)]
    if not aggs:
        return False
    if len(aggs) > 1:
        # U-5：IF/CASE 语义包裹（``if(sum(amt) is null, 0, sum(amt))`` 解析为
        # Case/If）的多个分支引用**同一聚合**（同 key + 同参数列）——本质是
        # 默认值/格式兜底而非聚合组合，放行（与单聚合同语义，如「金额合计，
        # 空置 0」）；不同聚合参数（``coalesce(sum(amt), sum(refund), 0)``）
        # 由 U-4 独立收集，此处仍视为组合不重复产出。
        if isinstance(target, (exp.If, exp.Case)):
            first_key = aggs[0].key.upper()
            first_col = _extract_col_name(
                aggs[0].this.expressions[0]
                if isinstance(aggs[0].this, exp.Distinct) and aggs[0].this.expressions
                else aggs[0].this
            )
            if all(
                a.key.upper() == first_key
                and _extract_col_name(
                    a.this.expressions[0]
                    if isinstance(a.this, exp.Distinct) and a.this.expressions
                    else a.this
                )
                == first_col
                for a in aggs
            ):
                return not any(
                    isinstance(n, (exp.Div, exp.Mul, exp.Add, exp.Sub, exp.Mod))
                    for n in target.walk()
                )
        return False
    # 单聚合且表达式内无算术组合节点（Div/Mul/Add/Sub/Mod）→ 纯格式包裹/条件聚合
    return not any(
        isinstance(n, (exp.Div, exp.Mul, exp.Add, exp.Sub, exp.Mod))
        for n in target.walk()
    )


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
        # U-3：相关/标量子查询投影（``(SELECT max(amt) FROM ods.b b WHERE b.d=a.d) mx``）
        # 不是当前 GROUP BY 的分组聚合——``target.find(exp.AggFunc)`` 会误取内层 MAX
        # 产出错误聚合候选 + 把子查询表混进源表。跳过（标量子查询属过滤/派生语义，
        # 由 _collect_derived_measures 按需收集）。
        if isinstance(target, exp.Subquery):
            continue
        # 裸聚合投影（P0-B）：非 Alias/Column 但本身是聚合函数（sum/count 等）
        if not isinstance(projection, exp.Alias) and not isinstance(projection, exp.Column):
            if not isinstance(target, exp.AggFunc):
                continue
            projection = exp.alias_(target, f"_col_{len(measures) + 1}")
        # V-1：Doris/StarRocks 位图/HLL 去重聚合（``bitmap_union(to_bitmap(uid))``、
        # ``hll_union(hll_hash(uid))``——工业 UV/DAU 标准写法）。sqlglot 解析为
        # ``exp.Anonymous``（非 AggFunc），``target.find(exp.AggFunc)`` 返回 None →
        # 此前整段静默 0 候选。按函数名识别，从内层表达式取真实列（to_bitmap(uid)
        # 里的 uid），映射去重计数语义 + needs_review（位图/HLL 是近似/集合语义）。
        if isinstance(target, exp.Anonymous):
            anon_fn = str(getattr(target, "this", "") or "").lower()
            if anon_fn in _BITMAP_HLL_AGGS:
                inner_col = next(
                    (c for c in target.walk() if isinstance(c, exp.Column)), None
                )
                bm: dict[str, Any] = {
                    "column": (
                        _extract_col_name(inner_col) if inner_col is not None else "*"
                    ),
                    "agg": "COUNT_DISTINCT",
                    "needs_review": True,
                }
                if enrich:
                    bm["alias"] = (
                        projection.alias_or_name
                        if isinstance(projection, exp.Alias)
                        else None
                    )
                    bm["table"] = _measure_table(select, target) or table
                    bm["sunk"] = sunk
                    try:
                        bm["expression"] = target.sql()
                    except Exception:  # noqa: BLE001 - 序列化失败仅降级简化式
                        bm["expression"] = f"COUNT_DISTINCT({bm['column']})"
                measures.append(bm)
                continue
        # U-4：COALESCE 多参数含多个聚合（``coalesce(sum(amt), sum(refund), 0)``）——
        # 每个聚合参数都是独立度量（回退/兜底口径），全部收集而非只取首个（此前
        # 只取第一个 sum(amt) 且 agg=None，sum(refund) 静默丢失）；单聚合 coalesce
        # （``coalesce(sum(amt),0)``）已由 _is_wrapped_aggregate 按格式包裹处理
        if isinstance(target, exp.Coalesce):
            # 聚合参数须用 walk() 收集——sqlglot 把首个聚合解析到 Coalesce.this、
            # 其余在 expressions（``coalesce(sum(amt), sum(refund), 0)`` 的
            # expressions 只剩 ``[Sum(SUM(refund)), Literal(0)]``）
            agg_args = [n for n in target.walk() if isinstance(n, exp.AggFunc)]
            if len(agg_args) > 1:
                for i, aagg in enumerate(agg_args):
                    an = _agg_display_name(aagg)
                    if an is None:
                        continue
                    acol_expr = aagg.this
                    if isinstance(acol_expr, exp.Distinct):
                        an = "COUNT_DISTINCT"
                        acol_expr = (
                            acol_expr.expressions[0] if acol_expr.expressions else None
                        )
                    am: dict[str, Any] = {
                        "column": _extract_col_name(acol_expr),
                        "agg": an,
                    }
                    if enrich:
                        am["alias"] = (
                            f"{projection.alias_or_name}_{i + 1}"
                            if isinstance(projection, exp.Alias)
                            and projection.alias_or_name
                            else None
                        )
                        am["table"] = _measure_table(select, aagg) or table
                        am["sunk"] = sunk
                        try:
                            am["expression"] = aagg.sql()
                        except Exception:  # noqa: BLE001 - 序列化失败仅降级简化式
                            am["expression"] = f"{an}({am['column']})"
                    measures.append(am)
                continue
        agg = target.find(exp.AggFunc) if target else None
        if agg is None:
            continue
        # P0-3d：聚合埋在算术/条件组合里（ROUND(SUM/NULLIF(COUNT)) 比率、SUM(CASE)/
        # NULLIF(COUNT(*)) 退货率）是「派生度量」而非独立聚合投影——由
        # _collect_derived_measures 收集（含完整 expression）；此处跳过内嵌聚合，
        # 避免与独立聚合（gmv=SUM(amount)）撞同一编码 / 内嵌列名未命中受控词根
        if not isinstance(target, exp.AggFunc) and not _is_wrapped_aggregate(target):
            continue
        agg_name = _agg_display_name(agg)
        if agg_name is None:
            # 无注册枚举可归一的统计聚合 → 跳过该投影（诚实不产出非法候选）
            continue
        # V-2：嵌套聚合（``sum(avg(x))``/``sum(count(*))`` 等）——投影里多于一个
        # AggFunc 时语义是「聚合的聚合」，不是简单 SUM(x)；expression 保留原结构但
        # 必须标记 needs_review 让用户人工核对（否则静默产出语义错误的 SUM(x) 被
        # 当成对的创建，U-2 同类最危险场景）。
        nested_agg = (
            len(list(target.find_all(exp.AggFunc))) > 1 if target is not None else False
        )
        # DISTINCT 修饰符：sqlglot 将 COUNT(DISTINCT x) 解析为 Count(this=Distinct(...))
        col_expr = agg.this
        multi_distinct_col: str | None = None
        if isinstance(col_expr, exp.Distinct):
            agg_name = "COUNT_DISTINCT"
            if len(col_expr.expressions or []) > 1:
                # V-5：``count(distinct col1, col2)`` 多列去重（Spark/Hive）——只取
                # 首列会丢失去重语义；合并列名展示并标记 needs_review（多列去重口径
                # 需人工确认）。
                multi_distinct_col = "+".join(
                    _extract_col_name(e) for e in col_expr.expressions
                )
                col_expr = None
            else:
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
        # V-4：``percentile_cont(0.5) WITHIN GROUP (ORDER BY amt)``——PercentileCont
        # 的 ``this`` 是分位数 Literal（0.5），真实列在 ``WithinGroup`` 的 ORDER BY
        # 里；``_extract_col_name`` 对 Literal 返回 ``*`` 导致列丢失。取投影内第一个
        # Column（ORDER BY 的 amt）。
        if agg.key.upper().startswith("PERCENTILE"):
            col_expr = (
                next((c for c in target.walk() if isinstance(c, exp.Column)), None)
                if target is not None
                else None
            )
        col_name = multi_distinct_col or _extract_col_name(col_expr)
        measure: dict[str, Any] = {"column": col_name, "agg": agg_name}
        if nested_agg:
            measure["needs_review"] = True
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
        # U-6/U-2：窗口函数包裹（``sum(amt) OVER (...)``，窗口计算非 GROUP BY 聚合）
        # 或集合/串聚合（collect_set/group_concat 等）——语义非普通分组聚合，候选
        # 标记需人工核对，避免用户误以为全表/分组聚合而直接注册
        if enrich and target.find(exp.Window) is not None:
            measure["needs_review"] = True
        if enrich:
            _agg_fn = (
                str(getattr(agg, "this", "")).upper()
                if isinstance(getattr(agg, "this", ""), str)
                else agg.key.upper()
            )
            if _agg_fn in _SET_STRING_AGG_KEYS:
                measure["needs_review"] = True
        measures.append(measure)
    return measures


def _collect_derived_measures(select: exp.Select) -> list[dict[str, Any]]:
    """无聚合包裹的派生投影（ROUND(SUM/NULLIF)/CASE 比率/COALESCE 列等）。

    P0-3d：真实 ETL 的「客单价 = ROUND(SUM(amount)/NULLIF(COUNT(DISTINCT
    user_id),0),2)」「退货率 = SUM(CASE...)/NULLIF(COUNT(*),0)」等核心派生指标
    此前被 ``_projection_measures`` 整体跳过——外层 SELECT 有聚合度量时，非聚合
    表达式投影静默缺失。此处对聚合承载的 SELECT 额外收集：仅收「表达式内仍含
    聚合函数」的派生投影（比率/条件聚合嵌套），纯列/截断表达式（``substr(dt)``
    等维度列）不含聚合不产出假候选。``agg=None`` + ``derived=True`` + 原始
    expression，候选构建标记「口径需核对」，注册聚合占位（口径由 expression 承载，
    对齐复合指标占位语义）；``sunk=True`` 用 alias 作编码锚点防与内嵌聚合度量撞码。

    **A7（第九轮）**：额外收集「引用已命名聚合列的算术派生列」——外层宽表 ETL
    的 ``all_order_cnt - session_side_order_cnt - region_org_order_cnt AS
    old_page_transfer_order_cnt``（转诊预约旧页面）这类派生指标**无内嵌聚合**
    （引用的都是内层子查询已命名的聚合别名列），此前 ``if not aggs: continue``
    把它整个跳过 → 核心派生指标静默缺失。判定：目标含算术组合节点（Add/Sub/
    Mul/Div/Mod）且至少一个 Column 引用属于本 SELECT/子查询的聚合投影别名 →
    产出派生候选（携带 ``deps_aliases`` 供候选构建解析依赖原子编码），口径为
    完整算术表达式（``agg=None`` + ``derived=True`` 与比率派生同语义）。
    """
    # 收集本 SELECT 及其子查询的投影别名（A7 识别「引用聚合别名列的算术派生」）
    known_aliases: set[str] = set()
    for sub in select.find_all(exp.Select):
        for proj in sub.expressions:
            if isinstance(proj, exp.Alias) and proj.alias_or_name:
                known_aliases.add(proj.alias_or_name.lower())
    out: list[dict[str, Any]] = []
    for projection in select.expressions:
        target = projection.this if isinstance(projection, exp.Alias) else projection
        alias = projection.alias_or_name if isinstance(projection, exp.Alias) else None
        if target is None or isinstance(target, exp.AggFunc):
            continue
        if isinstance(target, exp.Column):
            continue  # 纯列透传（gmv 等已由聚合度量捕获）不重复产出
        aggs = [n for n in target.walk() if isinstance(n, exp.AggFunc)]
        if not aggs:
            # A7：无内嵌聚合的算术组合——若其 Column 引用属于聚合别名（引用的都是
            # 内层已命名的聚合列），则是「引用聚合列的派生指标」而非维度列/普通列
            # 运算（substr(dt)/a+b 普通列不产出假候选）
            has_arith = isinstance(
                target, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod)
            ) or any(
                isinstance(n, (exp.Div, exp.Mul, exp.Add, exp.Sub, exp.Mod))
                for n in target.walk()
            )
            if not has_arith:
                continue
            col_refs = [c.name.lower() for c in target.find_all(exp.Column)]
            deps = [c for c in col_refs if c in known_aliases]
            if not deps:
                continue  # 引用普通列的运算（无聚合别名）→ 非度量
            col = deps[0]
            try:
                expr_sql = target.sql()
            except Exception:  # noqa: BLE001 - 序列化失败仅降级表达式占位
                expr_sql = alias or col
            out.append(
                {
                    "column": col,
                    "agg": None,
                    "derived": True,
                    "alias": alias,
                    "expression": expr_sql,
                    "sunk": True,
                    "deps_aliases": deps,
                }
            )
            continue
        # U-4：COALESCE 多聚合参数（``coalesce(sum(amt), sum(refund), 0)``）——
        # 已由 _projection_measures 逐参数独立收集，此处不重复产出派生候选
        if isinstance(target, exp.Coalesce) and len(aggs) > 1:
            continue
        # 单聚合 + 纯格式化包裹/条件过滤（IFNULL(SUM(x),0)/ROUND(SUM(x),2)/
        # COALESCE(COUNT(DISTINCT CASE...),0)）：仍是该聚合本身，已由
        # _projection_measures 捕获，不重复产出派生候选（CASE/IF 是聚合内部
        # 过滤条件非组合节点，与 _is_wrapped_aggregate 判定保持一致）
        if len(aggs) == 1:
            combined = any(
                isinstance(n, (exp.Div, exp.Mul, exp.Add, exp.Sub, exp.Mod))
                for n in target.walk()
            )
            if not combined:
                continue
        elif isinstance(target, (exp.If, exp.Case)):
            # U-5：IF/CASE 语义包裹多个分支引用同一聚合（``if(sum(amt) is null,
            # 0, sum(amt))`` 解析为 Case，两个分支都是 sum(amt)）——是单聚合的
            # 格式/默认值包裹，已由 _projection_measures 作为独立聚合捕获，
            # 此处不重复产出派生候选（与 _is_wrapped_aggregate 判定一致）
            first_key = aggs[0].key.upper()
            first_col = _extract_col_name(
                aggs[0].this.expressions[0]
                if isinstance(aggs[0].this, exp.Distinct) and aggs[0].this.expressions
                else aggs[0].this
            )
            if all(
                a.key.upper() == first_key
                and _extract_col_name(
                    a.this.expressions[0]
                    if isinstance(a.this, exp.Distinct) and a.this.expressions
                    else a.this
                )
                == first_col
                for a in aggs
            ):
                continue
        col = next(
            (c.name.lower() for c in target.walk() if isinstance(c, exp.Column)), "*"
        )
        try:
            expr_sql = target.sql()
        except Exception:  # noqa: BLE001 - 序列化失败仅降级表达式占位
            expr_sql = alias or col
        out.append(
            {
                "column": col,
                "agg": None,
                "derived": True,
                "alias": alias,
                "expression": expr_sql,
                "sunk": True,  # 用 alias 作编码锚点防与内嵌聚合度量（同列）撞码
            }
        )
    return out


def _pivot_measures(select: exp.Select) -> list[dict[str, Any]]:
    """U-8：PIVOT 展开的聚合度量提取。

    ``SELECT * FROM ods.a PIVOT(SUM(amt) FOR d IN (...))`` 的聚合在 Pivot 节点
    内部（``args['expressions']`` = ``[Sum(amt)]``），``*`` 投影找不到 AggFunc →
    measures=0（此前 PIVOT 完全丢失，连源表也因 walk 异常被误判为空）。从 Pivot
    聚合表达式提取度量：PIVOT 展开为宽表，聚合方式/口径需人工核对（needs_review）。
    """
    out: list[dict[str, Any]] = []
    for pv in select.find_all(exp.Pivot):
        for aagg in pv.args.get("expressions") or []:
            if not isinstance(aagg, exp.AggFunc):
                continue
            an = _agg_display_name(aagg)
            if an is None:
                continue
            col_expr = aagg.this
            if isinstance(col_expr, exp.Distinct):
                an = "COUNT_DISTINCT"
                col_expr = col_expr.expressions[0] if col_expr.expressions else None
            out.append(
                {
                    "column": _extract_col_name(col_expr),
                    "agg": an,
                    "alias": None,
                    "table": _measure_table(select, aagg) or _from_table(select),
                    "sunk": False,
                    "needs_review": True,
                    "expression": f"{an}({_extract_col_name(col_expr)}) [PIVOT 展开]",
                }
            )
    return out


def _extract_measures(select: exp.Select) -> list[dict[str, Any]]:
    """SELECT 投影度量；外层无聚合时下沉 FROM 子树找聚合投影。

    ETL 落宽表常见 ``insert overwrite ... select a.col1, a.cnt ... from (聚合子查询) a``
    透传形态——最外层投影只是改名/join 字典，聚合在子查询内。此时下沉收集聚合
    投影的度量，按 ``(alias, agg, table)`` 去重（UNION 多支同指标合并；P0-3b：
    去重键含 table，两个子查询同名列 ``t1.cnt``/``t2.cnt`` 不再被合并丢一支），
    并附带 ``alias/table/expression`` 供候选构建区分同列不同语义并还原口径。

    **A-1/2**：顶层投影也走 ``enrich=True`` 携带原始 ``expression``（如
    ``SUM(CASE WHEN status='paid' THEN amount END)``/``SUM(amount) OVER (...``）——
    此前顶层候选由 ``_build_atomic_candidate`` 用简化 ``SUM(col)`` 还原口径，
    CASE 过滤条件/窗口语义被丢弃，注册后指标变全表聚合（数据错误）。
    顶层不传 ``table``（候选的源表由 ``_physical_source_tables`` 过滤 CTE 别名后
    决定，避免顶层 measure 误挂 CTE 名）且 ``sunk=False``（编码锚点用真实列）。
    **P0-3d**：顶层聚合承载时额外收集派生比率/条件列（``_collect_derived_measures``）。
    """
    # U-8：PIVOT 展开的聚合度量（SELECT * FROM t PIVOT(SUM(amt)...) 的聚合在 Pivot
    # 节点内部而非 SELECT 投影——``*`` 投影找不到 AggFunc → measures=0）；顶层
    # 投影无度量时用 PIVOT 度量（有普通投影聚合时正常走投影收集，PIVOT 不重复产）
    pivot_measures = _pivot_measures(select)
    top_aggs = _projection_measures(select, enrich=True, table=None, sunk=False)
    if pivot_measures and not top_aggs:
        top_aggs = pivot_measures
    # P0-3d/A7：**无条件**收集派生投影（比率/条件聚合 + 引用已命名聚合列的算术
    # 派生列）——外层宽表透传形态（``select ... all_order_cnt, a-b-c AS d from
    # (聚合子查询)``）外层无聚合（top_aggs 空）但含算术派生列，此前派生收集只在
    # ``if top_aggs:`` 分支内调用导致「转诊预约旧页面」静默缺失；无聚合纯维度
    # SELECT（``SELECT a, b FROM t``）不产出（派生收集内部判定为空）。
    # 派生列不参与 alias_map 物理列映射（其 col 是聚合别名而非真实物理列）。
    derived = _collect_derived_measures(select)
    # A-4/P0-3c：全局子查询/CTE 投影别名 → 物理列映射（聚合参数是子查询/CTE 投影
    # 别名时解析为底层物理列——``SUM(x) FROM (SELECT amount AS x ...)`` 的
    # source_fields 若不解析会落 ``[orders, x]``（物理表+不存在的列），血缘/下游
    # 错乱）。简单透传 Alias(Column) → 列名；派生聚合列 Alias(AggFunc)（如
    # ``sum(amount) AS day_amt``）→ 底层聚合列 amount（P0-3c）；复杂表达式保留别名。
    alias_map: dict[str, str] = {}
    for sub in select.find_all(exp.Select):
        if sub is select:
            continue
        for proj in sub.expressions:
            if not isinstance(proj, exp.Alias) or not proj.alias_or_name:
                continue
            inner = proj.this
            if isinstance(inner, exp.Column):
                alias_map[proj.alias_or_name.lower()] = inner.name.lower()
            elif isinstance(inner, exp.AggFunc):
                col_expr = inner.this
                if isinstance(col_expr, exp.Distinct):
                    col_expr = col_expr.expressions[0] if col_expr.expressions else None
                alias_map[proj.alias_or_name.lower()] = _extract_col_name(col_expr)
    if top_aggs:
        measures = list(top_aggs)
        if alias_map:
            for m in measures:
                if m["column"] in alias_map:
                    m["column"] = alias_map[m["column"]]
        if derived:
            measures.extend(derived)
        return measures
    # 下沉透传形态：外层无聚合 → 下沉子查询收集聚合度量 + 外层派生列（A7）
    measures: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str | None]] = set()
    for sub in select.find_all(exp.Select):
        if sub is select:
            continue
        for m in _projection_measures(
            sub, enrich=True, table=_from_table(sub), sunk=True
        ):
            if alias_map and m["column"] in alias_map:
                m["column"] = alias_map[m["column"]]
            # P0-3b：去重键含来源表——两个子查询同名列（t1.cnt/t2.cnt）分属不同表
            # 时不再合并丢一支；同表同列同聚合仍合并（UNION 多支同指标）
            key = (m["alias"] or m["column"], m["agg"], m.get("table"))
            if key in seen:
                continue
            seen.add(key)
            measures.append(m)
    if derived:
        measures.extend(derived)
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


# 方言特有聚合函数名（文本命中才遍历方言择优——避免普通 SQL 每条全方言解析的性能损耗）。
# 注意函数名带下划线的（COUNT_BIG/collect_list/collect_set/approx_percentile）必须用
# 真实下划线形式，否则默认方言成功解析（降级非 AggFunc）时 hint 不命中 → 不触发择优
# → 该方言聚合 measures=0 推断退化。
_DIALECT_AGG_HINT = re.compile(
    r"\b(summerge|sumif|sumdistinct|avgmerge|avgif|countif|countmerge|maxmerge|"
    r"minmerge|maxif|minif|argmaxif|argminif|sumwithoverflow|avgweighted|anylast|"
    r"groupuniqarray|histogram|arbitrary|count_big|approx_distinct|approx_count_distinct|"
    r"approx_percentile|percentile_approx|uniq|uniqexact|quantile|grouparray|"
    r"topk|collect_list|collect_set|listagg|stringagg|anyif|corr|covar_pop|regr_slope|stddev|var_pop)\b",
    re.IGNORECASE,
)


def sql_has_arithmetic(sql: str) -> bool:
    """SQL 文本是否含四则运算/比率结构（复合指标判定依据，B4/R2 共享）。

    复合 = 多指标四则运算/比率（OneData）。两个独立聚合列共存（如
    ``SELECT SUM(a), SUM(b)``）**不构成**复合——仅当语句含 Div/Mul/Add/Sub/Mod
    运算时才应判复合，避免把「多度量并列」误判为「指标间运算」。该判定被
    ``sql_split``（批量合成复合候选）与 ``auto_fill``（单条类型推断）共用，
    保证批量/单条两路径的运算检测一致。
    """
    if not sql:
        return False
    try:
        ast = sqlglot.parse_one(sql)
        if ast is not None:
            for node in ast.find_all(exp.Binary):
                if isinstance(node, (exp.Div, exp.Mul, exp.Add, exp.Sub, exp.Mod)):
                    return True
    except Exception:
        pass
    # AST 解析失败时退化为正则：剥离注释与字符串字面量后检查算术运算符
    text = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql)
    text = re.sub(r"'[^']*'|\"[^\"]*\"", " ", text)
    return bool(re.search(r"[*/+\-]", text))


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


def _profile_from_union(ast: exp.Union, sql: str) -> SqlProfile:
    """U-1：顶层 UNION ALL/UNION 多源合并画像。

    ``SELECT d,sum(amt) FROM a GROUP BY d UNION ALL SELECT d,count(DISTINCT uid)
    FROM b GROUP BY d`` 是「线上+线下合并」「多子公司合并」等工业最常见的多源表
    形态——此前 ``ast.find(exp.Select)`` 只取第一个分支且 Union 顶层被跳过 →
    measures=[] 静默 0 候选（用户误以为 SQL 有问题）。遍历所有 Select 分支合并
    度量（去重键 alias/column/agg/table，与下沉去重对齐），源表已由
    ``_extract_source_tables``（walk 遍历 Union 两侧）收集，group_by 取首个有
    分组的分支，时间粒度/列取首个分支推断。
    """
    branches = list(ast.find_all(exp.Select))
    if not branches:
        return SqlProfile(sql=sql.strip())
    measures: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str | None]] = set()
    for b in branches:
        for m in _projection_measures(
            b, enrich=True, table=_from_table(b), sunk=True
        ):
            key = (m["alias"] or m["column"], m["agg"], m.get("table"))
            if key in seen:
                continue
            seen.add(key)
            measures.append(m)
    group_by: list[str] = []
    for b in branches:
        g = _extract_group_by(b)
        if g:
            group_by = g
            break
    filters: list[str] = []
    for b in branches:
        fs = _extract_filters(b)
        if fs:
            filters = fs
            break
    head = branches[0]
    time_column = _detect_time_column(head, group_by, filters)
    time_granularity = _detect_time_granularity(head)
    return SqlProfile(
        source_tables=_extract_source_tables(ast),
        group_by=group_by,
        measures=measures,
        filters=filters,
        time_column=time_column,
        time_granularity=time_granularity,
        sql=sql,
    )


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
    if isinstance(ast, exp.Union):
        # U-1：顶层 UNION ALL/UNION 多源合并——遍历全部分支合并度量（见
        # _profile_from_union），不再只取第一个分支
        return _profile_from_union(ast, sql.strip())
    if isinstance(ast, (exp.Insert, exp.Create, exp.Update, exp.Merge)):
        # 取源查询（CTAS / INSERT INTO ... SELECT）
        sub = ast.find(exp.Select)
        if sub is None:
            return SqlProfile(sql=sql.strip())
        select = sub
    if not isinstance(select, exp.Select):
        return SqlProfile(sql=sql.strip())

    source_tables = _extract_source_tables(ast)
    # P0-3a：主 FROM 表排首——``find_all(exp.Table)`` 的 walk 顺序会把 join 右侧
    # 字典表排前（``FROM (SELECT ... FROM ods.raw_event) a LEFT JOIN ods.ref_dict b``
    # → ref_dict 先于 raw_event），source_tables[0] 若为字典表会让无表前缀度量候选
    # 错挂 join 右表。主 FROM 表（穿透子查询）排首后，候选回退 tables[0] 得到正确
    # 主表；度量自身携带 table（_measure_table 穿透别名链）时不受顺序影响。
    main = _from_table(select)
    if main and main in source_tables:
        source_tables.remove(main)
        source_tables.insert(0, main)
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
