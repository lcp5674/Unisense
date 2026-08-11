"""dimension 混沌测试（对齐 gateways chaos，TD §15）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.core.exceptions import ExternalDependencyError
from app.main import app
from app.services.dimension.service import DimensionService


def _session() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    s.commit = MagicMock()
    s.rollback = MagicMock()
    s.flush = MagicMock()
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


async def test_create_dimension_returns_503_on_db_failure(
    writer_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(self: DimensionService, payload: object) -> object:
        raise ExternalDependencyError("db 不可达")

    monkeypatch.setattr(DimensionService, "create_dimension", boom)
    resp = await writer_client.post("/api/v1/dimensions", json=_DIM_BODY)
    assert resp.status_code == 503
    assert resp.json()["code"] == "EXTERNAL_DEPENDENCY_ERROR"
