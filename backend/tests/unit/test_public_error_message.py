"""异常脱敏助手单测（P0-3：批量端点异常信息脱敏）。

覆盖 public_error_message 三类分支：
- UnisenseError（业务异常）：透传用户可读消息
- FastAPI HTTPException：透传 detail
- 未知异常（DB/连接/底层库）：泛化为通用提示，不泄漏内部细节
"""

from __future__ import annotations

from fastapi import HTTPException

from app.core.exceptions import (
    BusinessError,
    ConflictError,
    NotFoundError,
    public_error_message,
)


def test_business_error_message_passthrough() -> None:
    """业务异常透传用户可读消息（批量失败明细需要具体原因）。"""
    err = BusinessError("指标已被废弃，无法提交", error_code="INVALID_TRANSITION")
    assert public_error_message(err) == "指标已被废弃，无法提交"


def test_conflict_error_message_passthrough() -> None:
    """ConflictError 透传（前端依赖该消息提示版本冲突）。"""
    err = ConflictError("版本已变更，请刷新后重试")
    assert public_error_message(err) == "版本已变更，请刷新后重试"


def test_not_found_error_message_passthrough() -> None:
    err = NotFoundError("指标不存在: nope")
    assert public_error_message(err) == "指标不存在: nope"


def test_http_exception_detail_passthrough() -> None:
    """FastAPI HTTPException 透传 detail。"""
    err = HTTPException(status_code=403, detail="无权限执行该操作")
    assert public_error_message(err) == "无权限执行该操作"


def test_unknown_exception_sanitized() -> None:
    """未知异常（含连接串/文件路径等内部细节）→ 泛化为通用提示。"""
    err = RuntimeError(
        "Connection to mysql://root:secret@db.internal:3306/universe failed "
        "(file:///etc/app/config.yaml)"
    )
    msg = public_error_message(err)
    assert "操作失败" in msg
    assert "root:secret" not in msg
    assert "db.internal" not in msg
    assert "/etc/app/config.yaml" not in msg
