"""中间件单测（app/core/middleware.py）。

覆盖：
- TraceIdMiddleware：无 header 时生成 UUID、透传 X-Trace-Id、绑定 contextvars。
- ErrorHandlerMiddleware：UnisenseError 统一响应（error_code→HTTP 状态映射、Retry-After）；
  未知 Exception 回落 500 INTERNAL_ERROR。
- SecurityHeadersMiddleware：六项安全响应头全部注入。
"""

from __future__ import annotations

import json
import uuid

import structlog
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from app.core.exceptions import UnisenseError
from app.core.middleware import (
    _ERROR_CODE_HTTP_STATUS,
    _SECURITY_HEADERS,
    ErrorHandlerMiddleware,
    SecurityHeadersMiddleware,
    TraceIdMiddleware,
)


def _request(headers: list[tuple[str, str]] | None = None) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or [])]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/health",
        "raw_path": b"/api/v1/health",
        "query_string": b"",
        "headers": raw_headers,
        "server": ("test", 80),
        "client": ("127.0.0.1", 1234),
        "scheme": "http",
    }
    return Request(scope)


class TestTraceIdMiddleware:
    async def test_generates_trace_id_and_echoes_back(self) -> None:
        mw = TraceIdMiddleware(lambda *a, **k: None)
        request = _request()

        async def call_next(req: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        response = await mw.dispatch(request, call_next)
        trace_id = response.headers.get("X-Trace-Id")
        assert trace_id
        # 合法 UUID
        uuid.UUID(trace_id)
        assert request.state.trace_id == trace_id

        # contextvars 已绑定
        ctx = structlog.contextvars.get_contextvars()
        assert ctx["trace_id"] == trace_id
        assert ctx["method"] == "GET"
        assert ctx["path"] == "/api/v1/health"

    async def test_passthrough_existing_trace_id(self) -> None:
        mw = TraceIdMiddleware(lambda *a, **k: None)
        request = _request(headers=[("X-Trace-Id", "trace-abc-123")])

        async def call_next(req: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        response = await mw.dispatch(request, call_next)
        assert response.headers["X-Trace-Id"] == "trace-abc-123"
        assert request.state.trace_id == "trace-abc-123"


class _MappedUnisenseError(UnisenseError):
    """error_code 命中 _ERROR_CODE_HTTP_STATUS 映射的异常（如 401/429）。"""

    error_code = "AUTH_INVALID_CREDENTIALS"
    http_status = 400


class _UnmappedUnisenseError(UnisenseError):
    """error_code 不在映射表中，回落到类自身 http_status。"""

    error_code = "NOT_FOUND"
    http_status = 404


class _RateLimitedUnisenseError(UnisenseError):
    error_code = "RATE_LIMITED"
    http_status = 429


class TestErrorHandlerMiddleware:
    async def test_unisense_error_unified_response(self) -> None:
        mw = ErrorHandlerMiddleware(lambda *a, **k: None)
        request = _request()

        async def call_next(req: Request) -> PlainTextResponse:
            raise _MappedUnisenseError("凭证无效")

        response = await mw.dispatch(request, call_next)
        assert response.status_code == _ERROR_CODE_HTTP_STATUS["AUTH_INVALID_CREDENTIALS"] == 401
        body = json.loads(response.body)
        assert body["code"] == "AUTH_INVALID_CREDENTIALS"
        assert body["message"] == "凭证无效"
        assert "trace_id" in body

    async def test_unisense_error_unmapped_code_falls_back_to_http_status(self) -> None:
        mw = ErrorHandlerMiddleware(lambda *a, **k: None)
        request = _request()

        async def call_next(req: Request) -> PlainTextResponse:
            raise _UnmappedUnisenseError("资源不存在")

        response = await mw.dispatch(request, call_next)
        assert response.status_code == 404
        body = json.loads(response.body)
        assert body["code"] == "NOT_FOUND"

    async def test_retry_after_header_from_ctx(self) -> None:
        mw = ErrorHandlerMiddleware(lambda *a, **k: None)
        request = _request()

        async def call_next(req: Request) -> PlainTextResponse:
            raise _RateLimitedUnisenseError("请求过于频繁", ctx={"retry_after": 60})

        response = await mw.dispatch(request, call_next)
        assert response.status_code == 429
        assert response.headers.get("Retry-After") == "60"

    async def test_generic_exception_returns_500(self) -> None:
        mw = ErrorHandlerMiddleware(lambda *a, **k: None)
        request = _request()

        async def call_next(req: Request) -> PlainTextResponse:
            raise RuntimeError("boom")

        response = await mw.dispatch(request, call_next)
        assert response.status_code == 500
        body = json.loads(response.body)
        assert body["code"] == "INTERNAL_ERROR"
        assert "trace_id" in body

    async def test_success_passthrough_unchanged(self) -> None:
        mw = ErrorHandlerMiddleware(lambda *a, **k: None)
        request = _request()

        async def call_next(req: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        response = await mw.dispatch(request, call_next)
        assert response.status_code == 200
        assert response.body == b"ok"


class TestSecurityHeadersMiddleware:
    async def test_all_security_headers_set(self) -> None:
        mw = SecurityHeadersMiddleware(lambda *a, **k: None)
        request = _request()

        async def call_next(req: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        response = await mw.dispatch(request, call_next)
        for key, value in _SECURITY_HEADERS.items():
            assert response.headers[key] == value, key
