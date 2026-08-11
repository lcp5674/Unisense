"""glossary 安全测试（对齐 gateways security_reverse，TD §13）。

覆盖：
① 术语写接口 RBAC 写闸门（非授权角色 403）；
② 冲突治理接口 RBAC 治理闸门（普通读者 403）；
③ 列表/冲突端点 SQL 注入守卫（400）；
④ 注入拦截返回 INJECTION_DETECTED（纵深防御）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app


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


_TERM_BODY = {
    "term_code": "T_GROSS_PROFIT",
    "name": "毛利",
    "definition": "收入减去成本",
    "domain": "finance",
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


async def test_create_term_requires_write_role_403(analyst_client: httpx.AsyncClient) -> None:
    """普通分析师无权创建术语。"""
    resp = await analyst_client.post("/api/v1/terms", json=_TERM_BODY)
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_resolve_conflict_requires_gov_role_403(viewer_client: httpx.AsyncClient) -> None:
    """冲突处置需治理角色，普通读者越权被拒。"""
    resp = await viewer_client.post(
        "/api/v1/terms/conflicts/1/resolve",
        json={"decision": "RESOLVED", "resolver_id": 11},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_list_terms_blocks_sql_injection_400(writer_client: httpx.AsyncClient) -> None:
    """术语列表端点 SQL 注入被守卫拦截。"""
    resp = await writer_client.get("/api/v1/terms", params={"search": "' OR '1'='1"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "INJECTION_DETECTED"


async def test_list_conflicts_blocks_sql_injection_400(viewer_client: httpx.AsyncClient) -> None:
    """冲突列表端点 SQL 注入被守卫拦截。"""
    resp = await viewer_client.get("/api/v1/terms/conflicts", params={"status": "' OR '1'='1"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "INJECTION_DETECTED"


async def test_resolve_conflict_ignores_client_resolver_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLAT-2: 冲突裁决忽略 client 传入的 resolver_id=11，使用认证身份(9)。"""
    import app.services.glossary.service as gsvc

    captured: dict[str, int] = {}

    async def fake(self, conflict_id, decision, resolver_id):
        captured["resolver_id"] = resolver_id
        return {"id": conflict_id, "decision": decision, "resolved": True}

    monkeypatch.setattr(gsvc.GlossaryService, "resolve_conflict", fake)
    async for admin in _client(9, "platform_admin"):
        resp = await admin.post(
            "/api/v1/terms/conflicts/1/resolve",
            json={"decision": "RESOLVED", "resolver_id": 11},
        )
        assert resp.status_code == 200
        assert captured["resolver_id"] == 9
