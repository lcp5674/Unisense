"""SQL 批量切分与多指标候选推断（FR-010 批量注册增强，场景A/B）。

场景A 多语句切分（一段长 SQL 含多个指标）：
- ``semicolon``：引号感知 ``;`` 扫描（单/双引号与反引号内的分号不算段边界）；
- ``statement``：sqlglot ``parse()`` 按语句语义切分（CTE/INSERT 单条天然正确）；
- ``custom``：用户自定义规则切分（``custom_rules`` 含 ``delimiters``/``start_markers`` 正则）；
  切分结果 ≤1 段时借助 LLM 按语义分段兜底（不可用/失败降级整段单候选）。

场景B 单语句多度量：``split_select_measures`` 把一条 SELECT 的多个聚合投影拆为
N 个原子候选（共享源表/维度/时间谓词，度量列/聚合/名称不同），并可合成一个
复合指标候选（``synthesize_composite``，依赖组内 N 原子）。

对齐 spec FR-010, plan.md SQL 批量注册（场景A/B 判定规则由用户确认：
按 ``;`` 切分 + CTE/INSERT 语义切分 + 用户自定义规则 + LLM 兜底；场景B 保留
多度量合成复合指标选项）。
"""

from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp

from app.services.semantic.auto_fill import (
    _field,
    build_profile,
    extract_biz_object,
    generate_metric_code,
    infer_metric,
)
from app.services.semantic.sql_infer import SqlProfile, parse_sql_profile

# 引号字符（单/双/反引号）——分号扫描须跳过字符串/标识符内的分号
_QUOTES = ('"', "'", "`")
# 阈值：自定义切分未生效（≤1 段）才触发 LLM 语义分段兜底
_LLM_SPLIT_MIN_SEGMENTS = 2


# ----------------------------------------------------------------
# 场景A：多语句切分
# ----------------------------------------------------------------


def _split_semicolon(sql: str) -> list[str]:
    """引号 + 注释感知 ``;`` 扫描切分（引号/注释内分号不算段边界）。

    行注释 ``-- ...`` 与块注释 ``/* ... */`` 内的分号一律不切断（保留在段内）。
    """
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in _QUOTES:
            quote = ch
            buf.append(ch)
        elif ch == "-" and nxt == "-":
            # 行注释：整段保留到行尾（含分号），不切断
            while i < n and sql[i] != "\n":
                buf.append(sql[i])
                i += 1
            continue
        elif ch == "/" and nxt == "*":
            # 块注释：整段保留到 */（含分号），不切断
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                buf.append(sql[i])
                i += 1
            if i + 1 < n:
                buf.append(sql[i])
                buf.append(sql[i + 1])
                i += 1
        elif ch == ";":
            segment = "".join(buf).strip()
            if segment:
                segments.append(segment)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        segments.append(tail)
    return segments


def _split_statement(sql: str) -> list[str]:
    """sqlglot 按语句语义切分（CTE/INSERT 单条天然正确；注释/引号内分号不误切）。

    多方言依次尝试（默认 → hive → spark）：默认方言遇 Hive 特有语法
    （如 ``create table ... stored as orc``）会整段抛错，回退 hive/spark 方言
    保证 ETL 脚本可切分（避免因一条 DDL 导致整段回退到分号切分）。
    """
    for dialect in (None, "hive", "spark"):
        try:
            asts = sqlglot.parse(sql, dialect=dialect)
        except Exception:
            continue
        segments: list[str] = []
        for ast in asts:
            if ast is None:
                continue
            text = ast.sql().strip()
            if text:
                segments.append(text)
        if len(segments) >= _LLM_SPLIT_MIN_SEGMENTS:
            return segments
    return []


def _split_by_start_markers(sql: str, start_markers: list[Any]) -> list[str]:
    """按起始标记正则切分：每个标记命中的位置作为新段起点。"""
    positions: list[int] = []
    for marker in start_markers:
        if not marker:
            continue
        try:
            positions.extend(m.start() for m in re.finditer(str(marker), sql))
        except re.error:
            continue  # 非法正则忽略（不阻断其余标记）
    positions = sorted(set(positions))
    if not positions:
        return []
    segments: list[str] = []
    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(sql)
        segment = sql[pos:end].strip()
        if segment:
            segments.append(segment)
    return segments


def _split_custom(sql: str, custom_rules: dict[str, Any] | None) -> list[str]:
    """用户自定义规则切分：先 ``delimiters`` 分隔符，再 ``start_markers`` 起始标记。

    两个维度都未命中或结果仍 ≤1 段时返回空列表（上层据此决定 LLM 兜底/降级单段）。
    """
    rules = custom_rules or {}
    delimiters = rules.get("delimiters") or []
    start_markers = rules.get("start_markers") or []
    if delimiters:
        pattern = "|".join(str(d) for d in delimiters if d)
        if pattern:
            parts = re.split(pattern, sql)
            segments = [p.strip() for p in parts if p.strip()]
            if len(segments) >= _LLM_SPLIT_MIN_SEGMENTS:
                return segments
    if start_markers:
        segments = _split_by_start_markers(sql, start_markers)
        if len(segments) >= _LLM_SPLIT_MIN_SEGMENTS:
            return segments
    return []


async def _llm_split(db: Any, sql: str) -> list[str] | None:
    """LLM 按语义分段（custom 规则未生效时兜底）；不可用/失败返回 None。"""
    try:
        from app.services.llm.config_service import LlmConfigService
        from app.services.llm.parse import parse_sql_split_result

        client = await LlmConfigService(db).build_client()
        if not getattr(client, "enabled", False):
            return None
        prompt = (
            "下面是一段包含多个指标的 SQL 脚本，请按指标语义拆分为独立语句。"
            "不要改写语句内容，只做切分；每条返回原始 SQL 片段。\n\n"
            f"SQL 脚本：\n{sql}\n\n"
            "请只返回 JSON（不要解释、不要 Markdown 代码块）："
            '{"statements": [{"sql": "片段1", "name": "指标1名", "reason": "一句话依据"}, ...]}'
        )
        resp = await client.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
        )
        raw = (resp.get("content") or "").strip()
        parsed = parse_sql_split_result(raw)
        if not parsed:
            return None
        return [item["sql"] for item in parsed]
    except Exception:
        return None  # LLM 不可用/超时/解析失败 → 降级单段


async def _llm_infer_period(db: Any, statement_sql: str) -> str | None:
    """LLM 从整段 SQL 推断统计周期（规则层无时间信号时兜底）。

    不同 SQL 场景（窗口函数 partition by 月份、无 GROUP BY 时间列、Oracle
    ``trunc(x,'MM')`` 等）规则层可能漏判——此时让 LLM 看整段 SQL 判断
    统计周期。不可用/超时/解析失败返回 None（上层降级规则层默认 day）。
    """
    try:
        from app.services.llm.config_service import LlmConfigService
        from app.services.llm.parse import parse_period_infer_result

        client = await LlmConfigService(db).build_client()
        if not getattr(client, "enabled", False):
            return None
        prompt = (
            "下面是一段指标定义 SQL。请判断该指标最可能的统计周期："
            "看 GROUP BY 的时间粒度/时间列的截断方式（如 substr(x,1,7) 是月、"
            "date_trunc('month',x) 是月、按日分区 dt 是日）。"
            "只返回 JSON（不要解释、不要 Markdown 代码块）："
            '{"period": "day|week|month|quarter|year|hour", "confidence": 0到1的小数, '
            '"reason": "一句话依据"}\n\n'
            f"SQL：\n{statement_sql}"
        )
        resp = await client.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
        )
        raw = (resp.get("content") or "").strip()
        parsed = parse_period_infer_result(raw)
        return parsed["period"] if parsed else None
    except Exception:
        return None  # LLM 不可用/超时/解析失败 → 降级规则层默认


async def split_sql_statements(
    sql: str,
    mode: str = "statement",
    custom_rules: dict[str, Any] | None = None,
    db: Any = None,
) -> list[str]:
    """按模式切分多语句 SQL。

    Args:
        sql: 完整 SQL 脚本。
        mode: ``semicolon`` / ``statement`` / ``custom``。
        custom_rules: custom 模式的自定义切分规则（delimiters/start_markers）。
        db: 异步会话（custom 模式 LLM 兜底需要；None 则跳过 LLM）。

    Returns:
        切分后的语句列表（至少 1 段；极端失败降级整段）。
    """
    if not sql or not sql.strip():
        return []
    if mode == "semicolon":
        segments = _split_semicolon(sql)
    elif mode == "custom":
        segments = _split_custom(sql, custom_rules)
        if len(segments) < _LLM_SPLIT_MIN_SEGMENTS and db is not None:
            llm_segments = await _llm_split(db, sql)
            if llm_segments:
                return llm_segments
    else:  # statement（默认）：语义切分失败时回退引号感知分号切分
        segments = _split_statement(sql)
        if len(segments) < _LLM_SPLIT_MIN_SEGMENTS:
            segments = _split_semicolon(sql)
    if not segments:
        segments = [sql.strip()]
    return segments


# ----------------------------------------------------------------
# 场景B：单语句多度量拆分
# ----------------------------------------------------------------


def _period_from_profile(profile: SqlProfile) -> str:
    """从画像推断统计周期（默认 day）。

    优先级：明确时间粒度（``substr(x,1,7) as month_id`` 等截断/别名信号，
    ``profile.time_granularity``）> 时间列名 token。ETL 截月表达式此前被
    取成底层列 create_date 回落 day——粒度信号直接修正这一误判。
    """
    if profile.time_granularity:
        return profile.time_granularity
    tc = (profile.time_column or "").lower()
    for token, period in (
        ("week", "week"), ("wk", "week"),
        ("month", "month"), ("mo", "month"),
        ("quarter", "quarter"), ("qtr", "quarter"),
        ("year", "year"), ("yr", "year"),
        ("hour", "hour"),
    ):
        if token in tc:
            return period
    return "day"


def _period_uncertain(profile: SqlProfile) -> bool:
    """规则层是否无法确定统计周期（无截断/别名粒度信号且无时间列）。

    True 时上层调用 LLM 兜底推断；False 时规则层结果可信（如 ``dt`` 分区=日、
    ``substr(x,1,7) as month_id``=月），不浪费 LLM 调用。
    """
    return profile.time_granularity is None and profile.time_column is None


def _group_key(columns: list[str]) -> str:
    """组标识：度量列名拼接（去下划线截断，保序去重）。

    段内**不含下划线**——4 段式指标编码用 ``_`` 分隔（域_业务对象_度量_周期），
    groupkey 若含下划线会把复合编码拆成 >4 段（如 ``{domain}_{biz}_{a_b}_day``
    变 5 段），违反 ``validate_metric_code`` 的 4 段约束。
    """
    parts: list[str] = []
    for col in columns:
        seg = str(col).replace("_", "")[:12]
        if seg and seg not in parts:
            parts.append(seg)
    key = "".join(parts)[:40]
    return key or "composite"


def split_select_measures(
    statement_sql: str, profile: SqlProfile | None = None
) -> list[dict[str, Any]]:
    """场景B：单语句多度量拆分为 N 个度量候选（共享源表/维度/时间谓词）。

    Args:
        statement_sql: 单条指标 SQL。
        profile: 已解析画像（缺省内部解析）。

    Returns:
        ``[{"source_table", "measure_column", "aggregation", "period", "group_key"}]``。
    """
    if profile is None:
        profile = parse_sql_profile(statement_sql)
    tables = profile.source_tables
    table = tables[0] if tables else None
    period = _period_from_profile(profile)
    group_key = _group_key([m["column"] for m in profile.measures])
    out: list[dict[str, Any]] = []
    for m in profile.measures:
        out.append(
            {
                # 下沉场景度量自带来源表（聚合所在子查询），优先于 tables[0]
                # （tables[0] 可能是 join 右侧的字典表）
                "source_table": m.get("table") or table,
                "measure_column": m["column"],
                "aggregation": m["agg"],
                "period": period,
                "group_key": group_key,
            }
        )
    return out


# ----------------------------------------------------------------
# 候选构建
# ----------------------------------------------------------------


def _build_atomic_candidate(
    *,
    idx: int,
    measure: dict[str, Any],
    table: str | None,
    period: str,
    domain_code: str | None,
    domain_defaults: dict[str, Any],
    time_column: str | None,
) -> dict[str, Any]:
    """构建原子候选：expression 模式推断（勿传多度量原 SQL，避免兄弟度量进口径），
    聚合方式覆盖为 SQL 解析值（比列名规则更可靠）。

    ``measure`` 可能来自下沉收集（ETL 透传 INSERT），自带 ``alias/table/expression``：
    key 用 alias 防同列不同语义冲突，源表/口径优先用度量自身携带值。
    """
    col = measure["column"]
    agg = measure["agg"] or "COUNT"
    alias = measure.get("alias")
    measure_table = measure.get("table") or table
    profile = build_profile(source_table=measure_table, measure_column=col, period=period)
    profile["domain_code"] = domain_code or ""
    result = infer_metric(profile, domain_defaults=domain_defaults or {})
    fields = result["fields"]
    fields["aggregation"] = _field(
        agg, "sql_parse", 0.95, f"SQL 度量 {col} 使用 {agg} 聚合"
    )
    expression = measure.get("expression") or f"{agg}({col})"
    definition: dict[str, Any] = {
        "expression": expression,
        "source_fields": [{"table": measure_table, "column": col}] if measure_table else [],
        "partition_key": time_column,
    }
    metric_code = result.get("metric_code_suggestion")
    if not metric_code and domain_code and measure_table and period:
        metric_code = generate_metric_code(domain_code, measure_table, col, period)
    return {
        "key": f"{idx}:{alias or col}",
        "metric_code": metric_code,
        "name": fields["name"]["value"],
        "type": "atomic",
        "source_table": measure_table,
        "measure_column": col,
        "aggregation": agg,
        "period": period,
        "unit": fields["unit"]["value"],
        "granularity": fields["granularity"]["value"],
        "definition_json": definition,
        "definition_mode": "expression",
        "statement_index": idx,
    }


def _build_composite_candidate(
    *,
    idx: int,
    sql: str,
    profile: SqlProfile,
    atoms: list[dict[str, Any]],
    domain_code: str | None,
    period: str,
) -> dict[str, Any] | None:
    """合成复合候选：依赖组内 N 原子编码，口径 SQL=原语句（保留多度量计算结构）。"""
    codes = [c["metric_code"] for c in atoms if c.get("metric_code")]
    if len(codes) < 2:
        return None
    table = atoms[0].get("source_table")
    biz = extract_biz_object(table) if table else "entity"
    groupkey = _group_key([str(c.get("measure_column", "")) for c in atoms])
    names = "、".join(str(c.get("name", "")) for c in atoms)
    return {
        "key": f"{idx}:composite",
        "metric_code": f"{domain_code}_{biz}_{groupkey}_day" if domain_code else None,
        # 原子名已含周期前缀（如「日金额」），复合名直接拼接避免「日日」重复
        "name": f"{names}复合"[:128],
        "type": "composite",
        "source_table": table,
        "measure_column": None,
        "aggregation": None,
        "period": period,
        "unit": None,
        "granularity": "day",
        "definition_json": {
            "sql": sql,
            "dependencies": codes,
            "source_tables": profile.source_tables,
        },
        "definition_mode": "sql",
        "dependencies": codes,
        "statement_index": idx,
    }


def _statement_meta(idx: int, sql: str, profile: SqlProfile) -> dict[str, Any]:
    """语句摘要（前端 Collapse 分组标题用）。"""
    return {
        "index": idx,
        "sql": sql[:2000],
        "source_tables": profile.source_tables,
        "measure_count": len(profile.measures),
        "group_by": profile.group_by,
    }


def _physical_source_tables(statement_sql: str, profile: SqlProfile) -> list[str]:
    """过滤 CTE 别名，仅保留真实物理源表。

    sqlglot 把 FROM 子句引用的 CTE 名也解析为 ``exp.Table``（如 ``WITH base AS (...)``
    中 ``FROM base`` 的 ``base``），直接取 ``source_tables[0]`` 会得到 CTE 别名而非
    物理表——导致指标编码/数仓层推断错挂到 CTE 名上。此处剔除 CTE 定义名。
    """
    tables = list(profile.source_tables)
    try:
        ast = sqlglot.parse_one(statement_sql)
        cte_names = {
            cte.alias_or_name.lower()
            for cte in ast.find_all(exp.CTE)
            if cte.alias_or_name
        }
        if cte_names:
            physical = [
                t for t in tables if t.split(".")[-1] not in cte_names
            ]
            if physical:
                return physical
    except Exception:
        pass
    return tables


def _domain_payload(
    suggestion: dict[str, Any] | None, domain_code: str | None
) -> dict[str, Any]:
    """域建议负载：用户未提供域时携带 suggest_domain 四态结果，供前端展示/确认。"""
    if suggestion is None:
        return {
            "code": domain_code,
            "name": None,
            "status": "user" if domain_code else "none",
            "confidence": None,
            "candidates": [],
            "matched_tables": [],
        }
    domain = suggestion.get("domain") or {}
    return {
        "code": domain.get("code") if domain else domain_code,
        "name": domain.get("name") if domain else None,
        "status": suggestion.get("status"),
        "confidence": domain.get("confidence") if domain else None,
        "candidates": suggestion.get("candidates", []),
        "matched_tables": suggestion.get("matched_tables", []),
    }


async def _load_domain_defaults(db: Any, domain_code: str) -> dict[str, Any]:
    """域默认值预设（推断最高优先级）；域不存在/异常/返回非 dict 时回退空。"""
    try:
        from app.services.subject_domain.service import SubjectDomainService

        defaults = await SubjectDomainService(db).get_defaults(domain_code)
        if isinstance(defaults, dict):
            return defaults
    except Exception:
        pass
    return {}


# ----------------------------------------------------------------
# 批量推断主函数
# ----------------------------------------------------------------


async def infer_sql_batch(
    db: Any,
    *,
    sql: str,
    split_mode: str = "statement",
    custom_rules: dict[str, Any] | None = None,
    domain_code: str | None = None,
    synthesize_composite: bool = False,
) -> dict[str, Any]:
    """SQL 批量推断主函数：切分 → 逐语句画像 → 候选生成 → 域建议。

    Args:
        db: 异步会话（域建议/自定义 LLM 分段需要）。
        sql: 大段 SQL 脚本。
        split_mode: 切分模式（semicolon/statement/custom）。
        custom_rules: custom 模式的自定义切分规则。
        domain_code: 用户显式指定的域（缺省则整段一次批量建议）。
        synthesize_composite: 单语句多度量时是否合成复合候选。

    Returns:
        ``{"statements", "candidates", "skipped", "domain"}``。
        ``candidates`` 的 ``key`` 稳定标识 ``{语句序号}:{度量列}``（原子）/
        ``{语句序号}:composite``（复合），前端勾选与后端创建均用它定位。
    """
    if not sql or not sql.strip():
        return {"statements": [], "candidates": [], "skipped": [], "domain": None}

    segments = await split_sql_statements(
        sql, mode=split_mode, custom_rules=custom_rules, db=db
    )
    if not segments:
        segments = [sql.strip()]

    # 域建议：用户未指定域时整段一次批量建议（目录/挂载反查 + LLM 兜底）
    suggestion: dict[str, Any] | None = None
    if not domain_code:
        from app.services.semantic.domain_suggest import suggest_domain

        suggestion = await suggest_domain(db, sql=sql)
        if suggestion.get("status") in ("unique", "llm"):
            domain_code = suggestion["domain"]["code"]

    domain_defaults = await _load_domain_defaults(db, domain_code) if domain_code else {}

    statements: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for idx, seg in enumerate(segments):
        profile = parse_sql_profile(seg)
        statements.append(_statement_meta(idx, seg, profile))
        if not profile.measures:
            skipped.append(
                {"index": idx, "sql": seg[:500], "reason": "未解析到聚合度量列"}
            )
            continue
        tables = _physical_source_tables(seg, profile)
        table = tables[0] if tables else None
        period = _period_from_profile(profile)
        # 规则层无时间信号（无截断/别名粒度、无时间列）→ LLM 兜底推断周期；
        # LLM 不可用/失败降级规则层默认，绝不阻断候选生成
        if _period_uncertain(profile) and db is not None:
            llm_period = await _llm_infer_period(db, seg)
            if llm_period:
                period = llm_period
        atoms: list[dict[str, Any]] = []
        for measure in profile.measures:
            atoms.append(
                _build_atomic_candidate(
                    idx=idx,
                    measure=measure,
                    table=table,
                    period=period,
                    domain_code=domain_code,
                    domain_defaults=domain_defaults,
                    time_column=profile.time_column,
                )
            )
        candidates.extend(atoms)
        if synthesize_composite:
            composite = _build_composite_candidate(
                idx=idx,
                sql=seg,
                profile=profile,
                atoms=atoms,
                domain_code=domain_code,
                period=period,
            )
            if composite:
                candidates.append(composite)

    return {
        "statements": statements,
        "candidates": candidates,
        "skipped": skipped,
        "domain": _domain_payload(suggestion, domain_code),
    }
