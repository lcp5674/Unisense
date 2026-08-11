"""quality 安全测试（D11 外部基准对账 RBAC 闸门，对齐 gateways security_reverse）。

覆盖：
① 基准导入/绑定/对账执行需写权限（analyst → 403）；
② 差异确认需治理权限（analyst → 403，metric_owner → 200）；
③ 基准/对账记录列表需读权限（viewer → 200）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from app.models.quality import ReconciliationStatus
from app.services.quality.schemas import BenchmarkResponse, ReconciliationRecordResponse


def _session() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())
    return session


async def _client(uid: int, role: str) -> AsyncIterator[httpx.AsyncClient]:
    session = _session()

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


@pytest.fixture
async def owner_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(9, "metric_owner"):
        yield c


@pytest.fixture
async def analyst_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(10, "analyst"):
        yield c


@pytest.fixture
async def viewer_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(11, "viewer"):
        yield c


async def test_benchmark_import_requires_write_role_403(analyst_client: httpx.AsyncClient) -> None:
    resp = await analyst_client.post(
        "/api/v1/quality/benchmarks/import",
        json={"source_id": "S", "metric_code": "M", "bench_date": "2024-01-01",
              "bench_value": "100", "provider": "audit"},
    )
    assert resp.status_code == 403


async def test_benchmark_bind_requires_write_role_403(analyst_client: httpx.AsyncClient) -> None:
    resp = await analyst_client.post(
        "/api/v1/quality/benchmarks/1/bind",
        json={"metric_code": "M2"},
    )
    assert resp.status_code == 403


async def test_reconciliation_run_requires_write_role_403(analyst_client: httpx.AsyncClient) -> None:
    resp = await analyst_client.post(
        "/api/v1/quality/reconciliation/run",
        json={"benchmark_id": 1, "metric_value": "100"},
    )
    assert resp.status_code == 403


async def test_reconciliation_confirm_requires_gov_role_403(
    analyst_client: httpx.AsyncClient,
) -> None:
    resp = await analyst_client.post(
        "/api/v1/quality/reconciliation-records/1/confirm",
        json={"decision": "reasonable"},
    )
    assert resp.status_code == 403


async def test_benchmark_list_read_role_200(viewer_client: httpx.AsyncClient) -> None:
    resp = await viewer_client.get("/api/v1/quality/benchmarks")
    assert resp.status_code == 200


async def test_reconciliation_records_list_read_role_200(viewer_client: httpx.AsyncClient) -> None:
    resp = await viewer_client.get("/api/v1/quality/reconciliation-records")
    assert resp.status_code == 200


async def test_benchmark_import_success(
    owner_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = BenchmarkResponse(
        id=1, source_id="S", metric_code="M", bench_date=date(2024, 1, 1),
        dims=None, bench_value=Decimal("100"), provider="audit",
        tolerance_pct=None, imported_by=9, created_at=datetime(2024, 1, 1),
    )
    monkeypatch.setattr(
        "app.services.quality.service.QualityService.import_benchmark",
        AsyncMock(return_value=fake),
    )
    resp = await owner_client.post(
        "/api/v1/quality/benchmarks/import",
        json={"source_id": "S", "metric_code": "M", "bench_date": "2024-01-01",
              "bench_value": "100", "provider": "audit"},
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["id"] == 1


async def test_reconciliation_confirm_success(
    owner_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = ReconciliationRecordResponse(
        id=1, benchmark_id=1, metric_code="M", metric_value=Decimal("103"),
        bench_value=Decimal("100"), diff_pct=Decimal("3"), window=None,
        status=ReconciliationStatus.CONFIRMED, owner_note="口径有误",
        decision="caliber_error", confirmed_by=9, checked_at=datetime(2024, 1, 2),
        created_at=datetime(2024, 1, 1),
    )
    monkeypatch.setattr(
        "app.services.quality.service.QualityService.confirm_reconciliation",
        AsyncMock(return_value=fake),
    )
    resp = await owner_client.post(
        "/api/v1/quality/reconciliation-records/1/confirm",
        json={"decision": "caliber_error", "owner_note": "口径有误"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "CONFIRMED"
