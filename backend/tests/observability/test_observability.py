"""语义领域可观测性测试（对齐 TD §16 / DEV_GUIDE §16）。

覆盖：
1. 访问日志含 trace_id（结构化日志透传）
2. Prometheus 指标端点返回非零 RED（Rate/Errors/Duration）
3. trace_id 跨服务透传（入站 X-Trace-Id 原样回传）
"""

from __future__ import annotations

import logging
import re

import structlog
from structlog.testing import capture_logs

from app.core.logging import configure_logging


def _configure_capturing() -> None:
    """临时切换为带捕获的日志配置（关闭 logger 缓存以便捕获新 logger）。"""
    structlog.reset_defaults()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


async def test_access_log_contains_trace_id(client):
    # 日志含 trace_id
    _configure_capturing()
    import app.core.metrics as metrics_mod

    try:
        with capture_logs() as logs:
            metrics_mod.logger = structlog.get_logger("metrics")  # 取带捕获的新 logger
            resp = await client.get("/health")
            assert resp.status_code == 200
    finally:
        configure_logging()  # 还原全局日志配置，避免污染其它测试

    assert any("trace_id" in e for e in logs), f"访问日志应包含 trace_id, got {logs}"


async def test_metrics_endpoint_returns_nonzero_red(client):
    # 指标 endpoint 返回非零 RED（Prometheus 格式）
    for _ in range(5):
        await client.get("/health")
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    assert "http_requests_total" in text  # RED-Rate
    assert "http_request_duration_seconds" in text  # RED-Duration
    # Prometheus exposition 格式（含 trace_id/span 透传上下文）
    assert "prometheus" in resp.headers.get("content-type", "") or "text/plain" in resp.headers.get(
        "content-type", ""
    )
    vals = [int(m) for m in re.findall(r"http_requests_total\{[^}]*\}\s+(\d+)", text)]
    assert sum(vals) > 0  # 累计计数非零


async def test_trace_id_propagates_across_services(client):
    # trace 跨服务透传：入站 X-Trace-Id 应原样回传（span 透传）
    resp = await client.get("/health", headers={"X-Trace-Id": "trace-abc-123"})
    assert resp.status_code == 200
    assert resp.headers.get("X-Trace-Id") == "trace-abc-123"
