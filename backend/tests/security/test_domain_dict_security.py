"""subject_domain + system_dict RBAC 安全测试（对齐 gateways security_reverse）。

背景：两模块此前 `_require_admin()` 为 pass+TODO（匿名可写），本轮接入真实 RBAC：
- 主题域写接口：仅 platform_admin/domain_admin；读接口 ALL_ROLES
- 系统字典写接口：仅 platform_admin；读接口 ALL_ROLES

覆盖：
① 匿名（无 token）访问写接口 → 401；
② 非授权角色（analyst）访问写接口 → 403；
③ 授权角色（platform_admin/domain_admin）访问写接口 → 业务可达（service 正常调用）；
④ 读接口：任意已登录角色 200。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app

_NOW = datetime.now(UTC)


def _session() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    s.commit = AsyncMock()
    s.rollback = AsyncMock()
    s.flush = AsyncMock()
    s.refresh = MagicMock()
    s.execute = AsyncMock(return_value=MagicMock())
    return s


async def _client(
    uid: int, role: str, *, authed: bool = True
) -> AsyncIterator[httpx.AsyncClient]:
    session = _session()

    async def fake_db() -> AsyncIterator[MagicMock]:
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    if authed:
        app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
            id=uid, role=role, domain="sales"
        )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def admin_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(1, "platform_admin"):
        yield c


@pytest.fixture
async def domain_admin_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(2, "domain_admin"):
        yield c


@pytest.fixture
async def analyst_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(10, "analyst"):
        yield c


@pytest.fixture
async def anon_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(0, "", authed=False):
        yield c


# ---------------------------------------------------------------- 主题域


async def test_create_domain_anon_401(anon_client: httpx.AsyncClient) -> None:
    resp = await anon_client.post(
        "/api/v1/domains/",
        json={"code": "sales", "name": "销售", "owner_id": 1},
    )
    assert resp.status_code in (401, 403)


async def test_create_domain_analyst_403(analyst_client: httpx.AsyncClient) -> None:
    resp = await analyst_client.post(
        "/api/v1/domains/",
        json={"code": "sales", "name": "销售", "owner_id": 1},
    )
    assert resp.status_code == 403


async def test_create_domain_admin_ok(
    admin_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = MagicMock()
    fake.id = 1
    fake.code = "sales"
    fake.name = "销售"
    fake.parent_id = None
    fake.level = 1
    fake.path = "/sales"
    fake.sort_order = 1
    fake.status = "active"
    fake.defaults_json = {}
    fake.description = None
    fake.owner_id = 1
    fake.metric_count = 0
    fake.created_at = _NOW
    fake.updated_at = _NOW
    monkeypatch.setattr(
        "app.services.subject_domain.service.SubjectDomainService.create_domain",
        AsyncMock(return_value=fake),
    )
    monkeypatch.setattr(
        "app.services.subject_domain.service.SubjectDomainService.get_domain_with_count",
        AsyncMock(return_value=fake),
    )
    resp = await admin_client.post(
        "/api/v1/domains/",
        json={"code": "sales", "name": "销售", "owner_id": 1},
    )
    assert resp.status_code == 201


async def test_update_domain_domain_admin_ok(
    domain_admin_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = MagicMock()
    fake.id = 1
    fake.code = "sales"
    fake.name = "销售域"
    fake.parent_id = None
    fake.level = 1
    fake.path = "/sales"
    fake.sort_order = 1
    fake.status = "active"
    fake.defaults_json = {}
    fake.description = None
    fake.owner_id = 1
    fake.metric_count = 0
    fake.created_at = _NOW
    fake.updated_at = _NOW
    monkeypatch.setattr(
        "app.services.subject_domain.service.SubjectDomainService.update_domain",
        AsyncMock(return_value=fake),
    )
    monkeypatch.setattr(
        "app.services.subject_domain.service.SubjectDomainService.get_domain_with_count",
        AsyncMock(return_value=fake),
    )
    resp = await domain_admin_client.put(
        "/api/v1/domains/sales",
        json={"name": "销售域"},
    )
    assert resp.status_code == 200


async def test_delete_domain_analyst_403(analyst_client: httpx.AsyncClient) -> None:
    resp = await analyst_client.delete("/api/v1/domains/sales")
    assert resp.status_code == 403


async def test_list_domains_analyst_200(analyst_client: httpx.AsyncClient) -> None:
    resp = await analyst_client.get("/api/v1/domains/")
    assert resp.status_code == 200


# ---------------------------------------------------------------- 系统字典


async def test_create_dict_anon_401(anon_client: httpx.AsyncClient) -> None:
    resp = await anon_client.post(
        "/api/v1/dicts/granularity",
        json={"code": "minute", "label": "分钟", "sort_order": 1},
    )
    assert resp.status_code in (401, 403)


async def test_create_dict_analyst_403(analyst_client: httpx.AsyncClient) -> None:
    resp = await analyst_client.post(
        "/api/v1/dicts/granularity",
        json={"code": "minute", "label": "分钟", "sort_order": 1},
    )
    assert resp.status_code == 403


async def test_create_dict_domain_admin_403(domain_admin_client: httpx.AsyncClient) -> None:
    # 系统字典仅 platform_admin 可管理，domain_admin 也应 403
    resp = await domain_admin_client.post(
        "/api/v1/dicts/granularity",
        json={"code": "minute", "label": "分钟", "sort_order": 1},
    )
    assert resp.status_code == 403


async def test_create_dict_admin_ok(
    admin_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = MagicMock()
    fake.id = 1
    fake.dict_type = "granularity"
    fake.code = "minute"
    fake.label = "分钟"
    fake.sort_order = 1
    fake.status = "active"
    fake.description = None
    fake.created_at = _NOW
    fake.updated_at = _NOW
    monkeypatch.setattr(
        "app.services.system_dict.service.SystemDictService.create_item",
        AsyncMock(return_value=fake),
    )
    monkeypatch.setattr(
        "app.services.system_dict.service.SystemDictService.get_ref_count",
        AsyncMock(return_value=0),
    )
    resp = await admin_client.post(
        "/api/v1/dicts/granularity",
        json={"code": "minute", "label": "分钟", "sort_order": 1},
    )
    assert resp.status_code == 201


async def test_update_dict_analyst_403(analyst_client: httpx.AsyncClient) -> None:
    resp = await analyst_client.put(
        "/api/v1/dicts/granularity/minute",
        json={"label": "分钟2"},
    )
    assert resp.status_code == 403


async def test_list_dicts_analyst_200(analyst_client: httpx.AsyncClient) -> None:
    resp = await analyst_client.get("/api/v1/dicts/granularity")
    assert resp.status_code == 200
