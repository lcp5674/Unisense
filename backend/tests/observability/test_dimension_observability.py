"""dimension 可观测测试（对齐 gateways observability，TD §14）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.api import dimension as dimension_api
from app.main import app
from app.services.dimension.service import DimensionService


@pytest.fixture
def audit_sink(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []

    async def fake_write_audit(db: object, **kwargs: object) -> None:
        records.append(kwargs)

    monkeypatch.setattr(dimension_api, "write_audit", fake_write_audit)
    return records


def _session() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    s.commit = AsyncMock()
    s.rollback = AsyncMock()
    s.flush = AsyncMock()
    s.refresh = AsyncMock()
    s.execute = AsyncMock()
    return s


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


_DIM_BODY = {
    "dim_code": "D_REGION",
    "name": "区域",
    "domain": "sales",
    "owner_id": 9,
}


@pytest.fixture
async def writer_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(9, "metric_owner"):
        yield c


@pytest.fixture
async def reader_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(11, "viewer"):
        yield c


async def test_create_dimension_writes_audit_record(
    writer_client: httpx.AsyncClient,
    audit_sink: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create(
        self: DimensionService, payload: object, actor_id: int | None = None
    ) -> object:
        # 返回真实字段结构对象（DimensionResponse.from_model 需要真实属性，MagicMock 会校验失败）
        return SimpleNamespace(
            id=1,
            dim_code="D_REGION",
            name="区域",
            domain="sales",
            type="SCD1",
            description=None,
            owner_id=9,
            status="DRAFT",
        )

    monkeypatch.setattr(DimensionService, "create_dimension", fake_create)
    resp = await writer_client.post("/api/v1/dimensions", json=_DIM_BODY)
    assert resp.status_code == 201
    assert len(audit_sink) == 1
    record = audit_sink[0]
    assert record["action"] == "dimension.create"
    assert record["entity_type"] == "dimension"
    assert record["actor_id"] == 9
    assert record["trace_id"]


async def test_response_contains_trace_id(
    reader_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list(self: DimensionService, *args: object, **kwargs: object) -> object:
        return ([], 0)

    monkeypatch.setattr(DimensionService, "list_dimensions", fake_list)
    resp = await reader_client.get("/api/v1/dimensions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trace_id"]
    assert resp.headers.get("X-Trace-Id")
    assert body["trace_id"] == resp.headers["X-Trace-Id"]


async def test_metrics_endpoint_exposes_red() -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/metrics")
    assert resp.status_code == 200
    assert "http_requests_total" in resp.text
