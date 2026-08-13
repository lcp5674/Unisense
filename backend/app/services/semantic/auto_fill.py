"""自动推断引擎（纯函数式，对齐 spec FR-010/FR-011, plan.md D3）。

输入：域code + 源表名 + 度量列 + 统计周期 + 域默认值预设
输出：指标编码建议 + 字段默认值 dict + 编码4段拆分

编码4段式：{domain}_{biz_object}_{measure}_{period}
- domain: 域编码（如 sales）
- biz_object: 从源表提取（去 dwd_/ods_/dim_ 前缀，取首词）
- measure: 度量列名（去下划线）
- period: 统计周期（如 day/month）
"""

from __future__ import annotations

import re
from typing import Any

import structlog

logger = structlog.get_logger("unisense.auto_fill")

# 源表名前缀清洗
_TABLE_PREFIXES = re.compile(r"^(dwd_|ods_|dws_|ads_|dim_|tmp_)", re.IGNORECASE)
# 合法编码字符
_CODE_SEGMENT_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
# 4段式完整编码
METRIC_CODE_PATTERN = re.compile(
    r"^[a-z][a-z0-9_]*_[a-z][a-z0-9_]*_[a-z][a-z0-9_]*_[a-z][a-z0-9_]*$"
)

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


def extract_biz_object(source_table: str) -> str:
    """从源表名提取业务对象段。

    规则：
    1. 去掉 dwd_/ods_/dim_ 等数仓层前缀
    2. 去掉库名前缀（如 dwd.sales_detail → sales_detail → sales）
    3. 取第一个下划线前的词作为 biz_object
    """
    # 去库名前缀
    table_name = source_table.split(".")[-1] if "." in source_table else source_table
    # 去数仓层前缀
    table_name = _TABLE_PREFIXES.sub("", table_name)
    # 取首词
    first_word = table_name.split("_")[0] if "_" in table_name else table_name
    return first_word.lower()


def extract_measure(measure_column: str) -> str:
    """从度量列名提取编码段（去下划线，小写）。"""
    return measure_column.replace("_", "").lower()


def generate_metric_code(
    domain: str,
    source_table: str,
    measure_column: str,
    period: str,
) -> str:
    """生成4段式指标编码建议。

    Returns:
        4段式编码，如 sales_sales_amount_day
    """
    biz_obj = extract_biz_object(source_table)
    measure = extract_measure(measure_column)
    return f"{domain}_{biz_obj}_{measure}_{period}"


def validate_metric_code(code: str) -> tuple[bool, str]:
    """校验指标编码4段格式。

    Returns:
        (is_valid, error_message)
    """
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


def auto_fill(
    domain_code: str,
    source_table: str | None = None,
    measure_column: str | None = None,
    period: str | None = None,
    domain_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """自动推断引擎主函数。

    Args:
        domain_code: 主题域编码
        source_table: 源表名（如 dwd.sales_detail）
        measure_column: 度量列名（如 amount）
        period: 统计周期（如 day）
        domain_defaults: 域级默认值预设

    Returns:
        {
            "metric_code_suggestion": str | None,
            "defaults": dict,   # 合并后的字段默认值
            "segments": dict,   # 4段拆分
        }
    """
    defaults: dict[str, Any] = {}
    segments: dict[str, str | None] = {
        "domain": domain_code,
        "biz_object": None,
        "measure": None,
        "period": period,
    }

    # 域默认值带入
    if domain_defaults:
        defaults = dict(domain_defaults)

    # 编码建议
    metric_code_suggestion: str | None = None
    if source_table and measure_column and period:
        biz_obj = extract_biz_object(source_table)
        measure = extract_measure(measure_column)
        metric_code_suggestion = f"{domain_code}_{biz_obj}_{measure}_{period}"
        segments["biz_object"] = biz_obj
        segments["measure"] = measure

    # 推断字段
    if source_table:
        inferred_layer = _infer_dw_layer(source_table)
        if inferred_layer:
            defaults.setdefault("dw_layer", inferred_layer)

    if measure_column:
        inferred_type = _infer_metric_type(measure_column)
        if inferred_type:
            defaults.setdefault("type", inferred_type)

    # 统计周期默认
    if period:
        defaults.setdefault("granularity", period)

    return {
        "metric_code_suggestion": metric_code_suggestion,
        "defaults": defaults,
        "segments": segments,
    }


def _infer_dw_layer(source_table: str) -> str | None:
    """从源表名推断数仓层。"""
    table_lower = source_table.lower()
    if table_lower.startswith("ods") or ".ods" in table_lower:
        return "ODS"
    if table_lower.startswith("dwd") or ".dwd" in table_lower:
        return "DWD"
    if table_lower.startswith("dws") or ".dws" in table_lower:
        return "DWS"
    if table_lower.startswith("ads") or ".ads" in table_lower:
        return "ADS"
    if table_lower.startswith("dim") or ".dim" in table_lower:
        return "DM"
    return None


def _infer_metric_type(measure_column: str) -> str | None:
    """从度量列名推断指标类型。

    简单启发式：包含 cnt/count → atomic；包含 rate/ratio → derived。
    """
    col_lower = measure_column.lower()
    if any(kw in col_lower for kw in ("cnt", "count", "num", "amount", "qty", "quantity")):
        return "atomic"
    if any(kw in col_lower for kw in ("rate", "ratio", "pct", "avg", "mean")):
        return "derived"
    return None
