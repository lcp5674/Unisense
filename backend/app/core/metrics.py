"""RED 指标采集与 Prometheus 文本导出（对齐 TD §16 / DEV_GUIDE §16）。

RED 方法：
- Rate：请求速率（``http_requests_total``）
- Errors：错误率（由 status 维度可推导 5xx 占比）
- Duration：请求时延（``http_request_duration_seconds`` summary）

为避免引入额外依赖，使用进程内计数器渲染 Prometheus exposition 文本格式。
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger

logger = get_logger("metrics")


class _MetricsStore:
    """进程内 RED 指标计数（单实例，被 MetricsMiddleware 与 /metrics 共用）。"""

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str, int], int] = defaultdict(int)
        self._latency_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._latency_count: dict[tuple[str, str], int] = defaultdict(int)

    def observe(self, method: str, path: str, status: int, duration: float) -> None:
        self._counts[(method, path, status)] += 1
        self._latency_sum[(method, path)] += duration
        self._latency_count[(method, path)] += 1

    def render_prometheus(self) -> str:
        lines: list[str] = []
        lines.append("# HELP http_requests_total RED-Rate: 按 method/path/status 累计请求数")
        lines.append("# TYPE http_requests_total counter")
        for (method, path, status), count in sorted(self._counts.items()):
            lines.append(
                f'http_requests_total{{method="{method}",path="{path}",status="{status}"}} {count}'
            )
        lines.append("# HELP http_request_duration_seconds RED-Duration: 请求时延（秒）")
        lines.append("# TYPE http_request_duration_seconds summary")
        for (method, path), total in sorted(self._latency_sum.items()):
            n = self._latency_count[(method, path)]
            lines.append(
                f'http_request_duration_seconds_sum{{method="{method}",path="{path}"}} {total:.6f}'
            )
            lines.append(
                f'http_request_duration_seconds_count{{method="{method}",path="{path}"}} {n}'
            )
        return "\n".join(lines) + "\n"


store = _MetricsStore()


class MetricsMiddleware(BaseHTTPMiddleware):
    """采集每个请求的 RED 指标，并输出结构化访问日志（含 trace_id）。"""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        start = time.perf_counter()
        response: Response = await call_next(request)
        duration = time.perf_counter() - start
        method = request.method
        path = request.url.path
        status = response.status_code
        store.observe(method, path, status, duration)
        # 访问日志：trace_id 由 TraceIdMiddleware 写入 request.state（显式带出，
        # 保证访问日志自包含、可被日志系统直接检索，不依赖 contextvars 合并）
        trace_id = getattr(request.state, "trace_id", "") or request.headers.get("X-Trace-Id", "")
        logger.info(
            "access",
            method=method,
            path=path,
            status=status,
            duration_ms=round(duration * 1000, 3),
            trace_id=trace_id,
        )
        return response


def render_metrics() -> Response:
    """Prometheus exposition 格式的指标响应。"""
    return Response(
        store.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
