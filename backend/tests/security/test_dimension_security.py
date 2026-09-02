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
        id=uid,
        role=role,
        domain="sales",
        roles_all=MagicMock(return_value=[role]),
        domains_all=MagicMock(return_value=["sales"]),
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
        from types import SimpleNamespace

        return SimpleNamespace(
            id=rec_id,
            metric_id=1,
            dim_code=None,
            expected_expr="SUM(a)",
            actual_expr="SUM(b)",
            status=data.decision,
            diff_summary=None,
            reviewed_by=reviewer_id,
            created_at=None,
        )

    monkeypatch.setattr(dsvc.DimensionService, "review_reconciliation", fake)
    async for admin in _client(9, "platform_admin"):
        resp = await admin.post(
            "/api/v1/dimensions/reconciliations/1/review",
            json={"decision": "APPROVED", "reviewer_id": 11},
        )
        assert resp.status_code == 200
        assert captured["reviewer_id"] == 9


async def test_create_dimension_cross_domain_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1-10：domain_admin 仅可在本域建维度，跨域创建被拒绝（403 FORBIDDEN）。"""
    async for admin in _client(9, "domain_admin"):  # 固定 user.domain="sales"
        resp = await admin.post(
            "/api/v1/dimensions",
            json={**_DIM_BODY, "domain": "finance"},  # 跨域：finance
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "FORBIDDEN"


async def test_domain_admin_cannot_update_other_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-10：domain_admin 编辑他域维度被域作用域守卫拦截（403 FORBIDDEN）。"""
    import app.services.dimension.service as dsvc
    from app.models.dimension import Dimension

    # 目标维度归属 finance（与 user.domain="sales" 不同）
    foreign_dim = Dimension(
        dim_code="D_REGION", name="区域", domain="finance",
        type="SCD1", owner_id=1, status="DRAFT",
    )

    async def fake_get(self, code):
        return foreign_dim

    monkeypatch.setattr(dsvc.DimensionService, "get_dimension", fake_get)
    async for admin in _client(9, "domain_admin"):
        resp = await admin.put(
            "/api/v1/dimensions/D_REGION",
            json={"name": "区域（改名）"},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "FORBIDDEN"


async def test_domain_admin_can_update_own_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-10 反例：domain_admin 编辑本域维度不被域守卫拦截（非 403）。"""
    import app.services.dimension.service as dsvc
    from app.models.dimension import Dimension

    own_dim = Dimension(
        dim_code="D_REGION", name="区域", domain="sales",
        type="SCD1", owner_id=1, status="DRAFT",
    )

    async def fake_get(self, code):
        return own_dim

    monkeypatch.setattr(dsvc.DimensionService, "get_dimension", fake_get)
    async for admin in _client(9, "domain_admin"):  # user.domain="sales" == own_dim.domain
        resp = await admin.put(
            "/api/v1/dimensions/D_REGION",
            json={"name": "区域（改名）"},
        )
        # 域守卫放行；后续失败属 DB mock 副作用，与域作用域无关
        assert resp.status_code != 403


async def test_null_domain_user_cannot_create_cross_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """P0 越权回归（第四轮审查）：domain=NULL 的 metric_owner 此前因
    ``_assert_domain_scope`` 的 ``and user.domain`` 短路可跨任意域创建维度
    （真实 API 实测 201）；现应 fail-closed 拒绝（403 FORBIDDEN）。
    """
    session = _session()

    async def fake_db() -> AsyncIterator[MagicMock]:
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=99,
        role="metric_owner",
        domain=None,
        domains_all=MagicMock(return_value=[]),  # 团队域亦为空 → 无任何权限域
        roles_all=MagicMock(return_value=["metric_owner"]),
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/dimensions",
            json={**_DIM_BODY, "domain": "sales"},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "FORBIDDEN"
    app.dependency_overrides.clear()
