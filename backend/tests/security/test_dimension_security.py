"""dimension 安全测试（对齐 gateways security_reverse，TD §13）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app

_TERM_ROLES = ("metric_owner", "domain_admin", "platform_admin")


def _session() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    s.commit = AsyncMock()
    s.rollback = AsyncMock()
    s.flush = AsyncMock()
    s.refresh = MagicMock()
    s.execute = MagicMock()
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
async def analyst_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(10, "analyst"):
        yield c


@pytest.fixture
async def viewer_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(11, "viewer"):
        yield c


async def test_create_dimension_requires_write_role_403(analyst_client: httpx.AsyncClient) -> None:
    resp = await analyst_client.post("/api/v1/dimensions", json=_DIM_BODY)
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_review_reconciliation_requires_gov_role(viewer_client: httpx.AsyncClient) -> None:
    resp = await viewer_client.post(
        "/api/v1/dimensions/reconciliations/1/review",
        json={"decision": "APPROVED", "reviewer_id": 11},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_list_dimensions_blocks_sql_injection_400(writer_client: httpx.AsyncClient) -> None:
    resp = await writer_client.get("/api/v1/dimensions", params={"search": "' OR '1'='1"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "INJECTION_DETECTED"


async def test_list_mappings_blocks_sql_injection_400(writer_client: httpx.AsyncClient) -> None:
    resp = await writer_client.get("/api/v1/dimensions/mappings", params={"search": "' OR '1'='1"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "INJECTION_DETECTED"


async def test_review_reconciliation_ignores_client_reviewer_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLAT-2: 对账复核忽略 client 传入的 reviewer_id=11，使用认证身份(9)。"""
    import app.services.dimension.service as dsvc

    captured: dict[str, int] = {}

    async def fake(self, rec_id, data, reviewer_id=None):
        captured["reviewer_id"] = reviewer_id
        return {"id": rec_id, "status": data.decision}

    monkeypatch.setattr(dsvc.DimensionService, "review_reconciliation", fake)
    async for admin in _client(9, "platform_admin"):
        resp = await admin.post(
            "/api/v1/dimensions/reconciliations/1/review",
            json={"decision": "APPROVED", "reviewer_id": 11},
        )
        assert resp.status_code == 200
        assert captured["reviewer_id"] == 9
