"""血缘领域安全反向测试（对齐 gateways security_reverse）。

覆盖：普通用户调写接口 -> 403 FORBIDDEN；SQL 注入 fuzz（注入）-> 被拦截
（INJECTION_DETECTED）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app


@pytest.fixture
async def analyst_client():
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=5, role="analyst")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def owner_client():
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=5, role="metric_owner")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, session
    app.dependency_overrides.clear()


async def test_analyst_cannot_parse_lineage_403(analyst_client):
    resp = await analyst_client.post(
        "/api/v1/lineage/parse",
        json={"sql": "INSERT INTO t SELECT a.id FROM a"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 403
    assert "FORBIDDEN" in resp.text


async def test_injection_keyword_blocked_on_impact(owner_client):
    client, _ = owner_client
    resp = await client.get(
        "/api/v1/lineage/impact?node=' OR 1=1 -- ",
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 400
    assert "INJECTION_DETECTED" in resp.text  # 注入关键字被守卫拦截
