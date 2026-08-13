"""RED 指标采集与 Prometheus 文本导出（对齐 TD §16 / DEV_GUIDE §16）。

RED 方法：
- Rate：请求速率（``http_requests_total``）
- Errors：错误率（由 status 维度可推导 5xx 占比）
- Duration：请求时延（``http_request_duration_seconds`` summary）

为避免引入额外依赖，使用进程内计数器渲染 Prometheus exposition 文本格式。
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger

logger = get_logger("metrics")

_UUID_PATTERN = re.compile(
    r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_NUM_ID_PATTERN = re.compile(r"/\d+(?=/|$)")


def _normalize_path(path: str) -> str:
    path = _UUID_PATTERN.sub("/{id}", path)
    path = _NUM_ID_PATTERN.sub("/{id}", path)
    return path


class _MetricsStore:
    """进程内 RED 指标计数（单实例，被 MetricsMiddleware 与 /metrics 共用）。"""

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str, int], int] = defaultdict(int)
        self._latency_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._latency_count: dict[tuple[str, str], int] = defaultdict(int)
        self._metric_publish_count: int = 0
        self._query_success_count: int = 0
        self._query_failure_count: int = 0
        self._llm_call_count: int = 0
        self._llm_failure_count: int = 0

    def observe(self, method: str, path: str, status: int, duration: float) -> None:
        path = _normalize_path(path)
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
        lines.append(f"unisense_metric_publish_total {self._metric_publish_count}")
        lines.append(f"unisense_query_success_total {self._query_success_count}")
        lines.append(f"unisense_query_failure_total {self._query_failure_count}")
        lines.append(f"unisense_llm_call_total {self._llm_call_count}")
        lines.append(f"unisense_llm_failure_total {self._llm_failure_count}")
        return "\n".join(lines) + "\n"

    def observe_metric_publish(self) -> None:
        self._metric_publish_count += 1

    def observe_query_result(self, success: bool) -> None:
        if success:
            self._query_success_count += 1
        else:
            self._query_failure_count += 1

    def observe_llm_call(self, success: bool) -> None:
        self._llm_call_count += 1
        if not success:
            self._llm_failure_count += 1


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
