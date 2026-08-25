"""LLM 结构化输出统一解析器单测（对齐 DEV_GUIDE §8b / gateways unit）。

覆盖：代码围栏剥离、字段别名、类型强转、范围校验、None 容错。
纯函数，无外部依赖。
"""

from __future__ import annotations

from app.services.llm.parse import (
    extract_numeric_field,
    extract_str_field,
    parse_batch_description_result,
    parse_bool_result,
    parse_description_result,
    parse_json_object,
    parse_sql_measures_result,
    strip_code_fence,
)


def test_strip_code_fence_json() -> None:
    assert strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_code_fence_plain() -> None:
    # 非围栏文本原样返回
    assert strip_code_fence('{"a": 1}') == '{"a": 1}'


def test_strip_code_fence_no_lang() -> None:
    assert strip_code_fence('```\n{"a": 1}\n```') == '{"a": 1}'


def test_parse_json_object_happy() -> None:
    assert parse_json_object('{"description": "x", "confidence": 0.5}') == {
        "description": "x",
        "confidence": 0.5,
    }


def test_parse_json_object_with_fence() -> None:
    assert parse_json_object('```json\n{"k": 1}\n```') == {"k": 1}


def test_parse_json_object_invalid_returns_none() -> None:
    assert parse_json_object("not json") is None


def test_parse_json_object_non_dict_returns_none() -> None:
    assert parse_json_object("[1, 2, 3]") is None


def test_parse_json_object_empty_returns_none() -> None:
    assert parse_json_object("") is None
    assert parse_json_object("   ") is None


def test_extract_str_field_alias() -> None:
    obj = {"desc": "字段说明"}
    assert extract_str_field(obj, "description", "desc") == "字段说明"


def test_extract_str_field_missing_returns_none() -> None:
    assert extract_str_field({"other": 1}, "description") is None


def test_extract_str_field_numeric_fallback() -> None:
    # 数字也兜底为文本
    assert extract_str_field({"description": 42}, "description") == "42"


def test_extract_numeric_field_range_clip() -> None:
    obj = {"confidence": 2.0}
    # 越界返回 None
    assert extract_numeric_field(obj, "confidence", min_value=0.0, max_value=1.0) is None


def test_extract_numeric_field_string_number() -> None:
    obj = {"confidence": "0.8"}
    assert extract_numeric_field(obj, "confidence", min_value=0.0, max_value=1.0) == 0.8


def test_extract_numeric_field_missing_returns_none() -> None:
    assert extract_numeric_field({}, "confidence") is None


def test_parse_description_result_happy() -> None:
    desc, conf = parse_description_result('{"description": "用户ID", "confidence": 0.9}')
    assert desc == "用户ID"
    assert conf == 0.9


def test_parse_description_result_alias_and_fence() -> None:
    # 别名 desc + 围栏 + 置信度越界后 None
    raw = '```json\n{"desc": "表说明", "confidence": 0.7}\n```'
    desc, conf = parse_description_result(raw)
    assert desc == "表说明"
    assert conf == 0.7


def test_parse_description_result_confidence_out_of_range_none() -> None:
    # confidence=2.0 越界（>1），整体返回 None
    desc, conf = parse_description_result('{"description": "x", "confidence": 2.0}')
    assert desc is None
    assert conf is None


def test_parse_description_result_missing_field_none() -> None:
    desc, conf = parse_description_result('{"description": "x"}')
    assert desc is None
    assert conf is None


def test_parse_description_result_empty_description_none() -> None:
    desc, conf = parse_description_result('{"description": "", "confidence": 0.8}')
    assert desc is None


def test_parse_bool_result_true() -> None:
    assert parse_bool_result('{"same": true}') is True


def test_parse_bool_result_false() -> None:
    assert parse_bool_result('{"same": false}') is False


def test_parse_bool_result_string_true() -> None:
    assert parse_bool_result('{"same": "true"}') is True
    assert parse_bool_result('{"same": "False"}') is False


def test_parse_bool_result_numeric() -> None:
    assert parse_bool_result('{"same": 1}') is True
    assert parse_bool_result('{"same": 0}') is False


def test_parse_bool_result_alias() -> None:
    assert parse_bool_result('{"is_same": true}', "same", "is_same") is True


def test_parse_bool_result_none() -> None:
    assert parse_bool_result("not json") is None
    assert parse_bool_result('{"unrelated": 1}') is None


def test_parse_batch_description_result_happy() -> None:
    raw = (
        '{"descriptions": ['
        '{"column_name": "amount", "description": "订单金额", "confidence": 0.8},'
        '{"column_name": "note", "description": "备注", "confidence": 0.7}'
        "]}"
    )
    out = parse_batch_description_result(raw, ["amount", "note"])
    assert out == {"amount": ("订单金额", 0.8), "note": ("备注", 0.7)}


def test_parse_batch_description_result_order_independent() -> None:
    # LLM 返回顺序与请求清单不同，按 column_name 匹配回填（顺序性保证）
    raw = (
        '{"descriptions": ['
        '{"column_name": "note", "description": "备注", "confidence": 0.7},'
        '{"column_name": "amount", "description": "订单金额", "confidence": 0.8}'
        "]}"
    )
    out = parse_batch_description_result(raw, ["amount", "note"])
    assert out["amount"] == ("订单金额", 0.8)
    assert out["note"] == ("备注", 0.7)


def test_parse_batch_description_result_filters_unknown() -> None:
    # 模型插报请求外的字段 → 被过滤，不污染结果
    raw = (
        '{"descriptions": ['
        '{"column_name": "amount", "description": "金额", "confidence": 0.8},'
        '{"column_name": "hacker", "description": "不该出现", "confidence": 0.9}'
        "]}"
    )
    out = parse_batch_description_result(raw, ["amount"])
    assert out == {"amount": ("金额", 0.8)}


def test_parse_batch_description_result_partial_missing() -> None:
    # 模型漏报某个字段 → 该字段不返回
    raw = '{"descriptions": [{"column_name": "a", "description": "A", "confidence": 0.8}]}'
    out = parse_batch_description_result(raw, ["a", "b"])
    assert out == {"a": ("A", 0.8)}
    assert "b" not in out


def test_parse_batch_description_result_fence_and_alias() -> None:
    # 围栏 + 元素内字段别名（name/desc）容错
    raw = '```json\n{"results": [{"name": "a", "desc": "字段A", "score": "0.8"}]}\n```'
    out = parse_batch_description_result(raw, ["a"])
    assert out == {"a": ("字段A", 0.8)}


def test_parse_batch_description_result_invalid_returns_empty() -> None:
    assert parse_batch_description_result("not json", ["a"]) == {}
    assert parse_batch_description_result('{"descriptions": "not array"}', ["a"]) == {}
    assert parse_batch_description_result('{"other": 1}', ["a"]) == {}


def test_parse_batch_description_result_confidence_out_of_range_skipped() -> None:
    # 单元素置信度越界 → 该元素被跳过，其余保留
    raw = (
        '{"descriptions": ['
        '{"column_name": "a", "description": "A", "confidence": 2.0},'
        '{"column_name": "b", "description": "B", "confidence": 0.6}'
        "]}"
    )
    out = parse_batch_description_result(raw, ["a", "b"])
    assert out == {"b": ("B", 0.6)}


# ---- parse_sql_measures_result（SQL 度量提取 LLM 兜底解析）----


def test_parse_sql_measures_result_happy() -> None:
    """合法结构：column/agg 必填，alias/table/period/name 可缺省，agg 归一。"""
    raw = (
        '{"measures": ['
        '{"column": "order_cnt", "agg": "COUNT_DISTINCT", "alias": "yyf_order_cnt",'
        ' "table": "wedw_dw.doctor_yyf_his_order_detail_df", "period": "day",'
        ' "name": "日订单量"},'
        '{"column": "biz_data", "agg": "sum", "alias": "quality_control_qc_report_cnt",'
        ' "table": "ods_track_event"},'
        '{"column": "amount", "agg": "approx_count_distinct"}'
        "]}"
    )
    out = parse_sql_measures_result(raw)
    assert out is not None
    assert len(out) == 3
    assert out[0] == {
        "column": "order_cnt",
        "agg": "COUNT_DISTINCT",
        "alias": "yyf_order_cnt",
        "table": "wedw_dw.doctor_yyf_his_order_detail_df",
        "period": "day",
        "name": "日订单量",
    }
    # 小写 sum 归一到大写；approx_count_distinct → COUNT_DISTINCT
    assert out[1]["agg"] == "SUM"
    assert out[2]["agg"] == "COUNT_DISTINCT"


def test_parse_sql_measures_result_filters_invalid() -> None:
    """缺 column / agg 非法 / 重复 column → 丢弃；整体无效返回 None。"""
    raw = (
        '{"measures": ['
        '{"column": "a", "agg": "SUM"},'
        '{"column": "a", "agg": "SUM"},'  # 重复列丢弃
        '{"column": "b", "agg": "NOT_AN_AGG"},'  # 非法聚合丢弃
        '{"agg": "SUM"},'  # 缺 column 丢弃
        '{"column": "c"}'  # 缺 agg 丢弃
        "]}"
    )
    out = parse_sql_measures_result(raw)
    assert out is not None
    assert len(out) == 1
    assert out[0]["column"] == "a"


def test_parse_sql_measures_result_invalid_returns_none() -> None:
    """非 JSON / 非 measures 数组 / 空数组 / 无有效度量 → None。"""
    assert parse_sql_measures_result("not json") is None
    assert parse_sql_measures_result('{"other": 1}') is None
    assert parse_sql_measures_result('{"measures": []}') is None
    assert parse_sql_measures_result('{"measures": [{"column": "a", "agg": "BAD"}]}') is None


def test_parse_sql_measures_result_fence_and_alias_keys() -> None:
    """代码围栏剥离 + 别名 key（metric_column/aggregation/from_table）兼容。"""
    raw = (
        "```json\n"
        '{"measures": [{"metric_column": "gmv", "aggregation": "SUM",'
        ' "from_table": "dwd_order_di", "metric_name": "成交额"}]}\n'
        "```"
    )
    out = parse_sql_measures_result(raw)
    assert out is not None
    assert out[0] == {
        "column": "gmv",
        "agg": "SUM",
        "table": "dwd_order_di",
        "name": "成交额",
    }


def test_parse_sql_measures_result_period_normalized() -> None:
    """P0-2：度量提取的 period 经 normalize_period 归一化（中文别名→白名单），
    非法周期丢弃——避免污染候选编码（此前直接透传 月度/daily 致非法编码段）。"""
    raw = (
        '{"measures": ['
        '{"column": "a", "agg": "SUM", "period": "月度"},'
        '{"column": "b", "agg": "SUM", "period": "daily"},'
        '{"column": "c", "agg": "SUM", "period": "weekly"},'
        '{"column": "d", "agg": "SUM", "period": "not_a_period"}'
        "]}"
    )
    out = parse_sql_measures_result(raw)
    assert out is not None
    by_col = {m["column"]: m for m in out}
    # 中文别名 / 英文全称 → 归一化白名单值
    assert by_col["a"].get("period") == "month"
    assert by_col["b"].get("period") == "day"
    assert by_col["c"].get("period") == "week"
    # 非法周期 → 丢弃该字段（度量本身保留，缺省由上层规则层补）
    assert "period" not in by_col["d"]
