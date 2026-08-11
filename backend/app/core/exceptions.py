"""异常分层体系。

对齐 DEV_GUIDE §15.1 和 TD §5.4。

异常层级::

    UnisenseError (基类)
    ├── BusinessError (4xx)
    │   ├── ValidationError   (422)
    │   ├── AuthError         (403)
    │   ├── NotFoundError     (404)
    │   └── ConflictError     (409)
    └── SystemError (5xx)
        └── ExternalDependencyError (500, retryable)
"""

from __future__ import annotations

from typing import Any


class UnisenseError(Exception):
    """所有业务异常基类。

    Attributes:
        error_code: 错误码（来自 TD §5.4 枚举）。
        http_status: HTTP 状态码。
        message: 用户可读的错误消息。
        trace_id: 链路追踪 ID。
        ctx: 附加上下文字典。
    """

    error_code: str = "INTERNAL_ERROR"
    http_status: int = 500

    def __init__(
        self,
        message: str = "",
        *,
        error_code: str | None = None,
        trace_id: str = "",
        ctx: dict[str, Any] | None = None,
    ) -> None:
        """初始化异常。

        Args:
            message: 用户可读的错误消息。
            error_code: 业务错误码（覆盖类默认；应取自 error_codes.ErrorCode）。
            trace_id: 链路追踪 ID（由中间件注入）。
            ctx: 附加上下文字典。
        """
        super().__init__(message)
        self.message = message
        self.error_code = error_code or type(self).error_code
        self.trace_id = trace_id
        self.ctx: dict[str, Any] = ctx or {}


class BusinessError(UnisenseError):
    """业务逻辑错误（4xx），可预期，须给用户友好提示。"""

    error_code: str = "BUSINESS_ERROR"
    http_status: int = 400


class ValidationError(BusinessError):
    """入参校验失败。"""

    error_code: str = "VALIDATION_ERROR"
    http_status: int = 422


class AuthError(BusinessError):
    """认证/授权失败。"""

    error_code: str = "FORBIDDEN"
    http_status: int = 403


class NotFoundError(BusinessError):
    """资源不存在。"""

    error_code: str = "NOT_FOUND"
    http_status: int = 404


class ConflictError(BusinessError):
    """资源冲突（并发修改/重名等）。"""

    error_code: str = "CONFLICT"
    http_status: int = 409


class SystemError(UnisenseError):  # noqa: A001
    """系统内部错误（5xx），不可预期，须告警。"""

    error_code: str = "INTERNAL_ERROR"
    http_status: int = 500


class ExternalDependencyError(SystemError):
    """外部依赖异常（DB/Redis/ES/LLM），含 retry 标记。

    Attributes:
        retryable: 是否可重试。
    """

    error_code: str = "EXTERNAL_DEPENDENCY_ERROR"
    http_status: int = 503

    def __init__(
        self,
        message: str = "",
        *,
        trace_id: str = "",
        ctx: dict[str, Any] | None = None,
        retryable: bool = True,
    ) -> None:
        """初始化外部依赖异常。

        Args:
            message: 错误消息。
            trace_id: 链路追踪 ID。
            ctx: 附加上下文。
            retryable: 是否可重试。
        """
        super().__init__(message, trace_id=trace_id, ctx=ctx)
        self.retryable = retryable
