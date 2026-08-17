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


#: Hive/Spark 变量占位符：``${hivevar:name}`` / ``${hiveconf:name}`` / ``${name}``。
#: 仅匹配合法变量名（字母/数字/下划线/点），避免误伤 SQL 中的其他 ``${...}`` 片段。
_HIVE_VAR_PATTERN = re.compile(
    r"\$\{(?:hivevar|hiveconf):([A-Za-z_][A-Za-z0-9_.]*)\}|\$\{([A-Za-z_][A-Za-z0-9_.]*)\}"
)
#: Hive ``set`` 命令：``set hivevar:name=value;`` / ``set hiveconf:name=value;``（可无分号）。
_HIVE_SET_PATTERN = re.compile(
    r"^\s*set\s+(?:hivevar|hiveconf):([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*([^;]+?)\s*;?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
#: Hive 注释行变量声明：``--hivevar name=value`` / ``-- hivevar:name=value``。
_HIVEVAR_COMMENT_PATTERN = re.compile(
    r"^\s*--\s*(?:hivevar|hiveconf)\s*:?\s*([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _collect_hive_vars(sql: str, variables: dict[str, str] | None) -> dict[str, str]:
    """收集待展开的 Hive 变量表（优先级低→高：显式传入 < set 命令 < 注释行）。

    显式传入的 ``variables`` 作为基线，SQL 文本内的 ``set hivevar:x=y;`` 与
    ``--hivevar x=y`` 声明逐行覆盖（SQL 内的声明更贴近本次任务）。
    """
    merged = dict(variables or {})
    for m in _HIVE_SET_PATTERN.finditer(sql):
        merged[m.group(1)] = m.group(2).strip()
    for m in _HIVEVAR_COMMENT_PATTERN.finditer(sql):
        merged[m.group(1)] = m.group(2).strip()
    return merged


def expand_variables(
    sql: str, dialect: str | None = None, variables: dict[str, str] | None = None
) -> str:
    """展开 Hive/Spark 变量占位符（解析前文本归一化，血缘语义不变）。

    生产模板 SQL 常以 ``${hivevar:date_id}`` / ``${hiveconf:dt}`` / ``${tbl}`` 引用
    变量；sqlglot 无法解析这些占位符会抛 ParseError 致血缘全丢。本函数在解析前
    把已知变量替换为字面值，未知占位符**保留原样**（sqlglot 解析失败时血缘降级
    为空而不崩——与整体降级策略一致）。

    仅对 Hive/Spark 方言展开（``${var}`` 是 Hive 模板语法），或调用方显式传入
    ``variables`` 时对任意方言展开。变量值来自：
    - 调用方显式 ``variables``（如批处理/API 参数）；
    - SQL 文本内的 ``set hivevar:name=value;`` / ``set hiveconf:name=value;`` 命令；
    - SQL 文本内的 ``--hivevar name=value`` 注释行。

    ``set`` 声明行本身保留（不影响血缘解析；sqlglot 对非查询语句降级为空）。
    """
    if not sql or not sql.strip():
        return sql
    d = (dialect or "").lower()
    if d not in ("hive", "spark") and not variables:
        return sql
    vars_ = _collect_hive_vars(sql, variables)
    if not vars_:
        return sql

    def _sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        # 未知变量保留占位符原样（解析失败降级，不伪造血缘）
        return vars_.get(name, m.group(0))

    return _HIVE_VAR_PATTERN.sub(_sub, sql)


def _preprocess_dialect(
    sql: str, dialect: str | None, variables: dict[str, str] | None = None
) -> str:
    """方言级语法归一化（解析前预处理，血缘语义不变）。

    sqlglot 30.x 对部分生产高频方言语法不支持（降级为 ``Command`` 或抛解析异常），
    这里在解析前做等价改写：
    - hive/spark：先展开 ``${var}`` 变量占位符（``expand_variables``）——模板 SQL
      不展开 sqlglot 无法解析，血缘全丢；展开后血缘语义不变。
    - mysql/doris/starrocks：``REPLACE INTO ... SELECT`` → ``INSERT INTO ... SELECT``
      （sqlglot 不支持 REPLACE；血缘语义等价——REPLACE 即覆盖式插入，来源表/列映射
      完全一致，仅写入动作不同）。
    - doris/starrocks：剥离 ``INSERT INTO t WITH LABEL 'xxx' SELECT ...`` 中的
      ``WITH LABEL 'xxx'`` 片段（sqlglot 不支持 Doris 的 LABEL 标记）。
    - doris/starrocks：剥离 ``CREATE TABLE ... AS SELECT`` 的物理分布/副本属性
      （``DISTRIBUTED BY ... [BUCKETS n]``、``PROPERTIES(...)``、``ENGINE=``）与
      AGGREGATE KEY 列定义的列级聚合类型后缀（``v INT SUM``）——
      sqlglot 25.x 对 ``CREATE TABLE t DISTRIBUTED BY HASH(id) BUCKETS 10 AS SELECT``
      或 ``v INT SUM`` 整体降级为 Command/抛 ParseError 致血缘全丢；这些子句仅描述
      物理布局/聚合方式，不影响 SELECT 源与目标表，剥离后血缘语义不变。
    - tsql/mssql：INSERT TOP (n) INTO t SELECT ... → 剥离 TOP (n) 行数限定
      （sqlglot 25.x 不支持该语法抛 ParseError 致血缘全丢；限行不影响来源表与列映射，
      血缘语义不变）。
    """
    if not sql or not sql.strip():
        return sql
    d = (dialect or "").lower()
    if d in ("hive", "spark") or variables:
        sql = expand_variables(sql, dialect, variables)
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
        # 剥离 AGGREGATE KEY / UNIQUE KEY 表键子句（``AGGREGATE KEY(id)``）：
        # sqlglot 25.x 仅支持 DUPLICATE KEY / PRIMARY KEY，遇 AGGREGATE/UNIQUE KEY
        # 降级为 Command 致血缘全丢；表键仅描述排序/去重语义，不影响 SELECT 源与
        # 目标表，剥离后血缘语义不变。
        sql = re.sub(
            r"\b(?:AGGREGATE|UNIQUE)\s+KEY\s*\([^)]*\)",
            " ",
            sql,
            flags=re.IGNORECASE,
        )
        # 剥离 AGGREGATE KEY 列定义的列级聚合类型后缀（``v INT SUM`` → ``v INT``）：
        # sqlglot 25.x 不支持列定义里的聚合类型（SUM/MAX/MIN/REPLACE/HLL_UNION 等）
        # 抛 ParseError 致血缘全丢；聚合类型仅描述列级聚合方式，不影响 SELECT 源与
        # 目标表，剥离后血缘语义不变。仅匹配「类型名 + 聚合类型」组合，不会误伤
        # SELECT 中的 ``SUM(v)``。
        sql = re.sub(
            r"\b(BOOLEAN|TINYINT|SMALLINT|INT|BIGINT|LARGEINT|FLOAT|DOUBLE|"
            r"DECIMAL(?:\([^)]*\))?|CHAR(?:\([^)]*\))?|VARCHAR(?:\([^)]*\))?|"
            r"STRING|DATE|DATETIME|TIMESTAMP)\s+"
            r"(SUM|MAX|MIN|REPLACE|HLL_UNION|BITMAP_UNION|PERCENTILE_UNION)\b",
            r"\1",
            sql,
            flags=re.IGNORECASE,
        )
    if d in ("tsql", "mssql"):
        # tsql ``INSERT TOP (n) INTO t SELECT ...``：sqlglot 25.x 不支持该语法直接抛
        # ParseError 致血缘全丢；剥离 TOP (n) 行数限定（不影响来源表与列映射，血缘
        # 语义不变——仅限制插入行数）。
        sql = re.sub(r"\bINSERT\s+TOP\s*\([^)]*\)", "INSERT", sql, flags=re.IGNORECASE)
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


def _multitable_branches(ast: exp.MultitableInserts) -> list[tuple[str, exp.Insert]]:
    """提取 Oracle ``INSERT ALL/FIRST`` 的各目标分支 ``(目标表名, Insert 分支)``。

    ``MultitableInserts.expressions`` 是 ``ConditionalInsert``（多目标/条件插入），
    每个 ``.this`` 是真正的 ``Insert``（``this`` 为 ``Schema``：目标表 + 列清单，
    ``args["expression"]`` 为 ``Values`` 行）。``_find_target`` 只认单目标，多目标
    需逐分支处理——否则非首目标表会被 ``find_all(exp.Table)`` 误当作源表，产生
    ``dws.t2->dws.t1`` 伪边污染血缘图。
    """
    out: list[tuple[str, exp.Insert]] = []
    for cond in ast.args.get("expressions", []) or []:
        ins = cond.this if isinstance(cond, exp.ConditionalInsert) else cond
        if not isinstance(ins, exp.Insert):
            continue
        if isinstance(ins.this, exp.Schema) and isinstance(ins.this.this, exp.Table):
            out.append((_norm_table(ins.this.this), ins))
    return out


def _multitable_table_edges(ast: exp.MultitableInserts) -> list[TableEdge]:
    """Oracle ``INSERT ALL/FIRST`` 表级血缘：source 查询的源表 → 每个目标表。"""
    source = ast.args.get("source")
    if not isinstance(source, (exp.Select, exp.SetOperation)):
        return []
    cte_names = _collect_ctes(ast)
    src_tables: set[str] = set()
    for tbl in source.find_all(exp.Table):
        src = _norm_table(tbl)
        if src and not _is_cte_ref(tbl, cte_names):
            src_tables.add(src)
    edges: list[TableEdge] = []
    seen: set[tuple[str, str]] = set()
    for target_name, _ins in _multitable_branches(ast):
        for src in src_tables:
            if src == target_name:
                continue
            key = (src, target_name)
            if key in seen:
                continue
            seen.add(key)
            edges.append(TableEdge(source=src, target=target_name))
    return edges


def _table_edges(ast: Any) -> list[TableEdge]:
    """单条已解析语句的表级血缘边（source -> target）。"""
    if isinstance(ast, exp.MultitableInserts):
        # Oracle INSERT ALL/FIRST 多目标：逐分支处理（见 _multitable_table_edges）
        return _multitable_table_edges(ast)
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
    sql: str,
    dialect: str | None = None,
    target_table: str | None = None,
    variables: dict[str, str] | None = None,
) -> list[TableEdge]:
    """抽取表级血缘。

    Args:
        sql: SQL 文本（支持注释/多语句，自动净化）。
        dialect: sqlglot dialect（可选，如 ``"mysql"`` / ``"hive"`` / ``"doris"`` /
            ``"clickhouse"``）。
        target_table: 可选落点表（方案 A+B）。SQL 自身无写入目标（纯 SELECT）但指定
            了该值时，把 FROM/JOIN 源表 → ``target_table`` 生成表级边；未指定时纯
            SELECT 保持无成边（返回空，由调用方降级展示上游依赖）。
        variables: 可选 Hive/Spark 变量表（``${hivevar:name}`` 占位符展开用，
            P5 宏展开；仅对 hive/spark 方言或显式传入时生效）。

    Returns:
        表级血缘边列表（source -> target）；解析失败或非法 SQL 时返回空列表（降级）。
    """
    edges: list[TableEdge] = []
    seen: set[tuple[str, str]] = set()
    sql = _preprocess_dialect(sql, dialect, variables)
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
    """判断某 scope 的表达式是否输出指定列名。

    集合运算子查询（``UNION/EXCEPT/INTERSECT``）的 scope 不聚合分支 sources，
    需展开各分支检查（如 ``SELECT x FROM (SELECT a.id AS x ... UNION SELECT b.uid AS x ...) u``
    的 x 在任一分支输出即算）。
    """
    expr = getattr(scope, "expression", None)
    if isinstance(expr, exp.SetOperation):
        return any(
            _projection_name(p) == col_name
            for branch in _branch_queries(expr)
            for p in (getattr(branch, "selects", None) or [])
        )
    selects = getattr(scope, "selects", None)
    if not selects:
        return False
    return any(_projection_name(p) == col_name for p in selects)


def _cte_outputs_column(cte: exp.CTE, col_name: str) -> bool:
    """CTE 定义 SELECT 是否输出指定列名（用于未限定列解析时判定某 CTE 是否含该列）。"""
    selects = getattr(cte.this, "selects", None) or []
    return any(_projection_name(p) == col_name for p in selects)


def _unnest_outputs_column(unnest: exp.Unnest, col_name: str) -> bool:
    """UNNEST 展开表是否输出指定列名。

    PG ``UNNEST(a.items) AS u(v)`` 的列别名列表是 ``[v]``；无列别名时
    （``AS u``）展开列名默认等于表别名 ``u``。
    """
    alias = unnest.args.get("alias")
    if not isinstance(alias, exp.TableAlias):
        return False
    cols = alias.args.get("columns") or []
    if cols:
        return any(c.name == col_name for c in cols)
    return alias.name == col_name


def _lateral_outputs_column(lateral: exp.Lateral, col_name: str) -> bool:
    """LATERAL VIEW 展开表是否输出指定列名。

    Hive ``LATERAL VIEW EXPLODE(a.tags) e AS tag`` 的别名（``TableAlias``）挂在
    ``Lateral`` 节点上、列清单是 ``[tag]``；无列清单时展开列名默认等于表别名。
    展开列的字段血缘来源是 EXPLODE 表达式内的叶子列（``a.tags``）。
    """
    alias = lateral.args.get("alias")
    if not isinstance(alias, exp.TableAlias):
        return False
    cols = alias.args.get("columns") or []
    if cols:
        return any(c.name == col_name for c in cols)
    return alias.name == col_name


def _resolve_setop_column(
    expr: exp.SetOperation,
    col_name: str,
    cte_map: dict[str, exp.CTE],
    dialect: str | None,
    depth: int,
) -> list[tuple[str, str]]:
    """集合运算输出列解析：逐分支找同名投影递归解析，收集所有分支来源。

    分支列名不同时按位置回退——UNION 输出列名取自首分支，
    ``SELECT id FROM (SELECT id FROM a UNION SELECT uid FROM b) u`` 的 ``u.id``
    位置对应第二分支的 ``uid``，应同时解析到 ``a.id`` 与 ``b.uid``。
    """
    branches = _branch_queries(expr)
    if not branches:
        return []
    ref_idx = -1
    for i, p in enumerate(getattr(branches[0], "selects", None) or []):
        if _projection_name(p) == col_name:
            ref_idx = i
            break
    out: list[tuple[str, str]] = []
    for branch in branches:
        branch_scope = _try_build_scope(branch)
        if branch_scope is None:
            continue
        projs = getattr(branch, "selects", None) or []
        target: exp.Expression | None = None
        for p in projs:
            if _projection_name(p) == col_name:
                target = p
                break
        if target is None and 0 <= ref_idx < len(projs):
            target = projs[ref_idx]
        if target is not None:
            out.extend(_resolve_projection(branch_scope, target, cte_map, dialect, depth + 1))
    return out


def _resolve_projection(
    scope: Any,
    projection: exp.Expression,
    cte_map: dict[str, exp.CTE],
    dialect: str | None,
    depth: int,
) -> list[tuple[str, str]]:
    """递归解析某个投影表达式，返回其所有叶子列的 (真实表名, 列名)。

    覆盖三类结构：
    - 普通叶子列：直接按当前 scope 解析；
    - 标量子查询（SELECT 列表 / CASE 分支的 ``(SELECT ...)``）：子查询内部列须在
      子查询自身 scope 内解析——未限定列归子查询表（``(SELECT max(v) FROM ods.d)``
      的 ``v`` 归 ``ods.d``），相关引用（``s.id``）回退外层 scope；
    - 命名窗口引用（``ROW_NUMBER() OVER w``）：PARTITION/ORDER 列定义在 Select 的
      ``WINDOW w AS (...)`` 子句而非投影表达式内，按名查窗口定义补充派生源。
    """
    if depth > _MAX_DEPTH:
        return []
    out: list[tuple[str, str]] = []
    select_windows = getattr(getattr(scope, "expression", None), "args", {}).get("windows") or []
    for wnd in projection.find_all(exp.Window):
        ref = getattr(wnd.args.get("alias"), "name", None)
        if not ref:
            continue
        for wdef in select_windows:
            if getattr(wdef, "alias_or_name", None) != ref:
                continue
            for col in wdef.find_all(exp.Column):
                out.extend(_resolve_column(scope, col, cte_map, dialect, depth + 1))
            break
    for leaf in projection.find_all(exp.Column):
        out.extend(_resolve_leaf_scope(scope, leaf, cte_map, dialect, depth))
    return out


def _innermost_subquery(leaf: exp.Column) -> exp.Subquery | None:
    """返回包含 ``leaf`` 的最内层标量子查询节点（沿 parent 链向上查找）。"""
    node = leaf.parent
    while node is not None:
        if isinstance(node, exp.Subquery):
            return node
        node = node.parent
    return None


def _resolve_leaf_scope(
    scope: Any,
    leaf: exp.Column,
    cte_map: dict[str, exp.CTE],
    dialect: str | None,
    depth: int,
) -> list[tuple[str, str]]:
    """解析投影中的单个叶子列——若位于标量子查询内部，用子查询自身 scope。

    子查询内部列有三种形态，需区分 scope：
    - 限定列命中子查询自身表（``d.id``）→ 子查询 scope 内解析（``ods.d.id``）；
    - 未限定列（``max(v)`` 的 ``v``）→ 子查询 scope 优先（``ods.d.v``），未命中
      再回退外层（避免误归属外层表产生 ``s.v`` 伪边）；
    - 限定列不在子查询来源（``s.id`` 相关引用）→ 外层 scope 解析。
    """
    subq = _innermost_subquery(leaf)
    if subq is None:
        return _resolve_column(scope, leaf, cte_map, dialect, depth)
    inner = _try_build_scope(subq.this)
    if inner is None:
        return _resolve_column(scope, leaf, cte_map, dialect, depth)
    inner_sources = getattr(inner, "sources", {}) or {}
    if not inner_sources:
        return _resolve_column(scope, leaf, cte_map, dialect, depth)
    if leaf.table and leaf.table in inner_sources:
        return _resolve_column(inner, leaf, cte_map, dialect, depth)
    if not leaf.table:
        resolved = _resolve_column(inner, leaf, cte_map, dialect, depth)
        if resolved:
            return resolved
    return _resolve_column(scope, leaf, cte_map, dialect, depth)


def _matching_unpivot(table: exp.Table, alias: str) -> exp.Pivot | None:
    """在源表节点上查找别名匹配的 UNPIVOT（``UNPIVOT (...) AS u``）。

    sqlglot 把 ``UNPIVOT`` 解析为挂在源表上的 ``exp.Pivot``（``unpivot=True``），
    其别名（``u``）不在 scope 的 sources 中，需按源表逐个匹配。
    """
    for piv in table.find_all(exp.Pivot):
        if not piv.args.get("unpivot"):
            continue
        p_alias = piv.args.get("alias")
        if isinstance(p_alias, exp.TableAlias) and p_alias.alias_or_name == alias:
            return piv
    return None


def _unpivot_output_sources(
    scope: Any,
    piv: exp.Pivot,
    col: exp.Column,
    cte_map: dict[str, exp.CTE],
    dialect: str | None,
    depth: int,
) -> list[tuple[str, str]]:
    """UNPIVOT 输出列解析。

    值列（``expressions`` 命名的列，如 ``UNPIVOT (v FOR ...)`` 的 ``v``）的血缘来源
    是 ``FOR ... IN (a, b, c)`` 列表里的源列（``s.a/s.b/s.c``，多源）；名列
    （``field.this``，如 ``k``）是列名字面量（元数据），不构成数据血缘，返回空。
    """
    field = piv.args.get("field")
    if not isinstance(field, exp.In):
        return []
    value_cols = [e.name for e in (piv.args.get("expressions") or [])]
    if col.name not in value_cols:
        return []
    out: list[tuple[str, str]] = []
    for src_col in field.expressions:
        out.extend(_resolve_column(scope, exp.column(src_col.name), cte_map, dialect, depth + 1))
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
    if src is None and qualifier:
        # UNPIVOT 输出列（``UNPIVOT (v FOR k IN (a, b, c)) AS u``）：qualifier ``u``
        # 是挂在源表节点上的 Pivot 别名（不出现在 sources）。值列 ``u.v`` 的血缘来源
        # 是 In 列表的源列（``s.a/s.b/s.c``）；名列 ``u.k`` 是列名字面量，无数据血缘。
        for _name, s in sources.items():
            if not isinstance(s, exp.Table):
                continue
            piv = _matching_unpivot(s, qualifier)
            if piv is not None:
                return _unpivot_output_sources(scope, piv, col, cte_map, dialect, depth)
    if src is None:
        # 未限定列：**优先** UNNEST/LATERAL VIEW 展开表的显式列名声明
        # （PG ``UNNEST(a.items) AS u`` / Hive ``EXPLODE(a.tags) e AS tag`` 明确声明
        # 展开列名，``SELECT u``/``SELECT tag`` 即数组元素列）。真实表的命中仅是
        # "表存在"猜测（可能指向不存在的列），故排在展开表显式声明之后。
        for _name, s in sources.items():
            if (
                isinstance(s, Scope)
                and isinstance(s.expression, exp.Unnest)
                and _unnest_outputs_column(s.expression, col.name)
            ):
                src = s
                break
            if (
                isinstance(s, Scope)
                and isinstance(s.expression, exp.Lateral)
                and _lateral_outputs_column(s.expression, col.name)
            ):
                src = s
                break
    if src is None:
        # 未限定列：其次真实表（非 CTE 引用）。CTE 引用可能不含该列——
        # 如 ``SELECT id FROM c1 JOIN ods.b USING(id)`` 里 ``max(v)`` 的 v 实际
        # 来自 ods.b，而 c1 只输出 id；若取第一个来源（CTE c1）会错误解析为空。
        for _name, s in sources.items():
            if isinstance(s, exp.Table) and s.name not in cte_map:
                src = s
                break
        if src is None:
            # 再次：输出该列的 CTE 引用（穿透定义解析）
            for _name, s in sources.items():
                if (
                    isinstance(s, exp.Table)
                    and s.name in cte_map
                    and _cte_outputs_column(cte_map[s.name], col.name)
                ):
                    src = s
                    break
        if src is None:
            # 最后：输出该列的子查询/派生表（推断列，优先级最低）
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
            # 进入 CTE 定义：找同名投影并递归解析。
            # CTE 定义为集合运算（``WITH x AS (SELECT ... UNION ...)``）时，Union
            # scope 不聚合分支 sources，需逐分支找含该列的投影递归解析；UNION 合并列
            # 同时来自多个分支（``a.id AS x`` / ``b.uid AS x``），收集所有分支来源。
            cte_select = cte.this
            if isinstance(cte_select, exp.SetOperation):
                return _resolve_setop_column(cte_select, col.name, cte_map, dialect, depth + 1)
            inner = build_scope(cte_select)
            if inner is None:
                return []
            for p in getattr(cte_select, "selects", []):
                if _projection_name(p) == col.name:
                    return _resolve_projection(inner, p, cte_map, dialect, depth + 1)
            return []
        return [(name, col.name)]

    if isinstance(src, Scope):
        if isinstance(src.expression, exp.Unnest):
            # UNNEST 展开表（PG 数组展开）：展开列的血缘来源是 Unnest 表达式
            # 的叶子列——``UNNEST(a.items) AS u(v)`` 的 ``u.v`` 应归属 ``a.items``，
            # 而非当作 ``a`` 的真实列或丢弃。
            return _resolve_projection(scope, src.expression, cte_map, dialect, depth + 1)
        if isinstance(src.expression, exp.Lateral):
            # LATERAL 有两种形态，需区分处理：
            # - PG/SQL Server 相关子查询（``LATERAL (SELECT v FROM d WHERE ...) x``）：
            #   ``Lateral.this`` 是 Subquery，``x.v`` 应进入内层 Select 解析（其 scope
            #   只含内层来源表，外层 ``s`` 不参与，避免 ``v`` 误归属外层表产生伪边）。
            # - Hive ``LATERAL VIEW EXPLODE(a.tags) e AS tag``：``Lateral.this`` 是
            #   Explode/UDTF，展开列的血缘来源是表达式内 EXPLODE 的叶子列（``a.tags``）。
            lateral_inner = src.expression.this
            if isinstance(lateral_inner, exp.Subquery):
                inner = lateral_inner.this
                inner_scope = build_scope(inner) or src
                for p in getattr(inner, "selects", []):
                    if _projection_name(p) == col.name:
                        return _resolve_projection(inner_scope, p, cte_map, dialect, depth + 1)
                return []
            return _resolve_projection(scope, src.expression, cte_map, dialect, depth + 1)
        if isinstance(src.expression, exp.SetOperation):
            # 集合运算子查询（``SELECT x FROM (... UNION ...) u``）：Union scope
            # 不聚合分支 sources，需逐分支找含该列的投影递归解析。UNION 合并列同时
            # 来自多个分支（``a.id AS x`` / ``b.uid AS x``），收集所有分支的来源。
            return _resolve_setop_column(src.expression, col.name, cte_map, dialect, depth + 1)
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
        # 自环边跳过：源表与目标表相同（如 ``INSERT INTO t SELECT id FROM t`` 是
        # 覆盖式更新，``t.id→t.id`` 不构成血缘流转），自环边会造成图谱节点自连。
        if source_table == target_name:
            continue
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
        # 命名窗口引用（``ROW_NUMBER() OVER w``）的 PARTITION/ORDER 列在 Select 的
        # WINDOW 子句而非投影内，``leaf_cols`` 为空但仍有派生源（``_resolve_projection``
        # 会按名补解析）——不能仅因无内联列就跳过。
        named_window = any(
            getattr(w.args.get("alias"), "name", None) for w in projection.find_all(exp.Window)
        )
        if not leaf_cols and not named_window:
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


def _emit_update_pair(
    lhs: exp.Expression,
    val: exp.Expression,
    amap: dict[str, str],
    target_name: str,
    cte_map: dict[str, exp.CTE],
    dialect: str | None,
    edges: list[FieldEdge],
) -> None:
    """为 UPDATE 的单个 SET 赋值（lhs ← val）解析字段血缘。

    ``lhs`` 是目标列（单列 or 多列合并分出的单列）、``val`` 是其值表达式。
    目标表按 LHS 列所属表独立判定（多表 UPDATE 跨 SET 时各被更新表都是目标，不能
    全局取首表）；LHS 无表限定（单表 UPDATE）时用主目标兜底。处理子查询值 /
    CTE 穿透 / 普通表达式三种来源。
    """
    target_col = _column_name(lhs)
    if not target_col:
        return
    lhs_alias = lhs.table if isinstance(lhs, exp.Column) else None
    item_target = amap.get(lhs_alias) if lhs_alias else target_name
    if not item_target:
        item_target = target_name
    if isinstance(val, exp.Subquery):
        # SET 值为子查询：子查询 SELECT 的投影列 → 目标列（无别名时用目标列名）
        sub = _try_build_scope(val.this)
        if sub is None:
            return
        star_ast = lhs.parent or lhs
        for projection in getattr(val.this, "selects", None) or []:
            if _projection_has_star(projection):
                edges.append(_star_edge(projection, sub, star_ast, item_target))
                continue
            sub_col = _projection_name(projection) or target_col
            is_bare = _is_bare_column_projection(projection)
            _emit_leaf_edges(
                sub, projection, cte_map, dialect, item_target, sub_col, is_bare, edges
            )
        return
    # 普通表达式：逐个解析值中的列引用到来源表
    is_bare = isinstance(val, exp.Column)
    expr_sql = None if is_bare else val.sql(dialect=dialect)
    for leaf in val.find_all(exp.Column):
        # 自引用：源列与 LHS 同属一张被更新表（如 ``t1.v = t1.v * 2``）→ 跳过
        if not leaf.table or leaf.table == lhs_alias:
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
                            target_table=item_target,
                            target_column=target_col,
                            expression=expr_sql,
                        )
                    )
                break
            continue
        src_t = amap.get(leaf.table)
        if not src_t or src_t == item_target:
            continue
        edges.append(
            FieldEdge(
                source_table=src_t,
                source_column=leaf.name,
                target_table=item_target,
                target_column=target_col,
                expression=expr_sql,
            )
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
    多表 UPDATE 跨 SET（``UPDATE t1 JOIN t2 SET t1.v=t2.v, t2.w=t1.x``）时，每个 SET
    项的目标表独立判定为 LHS 列所属表——字段级血缘完整双向（``t2.v→t1.v`` 与
    ``t1.x→t2.w``，字段节点不同不构成环）。表级血缘受血缘图 DAG 约束保持单目标
    （主目标 = 首个被 SET 更新表），互刷方向不入表级图谱。
    自引用（值列与目标同属目标表 / 无来源表限定的列）不产生跨表字段边。
    """
    amap = _build_alias_map(update)
    # CTE 引用（``FROM s`` 的 ``s``）不是真实表：从别名映射中剔除，避免伪表边
    amap = {k: v for k, v in amap.items() if k not in cte_map}
    for eq in getattr(update, "expressions", None) or []:
        if not isinstance(eq, exp.EQ):
            continue
        # 单列赋值 ``col = val`` 与 PG 多列合并赋值 ``(a,b) = (x,y)`` 统一展开为
        # 单列组按位置处理——多列合并时目标列组与值组逐列 zip 映射
        if isinstance(eq.this, exp.Tuple) and isinstance(eq.expression, exp.Tuple):
            pairs = list(zip(eq.this.expressions, eq.expression.expressions, strict=False))
        else:
            pairs = [(eq.this, eq.expression)]
        for lhs, val in pairs:
            _emit_update_pair(lhs, val, amap, target_name, cte_map, dialect, edges)


def _extract_multitable_edges(
    ast: exp.MultitableInserts,
    cte_map: dict[str, exp.CTE],
    dialect: str | None,
    edges: list[FieldEdge],
) -> None:
    """Oracle ``INSERT ALL/FIRST`` 字段级血缘：每个目标分支 VALUES 列 → 目标列。

    ``MultitableInserts`` 无单一 SELECT 投影——各分支的列引用在 ``Values`` 行中
    （``INTO dws.t1 (id) VALUES (s.id)``）。以 source 查询为作用域解析各分支
    VALUES 列的叶子列，按分支列清单映射目标列。
    """
    source = ast.args.get("source")
    if not isinstance(source, (exp.Select, exp.SetOperation)):
        return
    scope = _try_build_scope(source)
    if scope is None:
        return
    for target_name, ins in _multitable_branches(ast):
        cols = _insert_column_list(ins)
        values = ins.args.get("expression")
        if not isinstance(values, exp.Values):
            continue
        for row in values.expressions:
            items = row.expressions if isinstance(row, exp.Tuple) else [row]
            for i, val in enumerate(items):
                target_col = cols[i] if i < len(cols) else _column_name(val)
                if not target_col:
                    continue
                is_bare = _is_bare_column_projection(val)
                _emit_leaf_edges(
                    scope, val, cte_map, dialect, target_name, target_col, is_bare, edges
                )


def _extract_field_edges(ast: Any, dialect: str | None) -> list[FieldEdge]:
    """单条已解析语句的字段级血缘边（含 MERGE 专用路径与星号降级标记）。"""
    edges: list[FieldEdge] = []
    if isinstance(ast, exp.MultitableInserts):
        # Oracle INSERT ALL/FIRST 多目标：逐分支处理（见 _extract_multitable_edges）
        _extract_multitable_edges(ast, _collect_ctes(ast), dialect, edges)
        return edges
    target = _find_target(ast)
    target_name = _norm_table(target) if target is not None else ""
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
    sql: str,
    dialect: str | None = None,
    target_table: str | None = None,
    variables: dict[str, str] | None = None,
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
        variables: 可选 Hive/Spark 变量表（P5 宏展开，同 ``extract_table_lineage``）。

    Returns:
        字段级血缘边列表（含降级标记）；解析不可用或失败时返回空列表（降级）。
    """
    edges: list[FieldEdge] = []
    seen: set[tuple[object, ...]] = set()
    sql = _preprocess_dialect(sql, dialect, variables)
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


def extract_upstream_deps(
    sql: str, dialect: str | None = None, variables: dict[str, str] | None = None
) -> UpstreamDeps:
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
        variables: 可选 Hive/Spark 变量表（P5 宏展开，同 ``extract_table_lineage``）。

    Returns:
        ``UpstreamDeps``：去重排序的 ``tables`` / ``fields``；解析失败降级为空。
    """
    tables: set[str] = set()
    fields: set[str] = set()
    sql = _preprocess_dialect(sql, dialect, variables)
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
