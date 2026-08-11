"""语义层 Schema 校验测试（枚举字段非法值 → 422，而非穿透到 DB 抛 500）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.semantic.schemas import MetricCreateRequest, MetricPublishRequest


def _base_payload(**overrides) -> dict:
    payload = {
        "metric_code": "fin_gmv_daily",
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
