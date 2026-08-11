"""FastAPI 中间件。

对齐 TD §5（降级中间件/舱壁/trace_id 透传）和 DEV_GUIDE §15.2/§16.3。

中间件顺序:
    Request → TraceId → Logging → CORS → RateLimit → BodyParse
           → Auth → Authz → Validation → Handler → ErrorHandler → Response
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.exceptions import UnisenseError

logger = structlog.get_logger("unisense.middleware")

# 安全响应头（对齐 DEV_GUIDE §13.4）
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": "default-src 'self'",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}

# error_code → HTTP 状态码覆盖。
# 鉴权失败（令牌缺失/过期/无效、凭据错误）统一为 401；各类 FORBIDDEN 为 403。
# 其余错误回落到异常类自身的 http_status（如 404/409/422）。
_ERROR_CODE_HTTP_STATUS: dict[str, int] = {
    "AUTH_TOKEN_MISSING": 401,
    "AUTH_TOKEN_EXPIRED": 401,
    "AUTH_TOKEN_INVALID": 401,
    "AUTH_INVALID_CREDENTIALS": 401,
    "AUTH_APIKEY_MISSING": 401,
    "AUTH_APIKEY_INVALID": 401,
    "FORBIDDEN": 403,
    "FORBIDDEN_DOMAIN": 403,
    "FORBIDDEN_METRIC": 403,
    "FORBIDDEN_DIMENSION": 403,
    "FORBIDDEN_PII": 403,
    "FORBIDDEN_DEPRECATED": 403,
    "RATE_LIMITED": 429,
    "DEPENDENCY_DEGRADED_ENGINE": 503,
}


class TraceIdMiddleware(BaseHTTPMiddleware):
    """为每个请求生成/透传 trace_id。

    从 ``X-Trace-Id`` header 读取或生成 UUID，注入 contextvars 和响应 header。
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """注入 trace_id。

        Args:
            request: 请求对象。
            call_next: 下一个中间件。

        Returns:
            响应对象。
        """
        trace_id = request.headers.get("X-Trace-Id", str(uuid.uuid4()))
        request.state.trace_id = trace_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            trace_id=trace_id,
            method=request.method,
            path=request.url.path,
        )
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response


class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    """全局异常处理中间件。

    捕获 ``UnisenseError`` 返回统一格式；捕获 ``Exception`` 返回 500。
    统一响应格式: ``{code, message, trace_id, detail}``
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """捕获异常并返回统一错误响应。

        Args:
            request: 请求对象。
            call_next: 下一个中间件。

        Returns:
            响应对象。
        """
        trace_id = getattr(request.state, "trace_id", "") or request.headers.get("X-Trace-Id", "")
        try:
            return await call_next(request)
        except UnisenseError as exc:
            logger.warning(
                "business_error",
                error_code=exc.error_code,
                message=exc.message,
                trace_id=trace_id,
                ctx=exc.ctx,
            )
            return JSONResponse(
                status_code=_ERROR_CODE_HTTP_STATUS.get(exc.error_code, exc.http_status),
                content={
                    "code": exc.error_code,
                    "message": exc.message,
                    "trace_id": trace_id,
                    "detail": exc.ctx or None,
                },
            )
        except Exception as exc:
            logger.error(
                "unhandled_error",
                error=str(exc),
                trace_id=trace_id,
                exc_info=True,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "code": "INTERNAL_ERROR",
                    "message": "内部错误，请联系管理员（附 trace_id）",
                    "trace_id": trace_id,
                    "detail": None,
                },
            )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """安全响应头中间件（对齐 DEV_GUIDE §13.4）。"""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers[key] = value
        return response
