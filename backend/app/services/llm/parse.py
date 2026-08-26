"""LLM 结构化输出统一解析器（TD §11 韧性 / FR-023 格式保证）。

所有 LLM 输出的解析收敛到本模块，统一处理三类常见的「格式不确定性」：
- markdown 代码围栏（`` ```json ... ``` ``）包裹；
- 模型用别名替代约定字段名（如 ``desc`` / ``text`` / ``label`` 代替 ``description``）；
- 数值类型漂移（``"0.8"`` 字符串与 ``0.8`` 数字混用、置信度越界）。

设计原则：解析失败一律返回 ``None``（上层据此降级为「LLM 不可用」，绝不抛异常、
绝不静默误用错误字段），由调用方决定是否重试或人工兜底。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 匹配以 ``` 或 ```json 开头/结尾的代码围栏，捕获中间内容（忽略大小写与首尾空白）。
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def strip_code_fence(raw: str) -> str:
    """剥离 markdown 代码围栏；非围栏文本原样返回。"""
    stripped = raw.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    return stripped


def parse_json_object(raw: str) -> dict[str, Any] | None:
    """将 LLM 文本解析为 JSON 对象。

    非字符串、空串、非法 JSON 或非 dict 结构统一返回 ``None``。
    """
    if not raw or not raw.strip():
        return None
    try:
        parsed = json.loads(strip_code_fence(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def extract_str_field(obj: dict[str, Any], *aliases: str) -> str | None:
    """按别名顺序抽取首个非空字符串字段；数字也会转为文本兜底。"""
    for key in aliases:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            text = str(value).strip()
            if text:
                return text
    return None


def extract_numeric_field(
    obj: dict[str, Any],
    *aliases: str,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float | None:
    """抽取首个数值字段并强转为 float；越界或缺失返回 ``None``。"""
    for key in aliases:
        value = obj.get(key)
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if min_value is not None and number < min_value:
            return None
        if max_value is not None and number > max_value:
            return None
        return number
    return None


def parse_description_result(raw: str) -> tuple[str | None, float | None]:
    """解析字段/表描述推断结果。

    Returns:
        ``(description, confidence)``；任一缺失或越界返回 ``(None, None)``。
    """
    obj = parse_json_object(raw)
    if obj is None:
        return None, None
    description = extract_str_field(obj, "description", "desc", "text", "label", "summary")
    confidence = extract_numeric_field(
        obj, "confidence", "score", "prob", "certainty", min_value=0.0, max_value=1.0
    )
    if description is None or confidence is None:
        return None, None
    return description, confidence


def parse_batch_description_result(
    raw: str, expected_names: list[str]
) -> dict[str, tuple[str, float]]:
    """解析批量字段描述推断结果（一次 LLM 调用返回多个字段）。

    约定返回结构：
    ``{"descriptions": [{"column_name": "...", "description": "...", "confidence": 0.0}, ...]}``。
    **顺序性保证**：按 ``column_name`` 匹配回填（不依赖 LLM 返回顺序），且只保留
    请求期望的字段——模型漏报/插报/乱序都不会污染结果。

    Returns:
        ``{column_name: (description, confidence)}``；整体解析失败时返回空 dict。
    """
    obj = parse_json_object(raw)
    if obj is None:
        return {}
    items: Any = None
    for key in ("descriptions", "results", "fields", "items", "columns"):
        value = obj.get(key)
        if isinstance(value, list):
            items = value
            break
    if items is None:
        return {}
    expected_set = set(expected_names)
    out: dict[str, tuple[str, float]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        column_name = extract_str_field(item, "column_name", "name", "column", "field")
        if column_name is None or column_name not in expected_set:
            continue
        description = extract_str_field(item, "description", "desc", "text", "label", "summary")
        confidence = extract_numeric_field(
            item, "confidence", "score", "prob", "certainty", min_value=0.0, max_value=1.0
        )
        if description is not None and confidence is not None:
            out[column_name] = (description, confidence)
    return out


def parse_bool_result(raw: str, *aliases: str) -> bool | None:
    """解析布尔型判定结果（如同义判定 ``{"same": true}``）。

    Returns:
        ``True`` / ``False`` / ``None``（无法判定时）。
    """
    obj = parse_json_object(raw)
    if obj is None:
        return None
    keys = aliases or ("same", "is_same", "equal", "result", "match")
    for key in keys:
        value = obj.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("true", "yes", "1"):
                return True
            if normalized in ("false", "no", "0"):
                return False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return bool(value)
    return None


def parse_term_infer_result(raw: str) -> dict[str, Any] | None:
    """解析术语推断结果（定义/同义词/边界说明，基于术语名称生成）。

    约定返回结构：``{"definition", "synonyms": [...], "boundary", "confidence"}``。
    同义词可能为逗号/顿号分隔的字符串或列表——统一归一为列表；
    任一核心字段缺失都返回 None（上层降级为 LLM 不可用）。

    Returns:
        ``{"definition", "synonyms", "boundary", "confidence"}``；解析失败返回 ``None``。
    """
    obj = parse_json_object(raw)
    if obj is None:
        return None
    definition = extract_str_field(obj, "definition", "desc", "text", "meaning")
    confidence = extract_numeric_field(
        obj, "confidence", "score", "prob", "certainty", min_value=0.0, max_value=1.0
    )
    if definition is None or confidence is None:
        return None
    # 同义词：列表或字符串（逗号/顿号/分号分隔）统一归一为 list[str]
    synonyms: list[str] = []
    raw_syn = obj.get("synonyms") or obj.get("alias") or obj.get("aliases")
    if isinstance(raw_syn, list):
        for item in raw_syn:
            if isinstance(item, str) and item.strip():
                synonyms.append(item.strip())
    elif isinstance(raw_syn, str) and raw_syn.strip():
        for part in re.split(r"[,，;；、\n]+", raw_syn):
            if part.strip():
                synonyms.append(part.strip())
    boundary = extract_str_field(obj, "boundary", "exclusion", "note", "constraint")
    return {
        "definition": definition,
        "synonyms": synonyms,
        "boundary": boundary,
        "confidence": confidence,
    }


def parse_domain_infer_result(raw: str) -> dict[str, Any] | None:
    """解析业务域推断结果（LLM 兜底：表未被采集时从 SQL/表名推断域）。

    约定返回结构：``{"domain_code", "confidence", "reason"}``。
    ``domain_code``/``confidence`` 任一缺失或越界返回 ``None``
    （上层降级为无法建议，用户手动选域）。

    Returns:
        ``{"domain_code", "confidence", "reason"}``；解析失败返回 ``None``。
    """
    obj = parse_json_object(raw)
    if obj is None:
        return None
    domain_code = extract_str_field(obj, "domain_code", "domain", "code")
    confidence = extract_numeric_field(
        obj, "confidence", "score", "prob", min_value=0.0, max_value=1.0
    )
    if domain_code is None or confidence is None:
        return None
    reason = extract_str_field(obj, "reason", "note", "explanation", "basis")
    return {"domain_code": domain_code, "confidence": confidence, "reason": reason}


def parse_sql_split_result(raw: str) -> list[dict[str, Any]] | None:
    """解析 SQL 语义分段结果（LLM 兜底：用户自定义切分未生效时按语义拆语句）。

    约定返回结构：``{"statements": [{"sql", "name", "reason"}, ...]}``。
    ``sql`` 兼容别名（sql/text/statement/segment/content）；逐项去空去重，
    防止 LLM 幻觉产出重复/空白片段污染候选；整体无有效片段返回 ``None``
    （上层降级为单段整体处理）。

    Returns:
        ``[{"sql", "name", "reason"}]``；解析失败或无有效片段返回 ``None``。
    """
    obj = parse_json_object(raw)
    if obj is None:
        return None
    items: Any = None
    for key in ("statements", "segments", "items", "parts", "queries"):
        value = obj.get(key)
        if isinstance(value, list):
            items = value
            break
    if items is None:
        return None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        sql_text = extract_str_field(item, "sql", "text", "statement", "segment", "content")
        if sql_text is None or sql_text in seen:
            continue
        name = extract_str_field(item, "name", "title", "label", "metric_name")
        reason = extract_str_field(item, "reason", "note", "explanation", "basis")
        seen.add(sql_text)
        out.append({"sql": sql_text, "name": name, "reason": reason})
    if not out:
        return None
    return out


# LLM 周期推断白名单（与 sql_infer._KNOWN_GRAINS / 指标周期枚举对齐）
_PERIOD_WHITELIST = ("day", "week", "month", "quarter", "year", "hour")

# 周期别名（中文/英文全称/简写 → 白名单值）
_PERIOD_ALIASES = {
    "日": "day", "天": "day", "daily": "day", "1d": "day",
    "周": "week", "weekly": "week", "1w": "week",
    "月": "month", "月度": "month", "月均": "month", "monthly": "month", "1m": "month", "mon": "month",
    "季": "quarter", "季度": "quarter", "quarterly": "quarter", "qtr": "quarter",
    "年": "year", "年度": "year", "yearly": "year", "annually": "year",
    "时": "hour", "小时": "hour", "hourly": "hour",
}


def normalize_period(raw: str | None) -> str | None:
    """归一化统计周期到白名单值；缺失/非法返回 ``None``。

    兼容中文别名（``月``/``月度`` → ``month``）与英文全称/简写
    （``daily``/``1d`` → ``day``）。供 ``parse_period_infer_result`` 与
    ``parse_sql_measures_result`` 共用——两个 LLM 解析器对同一「period」
    字段必须用同一套白名单与别名，避免 SQL 度量提取兜底产出非法周期
    （如 ``月度``）污染候选编码。
    """
    if not raw:
        return None
    low = str(raw).strip().lower()
    if low in _PERIOD_ALIASES:
        low = _PERIOD_ALIASES[low]
    return low if low in _PERIOD_WHITELIST else None


def parse_period_infer_result(raw: str) -> dict[str, Any] | None:
    """解析统计周期推断结果（LLM 兜底：规则层无法确定时间粒度时从 SQL 推断）。

    约定返回结构：``{"period", "confidence", "reason"}``。
    ``period`` 经 ``normalize_period`` 归一化（白名单 + 中文/英文别名）；
    ``confidence`` 越界或缺任一关键字段返回 ``None``（上层降级为规则层默认周期）。

    Returns:
        ``{"period", "confidence", "reason"}``；解析失败返回 ``None``。
    """
    obj = parse_json_object(raw)
    if obj is None:
        return None
    period = extract_str_field(obj, "period", "granularity", "grain", "cycle")
    period_norm = normalize_period(period)
    if period_norm is None:
        return None
    confidence = extract_numeric_field(
        obj, "confidence", "score", "prob", min_value=0.0, max_value=1.0
    )
    if confidence is None:
        return None
    reason = extract_str_field(obj, "reason", "note", "explanation", "basis")
    return {"period": period_norm, "confidence": confidence, "reason": reason}


# LLM 度量提取聚合白名单（与 sql_infer._AGG_FUNCS 对齐）
_MEASURE_AGG_WHITELIST = {
    "SUM", "AVG", "COUNT", "COUNT_DISTINCT", "APPROX_DISTINCT", "MAX", "MIN",
    "MEDIAN", "PERCENTILE", "LAST_VALUE", "FIRST_VALUE",
}


def _normalize_measure_agg(agg: str) -> str | None:
    """归一化 LLM 返回的聚合方式到大写白名单；非法返回 None。

    ``approx_count_distinct``/``approx_distinct`` 归一到 ``COUNT_DISTINCT``、
    ``percentile_approx``/``percentile_cont`` 归一到 ``PERCENTILE``。
    """
    upper = agg.upper().replace(" ", "_").replace("-", "_")
    if upper in ("APPROX_COUNT_DISTINCT", "APPROX_DISTINCT"):
        return "COUNT_DISTINCT"
    if upper.startswith("PERCENTILE"):
        return "PERCENTILE"
    if upper in ("COUNT_DISTINCT", "DISTINCT_COUNT"):
        return "COUNT_DISTINCT"
    return upper if upper in _MEASURE_AGG_WHITELIST else None


def parse_sql_measures_result(raw: str) -> list[dict[str, Any]] | None:
    """解析 SQL 度量提取结果（LLM 兜底：规则层无法解析时从 SQL 提取聚合度量）。

    约定返回结构：``{"measures": [{"column", "agg", "alias", "table", "period",
    "name", "reason"}, ...], "source_table": ...}``。``column``/``agg`` 必填
    （agg 归一到大写白名单）；``alias``/``table``/``period``/``name`` 可缺省。
    逐项过滤：缺 column、agg 非法、column 重复的丢弃——防止 LLM 幻觉产出
    无聚合列/重复列污染候选；整体无有效度量返回 ``None``（上层降级为 skipped，
    不阻断批量解析）。

    Returns:
        ``[{"column", "agg", "alias"?, "table"?, "period"?, "name"?}]``；
        解析失败或无有效度量返回 ``None``。
    """
    obj = parse_json_object(raw)
    if obj is None:
        return None
    items: Any = None
    for key in ("measures", "metrics", "items", "aggregations", "candidates"):
        value = obj.get(key)
        if isinstance(value, list):
            items = value
            break
    if items is None:
        return None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        column = extract_str_field(
            item, "column", "col", "measure_column", "metric_column", "field", "expr"
        )
        if column is None or column in seen:
            continue
        agg = extract_str_field(
            item, "agg", "aggregation", "aggregate", "func", "function", "operator"
        )
        agg_norm = _normalize_measure_agg(agg) if agg else None
        if agg_norm is None:
            continue
        seen.add(column)
        measure: dict[str, Any] = {"column": column, "agg": agg_norm}
        alias = extract_str_field(item, "alias", "as", "label", "output_name")
        if alias and alias != column:
            measure["alias"] = alias
        table = extract_str_field(item, "table", "source_table", "src_table", "from_table")
        if table:
            measure["table"] = table
        period = extract_str_field(item, "period", "granularity", "grain", "cycle")
        period_norm = normalize_period(period)
        if period_norm:
            measure["period"] = period_norm
        name = extract_str_field(item, "name", "metric_name", "title", "desc")
        if name:
            measure["name"] = name
        reason = extract_str_field(item, "reason", "note", "explanation", "basis")
        if reason:
            measure["reason"] = reason
        out.append(measure)
    if not out:
        return None
    return out


def _coerce_bool(value: Any, default: bool = True) -> bool:
    """把 LLM 输出的 is_measure 字段布尔化（接受 bool/数字/中英文）。

    兼容 ``true``/``false``/``True``/``是``/``否``/``1``/``0`` 等形态；
    无法识别时回退 ``default``（缺省 True——保守原则：规则说有就保留）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "y", "是", "1"):
            return True
        if low in ("false", "no", "n", "否", "0"):
            return False
    return default


def parse_sql_candidates_annotations(raw: str) -> list[dict[str, Any]] | None:
    """解析候选批量补全结果（use_llm 显式模式：对规则候选做封闭选择）。

    约定返回结构：``{"candidates": [{"key", "is_measure", "name", "period",
    "confidence", "reason"}, ...]}``。``key`` 必填（对齐候选 ``{idx}:{alias|col}``
    稳定标识，LLM 只能对已解析候选做选择、不能新增/发明候选）；``is_measure``
    布尔化（``_coerce_bool``）；``name`` 非空且 ≤128 才采用；``period`` 经
    ``normalize_period`` 白名单归一；``confidence`` 越界丢弃。缺 key / 非 dict
    项丢弃——防 LLM 幻觉产出未知候选键污染结果。整体无有效项返回 ``None``
    （上层保持规则候选不动）。

    Returns:
        ``[{"key", "is_measure", "name"?, "period"?, "confidence"?, "reason"?}]``；
        解析失败或无有效项返回 ``None``。
    """
    obj = parse_json_object(raw)
    if obj is None:
        return None
    items: Any = None
    for key in ("candidates", "items", "annotations", "results"):
        value = obj.get(key)
        if isinstance(value, list):
            items = value
            break
    if items is None:
        return None
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = extract_str_field(item, "key", "candidate_key", "idx", "id")
        if key is None:
            continue
        ann: dict[str, Any] = {"key": key}
        is_measure = item.get("is_measure", item.get("is_metric", item.get("measure", True)))
        ann["is_measure"] = _coerce_bool(is_measure)
        name = extract_str_field(item, "name", "metric_name", "title", "desc")
        if name and len(name) <= 128:
            ann["name"] = name
        period = extract_str_field(item, "period", "granularity", "grain", "cycle")
        period_norm = normalize_period(period)
        if period_norm:
            ann["period"] = period_norm
        confidence = extract_numeric_field(
            item, "confidence", "score", "prob", min_value=0.0, max_value=1.0
        )
        if confidence is not None:
            ann["confidence"] = confidence
        reason = extract_str_field(item, "reason", "note", "explanation", "basis")
        if reason:
            ann["reason"] = reason
        out.append(ann)
    if not out:
        return None
    return out
