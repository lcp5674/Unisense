"""指标自动推断引擎（纯函数式，对齐 spec FR-010/FR-011, plan.md D3）。

输入（两种方式）：
1. 一段指标 SQL → 解析出源表 / GROUP BY / 度量聚合 / 时间谓词（见 ``sql_infer``）；
2. 域 + 源表 + 度量列 + 统计周期（最小输入）。

一次性推断 13 个字段 + 指标编码 + 口径定义/模式，每个字段返回可追溯的
``SuggestionField``：

    {"value", "source", "confidence", "reason"}

推断优先级（高 → 低）：域默认值 > SQL 解析 > 列元数据 > 规则 > AI/兜底。
这样组织已有的约定不会被 AI 覆盖，且每个值用户都知道从哪来、可信度多高。

枚举字段（type/aggregation/time_semantics/freshness/dw_layer/additivity/
serving_mode/metric_tier/unit/granularity）**全部走确定性规则**，绝不产出字典
校验会拒绝的非法 code；仅 ``name`` 与口径业务说明这类自然语言字段可选 LLM，
且 LLM 不可用/超时时自动降级到规则模板。
"""

from __future__ import annotations

import re
from typing import Any, TypedDict

import structlog

from app.services.semantic.sql_infer import SqlProfile, parse_sql_profile

logger = structlog.get_logger("unisense.auto_fill")

# 源表名前缀清洗
_TABLE_PREFIXES = re.compile(r"^(dwd_|ods_|dws_|ads_|dim_|tmp_)", re.IGNORECASE)
# 业务列名 → 中文标签（无列注释时用于名称生成，命中受控词根）
# 计数类对齐 _infer_unit 的 cnt/count/num 识别；金额/比率/时长类按列名/token 匹配。
_CN_COLUMN_LABELS: dict[str, str] = {
    # 计数类
    "register": "挂号次数",
    "visit": "就诊次数",
    "patient": "患者数",
    "prescription": "处方数",
    "drug": "药品数",
    "order": "订单量",
    "user": "用户数",
    "member": "会员数",
    "customer": "客户数",
    "student": "学生数",
    "employee": "员工数",
    "click": "点击次数",
    "play": "播放次数",
    "login": "登录次数",
    "pay": "支付次数",
    # 金额/费用类
    "amount": "金额",
    "amt": "金额",
    "fee": "费用",
    "cost": "成本",
    "revenue": "收入",
    "income": "收入",
    "expense": "费用",
    "sales": "销售额",
    "gmv": "成交额",
    "profit": "利润",
    "price": "金额",
    # 比率类
    "rate": "比率",
    "ratio": "占比",
    "percent": "占比",
    # 时长类
    "duration": "时长",
    "hours": "时长",
    "minutes": "时长",
}
# 计数类列名后缀（识别顺序：长后缀在前）
_COUNT_SUFFIXES = ("_quantity", "_count", "_cnt", "_num", "_qty")
# 合法编码字符
_CODE_SEGMENT_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
# 4 段式完整编码
METRIC_CODE_PATTERN = re.compile(
    r"^[a-z][a-z0-9_]*_[a-z][a-z0-9_]*_[a-z][a-z0-9_]*_[a-z][a-z0-9_]*$"
)


class SuggestionField(TypedDict):
    """单字段推断结果（可追溯）。"""

    value: Any
    source: str  # domain_default | sql_parse | column_meta | rule | llm | fallback
    confidence: float  # [0,1]
    reason: str


# 推断来源标签（用于前端徽标）
SOURCE_LABELS: dict[str, str] = {
    "domain_default": "域默认",
    "sql_parse": "SQL解析",
    "column_meta": "表元数据",
    "rule": "规则",
    "llm": "AI",
    "fallback": "兜底",
}

# 统计周期 → 中文标签（用于名称生成）
_PERIOD_CN = {
    "day": "日",
    "week": "周",
    "month": "月",
    "quarter": "季",
    "year": "年",
    "hour": "小时",
}

# 时间粒度 token 映射（group_by / 周期 → 字典 code）
_GRAIN_TOKENS = {
    "dt": "day",
    "date": "day",
    "day": "day",
    "_day": "day",
    "week": "week",
    "wk": "week",
    "month": "month",
    "mo": "month",
    "quarter": "quarter",
    "qtr": "quarter",
    "year": "year",
    "yr": "year",
    "hour": "hour",
    "_hour": "hour",
}

# 保留词（不可作为编码段）
RESERVED_WORDS: frozenset[str] = frozenset(
    {
        "metric",
        "index",
        "table",
        "column",
        "select",
        "from",
        "where",
        "group",
        "order",
        "limit",
        "insert",
        "update",
        "delete",
        "create",
        "drop",
        "alter",
        "grant",
        "revoke",
        "all",
        "null",
        "true",
        "false",
    }
)


def _field(value: Any, source: str, confidence: float, reason: str) -> SuggestionField:
    """构造 SuggestionField。"""
    return SuggestionField(value=value, source=source, confidence=confidence, reason=reason)


# ----------------------------------------------------------------
# 编码相关（向后兼容）
# ----------------------------------------------------------------


def extract_biz_object(source_table: str) -> str:
    """从源表名提取业务对象段。"""
    table_name = source_table.split(".")[-1] if "." in source_table else source_table
    table_name = _TABLE_PREFIXES.sub("", table_name)
    first_word = table_name.split("_")[0] if "_" in table_name else table_name
    return first_word.lower()


def extract_measure(measure_column: str) -> str:
    """从度量列名提取编码段（去下划线，小写）。"""
    return measure_column.replace("_", "").lower()


def generate_metric_code(domain: str, source_table: str, measure_column: str, period: str) -> str:
    """生成 4 段式指标编码建议。"""
    biz_obj = extract_biz_object(source_table)
    measure = extract_measure(measure_column)
    return f"{domain}_{biz_obj}_{measure}_{period}"


def validate_metric_code(code: str) -> tuple[bool, str]:
    """校验指标编码 4 段格式。"""
    if not code:
        return False, "指标编码不能为空"
    parts = code.split("_")
    if len(parts) != 4:
        return False, f"须符合4段格式（域_业务对象_度量_统计周期），当前{len(parts)}段"
    labels = ["域", "业务对象", "度量", "统计周期"]
    for i, part in enumerate(parts):
        if not _CODE_SEGMENT_PATTERN.match(part):
            return False, f"第{i + 1}段（{labels[i]}）格式错误：须小写字母开头+小写字母数字下划线"
        if part in RESERVED_WORDS:
            return False, f"第{i + 1}段（{labels[i]}）使用了保留词: {part}"
    return True, ""


# ----------------------------------------------------------------
# 输入画像归一化
# ----------------------------------------------------------------


def build_profile(
    *,
    source_table: str | None = None,
    measure_column: str | None = None,
    period: str | None = None,
    sql: str | None = None,
    measure_meta: dict[str, Any] | None = None,
    table_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """将多种输入归一化为统一画像 dict。

    SQL 优先：提供 sql 时解析出源表/维度/度量/时间谓词，覆盖 table+measure 入参。
    """
    profile_kwargs: dict[str, Any] = {
        "source_table": source_table,
        "measure_column": measure_column,
        "period": period,
        "measure_meta": measure_meta or {},
        "table_meta": table_meta or {},
    }
    if sql and sql.strip():
        parsed = parse_sql_profile(sql)
        # SQL 解析到的源表/度量优先；未解析到时回退到显式入参
        if not parsed.source_tables and source_table:
            parsed.source_tables = [source_table]
        if not parsed.measures and measure_column:
            parsed.measures = [{"column": measure_column, "agg": None}]
        profile_kwargs["sql_profile"] = parsed
        profile_kwargs["sql"] = sql.strip()
    return profile_kwargs


# ----------------------------------------------------------------
# 各字段推断规则
# ----------------------------------------------------------------


def _infer_aggregation(profile: dict[str, Any]) -> SuggestionField:
    """聚合方式：SQL 聚合函数 > 列名规则。"""
    sql_profile: SqlProfile | None = profile.get("sql_profile")
    measure_column: str | None = profile.get("measure_column")
    if sql_profile and sql_profile.measures:
        primary = sql_profile.measures[0]
        agg = primary["agg"]
        if agg:
            reason = f"SQL 主度量 {primary['column']} 使用 {agg} 聚合"
            if len(sql_profile.measures) > 1:
                reason += f"（共 {len(sql_profile.measures)} 个度量，取首个）"
            return _field(agg, "sql_parse", 0.95, reason)
        # 有度量但无聚合（如裸列）→ 视为 COUNT
        return _field(
            "COUNT", "sql_parse", 0.8, f"SQL 投影 {primary['column']} 未显式聚合，按 COUNT 计"
        )
    if measure_column:
        col = measure_column.lower()
        if any(k in col for k in ("uv", "user_id", "cust_id", "distinct", "unique")):
            return _field(
                "COUNT_DISTINCT", "rule", 0.75,
                f"列名含去重语义（{measure_column}）→ COUNT_DISTINCT",
            )
        # 余额/存量语义须先于金额类（避免 "bal" 误命中 end_bal 等）
        if any(k in col for k in ("balance", "end_bal", "stock", "latest", "balance_end")):
            return _field(
                "LAST_VALUE", "rule", 0.7, f"列名含余额/存量语义（{measure_column}）→ LAST_VALUE"
            )
        if any(
            k in col
            for k in (
                "amount", "amt", "gmv", "price", "cost",
                "revenue", "sales", "fee", "qty", "quantity",
            )
        ):
            return _field("SUM", "rule", 0.72, f"列名含金额/数量语义（{measure_column}）→ SUM")
        if any(k in col for k in ("avg", "mean", "score")):
            return _field("AVG", "rule", 0.7, f"列名含均值语义（{measure_column}）→ AVG")
        if any(k in col for k in ("cnt", "count", "num")):
            return _field("COUNT", "rule", 0.7, f"列名含计数语义（{measure_column}）→ COUNT")
    return _field(None, "fallback", 0.0, "缺少 SQL 或度量列，无法推断聚合方式")


def _infer_granularity(profile: dict[str, Any]) -> SuggestionField:
    """粒度（时间粒度 token）：来自 GROUP BY 时间列或统计周期。"""
    sql_profile: SqlProfile | None = profile.get("sql_profile")
    period: str | None = profile.get("period")
    group_by: list[str] = sql_profile.group_by if sql_profile else []
    # 1) GROUP BY 中的时间列
    for g in group_by:
        for token in _GRAIN_TOKENS:
            if token in g.lower():
                grain = _GRAIN_TOKENS[token]
                dims = [x for x in group_by if x != g]
                reason = f"GROUP BY 含时间列 {g} → {grain}"
                if dims:
                    reason += f"；复合维度：{grain}×{','.join(dims)}"
                return _field(grain, "sql_parse", 0.9, reason)
    # 2) 统计周期
    if period:
        grain = _GRAIN_TOKENS.get(period.lower(), period.lower())
        return _field(grain, "rule", 0.6, f"统计周期 {period} → 粒度 {grain}")
    return _field("day", "rule", 0.5, "未识别时间粒度，默认按日（day）")


def _col_signal(meta: dict[str, Any], *keywords: str) -> bool:
    """列名/类型/注释任一包含关键词。"""
    hay = " ".join(str(meta.get(k, "")) for k in ("type", "comment", "name", "label")).lower()
    return any(k.lower() in hay for k in keywords)


def _infer_unit(profile: dict[str, Any]) -> SuggestionField:
    """单位：列类型/注释/名称三重信号。"""
    meta: dict[str, Any] = profile.get("measure_meta", {}) or {}
    if meta:
        src = "column_meta"
        if _col_signal(
            meta,
            "amount", "gmv", "price", "cost", "revenue", "sales", "fee",
            "金额", "单价", "总价", "余额",
        ):
            return _field("CNY", src, 0.85, "列元数据（金额类）→ 单位 CNY")
        if _col_signal(meta, "rate", "ratio", "pct", "percent", "proportion", "率", "占比"):
            return _field("PERCENT", src, 0.85, "列元数据（比率类）→ 单位 PERCENT")
        if _col_signal(meta, "uv", "user", "customer", "member", "account", "人数", "用户"):
            return _field("PERSON", src, 0.8, "列元数据（人/用户类）→ 单位 PERSON")
        if _col_signal(meta, "duration", "second", "minute", "时长"):
            return _field("MINUTE", src, 0.78, "列元数据（时长类）→ 单位 MINUTE")
        if _col_signal(meta, "cnt", "count", "num", "qty", "quantity", "次数", "数量"):
            # unit 字典无 COUNT code；TIMES（次）为最通用计数单位，与字典/seed 对齐
            return _field("TIMES", src, 0.75, "列元数据（计数类）→ 单位 TIMES（次）")
    measure_column: str | None = profile.get("measure_column")
    if measure_column:
        col = measure_column.lower()
        if any(k in col for k in ("rate", "ratio", "pct")):
            return _field("PERCENT", "rule", 0.7, f"列名含比率语义（{measure_column}）→ PERCENT")
        if any(k in col for k in ("uv", "user", "customer")):
            return _field("PERSON", "rule", 0.68, f"列名含用户语义（{measure_column}）→ PERSON")
        if any(k in col for k in ("amount", "gmv", "price", "cost", "revenue")):
            return _field("CNY", "rule", 0.68, f"列名含金额语义（{measure_column}）→ CNY")
        if any(k in col for k in ("cnt", "count", "num")):
            return _field(
                "TIMES", "rule", 0.66, f"列名含计数语义（{measure_column}）→ TIMES（次）"
            )
    return _field(None, "fallback", 0.0, "无法从列元数据/名称推断单位，请手动指定")


def _is_ratio_expression(profile: dict[str, Any]) -> bool:
    """SQL 是否比率/复合表达式（含除法或跨度量运算）。"""
    sql_profile: SqlProfile | None = profile.get("sql_profile")
    if not sql_profile or not sql_profile.sql:
        return False
    sql = sql_profile.sql.lower()
    return bool(
        ("/" in sql and ("sum(" in sql or "count(" in sql))
        or (
            sql_profile.measures
            and len(sql_profile.measures) >= 2
            and ("/" in sql or "+" in sql or "-" in sql)
        )
    )


def _infer_type(profile: dict[str, Any]) -> SuggestionField:
    """指标类型：原子/派生/复合（OneData 语义，周期驱动）。

    原子指标 = 逻辑度量 + 基础统计粒度（日），不含时间周期；派生指标 = 原子 +
    时间周期（月/周/季/年/小时等非日周期）；复合指标 = 多指标四则运算/比率。
    SQL 解析出的指标天然带业务限定 + 周期，非日周期归为派生（对齐用户建模认知）。
    """
    sql_profile: SqlProfile | None = profile.get("sql_profile")
    measure_column: str | None = profile.get("measure_column")
    if _is_ratio_expression(profile):
        return _field("derived", "sql_parse", 0.9, "SQL 含比率/跨度量运算 → 派生指标")
    if measure_column and any(
        k in measure_column.lower() for k in ("rate", "ratio", "pct", "avg", "mean")
    ):
        return _field("derived", "rule", 0.7, f"列名含比率/均值语义（{measure_column}）→ 派生指标")
    period = profile.get("period")
    if period and period != "day":
        return _field(
            "derived", "sql_parse", 0.8,
            f"解析出时间周期 {period} → 派生指标（原子指标 + 时间周期）",
        )
    if sql_profile and sql_profile.measures:
        return _field("atomic", "sql_parse", 0.85, "单表单聚合直算（日粒度常态）→ 原子指标")
    if measure_column:
        return _field("atomic", "rule", 0.65, f"列 {measure_column} 单度量直算 → 原子指标")
    return _field("atomic", "fallback", 0.4, "默认按原子指标")


def _infer_time_semantics(profile: dict[str, Any]) -> SuggestionField:
    """时间语义：来自时间谓词。"""
    sql_profile: SqlProfile | None = profile.get("sql_profile")
    if not sql_profile:
        return _field("PERIOD", "rule", 0.5, "无时间谓词，默认 PERIOD（当期）")
    hay = " ".join(sql_profile.filters).lower()
    sql = (sql_profile.sql or "").lower()
    if "ytd" in sql or ("year(" in sql and "current_date" in sql):
        return _field(
            "YTD", "sql_parse", 0.82, "WHERE 含 YEAR(dt)=YEAR(CURRENT_DATE) → 年初至今 YTD"
        )
    if "ttm" in sql or ("12" in hay and "month" in hay):
        return _field("TTM", "sql_parse", 0.8, "滚动 12 个月 → 近 12 月 TTM")
    if "mom" in sql or "环比" in sql or "interval 1 month" in hay:
        return _field("MOM", "sql_parse", 0.8, "环比（interval 1 month / MOM）→ MOM")
    if "yoy" in sql or "同比" in sql or "interval 1 year" in hay:
        return _field("YOY", "sql_parse", 0.8, "同比（interval 1 year / YOY）→ YOY")
    if "avg" in sql and ("over" in sql or "partition" in sql):
        return _field("AVG", "sql_parse", 0.75, "含窗口平均值 → AVG")
    return _field("PERIOD", "sql_parse", 0.6, "未识别特定时间窗口，默认 PERIOD（当期）")


def _infer_freshness(profile: dict[str, Any]) -> SuggestionField:
    """新鲜度：来自表元数据/表名语义。"""
    table_meta: dict[str, Any] = profile.get("table_meta", {}) or {}
    if table_meta.get("freshness"):
        return _field(
            table_meta["freshness"], "column_meta", 0.85, "采集目录标明的刷新频率 → 新鲜度"
        )
    source_table: str | None = profile.get("source_table")
    sql_profile: SqlProfile | None = profile.get("sql_profile")
    tables = [source_table] + (sql_profile.source_tables if sql_profile else [])
    hay = " ".join(t for t in tables if t).lower()
    if any(k in hay for k in ("kafka", "stream", "realtime", "real_time", "binlog", "cdc")):
        return _field("REALTIME", "rule", 0.7, "源表/流表（kafka/stream/binlog）→ REALTIME")
    if "_hour" in hay or "hourly" in hay:
        return _field("HOURLY", "rule", 0.68, "源表名含小时分区（_hour/hourly）→ HOURLY")
    return _field("T1", "rule", 0.5, "默认按 T+1 批处理（T1）")


def _infer_dw_layer(profile: dict[str, Any]) -> SuggestionField | None:
    """数仓层：表名前缀（保留既有规则）。"""
    source_table: str | None = profile.get("source_table")
    sql_profile: SqlProfile | None = profile.get("sql_profile")
    table = source_table
    if (not table) and sql_profile and sql_profile.source_tables:
        table = sql_profile.source_tables[0]
    if not table:
        return None
    t = table.lower()
    mapping = [
        ("ods", "ODS"),
        ("dwd", "DWD"),
        ("dws", "DWS"),
        ("ads", "ADS"),
        ("dim", "DM"),
    ]
    for prefix, layer in mapping:
        if t.startswith(prefix) or f".{prefix}" in t:
            return _field(layer, "rule", 0.85, f"源表名含 {prefix} 前缀 → {layer}")
    return _field(None, "fallback", 0.0, "未识别数仓层前缀")


def _infer_additivity(agg_field: SuggestionField) -> SuggestionField:
    """可加性：由聚合方式驱动。"""
    agg = agg_field["value"]
    if agg in ("SUM", "COUNT", "COUNT_DISTINCT"):
        return _field("ADDITIVE", "rule", 0.88, f"{agg} 可跨维度相加 → ADDITIVE")
    if agg in ("AVG", "MEDIAN", "PERCENTILE"):
        return _field("NON_ADDITIVE", "rule", 0.88, f"{agg} 不可跨维度相加 → NON_ADDITIVE")
    if agg in ("MAX", "MIN", "LAST_VALUE"):
        return _field(
            "SEMI_ADDITIVE", "rule", 0.85,
            f"{agg} 仅对时间维度不可加 → SEMI_ADDITIVE（不可加维度：时间）",
        )
    return _field("ADDITIVE", "rule", 0.5, "默认 ADDITIVE")


def _infer_serving_mode(freshness_field: SuggestionField) -> SuggestionField:
    """服务模式：由新鲜度驱动。"""
    fresh = freshness_field["value"]
    mapping = {
        "REALTIME": ("REALTIME_ONLY", "实时数据源 → 仅实时服务模式"),
        "HOURLY": ("BATCH_REALTIME_DUAL", "小时级 → 批处理+实时双通道"),
        "T0": ("BATCH_ONLY", "T0 → 批处理服务模式"),
        "T1": ("BATCH_ONLY", "T+1 → 批处理服务模式"),
    }
    if fresh in mapping:
        val, reason = mapping[fresh]
        return _field(val, "rule", 0.8, reason)
    return _field("BATCH_ONLY", "rule", 0.6, "默认批处理服务模式")


def _core_keyword_in(profile: dict[str, Any], *keywords: str) -> bool:
    """度量列/源表/SQL 是否含核心指标关键词。"""
    sql_profile: SqlProfile | None = profile.get("sql_profile")
    hay = " ".join(
        [
            str(profile.get("measure_column", "")),
            str(profile.get("source_table", "")),
            (sql_profile.sql or "") if sql_profile else "",
        ]
    ).lower()
    return any(k.lower() in hay for k in keywords)


def _infer_metric_tier(
    profile: dict[str, Any], dw_layer_field: SuggestionField | None
) -> SuggestionField:
    """分级：数仓层 + 核心关键词。"""
    layer = dw_layer_field["value"] if dw_layer_field else None
    if layer in ("ADS", "DM") and _core_keyword_in(
        profile, "gmv", "revenue", "dau", "active", "retention", "留存", "营收"
    ):
        return _field("T1", "rule", 0.72, f"{layer} 层 + 核心指标关键词 → T1")
    if layer == "DWS":
        return _field("T2", "rule", 0.7, "DWS 层汇总指标 → T2")
    if layer in ("DWD", "ODS"):
        return _field("T3", "rule", 0.7, "DWD/ODS 层明细指标 → T3")
    if _core_keyword_in(profile, "gmv", "revenue", "dau", "active", "营收", "核心"):
        return _field("T1", "rule", 0.6, "含核心指标关键词 → T1")
    return _field("T2", "rule", 0.55, "默认 T2")


def _period_cn(period: str | None, grain: str | None) -> str:
    """周期中文标签。"""
    token = (period or grain or "day").lower()
    return _PERIOD_CN.get(token, _PERIOD_CN.get(_GRAIN_TOKENS.get(token, ""), "日"))


def _cn_column_label(col: str) -> str | None:
    """业务列名 → 中文标签（命中受控词根）；无法映射返回 None。

    计数后缀列（_cnt/_count/_num/_qty/_quantity）：主干命中词表 → 中文标签；
    未知主干 → 「xx次数」（含受控词根「次数」）。非计数列：全列名/逐 token 查
    金额/费用/比率词表。避免生成英文 slug 被命名校验拦截。
    """
    base = col.lower().strip()
    for suf in _COUNT_SUFFIXES:
        if base.endswith(suf):
            stem = base[: -len(suf)]
            last = stem.split("_")[-1]
            if not last:
                return None
            return _CN_COLUMN_LABELS.get(last, f"{last}次数")
    if base in _CN_COLUMN_LABELS:
        return _CN_COLUMN_LABELS[base]
    for token in base.split("_"):
        if token in _CN_COLUMN_LABELS:
            return _CN_COLUMN_LABELS[token]
    return None


def _measure_label(profile: dict[str, Any]) -> str:
    """度量中文标签（用于名称生成）。"""
    meta: dict[str, Any] = profile.get("measure_meta", {}) or {}
    comment = str(meta.get("comment", "")).strip()
    if comment:
        return comment
    measure_column: str | None = profile.get("measure_column")
    sql_profile: SqlProfile | None = profile.get("sql_profile")
    if measure_column:
        return _cn_column_label(measure_column) or measure_column.replace("_", " ")
    if sql_profile and sql_profile.measures:
        col = str(sql_profile.measures[0]["column"])
        return _cn_column_label(col) or col.replace("_", " ")
    return "指标"


def _infer_name(
    profile: dict[str, Any],
    grain: str | None,
    *,
    llm_name: str | None = None,
) -> SuggestionField:
    """指标名称：列注释优先 > AI 生成 > 规则模板。"""
    period_cn = _period_cn(profile.get("period"), grain)
    if llm_name and llm_name.strip():
        return _field(llm_name.strip(), "llm", 0.7, "AI 依据表结构/SQL 生成的业务命名")
    meta: dict[str, Any] = profile.get("measure_meta", {}) or {}
    comment = str(meta.get("comment", "")).strip()
    if comment:
        return _field(
            f"{period_cn}{comment}", "column_meta", 0.9,
            f"列注释「{comment}」+ 周期「{period_cn}」→ 名称",
        )
    measure_label = _measure_label(profile)
    return _field(
        f"{period_cn}{measure_label}", "rule", 0.5,
        f"规则模板：周期「{period_cn}」+ 度量「{measure_label}」",
    )


def _infer_definition(
    profile: dict[str, Any], agg_field: SuggestionField
) -> tuple[SuggestionField, SuggestionField]:
    """口径定义 + 定义模式。"""
    sql_profile: SqlProfile | None = profile.get("sql_profile")
    if sql_profile and sql_profile.sql:
        definition = {
            "sql": sql_profile.sql,
            "source_tables": sql_profile.source_tables,
            "expression": sql_profile.sql,
            "group_by": sql_profile.group_by,
            "filters": sql_profile.filters,
            "time_column": sql_profile.time_column,
            "measure_columns": [m["column"] for m in sql_profile.measures],
        }
        mode = _field("sql", "sql_parse", 0.9, "依据提供 SQL 生成 SQL 模式口径定义")
        return (
            _field(
                definition, "sql_parse", 0.9,
                "SQL 模式口径定义（含源表/维度/度量/时间谓词）",
            ),
            mode,
        )
    # 表达式模式
    measure_column: str | None = profile.get("measure_column")
    source_table: str | None = profile.get("source_table")
    sql_profile2: SqlProfile | None = profile.get("sql_profile")
    table = source_table or (
        sql_profile2.source_tables[0] if sql_profile2 and sql_profile2.source_tables else None
    )
    agg = agg_field["value"] or "SUM"
    col = measure_column or (
        sql_profile2.measures[0]["column"] if sql_profile2 and sql_profile2.measures else "*"
    )
    expr_definition: dict[str, Any] = {
        "expression": f"{agg}({col})",
        "source_fields": [{"table": table, "column": col}] if table else [],
        "partition_key": sql_profile2.time_column if sql_profile2 else None,
    }
    return (
        _field(
            expr_definition, "rule", 0.8,
            f"表达式模式口径：{agg}({col})" + (f" @ {table}" if table else ""),
        ),
        _field("expression", "rule", 0.8, "表达式模式口径定义"),
    )


# ----------------------------------------------------------------
# 主推断
# ----------------------------------------------------------------


def infer_metric(
    profile: dict[str, Any],
    *,
    domain_defaults: dict[str, Any] | None = None,
    llm_name: str | None = None,
) -> dict[str, Any]:
    """多字段指标推断主函数。

    Args:
        profile: build_profile 产出的画像 dict。
        domain_defaults: 域级默认值预设（最高优先级，覆盖所有规则推断）。
        llm_name: LLM 生成的指标名称（可选；提供则作为 name 的 AI 来源，否则规则兜底）。

    Returns:
        {
            "fields": {字段名: SuggestionField},
            "metric_code_suggestion": str | None,
            "segments": dict,
            "definition_json": dict,
            "definition_mode": str,
        }
    """
    domain_defaults = domain_defaults or {}

    agg_field = _infer_aggregation(profile)
    grain_field = _infer_granularity(profile)
    unit_field = _infer_unit(profile)
    type_field = _infer_type(profile)
    time_sem_field = _infer_time_semantics(profile)
    freshness_field = _infer_freshness(profile)
    dw_layer_field = _infer_dw_layer(profile)
    additivity_field = _infer_additivity(agg_field)
    serving_field = _infer_serving_mode(freshness_field)
    tier_field = _infer_metric_tier(profile, dw_layer_field)
    name_field = _infer_name(profile, grain_field["value"], llm_name=llm_name)
    definition_field, mode_field = _infer_definition(profile, agg_field)

    # 源表/度量列：优先用户显式输入，其次 SQL 解析结果（供前端回填到 Step 2）
    sql_profile: SqlProfile | None = profile.get("sql_profile")
    eff_table = profile.get("source_table") or (
        sql_profile.source_tables[0] if sql_profile and sql_profile.source_tables else None
    )
    eff_measure = profile.get("measure_column") or (
        sql_profile.measures[0]["column"] if sql_profile and sql_profile.measures else None
    )
    table_field = (
        _field(eff_table, "sql_parse" if eff_table != profile.get("source_table") else "input", 0.9,
               "SQL 解析源表" if eff_table != profile.get("source_table") else "用户指定源表")
        if eff_table else _field(None, "fallback", 0.0, "未识别源表")
    )
    measure_field = (
        _field(
            eff_measure,
            "sql_parse" if eff_measure != profile.get("measure_column") else "input",
            0.9,
            "SQL 解析度量列" if eff_measure != profile.get("measure_column") else "用户指定度量列",
        )
        if eff_measure
        else _field(None, "fallback", 0.0, "未识别度量列")
    )

    fields: dict[str, SuggestionField] = {
        "source_table": table_field,
        "measure_column": measure_field,
        "name": name_field,
        "type": type_field,
        "granularity": grain_field,
        "unit": unit_field,
        "aggregation": agg_field,
        "time_semantics": time_sem_field,
        "freshness": freshness_field,
        "dw_layer": dw_layer_field or _field(None, "fallback", 0.0, "未识别数仓层"),
        "additivity": additivity_field,
        "serving_mode": serving_field,
        "metric_tier": tier_field,
        "definition_json": definition_field,
        "definition_mode": mode_field,
    }

    # 域默认值覆盖（最高优先级，且标记来源为 domain_default）
    for key, val in domain_defaults.items():
        if val is None:
            continue
        if key in fields:
            fields[key] = _field(val, "domain_default", 1.0, f"域默认值覆盖：{val}")

    # 指标编码建议
    domain_code = str(domain_defaults.get("domain", "") or profile.get("domain_code", ""))
    source_table = profile.get("source_table")
    measure_column = profile.get("measure_column")
    period = profile.get("period")
    if not source_table and sql_profile and sql_profile.source_tables:
        source_table = sql_profile.source_tables[0]
    metric_code: str | None = None
    segments = {"domain": domain_code, "biz_object": None, "measure": None, "period": period}
    if source_table and measure_column and period:
        metric_code = generate_metric_code(domain_code, source_table, measure_column, period)
        segments = {
            "domain": domain_code,
            "biz_object": extract_biz_object(source_table),
            "measure": extract_measure(measure_column),
            "period": period,
        }

    definition_json = definition_field["value"]

    return {
        "fields": fields,
        "metric_code_suggestion": metric_code,
        "segments": segments,
        "definition_json": definition_json,
        "definition_mode": mode_field["value"],
    }


# ----------------------------------------------------------------
# 向后兼容：auto_fill（service.create_metric 仍调用）
# ----------------------------------------------------------------


def auto_fill(
    domain_code: str,
    source_table: str | None = None,
    measure_column: str | None = None,
    period: str | None = None,
    domain_defaults: dict[str, Any] | None = None,
    sql: str | None = None,
    measure_meta: dict[str, Any] | None = None,
    table_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """自动推断引擎主函数（向后兼容签名）。

    返回旧结构 ``{metric_code_suggestion, defaults, segments}`` 并附加
    ``fields`` / ``definition_json`` / ``definition_mode``，供新前端消费。
    """
    profile = build_profile(
        source_table=source_table,
        measure_column=measure_column,
        period=period,
        sql=sql,
        measure_meta=measure_meta,
        table_meta=table_meta,
    )
    profile["domain_code"] = domain_code
    enriched_defaults = dict(domain_defaults or {})
    enriched_defaults.setdefault("domain", domain_code)
    result = infer_metric(profile, domain_defaults=enriched_defaults)

    # 组装旧式 defaults（域默认优先，规则推断兜底）
    defaults: dict[str, Any] = {}
    if domain_defaults:
        defaults = dict(domain_defaults)
    for key in (
        "dw_layer", "type", "granularity", "unit", "aggregation",
        "time_semantics", "freshness", "additivity", "serving_mode", "metric_tier", "name",
    ):
        fld = result["fields"].get(key)
        if fld and fld["value"] is not None and key not in defaults:
            defaults[key] = fld["value"]

    return {
        "metric_code_suggestion": result["metric_code_suggestion"],
        "defaults": defaults,
        "segments": result["segments"],
        "fields": result["fields"],
        "definition_json": result["definition_json"],
        "definition_mode": result["definition_mode"],
    }

