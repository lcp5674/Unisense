"""日志脱敏 processor 单测。

覆盖 _redact_processor：敏感字段名黑名单、身份证/手机号/邮箱正则替换。
"""

from __future__ import annotations

from app.core.logging import _redact_processor


def test_redact_sensitive_field_names() -> None:
    event = {"password": "secret123", "token": "abc", "api_key": "k", "safe": "value"}
    out = _redact_processor(None, "info", event)
    assert out["password"] == "***REDACTED***"
    assert out["token"] == "***REDACTED***"
    assert out["api_key"] == "***REDACTED***"
    assert out["safe"] == "value"


def test_redact_id_card() -> None:
    event = {"msg": "用户身份证 110101199001011234 已登记"}
    out = _redact_processor(None, "info", event)
    assert "110101199001011234" not in out["msg"]
    assert "***ID***" in out["msg"]


def test_redact_phone() -> None:
    event = {"msg": "联系电话 13800138000"}
    out = _redact_processor(None, "info", event)
    assert "13800138000" not in out["msg"]
    assert "***PHONE***" in out["msg"]


def test_redact_email() -> None:
    event = {"msg": "邮箱 user@example.com 已注册"}
    out = _redact_processor(None, "info", event)
    assert "user@example.com" not in out["msg"]
    assert "***EMAIL***" in out["msg"]


def test_redact_leaves_normal_text() -> None:
    event = {"msg": "指标 sales_gmv_daily 已发布", "metric_code": "sales_gmv_daily"}
    out = _redact_processor(None, "info", event)
    assert out["msg"] == "指标 sales_gmv_daily 已发布"
    assert out["metric_code"] == "sales_gmv_daily"


def test_redact_non_string_values_kept() -> None:
    event = {"count": 42, "ratio": 0.5, "ok": True}
    out = _redact_processor(None, "info", event)
    assert out["count"] == 42
    assert out["ratio"] == 0.5
    assert out["ok"] is True
