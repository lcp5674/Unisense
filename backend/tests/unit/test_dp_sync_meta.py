"""dp 血缘同步元数据目录（枚举/排除规则）纯函数测试。

覆盖：内置默认排除恒生效 + 自定义追加语义（修「空列表关闭默认」bug）、
类型目录合并（内置+探测计数+未识别标注）、正则语法校验、命中统计预览。
"""

from __future__ import annotations

from app.services.lineage.dp_sync_meta import (
    DP_STEP_TYPES,
    DP_TASK_TYPES,
    catalog_with_counts,
    count_regex_matches,
    merged_exclude_table_patterns,
    validate_regex,
)
from app.services.lineage.dp_sync_parser import DEFAULT_EXCLUDE_TABLE_PATTERNS


def test_merged_exclude_keeps_defaults_when_custom_empty():
    """自定义为空/None 时仍保留内置默认（历史 bug：空列表关闭默认）。"""
    assert merged_exclude_table_patterns(None) == DEFAULT_EXCLUDE_TABLE_PATTERNS
    assert merged_exclude_table_patterns([]) == DEFAULT_EXCLUDE_TABLE_PATTERNS


def test_merged_exclude_appends_custom_dedup():
    """自定义追加且去重；内置已有的不重复添加。"""
    merged = merged_exclude_table_patterns([r"_tmp$", r"^adhoc", r"^dp_"])
    # 内置默认 _bak 等仍在
    assert r"_bak$" in merged
    # 自定义追加
    assert r"_tmp$" in merged
    assert r"^dp_" in merged
    # 与内置重复的自定义不重复添加
    assert merged.count(r"^adhoc") == 1


def test_catalog_with_counts_merges_builtin_and_detected():
    """内置 + 探测计数合并；探测到未内置值标注「未识别」；内置 0 条保留。"""
    items = catalog_with_counts({1: "SQL 任务"}, {1: 100, 3: 5})
    by_value = {i["value"]: i for i in items}
    assert by_value[1]["label"] == "SQL 任务"
    assert by_value[1]["known"] is True
    assert by_value[1]["count"] == 100
    assert by_value[3]["label"] == "类型 3（未识别）"
    assert by_value[3]["known"] is False
    assert by_value[3]["count"] == 5


def test_catalog_includes_builtin_with_zero_detected():
    """内置但当前无数据（0 条）仍保留，避免历史类型选项消失。"""
    items = catalog_with_counts(DP_TASK_TYPES, {})
    assert [i["value"] for i in items] == [1]
    assert items[0]["count"] == 0
    step_values = [i["value"] for i in catalog_with_counts(DP_STEP_TYPES, {})]
    assert step_values == [2, 7]


def test_validate_regex_reports_invalid():
    assert validate_regex(r"^tmp_") is None
    assert validate_regex("(") is not None


def test_count_regex_matches_stats_and_samples():
    tables = [
        "wedw_dwd.dp_out",
        "wedw_ods.visit_d",
        "wedw_dwd.tmp_x",
        "wedw_dwd.tmp_clean",
        "wedw_ods.tbl_bak",
    ]
    result = count_regex_matches(tables, [r"(^|\.)tmp_", r"_bak$"], max_samples=8)
    assert result["total"] == 5
    assert result["matched"] == 3  # tmp_x / tmp_clean / tbl_bak
    assert result["invalid_patterns"] == []
    matched = {s["table"] for s in result["samples"]}
    assert matched == {"wedw_dwd.tmp_x", "wedw_dwd.tmp_clean", "wedw_ods.tbl_bak"}


def test_count_regex_matches_reports_invalid_and_skips():
    tables = ["wedw_dwd.dp_out"]
    result = count_regex_matches(tables, [r"(^|\.)tmp_", "("], max_samples=8)
    assert result["matched"] == 0
    assert len(result["invalid_patterns"]) == 1
    assert result["invalid_patterns"][0]["pattern"] == "("
    assert "不合法" in result["invalid_patterns"][0]["error"]
