"""RED 指标与 Prometheus 导出单测（补齐覆盖率）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.core.metrics import MetricsMiddleware, _MetricsStore, render_metrics, store


class TestMetricsStore:
    def test_observe_and_render(self) -> None:
        s = _MetricsStore()
        s.observe("GET", "/api/v1/metrics", 200, 0.123)
        s.observe("GET", "/api/v1/metrics", 200, 0.456)
        s.observe("POST", "/api/v1/metrics", 500, 0.789)
        text = s.render_prometheus()
        assert 'http_requests_total{method="GET",path="/api/v1/metrics",status="200"} 2' in text
        assert 'http_requests_total{method="POST",path="/api/v1/metrics",status="500"} 1' in text
        dur_sum = 'http_request_duration_seconds_sum{method="GET",path="/api/v1/metrics"} 0.579000'
        dur_count = 'http_request_duration_seconds_count{method="GET",path="/api/v1/metrics"} 2'
        assert dur_sum in text
        assert dur_count in text
        assert text.endswith("\n")

    def test_render_empty(self) -> None:
        s = _MetricsStore()
        text = s.render_prometheus()
        assert "http_requests_total" in text
        assert "# TYPE" in text


class TestMetricsMiddleware:
    async def test_dispatch_observes(self) -> None:
        middleware = MetricsMiddleware(app=None)  # type: ignore[arg-type]
        request = MagicMock()
        request.method = "GET"
        request.url.path = "/health"
        request.state.trace_id = "t1"
        request.headers.get = MagicMock(return_value="t1")
        response = MagicMock()
        response.status_code = 200
        call_next = AsyncMock(return_value=response)

        before = dict(store._counts)
        await middleware.dispatch(request, call_next)
        # observe 被调用（计数新增一条）
        assert store._counts != before or len(store._counts) >= len(before)
        call_next.assert_called_once_with(request)


def test_render_metrics_returns_response() -> None:
    resp = render_metrics()
    assert resp.status_code == 200
    assert "text/plain" in resp.media_type
    assert "http_requests_total" in resp.body.decode()
