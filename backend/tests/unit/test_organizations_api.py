"""组织管理 API 测试（GET/POST /organizations + PATCH 状态机与自我保护）。

覆盖：列表（含用户数统计）、创建（成功/编码冲突）、更新（名称）、
自我保护（默认组织不可删 / 防自锁 / 有用户不可删）。
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
from app.models.user import Organization


def _make_org(**over: object) -> Organization:
    base: dict[str, object] = {
        "id": 1,
        "name": "默认组织",
        "code": "default",
        "status": "active",
        "created_at": datetime.now(UTC),
    }
    base.update(over)
    return Organization(**base)  # type: ignore[arg-type]


def _make_session(results: list[object] | None = None) -> MagicMock:
    """mock 会话：execute 依序返回给定结果（缺省全为 None 查询）；flush 填充 Organization.id。"""
    session = MagicMock()

    def _flush_side_effect(*args: object, **kwargs: object) -> None:
        for call in session.add.call_args_list:
            obj = call.args[0] if call.args else None
            if isinstance(obj, Organization) and getattr(obj, "id", None) is None:
                obj.id = 1

    results = results or []

    async def _execute_side_effect(*args: object, **kwargs: object) -> object:
        if results:
            return results.pop(0)
        r = MagicMock()
        r.scalar_one_or_none.return_value = None
        r.scalar.return_value = 0
        r.scalars.return_value.all.return_value = []
        return r

    session.execute = AsyncMock(side_effect=_execute_side_effect)
    session.add = MagicMock()
    session.flush = AsyncMock(side_effect=_flush_side_effect)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


def _scalar_one_none() -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none.return_value = None
    return r


def _scalar_one(value: object) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _scalar(value: object) -> MagicMock:
    r = MagicMock()
    r.scalar.return_value = value
    return r


@pytest.fixture
async def admin_client() -> AsyncIterator[httpx.AsyncClient]:
    """platform_admin 客户端 + mock 会话（默认空库）。"""
    session = _make_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="platform_admin", org_id=1
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_list_organizations_with_user_count() -> None:
    session = _make_session()
    # 1) total=1  2) rows=[org]  3) group counts  {1: 3}
    session.execute = AsyncMock(
        side_effect=[
            _scalar(1),
            MagicMock(scalars=lambda: MagicMock(all=lambda: [_make_org()])),
            MagicMock(all=lambda: [(1, 3)]),
        ]
    )

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="platform_admin", org_id=1
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/v1/organizations")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["code"] == "default"
    assert data["items"][0]["user_count"] == 3
    assert data["items"][0]["status"] == "active"


async def test_create_organization_success(admin_client: httpx.AsyncClient) -> None:
    resp = await admin_client.post(
        "/api/v1/organizations",
        json={"name": "金融事业部", "code": "finance_dept"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["name"] == "金融事业部"
    assert data["code"] == "finance_dept"
    assert data["status"] == "active"


async def test_create_organization_duplicate_code(admin_client: httpx.AsyncClient) -> None:
    """编码重复 → 409 ORG_EXISTS。"""
    dup = _make_org()

    async def fake_db():
        yield _make_session([_scalar_one(dup)])

    app.dependency_overrides[deps.get_db_session] = fake_db
    resp = await admin_client.post(
        "/api/v1/organizations",
        json={"name": "重复组织", "code": "finance_dept"},
    )
    app.dependency_overrides.pop(deps.get_db_session, None)
    assert resp.status_code == 409
    body = resp.json()
    assert "ORG_EXISTS" in body.get("code", "")


async def test_update_organization_name(admin_client: httpx.AsyncClient) -> None:
    """更新名称成功。"""
    org = _make_org()
    # execute 1) 查 org 命中 2) 查 user_count（PATCH deleted 才查，此处 0）3) refresh 后 user_count
    session = _make_session([_scalar_one(org), _scalar(0)])

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    resp = await admin_client.patch(
        "/api/v1/organizations/1",
        json={"name": "金融事业部（更名）"},
    )
    app.dependency_overrides.pop(deps.get_db_session, None)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["name"] == "金融事业部（更名）"


async def test_update_default_org_delete_protected(admin_client: httpx.AsyncClient) -> None:
    """默认组织不可删除 → 422 ORG_PROTECTED。"""
    org = _make_org(code="default")

    async def fake_db():
        yield _make_session([_scalar_one(org)])

    app.dependency_overrides[deps.get_db_session] = fake_db
    resp = await admin_client.patch(
        "/api/v1/organizations/1",
        json={"status": "deleted"},
    )
    app.dependency_overrides.pop(deps.get_db_session, None)
    assert resp.status_code == 422
    assert "ORG_PROTECTED" in resp.json().get("code", "")


async def test_update_default_org_suspend_protected(admin_client: httpx.AsyncClient) -> None:
    """默认组织不可停用（suspended）→ 422 ORG_PROTECTED（防止锁死默认租户）。"""
    org = _make_org(code="default")

    async def fake_db():
        yield _make_session([_scalar_one(org)])

    app.dependency_overrides[deps.get_db_session] = fake_db
    resp = await admin_client.patch(
        "/api/v1/organizations/1",
        json={"status": "suspended"},
    )
    app.dependency_overrides.pop(deps.get_db_session, None)
    assert resp.status_code == 422
    assert "ORG_PROTECTED" in resp.json().get("code", "")


async def test_update_self_org_suspend_protected(admin_client: httpx.AsyncClient) -> None:
    """不能停用当前管理员所属组织 → 422 ORG_SELF_LOCK。"""
    org = _make_org(id=1, code="main_dept")  # user.org_id=1

    async def fake_db():
        yield _make_session([_scalar_one(org)])

    app.dependency_overrides[deps.get_db_session] = fake_db
    resp = await admin_client.patch(
        "/api/v1/organizations/1",
        json={"status": "suspended"},
    )
    app.dependency_overrides.pop(deps.get_db_session, None)
    assert resp.status_code == 422
    assert "ORG_SELF_LOCK" in resp.json().get("code", "")


async def test_update_org_delete_with_users_conflict(admin_client: httpx.AsyncClient) -> None:
    """组织下有用户时删除 → 409 ORG_HAS_USERS。"""
    org = _make_org(id=2, code="sales_dept")

    async def fake_db():
        # 1) 查 org 命中 2) 查用户数=5 → 409
        yield _make_session([_scalar_one(org), _scalar(5)])

    app.dependency_overrides[deps.get_db_session] = fake_db
    resp = await admin_client.patch(
        "/api/v1/organizations/2",
        json={"status": "deleted"},
    )
    app.dependency_overrides.pop(deps.get_db_session, None)
    assert resp.status_code == 409
    assert "ORG_HAS_USERS" in resp.json().get("code", "")


async def test_viewer_forbidden_from_organizations() -> None:
    """viewer 访问组织管理 → 403。"""
    session = _make_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=9, role="viewer", org_id=1
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/v1/organizations")
    app.dependency_overrides.clear()
    assert resp.status_code == 403
