"""权限基线错位修复回归测试（assetmap/ai/observability）。

覆盖：
- assetmap /export.csv：对齐 assetmap:export 基线（platform_admin/domain_admin），
  viewer 此前可经 _READ_DEPS 全量导出资产清单，现 403。
- ai /nl2sql：对齐 ai:nl2sql 基线（platform_admin/domain_admin/metric_owner），
  viewer 此前可调（_READ_DEPS 曾用 _WRITE_ROLES 含 viewer），现 403。
- observability /feedback：用户自助提交反馈，analyst 此前被 _WRITE_ROLES 排除
  （页面可点但 403），现 200。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from httpx import ASGITransport

from app.api import deps
from app.main import app


def _make_user(role: str) -> MagicMock:
    from app.models.user import User

    u = MagicMock(spec=User, id=11, username=f"u_{role}")
    u.role = role
    u.roles_all.return_value = [role]
    u.has_role.side_effect = lambda r: r == role
    return u


def _client(user: MagicMock) -> httpx.AsyncClient:
    async def fake_db():
        s = MagicMock()
        s.commit = AsyncMock()
        yield s

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: user
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_assetmap_export_denied_for_viewer() -> None:
    """viewer 无 assetmap:export：资产清单导出被拒（403）。"""
    async with _client(_make_user("viewer")) as c:
        resp = await c.get("/api/v1/assetmap/export.csv")
    app.dependency_overrides.clear()
    assert resp.status_code == 403


async def test_ai_nl2sql_denied_for_viewer() -> None:
    """viewer 无 ai:nl2sql：AI 转 SQL 被拒（403）。"""
    async with _client(_make_user("viewer")) as c:
        resp = await c.post("/api/v1/ai/nl2sql", json={"nl_query": "统计门诊量"})
    app.dependency_overrides.clear()
    assert resp.status_code == 403


async def test_ai_nl2sql_allowed_for_metric_owner() -> None:
    """metric_owner 有 ai:nl2sql：放行。"""

    class _FakeAiService:
        @staticmethod
        def _is_unsafe(_q: str) -> bool:
            return False

        def __init__(self, _db, llm=None) -> None:
            self.ask = AsyncMock(return_value={"safe": True, "method": "llm", "sql": "SELECT 1"})

        async def close(self) -> None:
            return None

    with (
        patch(
            "app.api.ai.LlmConfigService",
            lambda db: MagicMock(build_client=AsyncMock(return_value=object())),
        ),
        patch("app.api.ai.AiService", _FakeAiService),
    ):
        async with _client(_make_user("metric_owner")) as c:
            resp = await c.post("/api/v1/ai/nl2sql", json={"nl_query": "统计门诊量"})
    app.dependency_overrides.clear()
    assert resp.status_code == 200


async def test_observability_feedback_allowed_for_analyst() -> None:
    """analyst 可提交反馈（用户自助，此前 403）。"""
    from types import SimpleNamespace


    fb = SimpleNamespace(
        id=1, user_id=11, target_type="metric", target_id="sales_gmv_day", target_name="销售GMV",
        rating=None, nps_score=None, category="feature", priority="medium", source_url=None,
        comment="希望支持更多图表", status="OPEN", clarification=None, clarified_at=None,
        resolution_note=None, resolver_id=None, resolved_at=None, created_at=None,
    )
    with patch(
        "app.api.observability.ObservabilityService",
        lambda db: MagicMock(submit_feedback=AsyncMock(return_value=fb)),
    ):
        async with _client(_make_user("analyst")) as c:
            resp = await c.post(
                "/api/v1/observability/feedback",
                json={
                    "target_type": "metric", "comment": "希望支持更多图表", "category": "FEATURE"
                },
            )
    app.dependency_overrides.clear()
    assert resp.status_code == 201
