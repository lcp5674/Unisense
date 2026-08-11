"""consume 服务可观测性测试：trace_id 透传、审计落盘、Prometheus 指标暴露。

对齐 tests/observability/test_quality_observability.py 的写法，使用内存依赖覆盖，
不依赖真实 MySQL / OLAP。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import consume as consume_api
from app.api.deps import get_db_session
from app.main import app
from app.models.consume import ApiClient
from app.services.consume.schemas import QueryResponse

logger = logging.getLogger(__name__)


class _FakeClient:
    id = 1
    client_id = "cli_obs"
    scope_domain = "M1"
    metric_whitelist = ["M1"]
    qps = 100
    daily_quota = 100_000
    created_by = 1


@pytest.fixture
async def obs_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    """覆盖 DB 会话与 X-Api-Key 鉴权依赖（内存），使 query 端点可无副作用运行。"""
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    async def fake_db():
        yield db

    app.dependency_overrides[get_db_session] = fake_db

    fake_client = _FakeClient()

    async def fake_auth() -> ApiClient:
        return fake_client  # type: ignore[return-value]

    app.dependency_overrides[consume_api.get_consume_client] = fake_auth

    async def fake_exec(req, cli):  # noqa: ANN001
        return QueryResponse(metric_code="M1", data={"value": 42})

    monkeypatch.setattr(consume_api.ConsumeService, "execute_query", staticmethod(fake_exec))

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_metrics_endpoint_exposed(obs_client: httpx.AsyncClient) -> None:
    """Prometheus RED 指标端点在 /metrics 暴露（平台级，含 consume 指标）。"""
    resp = await obs_client.get("/metrics")
    assert resp.status_code == 200
    # 平台级 Prometheus RED 指标：
    #   Rate(http_requests_total) 与 Duration(http_request_duration_seconds)
    assert "http_requests_total" in resp.text
    assert "http_request_duration_seconds" in resp.text


async def test_response_contains_trace_id(obs_client: httpx.AsyncClient) -> None:
    """每个响应必须带 X-Trace-Id（中间件注入，便于链路追踪）。"""
    resp = await obs_client.post(
        "/api/v1/consume/query",
        json={"metric_code": "M1", "date_range": "2024"},
        headers={"X-Api-Key": "cli_obs:secret"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("X-Trace-Id")


async def test_query_writes_audit_record(
    obs_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功查询必须写入审计（action=consume.query, subject=指标码）。"""
    calls: list[tuple[str, str]] = []
    details: list[dict] = []

    async def spy_audit(db, **kwargs):  # noqa: ANN001, ANN003
        calls.append((kwargs["action"], kwargs["entity_id"]))
        details.append(kwargs["detail"])
        return None

    monkeypatch.setattr(consume_api, "write_audit", spy_audit)

    resp = await obs_client.post(
        "/api/v1/consume/query",
        json={"metric_code": "M1", "date_range": "2024"},
        headers={"X-Api-Key": "cli_obs:secret"},
    )
    assert resp.status_code == 200
    assert ("consume.query", "M1") in calls
    # 审计必须标注数据分级（非 PII 指标为 INTERNAL，PII 指标为 PII）
    assert details[0]["data_classification"] in ("PII", "INTERNAL")
