"""推荐 API 角色覆盖回归测试（TD §12.9 / FR-16，P0 角色授权回归）。

GET /api/v1/recommend/metrics 的读权限 require_roles 必须覆盖 6 个角色：
platform_admin / domain_admin / metric_owner / reviewer / viewer / compliance_officer。
逐一调用断言「不 403 且有 data.items 数组」，并用 analyst（不在读权限列表）作为
负向对照，确认角色校验确实生效而非形同虚设。

PLAT-2 回归：推荐服务必须使用认证身份 user.id（而非客户端传参），防止 IDOR 越权。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api import deps
from app.main import app

# 与 app/api/recommend.py _READ_ROLES 对齐
_ALLOWED_ROLES = (
    "platform_admin",
    "domain_admin",
    "metric_owner",
    "reviewer",
    "viewer",
    "compliance_officer",
)

_ALLOWED_ITEM = {
    "metric_id": "m_sales_gmv",
    "via": "global_hot",
    "edge_type": "POPULAR",
    "reason": "全站热门指标",
}


@pytest.mark.parametrize("role", _ALLOWED_ROLES)
async def test_recommend_metrics_allowed_for_role(client, role: str) -> None:
    """6 个允许角色逐一调用 GET /recommend/metrics：不 403，且有 data.items 数组。"""
    with patch("app.api.recommend.RecommendService") as mock_svc:
        instance = mock_svc.return_value
        instance.recommend_metrics = AsyncMock(return_value=[_ALLOWED_ITEM])
        app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
            id=7, role=role, roles_all=lambda: [role], has_role=lambda r: r == role
        )

        resp = await client.get("/api/v1/recommend/metrics?limit=6")

    assert resp.status_code == 200, f"角色 {role} 不应被拒绝：{resp.text}"
    body = resp.json()
    assert body["code"] == "OK"
    assert isinstance(body["data"]["items"], list)
    assert body["data"]["items"][0]["metric_id"] == _ALLOWED_ITEM["metric_id"]


async def test_recommend_metrics_uses_authenticated_user_id(client) -> None:
    """PLAT-2 回归：服务以认证用户 id 调用，杜绝客户端传 user_id 越权。"""
    with patch("app.api.recommend.RecommendService") as mock_svc:
        instance = mock_svc.return_value
        instance.recommend_metrics = AsyncMock(return_value=[_ALLOWED_ITEM])
        app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
            id=42,
            role="viewer",
            roles_all=lambda: ["viewer"],
            has_role=lambda r: r == "viewer",
            domain="sales",
        )

        await client.get("/api/v1/recommend/metrics?limit=5")

    # 非 platform_admin 时须携带本域收敛（P1-5），mock 缺 domain 曾返回 MagicMock 致断言失真
    instance.recommend_metrics.assert_awaited_once_with(42, 5, domain="sales")


async def test_recommend_metrics_analyst_forbidden(client) -> None:
    """负向对照：analyst 不在读权限列表，必须返回 403。"""
    with patch("app.api.recommend.RecommendService"):
        app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
            id=1, role="analyst", roles_all=lambda: ["analyst"], has_role=lambda r: r == "analyst"
        )

        resp = await client.get("/api/v1/recommend/metrics")

    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_recommend_terms_role_allowed_for_viewer(client) -> None:
    """同路由组：GET /recommend/terms 对 viewer 开放（同 _READ_ROLES）。"""
    from app.models.term import Term
    from app.services.glossary.schemas import TermResponse

    term = Term(
        id=1,
        term_code="t1",
        name="n",
        definition="d",
        domain="x",
        synonyms=[],
        status="PUBLISHED",
        owner_id=1,
    )
    with patch("app.api.recommend.RecommendService") as mock_svc:
        instance = mock_svc.return_value
        instance.recommend_terms = AsyncMock(return_value=[TermResponse.from_model(term)])
        app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
            id=1,
            role="viewer",
            roles_all=lambda: ["viewer"],
            has_role=lambda r: r == "viewer",
            domain="sales",
        )

        resp = await client.get("/api/v1/recommend/terms")

    assert resp.status_code == 200
    assert resp.json()["data"]["items"][0]["term_code"] == "t1"
    # P1-5 术语域收敛：非 platform_admin 须携带本域
    instance.recommend_terms.assert_awaited_once_with(20, domain="sales")


async def test_recommend_terms_platform_admin_no_domain(client) -> None:
    """P1-5：platform_admin 不限域，/recommend/terms 调用不传 domain。"""
    from app.models.term import Term
    from app.services.glossary.schemas import TermResponse

    term = Term(
        id=1,
        term_code="t1",
        name="n",
        definition="d",
        domain="x",
        synonyms=[],
        status="PUBLISHED",
        owner_id=1,
    )
    with patch("app.api.recommend.RecommendService") as mock_svc:
        instance = mock_svc.return_value
        instance.recommend_terms = AsyncMock(return_value=[TermResponse.from_model(term)])
        app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
            id=1,
            role="platform_admin",
            roles_all=lambda: ["platform_admin"],
            has_role=lambda r: r == "platform_admin",
        )

        resp = await client.get("/api/v1/recommend/terms")

    assert resp.status_code == 200
    instance.recommend_terms.assert_awaited_once_with(20, domain=None)
