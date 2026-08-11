"""governance 可观测测试（对齐 gateways observability，TD §14）。

覆盖：
① 响应体与响应头均带 trace_id（全链路透传）；
② 授权/复核写操作落审计（action + entity_type 正确，PII 复核置 pii_access）；
③ /metrics 暴露 RED 指标。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.api import governance as governance_api
from app.main import app
from app.services.governance.schemas import (
    ClassificationRescanResult,
    GrantResponse,
    PermissionSnapshot,
    PiiReviewResult,
)
from app.services.governance.service import GovernanceService


@pytest.fixture
def audit_sink(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """拦截审计写入，便于断言字段。"""
    records: list[dict[str, Any]] = []

    async def fake_write_audit(db: Any, **kwargs: Any) -> None:
        records.append(kwargs)

    monkeypatch.setattr(governance_api, "write_audit", fake_write_audit)
    return records


async def _client(uid: int, role: str) -> AsyncIterator[httpx.AsyncClient]:
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())

    async def fake_db() -> AsyncIterator[MagicMock]:
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=uid, role=role, domain="sales"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_my_permissions_response_contains_trace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_snapshot(self: GovernanceService, user: Any) -> PermissionSnapshot:
        return PermissionSnapshot(
            user_id=user.id,
            role=user.role,
            home_domain="sales",
            allowed_actions=["read"],
            granted_domains=[],
            metric_whitelist=[],
            row_level_restricted=False,
            grants=[],
            expiring_soon=[],
        )

    monkeypatch.setattr(GovernanceService, "my_permissions", fake_snapshot)
    async for c in _client(12, "analyst"):
        resp = await c.get("/api/v1/me/permissions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["trace_id"]
        assert resp.headers.get("X-Trace-Id")
        assert body["data"]["user_id"] == 12


async def test_grant_writes_audit_record(
    monkeypatch: pytest.MonkeyPatch, audit_sink: list[dict[str, Any]]
) -> None:
    async def fake_grant(self: GovernanceService, payload: Any, actor_id: int) -> Any:
        return MagicMock(
            id=7,
            user_id=payload.user_id,
            role_id=None,
            domain=payload.domain,
            metric_whitelist=None,
            grant_type="READ",
            status="ACTIVE",
            row_level=False,
            expires_at=None,
            granted_by=actor_id,
            reason=None,
        )

    monkeypatch.setattr(GovernanceService, "grant", fake_grant)
    monkeypatch.setattr(
        GrantResponse,
        "model_validate",
        classmethod(
            lambda cls, obj: GrantResponse(
                id=7,
                user_id=2,
                domain="sales",
                grant_type="READ",
                status="ACTIVE",
                row_level=False,
            )
        ),
    )
    async for c in _client(1, "platform_admin"):
        resp = await c.post(
            "/api/v1/grants", json={"user_id": 2, "domain": "sales", "grant_type": "READ"}
        )
        assert resp.status_code == 200

    assert len(audit_sink) == 1
    record = audit_sink[0]
    assert record["action"] == "GRANT_CREATE"
    assert record["entity_type"] == "grants"
    assert record["actor_id"] == 1
    assert record["trace_id"]


async def test_pii_review_audit_marks_pii_access(
    monkeypatch: pytest.MonkeyPatch, audit_sink: list[dict[str, Any]]
) -> None:
    """PII 复核审计必须置 pii_access=True（合规留痕）。"""
    from datetime import UTC, datetime

    async def fake_review(self: GovernanceService, payload: Any, reviewer: Any) -> PiiReviewResult:
        return PiiReviewResult(
            metric_code=payload.metric_code,
            decision=payload.decision,
            compliance_reviewed=True,
            sensitivity_level="PII",
            masking_policy="hash",
            reviewer_id=reviewer.id,
            reviewed_at=datetime.now(UTC),
        )

    monkeypatch.setattr(GovernanceService, "pii_review", fake_review)
    async for c in _client(11, "compliance_officer"):
        resp = await c.post(
            "/api/v1/pii/review",
            json={"metric_code": "m1", "decision": "APPROVE", "comment": "脱敏后放行"},
        )
        assert resp.status_code == 200

    record = audit_sink[0]
    assert record["action"] == "PII_REVIEW"
    assert record["pii_access"] is True
    assert record["entity_id"] == "m1"


async def test_rescan_audit_carries_counters(
    monkeypatch: pytest.MonkeyPatch, audit_sink: list[dict[str, Any]]
) -> None:
    async def fake_rescan(self: GovernanceService, payload: Any) -> ClassificationRescanResult:
        return ClassificationRescanResult(
            scanned=3, changed=1, pii_found=1, degraded=1, model_version="rules-v1", items=[]
        )

    monkeypatch.setattr(GovernanceService, "classification_rescan", fake_rescan)
    async for c in _client(11, "compliance_officer"):
        resp = await c.post("/api/v1/classification/rescan", json={"source_id": "mysql-01"})
        assert resp.status_code == 200
        assert resp.json()["data"]["degraded"] == 1

    record = audit_sink[0]
    assert record["action"] == "CLASSIFICATION_RESCAN"
    assert record["detail"]["scanned"] == 3
    assert record["detail"]["degraded"] == 1


async def test_metrics_endpoint_exposes_red() -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/metrics")
    assert resp.status_code == 200
    assert "http_requests_total" in resp.text
