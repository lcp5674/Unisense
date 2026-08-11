"""采集领域安全反向测试（对齐 gateways security_reverse）。

覆盖：普通用户调写接口 -> 403 FORBIDDEN；SQL 注入 fuzz -> 被拦截
（INJECTION_DETECTED）；PII 元数据注册 -> 审计含 data_classification=PII。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from app.models.audit import AuditLog
from app.services.collector.schemas import DBCatalogResponse
from app.services.collector.service import CollectorService


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


async def test_normal_user_cannot_create_source_403(analyst_client):
    resp = await analyst_client.post(
        "/api/v1/data-sources",
        json={
            "source_id": "x",
            "name": "X",
            "source_type": "mysql",
            "connection_config": {"host": "h"},
            "domain": "d",
        },
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 403
    assert "FORBIDDEN" in resp.text


async def test_register_pii_catalog_writes_audit(owner_client, monkeypatch):
    client, session = owner_client
    pii_resp = DBCatalogResponse(
        source_id="s",
        entity_name="users",
        entity_type="TABLE",
        schema_def={"columns": ["user_name"]},
        etl_sql=None,
        sensitivity_level="PII",
        owner_id=None,
        upstream_signature="sig",
    )
    monkeypatch.setattr(CollectorService, "register_catalog", AsyncMock(return_value=pii_resp))
    resp = await client.post(
        "/api/v1/data-sources/s/catalogs",
        json={"source_id": "s", "entity_name": "users", "schema_json": {"columns": ["user_name"]}},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200
    audits = [c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], AuditLog)]
    assert audits, "PII 元数据注册必须写入审计"
    assert audits[0].pii_access is True
    assert audits[0].detail_json.get("data_classification") == "PII"


async def test_injection_keyword_blocked(owner_client):
    client, _ = owner_client
    resp = await client.get(
        "/api/v1/data-sources?keyword=' OR 1=1 -- ",
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 400
    assert "INJECTION_DETECTED" in resp.text
