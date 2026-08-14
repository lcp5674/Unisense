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
    "AUTH_REFRESH_EXPIRED": 401,
    "AUTH_REFRESH_REVOKED": 401,
    "AUTH_TOKEN_REVOKED": 401,
    "AUTH_INVALID_CREDENTIALS": 401,
    "PASSWORD_INCORRECT": 401,
    "AUTH_APIKEY_MISSING": 401,
    "AUTH_APIKEY_INVALID": 401,
    "FORBIDDEN": 403,
    "FORBIDDEN_DOMAIN": 403,
    "FORBIDDEN_METRIC": 403,
    "FORBIDDEN_DIMENSION": 403,
    "FORBIDDEN_PII": 403,
    "FORBIDDEN_DEPRECATED": 403,
    "RATE_LIMITED": 429,
    "AUTH_RATE_LIMITED": 429,
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
            headers: dict[str, str] = {}
            retry_after = exc.ctx.get("retry_after")
            if retry_after is not None:
                headers["Retry-After"] = str(int(retry_after))
            return JSONResponse(
                status_code=_ERROR_CODE_HTTP_STATUS.get(exc.error_code, exc.http_status),
                content={
                    "code": exc.error_code,
                    "message": exc.message,
                    "trace_id": trace_id,
                    "detail": exc.ctx or None,
                },
                headers=headers,
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


_MAX_BODY_SIZE = 10 * 1024 * 1024  # 10MB


class RequestBodySizeMiddleware(BaseHTTPMiddleware):
    """请求体大小限制中间件（SEC-11: 超过10MB返回413）。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method in ("POST", "PUT", "PATCH"):
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > _MAX_BODY_SIZE:
                trace_id = getattr(request.state, "trace_id", "")
                return JSONResponse(
                    status_code=413,
                    content={
                        "code": "REQUEST_TOO_LARGE",
                        "message": "请求体超过10MB限制",
                        "trace_id": trace_id,
                        "detail": None,
                    },
                )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """安全响应头中间件（对齐 DEV_GUIDE §13.4）。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers[key] = value
        return response


# 降级舱壁：命中依赖降级业务码（TD §5.2.5 ⑤）时统一标注 degraded，使上游区分
# 「依赖降级（可重试 503）」与「系统错误（500）」，且相邻能力不受单依赖故障级联。
_DEGRADED_DEPENDENCY_CODES: frozenset[str] = frozenset(
    {
        "DEPENDENCY_DEGRADED_ENGINE",
        "DEPENDENCY_DEGRADED_GRAPH",
        "DEPENDENCY_DEGRADED_LLM",
    }
)

# 依赖降级业务码 → 用户态降级文案（TD §5.2 降级矩阵：OLAP 查询返 503；
# Neo4j 血缘标 stale / LLM 取消 AI 预填）。
_DEGRADED_MESSAGES: dict[str, str] = {
    "DEPENDENCY_DEGRADED_ENGINE": "查询引擎暂不可用，请稍后重试",
    "DEPENDENCY_DEGRADED_GRAPH": "血缘图暂不可用，指标列表仍可浏览",
    "DEPENDENCY_DEGRADED_LLM": "AI 暂不可用，请手动填写",
}


class DegradationMiddleware(BaseHTTPMiddleware):
    """降级舱壁中间件（TD §5.2.5 ⑤ 舱壁隔离模式）。

    仅拦截依赖降级异常（``DEPENDENCY_DEGRADED_*``）：在响应体附加 ``degraded=true`` 与
    降级文案、响应头 ``X-Degraded: true`` 与 ``Retry-After``，使前端/上游能区分依赖降级与
    系统错误；单一依赖故障不级联为通用 5xx，相邻能力不受影响（舱壁）。

    非降级异常（含 500/4xx）原样上抛，由外层 ``ErrorHandlerMiddleware`` 统一处理，
    保证异常分层与错误码语义不被本中间件吞掉。
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """捕获依赖降级异常并标注降级响应；其余异常上抛。

        Args:
            request: 请求对象。
            call_next: 下一个中间件。

        Returns:
            降级标注后的 503 响应；或透传正常响应；非降级异常向上抛出。
        """
        trace_id = getattr(request.state, "trace_id", "") or request.headers.get("X-Trace-Id", "")
        try:
            return await call_next(request)
        except UnisenseError as exc:
            if exc.error_code not in _DEGRADED_DEPENDENCY_CODES:
                raise  # 非降级异常：交由外层 ErrorHandlerMiddleware 统一处理
            # 依赖降级：标注 degraded，附加降级文案与 Retry-After（上游按 error_code 路由退避）
            headers: dict[str, str] = {"X-Degraded": "true"}
            retry_after = exc.ctx.get("retry_after") if isinstance(exc.ctx, dict) else None
            if retry_after is not None:
                headers["Retry-After"] = str(int(retry_after))
            message = _DEGRADED_MESSAGES.get(exc.error_code, "依赖暂不可用，请稍后重试")
            return JSONResponse(
                status_code=503,
                content={
                    "code": exc.error_code,
                    "message": message,
                    "trace_id": trace_id,
                    "detail": exc.ctx or None,
                    "degraded": True,
                    "degradation_message": message,
                },
                headers=headers,
            )
