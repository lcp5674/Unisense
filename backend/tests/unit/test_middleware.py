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
    _DOCS_CSP,
    _ERROR_CODE_HTTP_STATUS,
    _RATE_LIMIT_DEFAULT_LIMIT,
    _RATE_LIMIT_DEFAULT_WINDOW,
    _SECURITY_HEADERS,
    DegradationMiddleware,
    ErrorHandlerMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    TraceIdMiddleware,
)


def _request(headers: list[tuple[str, str]] | None = None, path: str = "/api/v1/health") -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or [])]
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
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

    async def test_docs_path_uses_localized_csp(self) -> None:
        """/docs 页面使用本地化 CSP（资源同源、script-src 'self'），其余安全头保持。"""
        mw = SecurityHeadersMiddleware(lambda *a, **k: None)
        request = _request(path="/docs")

        async def call_next(req: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        response = await mw.dispatch(request, call_next)
        assert response.headers["Content-Security-Policy"] == _DOCS_CSP
        # 其余安全头不受影响
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        # 本地化 CSP 不含 'unsafe-inline' 的 script（外置 init 脚本），含 style 注入放行
        assert "script-src 'self'" in _DOCS_CSP
        assert "'unsafe-inline'" not in _DOCS_CSP.split("script-src")[1].split(";")[0]
        assert "style-src 'self' 'unsafe-inline'" in _DOCS_CSP
        # 文档页面禁止缓存（接口变更后始终拉取最新）
        assert response.headers["Cache-Control"] == "no-store"

    async def test_redoc_path_uses_localized_csp(self) -> None:
        mw = SecurityHeadersMiddleware(lambda *a, **k: None)
        request = _request(path="/redoc")

        async def call_next(req: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        response = await mw.dispatch(request, call_next)
        assert response.headers["Content-Security-Policy"] == _DOCS_CSP
        assert response.headers["Cache-Control"] == "no-store"

    async def test_openapi_json_not_cached(self) -> None:
        """/openapi.json 是动态生成的接口清单，禁止浏览器缓存，保证 docs 同步最新路由。"""
        mw = SecurityHeadersMiddleware(lambda *a, **k: None)
        request = _request(path="/openapi.json")

        async def call_next(req: Request) -> PlainTextResponse:
            return PlainTextResponse('{"openapi": "3.1.0"}')

        response = await mw.dispatch(request, call_next)
        assert response.headers["Cache-Control"] == "no-store"
        # openapi.json 属 API 响应，保持全局严格 CSP（非文档页面 CSP）
        assert response.headers["Content-Security-Policy"] == _SECURITY_HEADERS[
            "Content-Security-Policy"
        ]

    async def test_api_json_not_cached_by_docs_rule(self) -> None:
        """普通 API 响应不受文档 no-store 规则影响（无 Cache-Control 覆盖）。"""
        mw = SecurityHeadersMiddleware(lambda *a, **k: None)
        request = _request(path="/api/v1/metrics")

        async def call_next(req: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        response = await mw.dispatch(request, call_next)
        assert "Cache-Control" not in response.headers


class _DegradedEngineError(UnisenseError):
    """命中降级舱壁的依赖降级异常（DEPENDENCY_DEGRADED_ENGINE，503）。"""

    error_code = "DEPENDENCY_DEGRADED_ENGINE"
    http_status = 503


class _DegradedGraphError(UnisenseError):
    error_code = "DEPENDENCY_DEGRADED_GRAPH"
    http_status = 503


class _GenericUnisenseError(UnisenseError):
    """非降级异常，应上抛交由 ErrorHandlerMiddleware 处理。"""

    error_code = "INTERNAL_ERROR"
    http_status = 500


class TestDegradationMiddleware:
    async def test_annotates_engine_degradation_as_503_with_degraded_flag(self) -> None:
        mw = DegradationMiddleware(lambda *a, **k: None)
        request = _request()

        async def call_next(req: Request) -> PlainTextResponse:
            raise _DegradedEngineError(
                "查询引擎不可用", ctx={"retry_after": 30, "accept_stale": True}
            )

        response = await mw.dispatch(request, call_next)
        assert response.status_code == 503
        assert response.headers.get("X-Degraded") == "true"
        assert response.headers.get("Retry-After") == "30"
        body = json.loads(response.body)
        assert body["code"] == "DEPENDENCY_DEGRADED_ENGINE"
        assert body["degraded"] is True
        assert body["degradation_message"] == "查询引擎暂不可用，请稍后重试"
        # ctx 透传（如 accept_stale），不丢失原上下文
        assert body["detail"] == {"retry_after": 30, "accept_stale": True}

    async def test_maps_each_dependency_to_its_degradation_message(self) -> None:
        mw = DegradationMiddleware(lambda *a, **k: None)
        request = _request()

        async def call_next(req: Request) -> PlainTextResponse:
            raise _DegradedGraphError("血缘图不可用")

        response = await mw.dispatch(request, call_next)
        assert response.status_code == 503
        body = json.loads(response.body)
        assert body["degradation_message"] == "血缘图暂不可用，指标列表仍可浏览"

    async def test_non_degraded_error_reraised_for_outer_handler(self) -> None:
        # 非降级异常必须上抛（不吞），由外层 ErrorHandlerMiddleware 统一处理
        mw = DegradationMiddleware(lambda *a, **k: None)
        request = _request()

        async def call_next(req: Request) -> PlainTextResponse:
            raise _GenericUnisenseError("内部错误")

        try:
            await mw.dispatch(request, call_next)
            raise AssertionError("expected _GenericUnisenseError to propagate")
        except _GenericUnisenseError:
            pass

    async def test_success_response_passthrough_unchanged(self) -> None:
        mw = DegradationMiddleware(lambda *a, **k: None)
        request = _request()

        async def call_next(req: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        response = await mw.dispatch(request, call_next)
        assert response.status_code == 200
        assert response.body == b"ok"


class TestRateLimitMiddleware:
    """API 限流中间件（P1-12）单测。

    不依赖真实 Redis/全局计数状态：通过 monkeypatch 注入计数器返回值，断言
    超限返回 429 信封、未超限透传、探活路径跳过、规则桶匹配正确。
    """

    def _req_with_path(self, path: str) -> Request:
        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "server": ("test", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
        }
        return Request(scope)

    async def test_exceeded_returns_429_envelope(self, monkeypatch) -> None:
        import app.core.middleware as mw_mod

        # Redis 不可用 → 降级进程内；进程内计数返回 False（超限）
        async def _redis_raise(*a, **k):
            raise RuntimeError("redis down")

        async def _inproc_false(*a, **k):
            return False

        monkeypatch.setattr(mw_mod, "_check_rate_redis", _redis_raise)
        monkeypatch.setattr(mw_mod, "_check_rate_inproc", _inproc_false)

        mw = RateLimitMiddleware(lambda *a, **k: None)
        request = self._req_with_path("/api/v1/metric-definitions/compare/matrix")

        async def call_next(req: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        response = await mw.dispatch(request, call_next)
        assert response.status_code == 429
        body = json.loads(response.body)
        assert body["code"] == "RATE_LIMITED"
        assert body["message"] == "请求过于频繁，请稍后重试"
        assert response.headers.get("Retry-After") is not None

    async def test_within_limit_passes_through(self, monkeypatch) -> None:
        import app.core.middleware as mw_mod

        async def _redis_raise(*a, **k):
            raise RuntimeError("redis down")

        async def _inproc_true(*a, **k):
            return True

        monkeypatch.setattr(mw_mod, "_check_rate_redis", _redis_raise)
        monkeypatch.setattr(mw_mod, "_check_rate_inproc", _inproc_true)

        mw = RateLimitMiddleware(lambda *a, **k: None)
        request = self._req_with_path("/api/v1/metric-definitions/compare/matrix")

        async def call_next(req: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        response = await mw.dispatch(request, call_next)
        assert response.status_code == 200
        assert response.body == b"ok"

    async def test_health_path_skips_limiting(self, monkeypatch) -> None:
        import app.core.middleware as mw_mod

        # 即便计数器返回 False，探活路径也应跳过限流透传
        async def _inproc_false(*a, **k):
            return False

        monkeypatch.setattr(mw_mod, "_check_rate_inproc", _inproc_false)

        mw = RateLimitMiddleware(lambda *a, **k: None)
        request = self._req_with_path("/api/v1/health")

        async def call_next(req: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        response = await mw.dispatch(request, call_next)
        assert response.status_code == 200

    def test_bucket_rule_matching(self) -> None:
        from app.core.middleware import _rate_limit_bucket

        # LLM 生成类端点：60s 窗口 20 次
        _, win, lim = _rate_limit_bucket(
            self._req_with_path("/api/v1/metric-definitions/infer-description")
        )
        assert (win, lim) == (60, 20)
        # 导出/批量/compare：60s 窗口 60 次
        _, win, lim = _rate_limit_bucket(
            self._req_with_path("/api/v1/metric-definitions/compare/matrix")
        )
        assert (win, lim) == (60, 60)
        # 通用 API 默认
        _, win, lim = _rate_limit_bucket(self._req_with_path("/api/v1/metric-definitions/"))
        assert (win, lim) == (_RATE_LIMIT_DEFAULT_WINDOW, _RATE_LIMIT_DEFAULT_LIMIT)

    def test_client_key_untrusted_proxy_ignores_xff(self, monkeypatch) -> None:
        """S5（审查修复）：直连 IP 不在 trusted_proxies 时，忽略 X-Forwarded-For——
        攻击者伪造 XFF 无法绕过按 IP 限流。"""
        from app.core.config import Settings
        from app.core.middleware import _client_key

        fake_settings = Settings(
            _env_file=None,
            env="local",
            db_url="mysql+pymysql://u:p@localhost:3306/db",
            jwt_secret="x" * 40,
        )
        monkeypatch.setattr("app.core.config.settings", fake_settings)
        req = _request([("X-Forwarded-For", "1.2.3.4")])
        assert _client_key(req) != "1.2.3.4"  # 直连 IP（127.0.0.1），非伪造 XFF

    def test_client_key_trusted_proxy_uses_xff(self, monkeypatch) -> None:
        """S5：直连 IP 属于 trusted_proxies 时，才信任 X-Forwarded-For 首跳。"""
        from app.core.config import Settings
        from app.core.middleware import _client_key

        fake_settings = Settings(
            _env_file=None,
            env="local",
            db_url="mysql+pymysql://u:p@localhost:3306/db",
            jwt_secret="x" * 40,
            trusted_proxies="127.0.0.1",
        )
        monkeypatch.setattr("app.core.config.settings", fake_settings)
        req = _request([("X-Forwarded-For", "1.2.3.4")])
        assert _client_key(req) == "1.2.3.4"
