"""统一 API 响应信封（对齐 DEV_GUIDE §8 与错误处理器保持同构）。

成功响应结构：``{code, message, data, trace_id}``
错误响应结构：``{code, message, trace_id, detail}``（见 ErrorHandlerMiddleware）
trace_id 由 TraceIdMiddleware 写入 ``request.state.trace_id``。
"""

from __future__ import annotations

from typing import Generic, TypeVar

from fastapi import Request
from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一成功响应信封。"""

    code: str = Field(default="OK", description="业务码")
    message: str = Field(default="success", description="提示信息")
    data: T | None = Field(default=None, description="业务数据")
    trace_id: str | None = Field(default=None, description="链路追踪 ID")


def get_trace_id(request: Request) -> str:
    """从请求上下文读取链路追踪 ID（由 TraceIdMiddleware 注入 state）。"""
    trace_id = getattr(request.state, "trace_id", None)
    return trace_id if isinstance(trace_id, str) else ""


def ok(
    data: T | None = None,
    *,
    code: str = "OK",
    message: str = "success",
    trace_id: str | None = None,
) -> ApiResponse[T]:
    """构造统一成功响应。"""
    return ApiResponse(code=code, message=message, data=data, trace_id=trace_id)
