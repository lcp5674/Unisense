"""数据值脱敏纯函数测试（PII 合规增强 C-3：消费链路 hash/mask）。"""

from __future__ import annotations

from app.services.consume.masking import mask_rows, mask_value


def test_mask_value_none_passthrough() -> None:
    assert mask_value(None, "13800000000") == "13800000000"
    assert mask_value("none", "13800000000") == "13800000000"


def test_mask_value_none_value() -> None:
    assert mask_value("hash", None) is None
    assert mask_value("deny", None) is None


def test_mask_value_mask() -> None:
    assert mask_value("mask", "13800000000") == "138***00"
    # 短值整体打码
    assert mask_value("mask", "abc") == "***"


def test_mask_value_hash() -> None:
    out = mask_value("hash", "13800000000")
    assert len(out) == 16  # sha256 摘要截断 16 位 hex
    assert mask_value("hash", "13800000000") == out  # 确定性
    assert out != mask_value("hash", "13900000000")  # 不同值不同摘要


def test_mask_value_deny() -> None:
    assert mask_value("deny", "13800000000") is None


def test_mask_value_unknown_policy_failsafe() -> None:
    """未知策略 fail-safe：宁可打码不可泄露。"""
    assert mask_value("unknown", "13800000000") == "***"


def test_mask_rows_identity_when_none() -> None:
    rows = [{"phone": "13800000000", "amount": 1}]
    assert mask_rows(rows, ["phone"], "none") is rows
    assert mask_rows(None, ["phone"], "hash") is None
    assert mask_rows(rows, [], "hash") is rows


def test_mask_rows_masks_target_columns() -> None:
    rows = [{"phone": "13800000000", "name": "张三", "amount": 1}]
    out = mask_rows(rows, ["phone", "name"], "mask")
    assert out[0]["phone"] == "138***00"
    assert out[0]["name"] == "***"  # 2 字符短值整体打码
    assert out[0]["amount"] == 1
    # 原行不被修改（不可变语义）
    assert rows[0]["phone"] == "13800000000"


def test_mask_rows_missing_column_tolerated() -> None:
    rows = [{"phone": "13800000000"}]
    out = mask_rows(rows, ["phone", "not_exists"], "hash")
    assert out[0]["phone"] != "13800000000"
