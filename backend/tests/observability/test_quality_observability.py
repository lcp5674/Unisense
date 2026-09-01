"""quality 可观测测试（对齐 gateways observability，TD §14）。

覆盖：
① 响应体与响应头均带 trace_id（全链路透传）；
② 规则写操作落审计（action + entity_type 正确）；
③ /metrics 暴露 RED 指标。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from app.models.quality import QualityRuleMode, QualityRuleType, QualitySeverity
from app.services.quality.schemas import QualityRuleResponse
from app.services.quality.service import QualityService


@pytest.fixture
def audit_sink(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """拦截审计写入，便于断言字段。"""
    records: list[dict[str, object]] = []

    async def fake_write_audit(db: object, **kwargs: object) -> None:
        records.append(kwargs)

    import app.api.quality as quality_api

    monkeypatch.setattr(quality_api, "write_audit", fake_write_audit)
    return records


async def _client(
    uid: int,
    role: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    # create_rule 的 _assert_metric_domain 查询返回本域指标（sales），与 mock user 一致
    _metric = MagicMock()
    _metric.domain = "sales"
    session.execute = AsyncMock(return_value=MagicMock())
    session.execute.return_value.scalar_one_or_none.return_value = _metric

    async def fake_db() -> AsyncIterator[MagicMock]:
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=uid,
        role=role,
        domain="sales",
        roles_all=lambda: [role],
        has_role=lambda r: r == role,
    )

    async def fake_create(
        self: QualityService, payload: object, user_id: int
    ) -> QualityRuleResponse:
        return QualityRuleResponse(
            id=1,
            metric_id=1,
            rule_type=QualityRuleType.COMPLETENESS,
            threshold={"max": 100},
            rule_mode=QualityRuleMode.STATIC,
            severity=QualitySeverity.P0,
            enabled=True,
            notify_targets=None,
            created_by=user_id,
        )

    async def fake_list_rules(self: QualityService, *args: object, **kwargs: object) -> object:
        return ([], 0)

    monkeypatch.setattr(QualityService, "create_rule", fake_create)
    monkeypatch.setattr(QualityService, "list_rules", fake_list_rules)
    monkeypatch.setattr(QualityService, "list_events", fake_list_rules)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def owner_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(9, "metric_owner", monkeypatch):
        yield c


@pytest.fixture
async def viewer_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(11, "viewer", monkeypatch):
        yield c


async def test_create_rule_writes_audit_record(
    owner_client: httpx.AsyncClient, audit_sink: list[dict[str, object]]
) -> None:
    """规则注册写操作必须落审计（action/entity_type/actor/trace_id 正确）。"""
    resp = await owner_client.post(
        "/api/v1/quality/rules",
        json={
            "metric_id": 1,
            "rule_type": "COMPLETENESS",
            "threshold": {"max": 100},
            "severity": "P0",
        },
    )
    assert resp.status_code == 201
    assert len(audit_sink) == 1
    record = audit_sink[0]
    assert record["action"] == "quality_rule.create"
    assert record["entity_type"] == "quality_rule"
    assert record["actor_id"] == 9
    assert record["trace_id"]


async def test_response_contains_trace_id(viewer_client: httpx.AsyncClient) -> None:
    """读端点响应体与头均透传 trace_id。"""
    resp = await viewer_client.get("/api/v1/quality/rules")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trace_id"]
    assert resp.headers.get("X-Trace-Id")
    assert body["trace_id"] == resp.headers["X-Trace-Id"]


async def test_metrics_endpoint_exposes_red() -> None:
    """/metrics 暴露 RED 指标。"""
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/metrics")
    assert resp.status_code == 200
    assert "http_requests_total" in resp.text
