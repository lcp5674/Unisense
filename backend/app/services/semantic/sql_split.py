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

import asyncio
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
from app.services.semantic.sql_infer import (
    _INDUSTRIAL_DIALECTS,
    SqlProfile,
    parse_sql_profile,
    sql_has_arithmetic,
)

# 引号字符（单/双/反引号）——分号扫描须跳过字符串/标识符内的分号
_QUOTES = ('"', "'", "`")
# 阈值：自定义切分未生效（≤1 段）才触发 LLM 语义分段兜底
_LLM_SPLIT_MIN_SEGMENTS = 2
# P1-2：单批推断的 LLM 兜底调用总额度（度量提取 + 周期推断共用计数）——
# 多语句脚本若逐条失败语句都调 LLM 会打满配额/拖慢解析；超限降级 skipped
# 并标注 llm_limit（前端提示「已达本批 AI 兜底上限」），不阻断其余语句。
_LLM_BATCH_LIMIT = 5
# use_llm 显式模式的批级 LLM 预算（4×规则兜底）：用户主动选择 LLM 推断时放宽
# 逐语句兜底额度，但 max_batch_statements=100 上限仍保留防超大脚本爆炸；超限
# 语句降级 skipped(llm_limit) 不阻断（候选补全的整段单次调用也计入此预算）。
_LLM_BATCH_LIMIT_LLM = 20
# 逐语句 LLM 兜底（周期/度量提取）的并发上限——多语句同时触发兜底时用
# asyncio.gather 并发执行，把墙钟从「N×单次调用」降到「并发数内的单次调用」；
# 同时限制并发数防止同时打爆 LLM 网关（配合批级预算双护栏）。
_LLM_FALLBACK_CONCURRENCY = 4


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
    """按语句语义切分多语句 SQL，优先保留原文切片。

    三步策略：
    1. **默认方言语义切分**确定理想段数 + **引号/注释感知分号切分**（``_split_semicolon``）
       取原文切片：两者段数一致 → 无 CTE/INSERT 内分号 → 用原文切片。sqlglot 方言
       ``ast.sql()`` 序列化会改写方言语法（ClickHouse ``sumMerge`` → ``SUMMERGE``、
       ``countIf`` → ``COUNT_IF``），原文切片完整保留方言写法，后续 ``parse_sql_profile``
       方言识别不丢度量。
    2. 段数不一致（CTE/INSERT 内分号等语义边界）→ 用默认方言序列化（语义正确，
       罕见场景接受序列化改写）。
    3. 默认方言不足 2 段 → 工业方言语义切分（Doris/ClickHouse 等 DDL 语法，见
       ``sql_infer._INDUSTRIAL_DIALECTS``）；仍不足 → 返回原文分号切片兜底。

    Returns:
        至少 2 段的原文/序列化切片；极端失败返回 ``[]``（上层降级整段）。
    """
    semicolon_segments = _split_semicolon(sql)
    try:
        default_asts = sqlglot.parse(sql)
    except Exception:
        default_asts = None
    if default_asts is not None:
        default_segments = [
            ast.sql().strip()
            for ast in default_asts
            if ast is not None and ast.sql().strip()
        ]
        if len(default_segments) >= _LLM_SPLIT_MIN_SEGMENTS:
            if len(default_segments) == len(semicolon_segments):
                return semicolon_segments
            return default_segments
    for dialect in _INDUSTRIAL_DIALECTS:
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
    if len(semicolon_segments) >= _LLM_SPLIT_MIN_SEGMENTS:
        return semicolon_segments
    return []


# P2-3：custom 切分正则安全护栏——用户自定义规则（delimiters/start_markers）为任意
# 正则，直接 re.split/re.finditer 可能携带灾难性回溯（ReDoS）构造拖垮 worker。
# 校验：长度上限 + 嵌套量词检测 + 可编译性，任一不满足即跳过该规则（不阻断其余规则）。
_CUSTOM_REGEX_MAX_LEN = 200
# 灾难性回溯特征：嵌套量词/分组内量词或 alternation 后紧跟量词
# （如 (a+)+、(a*)*、(a|a)+、(.*.*)+、(a{1,3}){2,}）
_NESTED_QUANTIFIER_RE = re.compile(
    r"\([^()]*(?:[+*]|\{\d+,\d*\}|\|)[^()]*\)[+*]|\([^()]*\)\{[^}]*\}"
)


def _safe_custom_regex(pattern: Any) -> re.Pattern[str] | None:
    """校验并编译自定义切分正则；非法/危险（ReDoS 风险/过长）返回 None（跳过该规则）。"""
    if not isinstance(pattern, str):
        return None
    if not pattern.strip() or len(pattern) > _CUSTOM_REGEX_MAX_LEN:
        return None
    if _NESTED_QUANTIFIER_RE.search(pattern):
        return None
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _split_by_start_markers(sql: str, start_markers: list[Any]) -> list[str]:
    """按起始标记正则切分：每个标记命中的位置作为新段起点。

    P2-3：每个标记经 ``_safe_custom_regex`` 校验（长度/嵌套量词/可编译），
    危险或非法正则跳过，避免 ReDoS 拖垮 worker。
    """
    positions: list[int] = []
    for marker in start_markers:
        compiled = _safe_custom_regex(marker)
        if compiled is None:
            continue
        try:
            positions.extend(m.start() for m in compiled.finditer(sql))
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
    P2-3：delimiters 逐条经 ``_safe_custom_regex`` 校验（长度/嵌套量词/可编译），
    危险或非法规则跳过，避免拼接出灾难性回溯正则（ReDoS）。
    """
    rules = custom_rules or {}
    delimiters = rules.get("delimiters") or []
    start_markers = rules.get("start_markers") or []
    if delimiters:
        compiled_parts = [
            c.pattern for c in (_safe_custom_regex(d) for d in delimiters) if c is not None
        ]
        if compiled_parts:
            pattern = "|".join(compiled_parts)
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


async def _llm_infer_period(
    db: Any, full_sql: str, focus_sql: str | None = None
) -> str | None:
    """LLM 从整段 SQL 推断统计周期（规则层无时间信号时兜底）。

    不同 SQL 场景（窗口函数 partition by 月份、无 GROUP BY 时间列、Oracle
    ``trunc(x,'MM')`` 等）规则层可能漏判——此时让 LLM 看整段 SQL 判断
    统计周期。传**完整脚本 + 焦点语句**：完整脚本提供上下文（CTE/前置语句/
    注释），LLM 只针对焦点语句下结论——切分后的单段可能已丢失来源上下文。
    不可用/超时/解析失败返回 None（上层降级规则层默认 day）。
    """
    try:
        from app.services.llm.config_service import LlmConfigService
        from app.services.llm.parse import parse_period_infer_result

        client = await LlmConfigService(db).build_client()
        if not getattr(client, "enabled", False):
            return None
        prompt = (
            "下面是一段完整的 SQL 脚本（可能含多条语句/建表/注释）：\n"
            f"{full_sql}\n\n"
            "请判断以下【焦点语句】对应的指标最可能的统计周期："
            "看 GROUP BY 的时间粒度/时间列的截断方式（如 substr(x,1,7) 是月、"
            "date_trunc('month',x) 是月、按日分区 dt 是日）。\n"
            f"焦点语句：\n{focus_sql or full_sql}\n\n"
            "只返回 JSON（不要解释、不要 Markdown 代码块）："
            '{"period": "day|week|month|quarter|year|hour", "confidence": 0到1的小数, '
            '"reason": "一句话依据"}'
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


async def _llm_infer_measures(
    db: Any, full_sql: str, focus_sql: str | None = None
) -> list[dict[str, Any]] | None:
    """LLM 兜底：规则层解析不出聚合度量时，让 LLM 从 SQL 提取度量清单。

    触发时机：``parse_sql_profile`` 返回空 ``measures``（sqlglot 不支持方言 /
    Doris 剥离后仍失败 / 极端嵌套等）。传**完整脚本 + 焦点语句**：完整脚本提供
    上下文（CTE 定义、前置 SET/变量、注释、兄弟语句），LLM 只从焦点语句中提取
    度量——方言语句的字段来源/口径常依赖前置定义，只看切分后的单段会信息丢失。
    LLM 提取 ``[{column, agg, alias, table, period, name}]``，结构对齐
    ``_build_atomic_candidate`` 下沉场景可消费的 measure 字段（alias/table 用于
    区分同列多语义、source_fields 还原口径）。不可用/超时/解析失败返回 ``None``
    （上层降级 skipped，绝不阻断批量解析）。

    Args:
        db: 异步会话（LLM 客户端构建需要）。
        full_sql: 完整原始 SQL 脚本（上下文）。
        focus_sql: 待提取度量的焦点语句（缺省用完整脚本）。

    Returns:
        ``[{"column", "agg", "alias"?, "table"?, "period"?, "name"?}]``；
        失败返回 ``None``。
    """
    try:
        from app.services.llm.config_service import LlmConfigService
        from app.services.llm.parse import parse_sql_measures_result

        client = await LlmConfigService(db).build_client()
        if not getattr(client, "enabled", False):
            return None
        prompt = (
            "下面是一段完整的 SQL 脚本（可能含多条语句/建表/CTE/注释等）：\n"
            f"{full_sql}\n\n"
            "请仅从以下【焦点语句】中提取指标度量列（完整脚本仅供理解字段来源与"
            "口径上下文，不要提取其它语句的度量）：\n"
            f"{focus_sql or full_sql}\n\n"
            "判断标准：被 SUM/COUNT/AVG/MAX/MIN/COUNT(DISTINCT) 等聚合函数包裹的"
            "列或表达式（含 CASE WHEN 聚合、COUNT(DISTINCT CASE WHEN ...)、方言"
            "聚合 sumIf/sumMerge/countIf/approx_distinct 等），每个聚合对应一个度量。\n"
            "只返回 JSON（不要解释、不要 Markdown 代码块）："
            '{"measures": [{"column": "度量列名", "agg": "SUM|COUNT|COUNT_DISTINCT|AVG|MAX|MIN",'
            ' "alias": "投影别名(若有)", "table": "来源物理表(能判断才填)",'
            ' "period": "day|week|month|quarter|year|hour", "name": "简短中文指标名建议"}, ...],'
            ' "source_table": "主来源表(无法判断留null)"}\n'
            "如果焦点语句确实没有聚合度量，返回 {\"measures\": []}。\n"
        )
        resp = await client.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
        )
        raw = (resp.get("content") or "").strip()
        parsed = parse_sql_measures_result(raw)
        return parsed if parsed else None
    except Exception:
        return None  # LLM 不可用/超时/解析失败 → 降级 skipped


async def _llm_annotate_candidates(
    db: Any,
    full_sql: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """LLM 批量补全候选（use_llm 显式模式）：对规则解析出的候选做**封闭选择**。

    设计（对齐「规则锚定 + LLM 补全 + 规范收敛」架构，解决"一次性给 LLM +
    每次输出一样 + 准确"不可能三角）：
    - **锚定**：候选由 sqlglot 规则解析产出（column/agg/table/period 确定可审计）；
    - **LLM 角色收缩**：LLM 不做自由生成（列名/聚合/源表不让 LLM 发明，杜绝
      幻觉列污染候选），只对每个候选做 ``is_measure``（是否真度量）/ ``name``
      （中文指标名润色）/ ``period``（白名单周期）的**封闭选择**——决策面从
      「生成几十个字段」压缩到「对 N 个候选各做 2-3 个选择」，幻觉空间被几何级压缩；
    - **单次调用**：整段 SQL 只花 **1 次** LLM 调用（N 个候选打包进一次 JSON 输出），
      ``temperature=0`` + ``json_object`` 约束输出分布稳定；
    - **规范收敛**由调用方（``infer_sql_batch`` → ``_apply_candidate_annotations``）
      执行：白名单 / 列名回映 / 稳定排序 / 置信度——LLM 的随机性在算法层被剥掉。

    Args:
        db: 异步会话（LLM 客户端构建需要）。
        full_sql: 完整原始 SQL 脚本（上下文）。
        candidates: 规则解析出的候选清单（锚点，含 ``key`` 稳定标识）。

    Returns:
        ``[{key, is_measure, name?, period?, confidence?, reason?}]``；
        失败返回 ``None``（上层保持规则候选不动，绝不阻断）。
    """
    try:
        from app.services.llm.config_service import LlmConfigService
        from app.services.llm.parse import parse_sql_candidates_annotations

        client = await LlmConfigService(db).build_client()
        if not getattr(client, "enabled", False):
            return None
        rows = "\n".join(
            f"- key={c['key']} | 度量列={c.get('measure_column') or '-'} | "
            f"聚合={c.get('aggregation') or '-'} | 源表={c.get('source_table') or '-'} | "
            f"建议名={c.get('name') or '-'} | 建议周期={c.get('period') or '-'}"
            for c in candidates
        )
        prompt = (
            "下面是一段完整的 SQL 脚本（可能含多条语句/建表/CTE/注释等）：\n"
            f"{full_sql}\n\n"
            "已用程序从其中解析出以下候选指标（key 是稳定标识），请逐一对每个候选"
            "做封闭选择（不要新增/发明候选，不要改写度量列/聚合/源表）：\n"
            f"{rows}\n\n"
            "对每个候选判断：\n"
            "1. is_measure：它真的是一个业务度量指标吗？（true/false；例如投影里的"
            "分组键/常量/普通业务键不是度量）\n"
            "2. name：更准确的简短中文指标名（在候选基础上润色，不要发明不在 SQL 里的指标）\n"
            "3. period：统计周期，只从 day|week|month|quarter|year|hour 选"
            "（看 GROUP BY 时间粒度）\n"
            "4. confidence：0 到 1 的小数，你对以上判断的确信度\n"
            "只返回 JSON（不要解释、不要 Markdown 代码块）："
            '{"candidates": [{"key": "候选key", "is_measure": true, "name": "中文名",'
            ' "period": "day", "confidence": 0.9, "reason": "一句话依据"}, ...]}'
        )
        resp = await client.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=2048,
        )
        raw = (resp.get("content") or "").strip()
        parsed = parse_sql_candidates_annotations(raw)
        return parsed if parsed else None
    except Exception:
        return None  # LLM 不可用/超时/解析失败 → 保持规则候选不动


async def split_sql_statements(
    sql: str,
    mode: str = "statement",
    custom_rules: dict[str, Any] | None = None,
    db: Any = None,
    llm_budget: dict[str, int] | None = None,
) -> list[str]:
    """按模式切分多语句 SQL。

    Args:
        sql: 完整 SQL 脚本。
        mode: ``semicolon`` / ``statement`` / ``custom``。
        custom_rules: custom 模式的自定义切分规则（delimiters/start_markers）。
        db: 异步会话（custom 模式 LLM 兜底需要；None 则跳过 LLM）。
        llm_budget: 批级 LLM 调用预算 ``{"used", "limit"}``（P1-1：custom 切分
            LLM 兜底计入批量限额，防多语句脚本打满 LLM 配额；None 表示不限额）。

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
            llm_segments = None
            if llm_budget is None or llm_budget["used"] < llm_budget["limit"]:
                if llm_budget is not None:
                    llm_budget["used"] += 1
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

    **注意（P2-7）**：本函数当前无任何端点/服务调用（仅测试引用），属保留的
    场景B 公共 API；``infer_sql_batch`` 内部用 ``_build_atomic_candidate``（功能
    更全：含编码/口径/别名锚点），不经过本函数。如需启用场景B 独立入口，应
    先对齐候选字段结构（``source_table/measure_column/aggregation/period`` 仅
    覆盖原子候选最小集）。

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
    suggested_domain_code: str | None = None,
    source: str = "rule",
    raw_sql: str | None = None,
    column_comments: dict[str, dict[str, str]] | None = None,
    comment_table: str | None = None,
) -> dict[str, Any]:
    """构建原子候选：expression 模式推断（勿传多度量原 SQL，避免兄弟度量进口径），
    聚合方式覆盖为 SQL 解析值（比列名规则更可靠）。

    ``measure`` 可能来自下沉收集（ETL 透传 INSERT），自带 ``alias/table/expression``：
    key 用 alias 防同列不同语义冲突，源表/口径优先用度量自身携带值。
    ``source``（P2-2）：``rule``（规则层可靠产出）或 ``llm``（LLM 兜底提取，
    前端据此加「AI 推断」Tag 让用户复核）。
    ``raw_sql``（P1-1/P2-5）：候选所属语句原始 SQL 原文切片——后端候选直接携带，
    API 消费者/集成链路提交时无需再从语句 meta 反查（口径溯源闭合）。
    """
    col = measure["column"]
    derived = bool(measure.get("derived"))
    # 派生比率/条件列（P0-3d，agg=None）聚合占位 SUM（口径由 expression 承载，
    # 对齐复合指标占位语义）；普通度量缺失聚合兜底 COUNT（既有行为）
    agg = measure["agg"] or ("COUNT" if not derived else "SUM")
    alias = measure.get("alias")
    measure_table = measure.get("table") or table
    # 下沉度量（A-4：ETL 透传 INSERT 内层子查询的聚合投影）同列多语义（多个
    # ``SUM(CASE WHEN ...)`` 分支同落一列）时用 alias 作命名锚点生成编码/名称，
    # 避免 N 个候选撞同一编码；口径列仍为原始 col（expression/source_fields 不受
    # 影响）。**顶层投影**（sunk=False，含裸聚合生成别名）用真实列作锚点——alias
    # 仅为投影别名，若用 alias 会改变既有编码语义（如 SUM(amount) AS gmv → 本应
    # 用 amount 而非 gmv）。
    code_col = alias if (alias and alias != col and measure.get("sunk")) else col
    # A-5：建表 DDL 列注释反查——数仓开发在 ``create table`` 里写的中文注释是
    # 名称/单位推断最权威来源（「月活」不再落成「doctor次数」）。按 (表, 列) 反查，
    # 命中则注入 ``measure_meta.comment``，``_infer_name``/``_infer_unit`` 自动消费。
    # 注释表优先用 INSERT 目标表（``comment_table``，列注释定义处），ETL 下沉场景
    # 度量源表是内层子查询、与建表目标表不同——回退度量源表反查。
    measure_meta: dict[str, Any] = {}
    if column_comments:
        for tbl in (comment_table, measure_table):
            if not tbl:
                continue
            col_key = str(code_col or col).lower()
            comment = (column_comments.get(str(tbl).split(".")[-1]) or {}).get(col_key)
            if comment:
                measure_meta = {"comment": comment}
                break
    # C（A/C 三轮增强）：下沉聚合候选的别名是数仓业务列名，注入 measure_meta 供
    # _infer_name/_measure_label 优先消费——同列多 count（all_order_cnt/
    # session_side_order_cnt）名称不再全部雷同「日订单量」
    if alias and alias != col:
        measure_meta = {**measure_meta, "alias": alias}
    profile = build_profile(
        source_table=measure_table,
        measure_column=code_col,
        period=period,
        measure_meta=measure_meta,
    )
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
    # Q2：数仓详细口径（dw_definition）= 候选所属语句的完整原始 SQL——SQL 智能推断
    # 得到的指标创建后，口径在 MetricDetail/目录展开「数仓详细口径（数仓开发）」区块
    # 直接可见，无需用户手动再填（此前推断链路未写 dw_definition，数仓口径恒空）。
    if raw_sql:
        definition["dw_definition"] = raw_sql
    # A-1/2 人工核对标识：CASE 条件聚合/窗口函数/下沉子查询度量的口径非简单 SUM(col)，
    # 注册后口径可能不直观（CASE 过滤条件、OVER 窗口语义在 expression 中保留但前端
    # 需提示人工核对）；前端据此加「口径需核对」Tag，避免用户误以为全表聚合。
    _expr_upper = (expression or "").upper()
    needs_review = bool(
        "CASE" in _expr_upper
        or " OVER " in f" {_expr_upper} "
        or measure.get("sunk")
        # U-6/U-2/U-8：窗口函数包裹 / 集合串聚合 / PIVOT 展开的度量——语义非普通
        # 分组聚合，由 _projection_measures/_pivot_measures 标记 needs_review，
        # 候选据此加「口径需核对」Tag（U-2 集合聚合已归一 COUNT_DISTINCT，PIVOT/
        # 窗口需人工核对聚合语义）
        or measure.get("needs_review")
    )
    metric_code = result.get("metric_code_suggestion")
    if metric_code and not domain_code:
        # 域未确定（多域/无域建议）时不得 bake-in 首段为空的非法编码——
        # ``generate_metric_code("", ...)`` 会产出 ``_order_amount_day``（P0 缺陷，
        # 批量创建时 pydantic 校验整批 500）。候选编码改由前端在用户选域后按
        # 最终域重新生成（前端提交时若 metric_code 为空则用 selectedDomain 拼 4 段）。
        metric_code = None
    if not metric_code and domain_code and measure_table and period:
        metric_code = generate_metric_code(domain_code, measure_table, code_col, period)
    # LLM 兜底场景自带建议名（measure["name"]）优先；规则层（无 name）走 auto_fill
    candidate_name = measure.get("name") or fields["name"]["value"]
    # OneData 语义类型判定（周期驱动 + 比率/运算信号，变体口径）：原子指标 = 逻辑
    # 度量 + 基础统计粒度（日），不含业务限定与时间周期；派生指标 = 原子指标 + 业务
    # 限定 + 时间周期（月/周/季/年/小时等非日周期）。
    # SQL 解析出的候选天然带业务限定 + 周期，业务限定与非日周期均归派生（对齐用户
    # 建模认知——「本月活跃医生数」= 活跃医生数 + 业务限定 + 月周期）。日粒度作为
    # 原子常态统计粒度不视为派生周期。
    # R1（二次审查）：比率/条件度量（derived=True，如 ROUND(SUM/NULLIF)、CASE 比率等
    # 无聚合包裹的表达式）在整句含四则运算时归复合（OneData：多指标运算/比率=复合），
    # 与单条 auto_suggest 的 _infer_type 对齐——修复批量/单条两路径类型分裂；纯条件
    # 聚合（count(distinct case...)）无算术运算时仍走周期驱动（归派生/原子）。
    # S3（三轮审查）：列名比率规则（rate/ratio/pct→复合）补齐到批量路径——此前只在
    # 单条 _infer_type，两路径对 SELECT SUM(refund_rate)（日粒度）判定分裂（单条复合/
    # 批量原子）；与单条规则完全一致后统一。
    _col_lower = (col or "").lower()
    # A7：引用已命名聚合列的算术派生列（all_order_cnt - session_side_order_cnt -
    # region_org_order_cnt）→ 指标间运算归复合（与 _build_composite_candidate 语义
    # 一致），deps_aliases 供阶段 3 解析依赖原子编码
    deps_aliases = measure.get("deps_aliases") or []
    if (
        (derived and raw_sql and sql_has_arithmetic(raw_sql))
        or any(k in _col_lower for k in ("rate", "ratio", "pct"))
        or bool(deps_aliases)
    ):
        cand_type = "composite"
    elif period and period != "day":
        cand_type = "derived"
    else:
        cand_type = "atomic"
    return {
        "key": f"{idx}:{alias or col}",
        "metric_code": metric_code,
        "name": candidate_name,
        "type": cand_type,
        "source_table": measure_table,
        "measure_column": col,
        # 派生比率/条件列（P0-3d）：聚合占位 None——前端展示「派生表达式」而非
        # 伪装成标准聚合；批量创建 Phase1 用 ``or "SUM"`` 占位（口径由 expression
        # 承载），与复合指标占位语义一致
        "aggregation": None if derived else agg,
        "period": period,
        "unit": fields["unit"]["value"],
        "granularity": fields["granularity"]["value"],
        "definition_json": definition,
        "definition_mode": "expression",
        "statement_index": idx,
        # OneData 接线（生产就绪审查 P2）：SQL 无法推断逻辑度量，恒空——前端批量
        # 候选行提供「关联逻辑度量」选择器补全后透传；此处保持契约键存在
        "measure_id": measure.get("measure_id"),
        # 口径溯源（P1-1/P2-5）：候选所属语句原始 SQL（原文切片，批量创建透传落
        # Metric.raw_sql——候选仅表达式，整句口径原文可据此反查）
        "raw_sql": raw_sql,
        "suggested_domain_code": suggested_domain_code,
        # P2-2：候选来源（rule=规则层 / llm=LLM 兜底），前端「AI 推断」复核标识
        "source": source,
        # A-1/2：CASE/窗口/下沉口径需人工核对标识（前端「口径需核对」Tag）
        "needs_review": needs_review,
        # P0-3d：派生比率/条件列（ROUND(SUM/NULLIF)/CASE 比率等无聚合包裹的表达式）
        # ——前端据此展示「派生表达式」并在口径核对区呈现完整 expression
        "derived": derived,
        # A7：引用已命名聚合列的算术派生候选——被引用的聚合别名（阶段 3 后处理
        # 解析为依赖原子编码 dependencies；前端据此展示依赖关系/作必填校验）
        "deps_aliases": deps_aliases or None,
        # 投影别名（下沉聚合/派生列）：C 名称去重后缀与前端展示的业务标识
        "alias": alias,
    }


def _build_composite_candidate(
    *,
    idx: int,
    sql: str,
    profile: SqlProfile,
    atoms: list[dict[str, Any]],
    domain_code: str | None,
    period: str,
    suggested_domain_code: str | None = None,
    raw_sql: str | None = None,
) -> dict[str, Any] | None:
    """合成复合候选：依赖组内 N 原子编码，口径 SQL=原语句（保留多度量计算结构）。

    编码/粒度使用语句实际统计周期（``period``），不再硬编码 ``_day``/``day``——
    月粒度 ETL 的复合指标此前生成 ``xxx_day``（粒度 day）与实际口径（month）不符，
    注册后编码与粒度均失真（P1-3 一致性缺陷）。
    ``raw_sql``（P1-1/P2-5）：复合候选所属语句原始 SQL，批量创建透传落 Metric.raw_sql。
    仅当语句含四则运算/比率时才合成（B4）——无运算的多度量并列不构成复合。
    """
    if not sql_has_arithmetic(sql):
        return None
    # R1：含算术的比率列已被 _build_atomic_candidate 判为 composite，不应再作为
    # 合成复合的依赖原子（避免「复合依赖复合」嵌套混乱）。合成复合只基于纯原子候选。
    atoms = [c for c in atoms if c.get("type") == "atomic"]
    codes = [c["metric_code"] for c in atoms if c.get("metric_code")]
    if len(codes) < 2:
        return None
    table = atoms[0].get("source_table")
    biz = extract_biz_object(table) if table else "entity"
    groupkey = _group_key([str(c.get("measure_column", "")) for c in atoms])
    names = "、".join(str(c.get("name", "")) for c in atoms)
    return {
        "key": f"{idx}:composite",
        "metric_code": f"{domain_code}_{biz}_{groupkey}_{period}" if domain_code else None,
        # 原子名已含周期前缀（如「日金额」），复合名直接拼接避免「日日」重复
        "name": f"{names}复合"[:128],
        "type": "composite",
        "source_table": table,
        "measure_column": None,
        "aggregation": None,
        "period": period,
        "unit": None,
        "granularity": period,
        "definition_json": {
            "sql": sql,
            "dependencies": codes,
            "source_tables": profile.source_tables,
            # Q2：数仓详细口径 = 复合所属语句完整 SQL（与原子候选 dw_definition 语义一致）
            **({"dw_definition": raw_sql} if raw_sql else {}),
        },
        "definition_mode": "sql",
        "dependencies": codes,
        "statement_index": idx,
        "raw_sql": raw_sql,
        "suggested_domain_code": suggested_domain_code,
    }


def _statement_meta(
    idx: int, sql: str, profile: SqlProfile, suggested_domain: str | None = None
) -> dict[str, Any]:
    """语句摘要（前端 Collapse 分组标题用）。

    ``suggested_domain``（P2-10）：整段域建议为 multiple/none 时逐语句反查的
    域编码——跨域脚本各语句表可能分属不同域，前端据此在语句级提示建议域。
    """
    return {
        "index": idx,
        "sql": sql[:2000],
        "source_tables": profile.source_tables,
        "measure_count": len(profile.measures),
        "group_by": profile.group_by,
        "suggested_domain": suggested_domain,
    }


# 建表 DDL 列注释提取的兜底正则（sqlglot 解析失败时用）：先定位 ``create table``
# 段及其表名，再抓该段内 ``col type comment '...'`` 的列级注释。列注释是数仓开发
# 在建表 DDL 中写的中文语义，是候选名称/单位推断最权威的来源（优先于词表兜底）。
_CREATE_TABLE_RE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?([`\"\w.]+)", re.IGNORECASE
)
_COLUMN_COMMENT_RE = re.compile(
    r"([`\"\w]+)\s+[^,(\n]*?\bcomment\s*['\"]([^'\"]+)['\"]", re.IGNORECASE | re.DOTALL
)


def _extract_column_comments(sql: str) -> dict[str, dict[str, str]]:
    """从 SQL 脚本提取建表 DDL 的列注释映射 ``{表名: {列名: 注释}}``。

    批量 SQL 脚本常含 ``create table ... ( col type comment '中文' )`` 建表语句，
    列注释承载数仓开发写的中文语义——候选名称/单位推断直接消费它，解决
    「建表有『月活』注释，候选却叫『月doctor次数』」的断链。实现：

    - **sqlglot 主路径**：``sqlglot.parse`` 逐语句找 ``Create``，遍历 ``ColumnDef``
      的 ``CommentColumnConstraint`` 提取列注释（Hive/MySQL 等方言通用）；
    - **正则兜底**：sqlglot 解析失败时用 ``_CREATE_TABLE_RE``/``_COLUMN_COMMENT_RE``
      粗提取（表名定位 + 段内 ``comment '...'`` 抓取）。

    表名去库前缀（INSERT 投影/度量携带的 ``measure_table`` 常不带库前缀）、
    列名小写归一（候选 ``code_col`` 小写反查）；无注释/异常返回空 dict（不阻断）。
    """
    comments: dict[str, dict[str, str]] = {}
    try:
        for ast in sqlglot.parse(sql):
            if ast is None or not isinstance(ast, exp.Create):
                continue
            table = ast.this
            # 带列定义的 Create 其 this 是 Schema（alias_or_name 为空串），实际
            # 表名在 Schema.this（Table）——否则标准单引号注释 DDL 会被跳过
            if isinstance(table, exp.Schema):
                table = table.this
            table_name = (
                table.alias_or_name.lower() if hasattr(table, "alias_or_name") else None
            )
            if not table_name:
                continue
            table_cols = comments.setdefault(table_name, {})
            for node in ast.find_all(exp.ColumnDef):
                col = (node.alias_or_name or "").lower()
                if not col:
                    continue
                for c in node.constraints:
                    k = c.kind
                    if not isinstance(k, exp.CommentColumnConstraint):
                        continue
                    lit = k.this
                    comment = (
                        lit.this if isinstance(lit, exp.Literal) else str(lit)
                    ).strip()
                    if comment:
                        table_cols[col] = comment
    except Exception:  # noqa: BLE001 - sqlglot 解析失败降级正则兜底
        comments = {}
        for m in _CREATE_TABLE_RE.finditer(sql):
            table_name = m.group(1).strip("`\"'").split(".")[-1].lower()
            segment = sql[m.end() :]
            end = segment.find(";")
            if end != -1:
                segment = segment[:end]
            table_cols = comments.setdefault(table_name, {})
            for cm in _COLUMN_COMMENT_RE.finditer(segment):
                col = cm.group(1).strip("`\"'").lower()
                comment = cm.group(2).strip()
                if col and comment:
                    table_cols[col] = comment
    return comments


def _insert_target_table(statement_sql: str) -> str | None:
    """提取 INSERT 语句的目标表名（去库前缀小写）。

    ETL 下沉场景候选的度量源表是内层子查询（如 ``doctor_visit_agent_info_da``），
    而列注释定义在 INSERT 目标表（如 ``doctor_active_month_di``）上——注释反查
    必须以目标表为准，否则「建表有『月活』注释」对不上「度量来自源表」。sqlglot
    解析失败/非 INSERT 返回 None（上层回退度量源表反查）。
    """
    try:
        ast = sqlglot.parse_one(statement_sql)
        for node in ast.find_all(exp.Insert):
            target = getattr(node, "this", None)
            name = getattr(target, "alias_or_name", None)
            if name:
                return str(name).lower()
    except Exception:  # noqa: BLE001 - 目标表提取失败不影响候选构建
        pass
    return None


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


def _classify_no_measure(seg: str, profile: SqlProfile, llm_tried: bool) -> str:
    """无聚合度量语句的跳过原因分类（前端友好文案的稳定 code）。

    优先级：LLM 兜底已尝试仍失败 → ``llm_infer_failed``；无 SELECT 的纯 DDL/
    非查询语句（drop/create/alter/truncate/comment 等）→ ``ddl_only``；含
    SELECT 且含聚合关键字但规则层未解析出度量 → ``parse_failed``（方言/结构
    异常，值得 LLM 兜底）；含 SELECT 但确实无聚合关键字 → ``no_aggregate``。
    前端按 code 映射中文文案，避免一律「请检查是否含 SELECT + 聚合函数」。
    """
    if llm_tried:
        return "llm_infer_failed"
    lower = seg.lower()
    if re.search(r"\bselect\b", lower) is None:
        return "ddl_only"
    # 标准聚合词 + 方言变体前缀（sumMerge/sumIf/countIf/approx_distinct 等）：
    # 只要语句疑似含聚合但规则层未解析出度量，就值得 LLM 兜底（避免 ClickHouse/
    # StarRocks 等方言聚合被误判「确实无聚合」而跳过）。
    if re.search(
        r"\b(sum|count|avg|max|min|median|percentile|distinct)[a-z_]*\b", lower
    ):
        return "parse_failed"
    return "no_aggregate"


# ----------------------------------------------------------------
# 批量推断主函数
# ----------------------------------------------------------------


def _apply_candidate_period(cand: dict[str, Any], period: str) -> None:
    """应用 LLM 周期覆盖并保持编码/粒度一致（确定性回映）。

    仅当候选 metric_code 为 4 段式（域_业务对象_度量_周期）时才替换末段为
    新周期（业务段不受 LLM 影响，避免用 key 派生 code_col 引入业务段偏差）；
    多域候选（编码为空）或编码非 4 段时保守只改 period 字段，避免编码失真。
    """
    code = cand.get("metric_code")
    if code:
        parts = str(code).split("_")
        if len(parts) == 4:
            parts[-1] = period
            cand["metric_code"] = "_".join(parts)
    cand["period"] = period
    cand["granularity"] = period
    # B2：周期驱动类型回映——LLM 覆盖周期后类型必须与周期同步（非日→派生、日→原子）；
    # 复合指标保持复合（周期是其属性，不因周期覆盖降级）。
    if cand.get("type") == "composite":
        return
    if period and period != "day":
        cand["type"] = "derived"
    else:
        cand["type"] = "atomic"


def _candidate_measure_view(cand: dict[str, Any]) -> dict[str, Any]:
    """候选 → 度量视图（LLM 校验层输入：与 ``SqlProfile.measures`` 同构）。

    候选的 ``measure_column`` 是度量锚点（alias 同列语义），聚合/源表直接映射；
    派生/复合候选聚合为空（agg=None），校验层按派生处理。
    """
    return {
        "column": cand.get("measure_column") or cand.get("name") or "",
        "agg": cand.get("aggregation"),
        "alias": cand.get("measure_column"),
        "table": cand.get("source_table"),
        "derived": bool(cand.get("derived")) or cand.get("type") == "composite",
    }


def _apply_candidate_validation(
    candidates: list[dict[str, Any]],
    validation: dict[str, Any],
    tables: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """批量候选的 LLM 校验规范收敛（方案 A 默认全量校验，纯确定性算法）。

    对规则候选做**验证而非改写**：
    - **聚合纠正**：LLM 值命中注册枚举白名单且置信度 ≥0.7 才采纳（对齐
      ``merge_validation`` 阈值）；否则保留规则（防幻觉非法枚举整批失败）。
    - **源表纠正**：LLM 选的表必须在规则解析出的源表集合内。
    - **is_measure=false**：高置信度（≥0.7）才移出候选 → 记 skipped(llm_not_measure)；
      低置信度保守保留并标记 ``needs_review``。
    - **漏检扫描**：LLM 的 ``missed`` 过列名合法性 + 聚合白名单后，**不自动加候选**
      （候选构建需语句序号/域默认值等上下文，自动加易失真），而是返回响应级
      ``missed`` 报告供前端提示用户核对（召回缺口透明化）。
    - **周期覆盖**：LLM 的 period 白名单覆盖经 ``_apply_candidate_period`` 回映。

    Returns:
        ``(收敛后的候选清单, 校验摘要)``；摘要含 ``agg_corrected``/``dropped``/
        ``needs_review``/``missed``/``period_override``。
    """
    from app.services.semantic.sql_validation import AGG_ENUM

    items_by_key = {str(it["key"]): it for it in validation.get("items", [])}
    summary: dict[str, Any] = {
        "agg_corrected": [],
        "dropped": [],
        "needs_review": [],
        "missed": [],
        "period_override": validation.get("period"),
    }
    kept: list[dict[str, Any]] = []
    for i, cand in enumerate(candidates):
        item = items_by_key.get(str(i))
        if item is None:
            kept.append(cand)
            continue
        conf = item.get("confidence")
        # B4.1：复合候选是刻意合成的多指标聚合体（依赖 + 口径 SQL），非待校验的单度量
        # —— LLM 把它判为「非度量」/改聚合都不采纳，防真实 API 链路把复合候选剔除
        #（此前 _candidate_measure_view 把复合当 derived 度量交给 LLM，is_measure=false
        #  高置信 → 复合被静默丢弃，复合指标功能端到端不可见）。
        if not item.get("is_measure", True) and cand.get("type") != "composite":
            if conf is None or conf >= 0.7:
                summary["dropped"].append(
                    {
                        "index": cand.get("statement_index", 0),
                        "sql": str(cand.get("name") or cand.get("measure_column") or "")[:500],
                        "reason": "llm_not_measure",
                    }
                )
                continue
            summary["needs_review"].append(
                {"key": cand.get("key"), "reason": "LLM 判定非度量但置信度低"}
            )
        agg = item.get("agg")
        if (
            cand.get("type") != "composite"
            and agg
            and agg in AGG_ENUM
            and agg != cand.get("aggregation")
            and (conf is None or conf >= 0.7)
        ):
            summary["agg_corrected"].append(
                {
                    "key": cand.get("key"),
                    "column": cand.get("measure_column"),
                    "to": agg,
                }
            )
            cand["aggregation"] = agg
        table = item.get("table")
        if table and table in tables and table != cand.get("source_table"):
            cand["source_table"] = table
        period = item.get("period")
        if period and period != cand.get("period"):
            _apply_candidate_period(cand, period)
        if conf is not None and conf < 0.7:
            summary["needs_review"].append(
                {"key": cand.get("key"), "reason": "LLM 校验置信度低，建议人工核对"}
            )
        cand["llm_validated"] = True
        cand["llm_confidence"] = conf
        kept.append(cand)
    # 漏检扫描 → 响应级报告（不自动加候选）
    existing = {(str(c.get("measure_column") or c.get("name"))).lower() for c in kept}
    for missed in validation.get("missed", []):
        from app.services.semantic.sql_validation import _sane_column

        column = str(missed.get("column") or "").strip()
        agg = missed.get("agg")
        if not _sane_column(column) or agg not in AGG_ENUM:
            continue
        if column.lower() in existing:
            continue
        existing.add(column.lower())
        summary["missed"].append(
            {
                "column": column,
                "agg": agg,
                "alias": (missed.get("alias") or "").strip() or None,
            }
        )
    kept.sort(key=lambda c: (c.get("statement_index", 0), c.get("key", "")))
    return kept, summary


def _apply_candidate_annotations(
    candidates: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """规范收敛层：把 LLM 封闭选择应用到候选清单（纯确定性算法）。

    与 LLM 的随机性解耦——无论 LLM 这次输出顺序怎么变、个别判断怎么漂，
    最终落库的候选集合在此层被收敛成同一个结果（"每次业务结果一样"的实现）：

    - **白名单**：周期只认 ``normalize_period`` 白名单值（LLM 产非法值直接丢弃该
      覆盖）；聚合**不覆盖**（规则解析已过枚举白名单，防幻觉非法 code 整批失败）。
    - **列名回映**：LLM 不能新增/发明度量列（封闭选择），只能对规则候选做
      ``is_measure`` 判定；``is_measure=false`` 且高置信度（≥0.7）才移出候选 →
      记 skipped(llm_not_measure)，低置信度保守保留（规则说有就保留）。
    - **稳定排序**：按 ``(statement_index, key)`` 确定性排序（不受 LLM 输出顺序影响）。
    - **置信度**：每候选携带 ``llm_confidence``（低置信度标记需人工确认，不静默落库）。

    Returns:
        ``(收敛后的候选清单, 被 LLM 判为非度量的 skipped 明细)``。
    """
    ann_by_key = {a["key"]: a for a in annotations}
    kept: list[dict[str, Any]] = []
    llm_skipped: list[dict[str, Any]] = []
    for cand in candidates:
        ann = ann_by_key.get(cand["key"])
        if ann is None:
            kept.append(cand)
            continue
        if not ann.get("is_measure", True) and cand.get("type") != "composite":
            # S1（三轮审查）：复合候选是刻意合成的多指标聚合体（依赖 + 口径 SQL/
            # 表达式），非待 LLM 判定「是否度量」的单列——与默认路径
            # _apply_candidate_validation 的 B4.1 豁免对齐，防 use_llm 模式把自动
            # 复合候选（measure_column/aggregation 为占位）当作非度量静默剔除，
            # 致复合功能在 LLM 批量模式下端到端不可见。
            conf = ann.get("confidence")
            if conf is None or conf >= 0.7:
                llm_skipped.append(
                    {
                        "index": cand.get("statement_index", 0),
                        "sql": str(cand.get("name") or cand.get("measure_column") or "")[:500],
                        "reason": "llm_not_measure",
                    }
                )
                continue
        name = ann.get("name")
        if name:
            cand["name"] = name[:128]
        period = ann.get("period")
        if period and period != cand.get("period"):
            _apply_candidate_period(cand, period)
        cand["source"] = "llm"
        cand["llm_confidence"] = ann.get("confidence")
        kept.append(cand)
    kept.sort(key=lambda c: (c.get("statement_index", 0), c.get("key", "")))
    return kept, llm_skipped


async def infer_sql_batch(
    db: Any,
    *,
    sql: str,
    split_mode: str = "statement",
    custom_rules: dict[str, Any] | None = None,
    domain_code: str | None = None,
    synthesize_composite: bool = False,
    use_llm: bool = False,
) -> dict[str, Any]:
    """SQL 批量推断主函数：切分 → 逐语句画像 → 候选生成 → 域建议 → LLM 补全。

    Args:
        db: 异步会话（域建议/自定义 LLM 分段需要）。
        sql: 大段 SQL 脚本。
        split_mode: 切分模式（semicolon/statement/custom）。
        custom_rules: custom 模式的自定义切分规则。
        domain_code: 用户显式指定的域（缺省则整段一次批量建议）。
        synthesize_composite: 单语句多度量时是否合成复合候选。
        use_llm: 显式 LLM 模式——对规则解析出的候选做一次 LLM 批量补全
            （``_llm_annotate_candidates`` 封闭选择：名称润色/周期校正/非度量
            过滤）+ 规范收敛（``_apply_candidate_annotations``），整段 SQL 只花
            1 次调用；LLM 不可用/失败保持规则候选不动，绝不阻断。批级 LLM 预算
            相应放宽到 ``_LLM_BATCH_LIMIT_LLM``（规则兜底模式的 4 倍）。

    Returns:
        ``{"statements", "candidates", "skipped", "domain"}``。
        ``candidates`` 的 ``key`` 稳定标识 ``{语句序号}:{度量列}``（原子）/
        ``{语句序号}:composite``（复合），前端勾选与后端创建均用它定位。
    """
    if not sql or not sql.strip():
        return {"statements": [], "candidates": [], "skipped": [], "domain": None}

    # P1-1：批级 LLM 调用预算（切分兜底 + 整段域建议 + 度量提取 + 周期推断 +
    # 逐语句域建议 + use_llm 候选补全全部计入），超限后不再调 LLM——防多语句脚本
    # 打满 LLM 配额/拖慢解析；域建议仅在目录/挂载未命中时消耗（内部 best-effort）。
    # use_llm 显式模式放宽到 _LLM_BATCH_LIMIT_LLM（用户主动选择 LLM，配额更宽）。
    llm_limit = _LLM_BATCH_LIMIT_LLM if use_llm else _LLM_BATCH_LIMIT
    llm_budget = {"used": 0, "limit": llm_limit}

    segments = await split_sql_statements(
        sql, mode=split_mode, custom_rules=custom_rules, db=db, llm_budget=llm_budget
    )
    if not segments:
        segments = [sql.strip()]

    # A-5：全脚本一次提取建表 DDL 列注释（候选名称/单位推断消费，见
    # ``_build_atomic_candidate``）——切分后单段 INSERT 看不到建表注释，须从完整
    # 脚本提取再按 (表, 列) 反查注入。
    column_comments = _extract_column_comments(sql)

    # P1-2：批量解析语句数上限（生产护栏）——超大脚本（数百条语句）会触发逐语句
    # LLM 兜底/域建议拖慢解析，超限直接拒绝，提示用户分批解析（前端友好文案）。
    max_batch_statements = 100
    if len(segments) > max_batch_statements:
        from app.core.exceptions import BusinessError

        raise BusinessError(
            f"SQL 语句数（{len(segments)}）超过单次批量解析上限 {max_batch_statements}，"
            "请分批解析",
            error_code="SQL_BATCH_TOO_MANY_STATEMENTS",
            ctx={"statement_count": len(segments), "limit": max_batch_statements},
        )

    # 域建议：用户未指定域时整段一次批量建议（目录/挂载反查 + LLM 兜底）
    suggestion: dict[str, Any] | None = None
    if not domain_code:
        from app.services.semantic.domain_suggest import suggest_domain

        suggestion = await suggest_domain(db, sql=sql, llm_budget=llm_budget)
        if suggestion.get("status") in ("unique", "llm"):
            domain_code = suggestion["domain"]["code"]

    domain_defaults = await _load_domain_defaults(db, domain_code) if domain_code else {}

    statements: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    # P2-10：整段域建议未定/多域时，逐语句反向建议域——跨域脚本各语句表可能
    # 分属不同域（跨域共用 DWD 层表是常态），候选携带语句级建议域供前端提示。
    # 整段唯一/LLM 已定域或用户显式指定域时不重复建议（避免 N 次 DB 查询）。
    per_stmt_suggest = bool(
        not domain_code and suggestion and suggestion.get("status") in ("multiple", "none")
    )
    # ---- 阶段 1：纯规则解析（串行、无 LLM），收集逐语句兜底需求 ----
    # 两阶段设计：多语句同时触发 LLM 兜底（周期/度量提取/逐语句域建议）时，
    # 阶段 2 用 asyncio.gather 并发执行，把墙钟从「N×单次调用」降到并发数内
    # 单次调用——对齐 R-2 韧性（避免多语句脚本逐条串行 LLM 兜底拖到几十秒）。
    # 行为等价：预算语义（阶段 1 先检查后分配）、skipped 分类、候选顺序（阶段
    # 3 按语句 index 合并）均与串行一致。逐语句域建议也并入阶段 2 并发——其内部
    # 预算「检查 used<limit 与 used+=1 之间无 await 分隔」（asyncio 单线程下原子
    # 安全，不会超发）、结果不依赖调用顺序，与周期/度量兜底同批 gather 安全。
    stmt_records: list[dict[str, Any]] = []
    pending_period: list[int] = []  # 规则有度量但周期不确定 → _llm_infer_period
    pending_measures: list[dict[str, Any]] = []  # 规则无度量且 parse_failed → _llm_infer_measures
    pending_domain: list[int] = []  # 整段域建议 multiple/none → 逐语句建议域（P2-10）
    for idx, seg in enumerate(segments):
        # P0-A 兜底：单语句画像解析/方言提取意外异常绝不炸整批——降级 skipped
        # 继续后续语句（候选构建本身也有 try 保护，此处覆盖画像层）
        try:
            profile = parse_sql_profile(seg)
        except Exception:  # noqa: BLE001 - 单语句异常仅降级跳过该句
            skipped.append({"index": idx, "sql": seg[:500], "reason": "parse_failed"})
            continue
        if per_stmt_suggest:
            # P2-10：跨域脚本逐语句建议域——不在阶段 1 串行 await，收集进
            # pending_domain，阶段 2 与周期/度量兜底同批 gather 并发执行（避免
            # N 条语句各自等一次 LLM/DB 反查串行拖慢）；结果按 idx 回填。
            pending_domain.append(idx)
        statements.append(_statement_meta(idx, seg, profile))
        if not profile.measures:
            # 规则层无聚合度量：仅对含 SELECT 的语句尝试 LLM 兜底提取（纯 DDL
            # 如 drop/create 非查询语句不浪费 LLM 调用）；LLM 不可用/失败才按
            # 原因分类进 skipped（绝不因单条解析失败阻断整批）。
            reason_code = _classify_no_measure(seg, profile, llm_tried=False)
            llm_tried = reason_code == "parse_failed" and db is not None
            if llm_tried:
                if llm_budget["used"] >= llm_budget["limit"]:
                    # 已达批级限额：不再调 LLM，降级 skipped（前端提示 AI 兜底上限）
                    skipped.append(
                        {
                            "index": idx,
                            "sql": seg[:500],
                            "reason": "llm_limit",
                        }
                    )
                    continue
                llm_budget["used"] += 1
                pending_measures.append(
                    {
                        "idx": idx,
                        "seg": seg,
                        "profile": profile,
                    }
                )
            else:
                skipped.append(
                    {
                        "index": idx,
                        "sql": seg[:500],
                        "reason": _classify_no_measure(seg, profile, llm_tried=False),
                    }
                )
            continue
        tables = _physical_source_tables(seg, profile)
        table = tables[0] if tables else None
        base_period = _period_from_profile(profile)
        # 规则层无时间信号（无截断/别名粒度、无时间列）→ LLM 兜底推断周期；
        # LLM 不可用/失败降级规则层默认，绝不阻断候选生成
        needs_period = (
            _period_uncertain(profile)
            and db is not None
            and llm_budget["used"] < llm_budget["limit"]
        )
        if needs_period:
            llm_budget["used"] += 1
            pending_period.append(idx)
        stmt_records.append(
            {
                "idx": idx,
                "seg": seg,
                "profile": profile,
                "table": table,
                "base_period": base_period,
                "needs_period": needs_period,
            }
        )

    # ---- 阶段 2：并发执行逐语句 LLM 兜底（预算已在阶段 1 分配） ----
    # 每类兜底独立 gather + 信号量限并发（_LLM_FALLBACK_CONCURRENCY），防止同时
    # 打爆 LLM 网关；单任务失败（LLM 不可用/超时）返回 None 不阻断其余。
    llm_period_map: dict[int, str | None] = {}
    if pending_period:
        period_sem = asyncio.Semaphore(_LLM_FALLBACK_CONCURRENCY)

        async def _run_period(stmt_idx: int) -> tuple[int, str | None]:
            async with period_sem:
                # 传完整脚本 + 焦点语句：方言语句字段来源/口径常依赖前置语句
                # 定义（CTE/SET/变量），只看单段会信息丢失影响推断
                return stmt_idx, await _llm_infer_period(
                    db, full_sql=sql, focus_sql=segments[stmt_idx]
                )

        for stmt_idx, period in await asyncio.gather(
            *[_run_period(i) for i in pending_period]
        ):
            llm_period_map[stmt_idx] = period
    llm_measures_map: dict[int, list[dict[str, Any]] | None] = {}
    if pending_measures:
        measures_sem = asyncio.Semaphore(_LLM_FALLBACK_CONCURRENCY)

        async def _run_measures(item: dict[str, Any]) -> tuple[int, list[dict[str, Any]] | None]:
            async with measures_sem:
                return item["idx"], await _llm_infer_measures(
                    db, full_sql=sql, focus_sql=item["seg"]
                )

        for item, measures in await asyncio.gather(
            *[_run_measures(it) for it in pending_measures]
        ):
            llm_measures_map[item] = measures

    # 逐语句域建议（P2-10）同批并发：目录/挂载反查命中不耗 LLM；未命中才内部
    # 检查/递增批级预算（检查+递增无 await 分隔，asyncio 单线程下原子安全）。
    # 预算分配顺序与串行版略有差异（串行为逐句先域后周期，并发为周期/度量先
    # 预留、域建议后占用），但预算本是 best-effort 软护栏（防打满配额），不
    # 影响候选生成正确性；LLM 不可用/异常返回 None（该语句域建议为无，不阻断）。
    seg_domain_map: dict[int, str | None] = {}
    if pending_domain:
        domain_sem = asyncio.Semaphore(_LLM_FALLBACK_CONCURRENCY)

        async def _run_domain(stmt_idx: int) -> tuple[int, str | None]:
            async with domain_sem:
                try:
                    from app.services.semantic.domain_suggest import suggest_domain

                    seg_suggestion = await suggest_domain(
                        db, sql=segments[stmt_idx], llm_budget=llm_budget
                    )
                    if seg_suggestion.get("status") in ("unique", "llm"):
                        return stmt_idx, seg_suggestion["domain"]["code"]
                except Exception:
                    pass  # 逐语句建议失败不影响候选生成
                return stmt_idx, None

        for stmt_idx, code in await asyncio.gather(
            *[_run_domain(i) for i in pending_domain]
        ):
            seg_domain_map[stmt_idx] = code

    # ---- 阶段 3：按语句 index 回填合并候选（顺序与串行一致） ----
    built_by_idx: dict[int, list[dict[str, Any]]] = {}

    def _push_candidate(stmt_idx: int, candidate: dict[str, Any]) -> None:
        built_by_idx.setdefault(stmt_idx, []).append(candidate)

    for rec in stmt_records:
        idx = rec["idx"]
        period = rec["base_period"]
        if rec["needs_period"] and llm_period_map.get(idx):
            period = llm_period_map[idx]
        # A-5：INSERT 目标表（列注释定义处）——下沉场景度量源表是内层子查询，
        # 注释反查须以目标表为准
        comment_table = _insert_target_table(rec["seg"])
        atoms: list[dict[str, Any]] = []
        for measure in rec["profile"].measures:
            atom = _build_atomic_candidate(
                idx=idx,
                measure=measure,
                table=rec["table"],
                period=period,
                domain_code=domain_code,
                domain_defaults=domain_defaults,
                time_column=rec["profile"].time_column,
                suggested_domain_code=seg_domain_map.get(idx),
                raw_sql=rec["seg"],
                column_comments=column_comments,
                comment_table=comment_table,
            )
            atoms.append(atom)
            _push_candidate(idx, atom)
        # A7：解析「引用已命名聚合列的算术派生候选」的依赖原子编码——deps_aliases
        # 是内层聚合别名，映射为语句内对应原子候选的 metric_code（dependencies），
        # 供批量创建血缘注册上游边/前端依赖校验。映射键用候选 alias（业务别名，
        # 与 deps_aliases 同源）；measure_column 是底层列（count(1) → ``*``）不匹配
        alias_code_map: dict[str, str] = {}
        for a in atoms:
            # 只映射原子候选——派生/复合候选的 measure_column 可能是 deps[0]
            # （如 old_page_transfer_order_cnt 的 column=region_org_order_cnt），
            # 若纳入会覆盖原子的别名映射导致依赖错配成自身
            if a.get("type") != "atomic":
                continue
            code = a.get("metric_code")
            if not code:
                continue
            for k in (a.get("alias"), a.get("measure_column")):
                if k:
                    alias_code_map[str(k).lower()] = code
        for atom in atoms:
            deps = atom.get("deps_aliases") or []
            if deps:
                codes = [
                    c
                    for c in (
                        alias_code_map.get(d.lower()) for d in deps
                    )
                    if c
                ]
                if codes:
                    atom["dependencies"] = list(dict.fromkeys(codes))
                atom["type"] = "composite"
                atom["needs_review"] = True
        # C（A/C 三轮增强）：语句内候选名称去重——多个 count 候选（all_order_cnt/
        # session_side_order_cnt/region_org_order_cnt）别名语义无法被词表区分时名称
        # 仍可能雷同（「日订单量」×3），附加别名后缀让用户可区分（批量向导/行内
        # 可再改中文）
        seen_names: dict[str, int] = {}
        for atom in atoms:
            nm = str(atom.get("name") or "").strip()
            if not nm:
                continue
            if seen_names.get(nm):
                suffix = atom.get("alias") or atom.get("measure_column")
                if suffix:
                    atom["name"] = f"{nm}（{suffix}）"
            else:
                seen_names[nm] = 1
        # B（A/B/C 三轮增强）：语句含算术/派生列时**自动合成复合**——不再仅依赖
        # 前端 synthesize_composite 开关（默认 false 曾让「外层派生列」SQL 只出原子）。
        # 已解析出命名派生候选（A7）时不重复合成整语句复合（避免「转诊预约旧页面」
        # 之外再多一个拼接复合）；无命名派生但含运算（SELECT SUM(a)/SUM(b)）才自动
        # 合成整语句复合兜底；用户显式开关仍优先（含全部原子运算视图）。
        has_named_derived = any(a.get("deps_aliases") for a in atoms)
        should_auto_composite = (
            not has_named_derived and sql_has_arithmetic(rec["seg"])
        )
        if synthesize_composite or should_auto_composite:
            composite = _build_composite_candidate(
                idx=idx,
                sql=rec["seg"],
                profile=rec["profile"],
                atoms=atoms,
                domain_code=domain_code,
                period=period,
                suggested_domain_code=seg_domain_map.get(idx),
                raw_sql=rec["seg"],
            )
            if composite:
                _push_candidate(idx, composite)
    for item in pending_measures:
        idx = item["idx"]
        seg = item["seg"]
        llm_measures = llm_measures_map.get(idx)
        if llm_measures:
            base_period = _period_from_profile(item["profile"])
            comment_table = _insert_target_table(seg)
            for measure in llm_measures:
                _push_candidate(
                    idx,
                    _build_atomic_candidate(
                        idx=idx,
                        measure=measure,
                        table=None,
                        period=measure.get("period") or base_period,
                        domain_code=domain_code,
                        domain_defaults=domain_defaults,
                        time_column=item["profile"].time_column,
                        suggested_domain_code=seg_domain_map.get(idx),
                        source="llm",
                        raw_sql=seg,
                        column_comments=column_comments,
                        comment_table=comment_table,
                    ),
                )
        else:
            skipped.append(
                {
                    "index": idx,
                    "sql": seg[:500],
                    "reason": _classify_no_measure(seg, item["profile"], llm_tried=True),
                }
            )
    for stmt_idx in sorted(built_by_idx):
        candidates.extend(built_by_idx[stmt_idx])

    # 回填逐语句建议域到 statements（阶段 2 并发结果，按 idx 写入，前端语句
    # 折叠标题据此展示「建议域」；候选的 suggested_domain_code 已在构建时注入）
    for idx in pending_domain:
        statements[idx]["suggested_domain"] = seg_domain_map.get(idx)

    # LLM 校验层（方案 A 默认全量校验，仅规则模式）：规则解析可能「静默解析错」
    # （漏度量/聚合归一错/条件聚合丢失），对规则候选做一次 LLM 封闭选择校验
    # （is_measure/聚合/源表/周期）+ 漏检扫描，经 ``_apply_candidate_validation``
    # 确定性收敛（白名单/源表回映/置信度）。LLM 不可用/失败保持规则候选不动，
    # 绝不阻断；整段只花 1 次调用（use_llm 显式模式由 _llm_annotate_candidates
    # 承担更完整的补全，不再重复校验）。
    validation_summary: dict[str, Any] = {}
    if (
        not use_llm
        and candidates
        and db is not None
        and llm_budget["used"] < llm_budget["limit"]
    ):
        from app.services.semantic.sql_validation import llm_validate_measures

        llm_budget["used"] += 1
        tables_union = sorted(
            {str(c["source_table"]) for c in candidates if c.get("source_table")}
        )
        validation = await llm_validate_measures(
            db, sql, [_candidate_measure_view(c) for c in candidates], tables_union
        )
        if validation:
            candidates, validation_summary = _apply_candidate_validation(
                candidates, validation, tables_union
            )
            for dropped in validation_summary.get("dropped", []):
                skipped.append(dropped)

    # use_llm 显式模式：对规则候选做一次 LLM 批量补全（封闭选择）+ 规范收敛。
    # 整段 SQL 只花 1 次调用；LLM 不可用/失败保持规则候选不动，绝不阻断。
    # LLM 判为非度量（高置信度）的候选移入 skipped(llm_not_measure)，前端展示。
    if use_llm and candidates and db is not None and llm_budget["used"] < llm_budget["limit"]:
        llm_budget["used"] += 1
        annotations = await _llm_annotate_candidates(db, sql, candidates)
        if annotations:
            candidates, llm_skipped = _apply_candidate_annotations(
                candidates, annotations
            )
            if llm_skipped:
                skipped.extend(llm_skipped)

    # P1-2：候选数上限（生产护栏）——单请求产出过多候选（语句数 × 每语句多度量）
    # 会拖慢前端渲染与后续批量创建，超限拒绝并提示缩减范围。
    max_batch_candidates = 200
    if len(candidates) > max_batch_candidates:
        from app.core.exceptions import BusinessError

        raise BusinessError(
            f"批量解析候选数（{len(candidates)}）超过单次上限 {max_batch_candidates}，"
            "请缩减 SQL 范围",
            error_code="SQL_BATCH_TOO_MANY_CANDIDATES",
            ctx={"candidate_count": len(candidates), "limit": max_batch_candidates},
        )

    return {
        "statements": statements,
        "candidates": candidates,
        "skipped": skipped,
        "domain": _domain_payload(suggestion, domain_code),
        "validation": validation_summary,
    }
