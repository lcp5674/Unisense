"""语义层 Schema 校验测试（枚举字段非法值 → 422，而非穿透到 DB 抛 500）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.semantic.schemas import MetricCreateRequest, MetricPublishRequest


def _base_payload(**overrides) -> dict:
    payload = {
        "metric_code": "fin_gmv_amount_daily",
        "name": "GMV",
        "domain": "fin",
        "type": "atomic",
        "granularity": "DAY",
        "unit": "元",
        "aggregation": "SUM",
        "time_semantics": "PERIOD",
        "freshness": "T1",
        "dw_layer": "DWS",
        "metric_tier": "T2",
        "serving_mode": "BATCH_ONLY",
        "additivity": "ADDITIVE",
        "definition_json": {"expression": "SUM(amount)"},
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("aggregation", "MEAN"),
        ("time_semantics", "FOREVER"),
        ("freshness", "WEEKLY"),
        ("dw_layer", "STAGE"),
        ("metric_tier", "T4"),
        ("serving_mode", "STREAM_ONLY"),
        ("additivity", "PARTIAL"),
        ("type", "weird"),
    ],
)
def test_enum_fields_reject_invalid_values(field, bad_value):
    with pytest.raises(ValidationError):
        MetricCreateRequest(**_base_payload(**{field: bad_value}))


def test_valid_enum_values_accepted():
    req = MetricCreateRequest(**_base_payload())
    assert req.aggregation == "SUM"
    assert req.metric_tier == "T2"


def test_publish_version_optional():
    req = MetricPublishRequest(change_reason="首次发布")
    assert req.version is None
    req2 = MetricPublishRequest(version=3, change_reason="发布指定版本")
    assert req2.version == 3


# ---- definition_json 校验：SQL 语法 + source_tables 规范化 ----

def test_definition_sql_valid_accepted():
    """合法 SQL 通过校验，并保留在 definition_json.sql。"""
    req = MetricCreateRequest(
        **_base_payload(
            definition_json={
                "sql": (
                    "SELECT SUM(amount) AS gmv FROM catalog.sales.orders "
                    "WHERE dt >= '2026-01-01'"
                )
            }
        )
    )
    assert "SELECT" in req.definition_json["sql"]


def test_definition_sql_invalid_rejected():
    """非法 SQL（语法错误）→ 422。"""
    with pytest.raises(ValidationError) as exc:
        MetricCreateRequest(
            **_base_payload(definition_json={"sql": "SELEC FROM WHERE"})
        )
    assert "SQL 语法错误" in str(exc.value)


def test_definition_sql_empty_rejected():
    """空 SQL → 422。"""
    with pytest.raises(ValidationError) as exc:
        MetricCreateRequest(**_base_payload(definition_json={"sql": "   "}))
    assert "非空字符串" in str(exc.value)


def test_definition_source_tables_normalized():
    """source_tables 规范化：去重 + 去空白 + 转字符串。"""
    req = MetricCreateRequest(
        **_base_payload(
            definition_json={
                "expression": "SUM(amount)",
                "source_tables": [" catalog.sales.orders ", "catalog.sales.orders", "  "],
            }
        )
    )
    assert req.definition_json["source_tables"] == ["catalog.sales.orders"]


def test_definition_source_tables_not_list_rejected():
    """source_tables 非数组 → 422。"""
    with pytest.raises(ValidationError) as exc:
        MetricCreateRequest(
            **_base_payload(definition_json={"source_tables": "catalog.sales.orders"})
        )
    assert "必须为数据表名数组" in str(exc.value)


def test_definition_update_sql_invalid_rejected():
    """更新请求的 definition_json 同样校验 SQL 语法（非法 SQL → 422）。"""
    from app.services.semantic.schemas import MetricUpdateRequest

    with pytest.raises(ValidationError):
        MetricUpdateRequest(
            change_reason="更新口径",
            definition_json={"sql": "SELEC FORM sales"},
        )


def test_definition_update_none_passes():
    """更新请求 definition_json 为 None 时跳过校验。"""
    from app.services.semantic.schemas import MetricUpdateRequest

    req = MetricUpdateRequest(change_reason="更新指标名称")
    assert req.definition_json is None
