"""数据值脱敏纯函数（PII 合规增强 C-3：消费链路 hash/mask）。

与 ``governance/policy.py masking_for``（敏感级 → 策略）配合：``masking_for``
决定"用哪种策略"，本模块决定"具体怎么处理一个值"。

策略语义：
- ``none``：原样返回（不脱敏）。
- ``mask``：保留前 3 / 后 2 字符，中间 ``***``（如 13800000000 → 138****00）。
- ``hash``：SHA-256 摘要前 16 位（不可逆，匿名化）。
- ``deny``：返回 None（调用方须在查询层拒绝，此处兜底置空）。
"""

from __future__ import annotations

import hashlib
from typing import Any

#: hash 摘要截断长度（16 位 hex = 64 bit，足够匿名化且不泄露原文）
_HASH_TRUNC = 16
#: mask 保留的前/后字符数
_MASK_HEAD = 3
_MASK_TAIL = 2
_MASK_GLUE = "***"


def mask_value(policy: str | None, value: Any) -> Any:
    """按策略对单个值脱敏。

    Args:
        policy: none / mask / hash / deny（None 视为 none）。
        value: 原始值（None 原样返回）。

    Returns:
        脱敏后的值；``deny`` 返回 None。
    """
    if value is None:
        return None
    p = (policy or "none").strip().lower()
    if p == "none":
        return value
    if p == "deny":
        return None
    if p == "hash":
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:_HASH_TRUNC]
    if p == "mask":
        s = str(value)
        if len(s) <= _MASK_HEAD + _MASK_TAIL:
            return _MASK_GLUE
        return s[:_MASK_HEAD] + _MASK_GLUE + s[-_MASK_TAIL:]
    # 未知策略 fail-safe：宁可打码不可泄露
    return _MASK_GLUE


def mask_rows(
    rows: list[dict[str, Any]] | None, columns: list[str], policy: str
) -> list[dict[str, Any]] | None:
    """对结果行中指定列的原始值批量脱敏（就地复制，不修改入参）。

    Args:
        rows: 查询结果行（每行 dict）。
        columns: 需脱敏的列名集合。
        policy: 脱敏策略（none 时直接返回原样）。

    Returns:
        脱敏后的新行列表；rows 为 None 或 columns 为空返回原引用。
    """
    if not rows or not columns or (policy or "none") == "none":
        return rows
    target = {c for c in columns if c}
    if not target:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        new_row = dict(row)
        for col in target:
            if col in new_row:
                new_row[col] = mask_value(policy, new_row[col])
        out.append(new_row)
    return out
