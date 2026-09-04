"""dp 调度 SQL 节点解析器（sqlglot 主解析 + 复杂度分级）。

对齐 `spec/dp-lineage-ingest/plan.md` §4（解析管线）：
- 单节点（step）的 ``script_info`` 经 sqlglot 确定性解析（表级 L1 + 字段级 L2 + DDL）
- 明显临时表按排除规则过滤（不入图）
- 复杂度分级：命中特征（子查询深度/CTE/窗口/多 join/解析失败）→ 复杂节点，
  由调用方决定是否走 LLM 确认（D6 分级确认方案 B）
- 三态判定：``ok``（有真实流转可直接入库）/ ``complex``（需 LLM 确认）/
  ``no_flow``（能解析但无数据流转，跳过）/ ``failed``（解析失败，走 LLM 兜底）

本模块为纯函数/无副作用（不连 DB、不调 LLM），便于单元测试与复用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

import sqlglot

from app.services.lineage.parser import (
    DDLEdge,
    FieldEdge,
    TableEdge,
    _preprocess_dialect,
    _split_statements,
    extract_ddl_lineage,
    extract_field_lineage,
    extract_table_lineage,
)

#: 默认复杂度分级规则（可经 dp_sync_config.llm_complexity_rules 覆盖，前端可配置）。
DEFAULT_COMPLEXITY_RULES: dict = {
    "max_subquery_depth": 2,  # 子查询嵌套超过该深度 → 复杂
    "max_cte_count": 3,  # CTE 数超过 → 复杂
    "max_join_count": 4,  # JOIN 数超过 → 复杂
    "has_window": True,  # 含窗口函数 → 复杂
    "has_parse_error": True,  # 任一句子解析失败 → 复杂/失败
    "has_lateral": False,  # 含 LATERAL VIEW → 复杂（保留开关，默认关）
}

#: 明显临时表/中间清理表名特征（默认排除；可由 exclude_table_patterns 覆盖追加）。
DEFAULT_EXCLUDE_TABLE_PATTERNS: list[str] = [
    r"(^|\.)(tmp|temp)[\d_]*$",
    r"(^|\.)tmp_",
    r"_bak$",
    r"(^|\.)adhoc",
]

#: 识别「set 变量赋值」语句（如 ``set hive.exec.dynamic.partition=true``）。
_SET_STMT_RE = re.compile(r"^\s*set\s+", re.IGNORECASE)
#: 识别 USE 切库语句。
_USE_STMT_RE = re.compile(r"^\s*use\s+", re.IGNORECASE)
#: 提取 USE 语句中的库名（支持反引号包裹）。
_USE_DB_RE = re.compile(r"^\s*use\s+[`\"]?([A-Za-z0-9_\-]+)[`\"]?\s*;?\s*$", re.IGNORECASE)

#: dp 调度平台注入的运行期宏占位符（SQL 内无 set 定义，由调度系统运行时替换）。
#: 形如 ``${DATA_DATE}`` / ``${HIVE_DATA_DATE-1}`` / ``${tmp_tabname}``。血缘解析
#: 只关心表/列结构，不关心宏的实际运行值——统一替换为稳定的基准值即可使 sqlglot
#: 可解析；偏移宏按基准日期推算真实日期，保证同脚本内不同偏移展开为互异值，
#: 避免 ``tab_${DATA_DATE}`` 与 ``tab_${DATA_DATE-1}`` 碰撞成同一张表。
_SCHED_MACRO_RE = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*?)(?P<off>[-+]\d+)?\}"
)
#: 日期类宏名（可按 ±N 天偏移）。
_DATE_MACRO_NAMES = {"DATA_DATE", "HIVE_DATA_DATE", "PART_DATE", "TODAY"}
#: 宏展开基准日期（血缘结构语义与日期取值无关，取固定基准即可）。
_MACRO_BASE_DATE = date(2026, 1, 1)


def _expand_schedule_macros(sql: str) -> str:
    """展开 dp 调度宏占位符（解析前文本归一化，血缘语义不变）。

    - 日期宏（DATA_DATE/HIVE_DATA_DATE/PART_DATE/TODAY，含 ``±N`` 天偏移）：
      按基准日期推算并输出 ``YYYYMMDD`` 无横杠（表名/标识符上下文安全），
      偏移互异防同名碰撞；
    - ``YYYY`` → 基准年、``HH`` → ``00``、``tmp_tabname`` → 稳定临时名；
    - ``EXEC_TIME``/``exec_time`` → 稳定时间戳字面量；
    - 其他未知宏 → ``x``（字母占位，避免数字开头破坏标识符；值上下文亦合法）。
    """
    if not sql or "${" not in sql:
        return sql

    def _sub(m: re.Match[str]) -> str:
        name = m.group("name")
        off = int(m.group("off") or 0) if m.group("off") else 0
        if name in _DATE_MACRO_NAMES:
            d = _MACRO_BASE_DATE + timedelta(days=off)
            return d.strftime("%Y%m%d")
        if name == "YYYY":
            return str(_MACRO_BASE_DATE.year)
        if name == "HH":
            return "00"
        if name == "tmp_tabname":
            return "tmp_dp_parse"
        if name in ("EXEC_TIME", "exec_time"):
            return _MACRO_BASE_DATE.strftime("%Y%m%d%H%M%S")
        return "x"

    return _SCHED_MACRO_RE.sub(_sub, sql)


#: 作列名/表别名常见、且被 sqlglot 当作保留字导致整条语句解析失败的裸词。
#: 仅当「替换后能解析成功」才采用（破坏语法的替换会被丢弃），故集合可安全
#: 保守扩大；``order by``/``partition by``/``rows between`` 等语法结构词在
#: 整段替换后 parse 失败自动回退逐词重试，不会误伤。实测 dp 脚本 ``lock``
#: （精神科「是否锁档」字段）为高频触发词。
_QUOTABLE_RESERVED_WORDS: tuple[str, ...] = (
    "lock",
    "key",
    "value",
    "rank",
    "rows",
    "range",
    "offset",
    "bucket",
    "stats",
)


def _quote_reserved_column_words(sql: str, dialect: str | None) -> str:
    """把裸保留字标识符加反引号并验证可解析；无法安全替换则原样返回。

    dp 真实调度脚本存在未加引号的保留字列名（如 ``select lock ... ``），
    sqlglot 严格方言解析失败 → 整条数据流语句被误判 failed 走人工（此前 16 张
    unparseable 中 2 张根因）。阶段 1 全量替换候选词（一次处理多裸列）parse 成功
    即用；阶段 2 逐个词独立替换（``order by`` 等语法词被全量替换破坏时回退），
    任一成功即用。仅替换「替换后能解析」的形态——不改变语句语义（列名等价）。
    """
    if not sql or not any(w in sql.lower() for w in _QUOTABLE_RESERVED_WORDS):
        return sql
    if _parse_ok(sql, dialect):
        return sql  # 原句本就可解析，无需保护

    def _quote(text: str, words: tuple[str, ...]) -> str:
        out = text
        for w in words:
            pat = re.compile(rf"(?<![A-Za-z0-9_`]){w}(?![A-Za-z0-9_`])", re.IGNORECASE)
            out = pat.sub(lambda m, w=w: f"`{w}`", out)
        return out

    # 阶段 1：全部候选词一次替换
    all_quoted = _quote(sql, _QUOTABLE_RESERVED_WORDS)
    if _parse_ok(all_quoted, dialect):
        return all_quoted
    # 阶段 2：逐个词独立替换（order by/partition by 等语法词被误包时回退）
    for w in _QUOTABLE_RESERVED_WORDS:
        one_quoted = _quote(sql, (w,))
        if one_quoted != sql and _parse_ok(one_quoted, dialect):
            return one_quoted
    return sql


def _parse_ok(sql: str, dialect: str | None) -> bool:
    """整段是否可解析（任一语句失败即 False，与 _has_parse_error 一致）。"""
    if not sql or not sql.strip():
        return True
    for stmt in _split_statements(sql):
        try:
            if sqlglot.parse_one(stmt, dialect=dialect) is None:
                return False
        except Exception:  # noqa: BLE001 —— 单语句失败即整体不可解析
            return False
    return True


@dataclass
class StepParseOutcome:
    """单节点解析结果（sqlglot 阶段，未入 LLM）。

    status:
        - ``ok``：有真实数据流转（表级边非空），可直接入库（简单）或待 LLM 确认（复杂）
        - ``no_flow``：能解析但无数据流转（纯建表 DDL/drop/set/use），跳过
        - ``failed``：存在解析失败语句且无可用流转，走 LLM 兜底
    """

    status: str
    table_edges: list[TableEdge] = field(default_factory=list)
    field_edges: list[FieldEdge] = field(default_factory=list)
    ddl_edges: list[DDLEdge] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    error: str | None = None
    used_db: str | None = None

    @property
    def is_complex(self) -> bool:
        """命中复杂度特征（status=ok 时表示需 LLM 确认）。"""
        return bool(self.features)


def _qualify_sql_text(sql: str, dialect: str | None) -> tuple[str, str | None]:
    """按语句级 USE 作用域给无前缀表名补默认库，返回 (改写后 SQL, 最后 use 库)。

    血缘 parser 的 extract_* 不维护 USE 作用域，dp 脚本大量 ``use db; create
    table t ...``（无库前缀）——若不补库，产出节点 ``t`` 与带库前缀产出表 ``db.t``
    无法对齐。逐语句跟踪 USE 并对该语句内所有无 ``db`` 前缀的表节点注入默认库
    （再序列化回 SQL 交给 extract_*），支持脚本中途切换多个库的正确语义。
    """
    default_db: str | None = None
    parts: list[str] = []
    for stmt in _split_statements(sql):
        m = _USE_DB_RE.match(stmt)
        if m:
            default_db = m.group(1)
            parts.append(stmt)
            continue
        if not default_db or _SET_STMT_RE.match(stmt):
            parts.append(stmt)
            continue
        try:
            ast = sqlglot.parse_one(stmt, dialect=dialect)
        except Exception:  # noqa: BLE001 —— 无法解析的语句保留原文，extract 自行降级
            parts.append(stmt)
            continue
        changed = False
        for node in ast.walk():
            if isinstance(node, sqlglot.exp.Table) and not node.db:
                node.set("db", sqlglot.exp.to_identifier(default_db))
                changed = True
        parts.append(ast.sql(dialect) if changed else stmt)
    return ";\n".join(parts), default_db


def _matches_any(name: str, patterns: list[str]) -> bool:
    """表名是否命中任一排除正则（``catalog.db.table`` 全名匹配）。"""
    return any(re.search(p, name) for p in patterns)


def _filter_table_edges(
    edges: list[TableEdge], exclude_patterns: list[str]
) -> list[TableEdge]:
    """剔除源或目标命中排除规则的表级边。"""
    if not exclude_patterns:
        return edges
    return [
        e
        for e in edges
        if not (
            _matches_any(e.source, exclude_patterns)
            or _matches_any(e.target, exclude_patterns)
        )
    ]


def _filter_field_edges(
    edges: list[FieldEdge], exclude_patterns: list[str]
) -> list[FieldEdge]:
    """剔除源/目标表命中排除规则的字段级边。"""
    if not exclude_patterns:
        return edges
    return [
        e
        for e in edges
        if not (
            _matches_any(e.source_table, exclude_patterns)
            or _matches_any(e.target_table, exclude_patterns)
        )
    ]


def _parse_statements(sql: str, dialect: str | None) -> list | None:
    """解析多语句 SQL，返回 AST 列表；整体失败返回 None。

    部分语句无法识别时 sqlglot 在列表中置 None（不抛异常），由调用方按
    「存在解析失败语句」处理。
    """
    try:
        return sqlglot.parse(sql, dialect=dialect)
    except Exception:
        return None


def detect_complexity_features(
    sql: str,
    dialect: str | None,
    rules: dict | None = None,
) -> list[str]:
    """检测 SQL 是否命中复杂度分级特征，返回命中特征名列表（空 = 简单）。

    特征（对应 ``DEFAULT_COMPLEXITY_RULES`` 键）：
        subquery_depth / cte_count / join_count / window / parse_error
    """
    cfg = {**DEFAULT_COMPLEXITY_RULES, **(rules or {})}
    features: list[str] = []
    stmts = _parse_statements(sql, dialect)
    if stmts is None:
        features.append("parse_error")
        return features

    for ast in stmts:
        if ast is None:
            if cfg.get("has_parse_error"):
                features.append("parse_error")
            continue
        # 子查询深度
        max_depth = 0
        for node in ast.walk():
            if isinstance(node, sqlglot.exp.Subquery):
                depth = 1
                parent = node.parent
                while parent is not None:
                    if isinstance(parent, sqlglot.exp.Subquery):
                        depth += 1
                    parent = parent.parent
                max_depth = max(max_depth, depth)
        if max_depth > int(cfg.get("max_subquery_depth", 2)):
            features.append("subquery_depth")
        # CTE 数
        cte_count = sum(
            1 for node in ast.walk() if isinstance(node, sqlglot.exp.CTE)
        )
        if cte_count > int(cfg.get("max_cte_count", 3)):
            features.append("cte_count")
        # JOIN 数
        join_count = sum(
            1 for node in ast.walk() if isinstance(node, sqlglot.exp.Join)
        )
        if join_count > int(cfg.get("max_join_count", 4)):
            features.append("join_count")
        # 窗口函数
        has_window = any(
            isinstance(node, sqlglot.exp.Window) for node in ast.walk()
        )
        if has_window and cfg.get("has_window"):
            features.append("window")
        # LATERAL VIEW（Hive 特性，可选开关）
        has_lateral = any(
            isinstance(node, sqlglot.exp.Lateral) for node in ast.walk()
        )
        if has_lateral and cfg.get("has_lateral"):
            features.append("lateral")
    return list(dict.fromkeys(features))


def _has_parse_error(sql: str, dialect: str | None) -> bool:
    """是否存在解析失败语句（与 extract 逐语句容错语义一致）。

    不用整段 ``sqlglot.parse``（tolerant 列表含 None 判定）——整段解析对
    空 ``;`` 元素/个别方言偶发误判 None，把可解析的数据流脚本误伤为 failed；
    逐语句 ``parse_one`` 失败即抛错，判定精确且与 ``extract_*`` 的容错路径
    完全同源（失败的语句被跳过、成功的语句照常出边）。
    """
    for stmt in _split_statements(sql):
        try:
            if sqlglot.parse_one(stmt, dialect=dialect) is None:
                return True
        except Exception:  # noqa: BLE001 —— 单语句解析失败即视为存在失败语句
            return True
    return False


#: 数据流关键字——存在即意味着脚本可能在搬移数据（SQL 演进宏/方言失败时仍应
#: 归 unparseable 交 LLM/人工；纯 DDL 建表/drop/set 不含这些词则直接 no_flow 跳过）。
_DATAFLOW_KEYWORD_RE = re.compile(
    r"\b(?:insert\s+(?:into|overwrite)|as\s+select|"
    r"select\s+|load\s+data|create\s+table\s+[^;()]*\s+as\s+select|"
    r"overwrite\s+table|into\s+table)\b",
    re.IGNORECASE,
)

#: DDL 结构关键字——判定「解析失败但确实在操作表结构」（宏/方言 DDL 建表），
#: 区别于完全不可识别的垃圾文本（shell 残留等，仍归 failed 交 LLM/人工甄别）。
_DDL_STRUCT_KEYWORD_RE = re.compile(
    r"\b(?:create\s+(?:external\s+)?table|drop\s+table|alter\s+table|"
    r"truncate\s+table|set\s+|use\s+\w+|add\s+jar|create\s+temporary\s+function)\b",
    re.IGNORECASE,
)


def _has_dataflow_keyword(sql: str) -> bool:
    """去注释/字符串后粗判脚本是否含真实数据搬移语句。

    解析失败的脚本（宏 `${DATA_DATE}`、方言建表等）若**不含**任何数据流关键字，
    判定为「纯 DDL 无流转」（如 ``create table x (列定义)``）——血缘上无输入输出，
    人工抉择无可采纳对象，应 no_flow 跳过而非堆 unparseable 待抉择单。
    """
    if not sql:
        return False
    cleaned = re.sub(r"--[^\n]*", "", sql)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    return bool(_DATAFLOW_KEYWORD_RE.search(cleaned))


def _is_ddl_only_script(sql: str) -> bool:
    """脚本无数据流但含 DDL 结构词（宏/方言建表、set/use 等表结构操作）。

    与垃圾文本（shell 残留等）区分：后者无任何 DDL 结构词，仍归 failed
    交 LLM/人工甄别，避免把「非 SQL 脚本」静默吞掉。
    """
    if not sql:
        return False
    cleaned = re.sub(r"--[^\n]*", "", sql)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    return bool(_DDL_STRUCT_KEYWORD_RE.search(cleaned))


def parse_dp_step(
    sql: str,
    dialect: str | None = "hive",
    exclude_patterns: list[str] | None = None,
    rules: dict | None = None,
    target_table: str | None = None,
    schema_columns: dict[str, list[str]] | None = None,
) -> StepParseOutcome:
    """解析单个 dp SQL 节点，产出三态判定结果。

    Args:
        sql: 节点 ``script_info`` 原文（可含 set/use/drop 前缀与多语句）。
        dialect: sqlglot 方言，dp 脚本为 Hive/Spark，默认 ``"hive"``。
        exclude_patterns: 排除表名正则（默认 ``DEFAULT_EXCLUDE_TABLE_PATTERNS``）。
        rules: 复杂度分级规则（默认 ``DEFAULT_COMPLEXITY_RULES``）。
        target_table: 可选落点表（纯 SELECT 场景显式指向产出表）。
        schema_columns: 可选源表列清单（方案 3 star 展开），透传给字段解析。
    """
    if not sql or not sql.strip():
        return StepParseOutcome(status="no_flow", error="空脚本")
    patterns = (
        exclude_patterns
        if exclude_patterns is not None
        else list(DEFAULT_EXCLUDE_TABLE_PATTERNS)
    )
    # 先做 set 变量展开（复用血缘 parser 的变量展开能力），保证方言/变量形态可解析。
    try:
        prepared = _preprocess_dialect(sql, dialect, None)
    except Exception:  # noqa: BLE001 —— 预处理失败仍交由原生解析尝试
        prepared = sql
    # dp 调度宏展开：${DATA_DATE} 等由调度平台运行时注入（SQL 内无 set 定义），
    # 不展开 sqlglot 无法解析——统一替换为稳定基准值使数据流可解析（血缘结构不变）。
    prepared = _expand_schedule_macros(prepared)

    # 按语句级 USE 作用域给无前缀表名补默认库（dp 脚本标准形态：use db; create ...）
    qualified, used_db = _qualify_sql_text(prepared, dialect)
    # 保留字列保护：裸保留字列名（lock/key/value…）令 sqlglot 整条失败——仅在
    # 「替换后能解析」时改写，使含真实数据流的脚本不再被误判 failed（语义不变）。
    qualified = _quote_reserved_column_words(qualified, dialect)
    table_edges = extract_table_lineage(qualified, dialect, target_table=target_table)
    field_edges = extract_field_lineage(
        qualified, dialect, target_table=target_table, schema_columns=schema_columns
    )
    ddl_edges = extract_ddl_lineage(qualified, dialect)

    # 排除明显临时表
    filtered_table = _filter_table_edges(table_edges, patterns)
    filtered_field = _filter_field_edges(field_edges, patterns)

    parse_error = _has_parse_error(qualified, dialect)

    # 三态判定
    if filtered_table:
        # 有真实流转：命中复杂特征 → complex（status 仍 ok，由调用方决定 LLM 确认）
        features = detect_complexity_features(prepared, dialect, rules)
        return StepParseOutcome(
            status="ok",
            table_edges=filtered_table,
            field_edges=filtered_field,
            ddl_edges=ddl_edges,
            features=features,
            used_db=used_db,
        )
    # 无表级流转：区分「能解析但纯建表/无流转」与「解析失败」
    if parse_error:
        if not _has_dataflow_keyword(qualified) and _is_ddl_only_script(qualified):
            # 解析失败但脚本是纯 DDL 结构（宏/方言建表，无数据搬移）——
            # 血缘上无输入输出，判 no_flow 跳过，避免噪音淹没人工抉择工作台。
            return StepParseOutcome(
                status="no_flow",
                table_edges=filtered_table,
                field_edges=filtered_field,
                ddl_edges=ddl_edges,
                features=["no_dataflow"],
                error="解析失败且无数据流关键字（纯 DDL/宏建表，跳过）",
                used_db=used_db,
            )
        return StepParseOutcome(
            status="failed",
            table_edges=filtered_table,
            field_edges=filtered_field,
            ddl_edges=ddl_edges,
            features=["parse_error"],
            error="存在解析失败语句且无可用数据流转",
            used_db=used_db,
        )
    return StepParseOutcome(
        status="no_flow",
        table_edges=filtered_table,
        field_edges=filtered_field,
        ddl_edges=ddl_edges,
        error="能解析但无数据流转（纯建表 DDL/drop/set/use）",
        used_db=used_db,
    )
