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
