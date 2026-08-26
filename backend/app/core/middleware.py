"""FastAPI 中间件。

对齐 TD §5（降级中间件/舱壁/trace_id 透传）和 DEV_GUIDE §15.2/§16.3。

中间件顺序:
    Request → TraceId → Logging → CORS → RateLimit → BodyParse
           → Auth → Authz → Validation → Handler → ErrorHandler → Response
"""

from __future__ import annotations

import asyncio
import time
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
    "ORG_DISABLED": 401,
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


# ---------------------------------------------------------------- API 限流（P1-12）
#
# 此前 main.py 注释声称有 RateLimit 中间件但从未实现注册（middleware.py:6 注释与
# 实际不符）——导出（assetmap/audit/lineage export.csv）、LLM（auto-suggest/
# suggest-rename/infer-description force=true）与 compare 均无限流，可被刷耗 LLM
# 额度 / 批量拉取全量资产。此处落地统一限流中间件。

# 限流规则：按顺序匹配第一个命中的端点类。
# - LLM 生成类端点（消耗模型额度）：最严格——60 秒窗口 20 次，防刷耗 LLM 预算
# - 导出/批量类端点（CSV 全量拉取）：60 秒窗口 60 次
_RATE_LIMIT_RULES: list[tuple[tuple[str, ...], int, int]] = [
    (
        (
            "infer-description",
            "suggest-rename",
            "suggest-rename-name",
            "auto-suggest",
            "conflicts/check",  # P1-1: 冲突预检会逐对调 LLM，同 LLM 严格档
            "parse-sql-batch",  # P1-2: 批量解析含域建议/自定义分段 LLM 兜底，同 LLM 严格档
        ),
        60,
        20,
    ),
    (("/export", "export.csv", "batch-register", "compare/matrix"), 60, 60),
]
# 通用 API 默认：60 秒窗口 600 次（防暴力刷，不干扰正常使用）
_RATE_LIMIT_DEFAULT_WINDOW = 60
_RATE_LIMIT_DEFAULT_LIMIT = 600
# 不参与限流的路径（运维探活/健康检查）。按后缀匹配以兼容 /api/v1 前缀
# （实际路由带前缀，裸名无法命中，否则健康检查/metrics 会被限流导致监控误判）。
_RATE_LIMIT_SKIP_PATHS = ("/health", "/healthz", "/metrics", "/openapi.json", "/docs")


def _rate_limit_bucket(request: Request) -> tuple[str, int, int]:
    """计算限流桶：返回 (bucket_key, window_seconds, limit)。

    bucket 键 = ``rl:{client_ip}:{path}``（同 IP 对同端点共享一个计数桶，
    不同端点独立计数，避免单一端点刷量挤占全站额度）。
    """
    path = request.url.path
    window, limit = _RATE_LIMIT_DEFAULT_WINDOW, _RATE_LIMIT_DEFAULT_LIMIT
    for substrings, win, lim in _RATE_LIMIT_RULES:
        if any(s in path for s in substrings):
            return f"rl:{_client_key(request)}:{path}", win, lim
    return f"rl:{_client_key(request)}:{path}", window, limit


def _client_key(request: Request) -> str:
    """客户端标识：优先真实客户端 IP（X-Forwarded-For 首跳），回退直连 IP。"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


async def _check_rate_redis(key: str, window: int, limit: int) -> bool:
    """Redis 固定窗口计数（跨进程一致）。Redis 不可用时由调用方降级。"""
    from app.db.redis import get_redis

    redis = get_redis()
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window)
    return count <= limit


# 进程内降级计数（Redis 不可用时的兜底，单进程语义；生产多 worker 依赖 Redis）
_inproc_rates: dict[str, tuple[float, int]] = {}
_inproc_lock = asyncio.Lock()


async def _check_rate_inproc(key: str, window: int, limit: int) -> bool:
    """进程内固定窗口计数（Redis 降级）。"""
    async with _inproc_lock:
        now = time.monotonic()
        start, count = _inproc_rates.get(key, (0.0, 0))
        if now - start >= window:
            _inproc_rates[key] = (now, 1)
            return True
        if count >= limit:
            return False
        _inproc_rates[key] = (start, count + 1)
        return True


class RateLimitMiddleware(BaseHTTPMiddleware):
    """API 限流中间件（P1-12）。

    按 ``IP + 端点`` 固定窗口计数：Redis 计数（多 worker 一致），Redis 不可用
    降级为进程内计数。LLM 生成类端点最严格（防刷耗模型额度），导出/批量次之，
    通用 API 宽松。超限返回 429 + ``Retry-After``（对齐 ErrorHandler 信封，
    前端 ``request()`` 统一解析 ``err.message`` 展示中文提示）。
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if request.method == "OPTIONS" or path in _RATE_LIMIT_SKIP_PATHS or any(
            path.endswith(p) for p in _RATE_LIMIT_SKIP_PATHS
        ):
            return await call_next(request)
        key, window, limit = _rate_limit_bucket(request)
        try:
            allowed = await _check_rate_redis(key, window, limit)
        except Exception:
            allowed = await _check_rate_inproc(key, window, limit)
        if not allowed:
            trace_id = getattr(request.state, "trace_id", "") or request.headers.get(
                "X-Trace-Id", ""
            )
            logger.warning(
                "rate_limited",
                path=request.url.path,
                client=_client_key(request),
                window=window,
                limit=limit,
            )
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(window)},
                content={
                    "code": "RATE_LIMITED",
                    "message": "请求过于频繁，请稍后重试",
                    "trace_id": trace_id,
                    "detail": {"window_seconds": window, "limit": limit},
                },
            )
        return await call_next(request)
