"""冲突领域安全测试（对齐 gateways security_reverse）。

覆盖：① 裁决端点 RBAC 写闸门（非治理角色 403）；
② 列表端点 SQL 注入守卫（400）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app


@pytest.fixture
async def owner_client():
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    # 非治理角色（metric_owner）尝试裁决
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=9, role="metric_owner")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_arbitrate_requires_gov_role_403(owner_client):
    resp = await owner_client.post(
        "/api/v1/conflicts/CF-DUMMY/arbitrate",
        json={"decision": "merge", "arbitrator_id": 9, "reason": "x"},
    )
    assert resp.status_code == 403


@pytest.fixture
async def analyst_client():
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=10, role="analyst")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_list_rejects_sql_injection_400(analyst_client):
    resp = await analyst_client.get("/api/v1/conflicts", params={"domain": "' OR '1'='1"})
    assert resp.status_code == 400
