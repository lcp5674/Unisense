"""权限基线错位修复回归测试（assetmap/ai/observability/collector/lineage/metric-ops）。

覆盖：
- assetmap /export.csv：对齐 assetmap:export 基线（platform_admin/domain_admin），
  viewer 此前可经 _READ_DEPS 全量导出资产清单，现 403。
- ai /nl2sql：对齐 ai:nl2sql 基线（platform_admin/domain_admin/metric_owner），
  viewer 此前可调（_READ_DEPS 曾用 _WRITE_ROLES 含 viewer），现 403。
- observability /feedback：用户自助提交反馈，analyst 此前被 _WRITE_ROLES 排除
  （页面可点但 403），现 200。
- observability 运营统计（/overview 等）：对齐 observability:view 基线（仅
  平台/域管理员），viewer/reviewer/metric_owner 此前可直调拉取全局 OPS 遥测，现 403。
- collector 采集运维（/collection-runs/summary 等）：对齐 data-sources:view/
  collection-tasks:view/collection-history:view 基线（平台/域管理员/指标负责人），
  viewer 此前可直调读资产规模/采集运行状态，现 403；/data-sources 列表保留全员
  （查询工作台/维度映射的源选择器，已 org 收敛 + 凭据脱敏）。
- metrics 口径一致率/评测（/metric-definitions/consistency/stats 等）：对齐
  metric:create 基线，viewer 此前可读部门间冲突数，现 403。
- lineage 血缘（/lineage/edges 等）：对齐 lineage:view 基线（平台/域管理员、
  指标负责人、合规、分析师），viewer/reviewer 无页面入口也不得直调，现 403。
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
    u.domain = None
    u.org_id = 1
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


async def test_observability_overview_denied_for_viewer() -> None:
    """viewer 无 observability:view：全局 OPS 遥测（依赖熔断/风险雷达/审计计数）被拒。"""
    async with _client(_make_user("viewer")) as c:
        resp = await c.get("/api/v1/observability/overview")
    app.dependency_overrides.clear()
    assert resp.status_code == 403


async def test_observability_overview_denied_for_metric_owner() -> None:
    """metric_owner 无 observability:view：运营统计同样被拒（此前可直调）。"""
    async with _client(_make_user("metric_owner")) as c:
        resp = await c.get("/api/v1/observability/overview")
    app.dependency_overrides.clear()
    assert resp.status_code == 403


async def test_observability_overview_allowed_for_domain_admin() -> None:
    """domain_admin 有 observability:view：放行（mock 服务返回值）。"""
    with patch(
        "app.api.observability.ObservabilityService",
        lambda db: MagicMock(overview_stats=AsyncMock(return_value={"assets": {}})),
    ):
        async with _client(_make_user("domain_admin")) as c:
            resp = await c.get("/api/v1/observability/overview")
    app.dependency_overrides.clear()
    assert resp.status_code == 200


async def test_observability_feedback_list_denied_for_viewer() -> None:
    """viewer 无 feedback:view：不可读他人反馈列表（org 级反馈视图）。"""
    async with _client(_make_user("viewer")) as c:
        resp = await c.get("/api/v1/observability/feedback")
    app.dependency_overrides.clear()
    assert resp.status_code == 403


async def test_observability_metric_health_allowed_for_viewer() -> None:
    """viewer 可读指标健康度摘要（总览仪表卡片数据源，P0-3 收敛，非 OPS 遥测）。"""
    with patch(
        "app.api.observability.ObservabilityService",
        lambda db: MagicMock(metric_health_stats=AsyncMock(return_value={"metric_health": {}})),
    ):
        async with _client(_make_user("viewer")) as c:
            resp = await c.get("/api/v1/observability/metrics/health")
    app.dependency_overrides.clear()
    assert resp.status_code == 200


async def test_observability_nps_stats_denied_for_viewer() -> None:
    """viewer 无 feedback:view：NPS 分布统计被拒。"""
    async with _client(_make_user("viewer")) as c:
        resp = await c.get("/api/v1/observability/nps/stats")
    app.dependency_overrides.clear()
    assert resp.status_code == 403


async def test_observability_nps_stats_allowed_for_metric_owner() -> None:
    """metric_owner 有 feedback:view（反馈中心展示 NPS）：放行。"""
    with patch(
        "app.api.observability.ObservabilityService",
        lambda db: MagicMock(nps_stats=AsyncMock(return_value={"score": 0})),
    ):
        async with _client(_make_user("metric_owner")) as c:
            resp = await c.get("/api/v1/observability/nps/stats")
    app.dependency_overrides.clear()
    assert resp.status_code == 200


async def test_collector_run_summary_denied_for_viewer() -> None:
    """viewer 无 collection-history:view：采集运行汇总（状态/成功率）被拒。"""
    async with _client(_make_user("viewer")) as c:
        resp = await c.get("/api/v1/collection-runs/summary")
    app.dependency_overrides.clear()
    assert resp.status_code == 403


async def test_collector_run_summary_allowed_for_metric_owner() -> None:
    """metric_owner 有 collection-history:view：放行（mock 服务返回值）。"""
    with patch(
        "app.api.collector.CollectorService",
        lambda db: MagicMock(get_collection_run_summary=AsyncMock(return_value={})),
    ):
        async with _client(_make_user("metric_owner")) as c:
            resp = await c.get("/api/v1/collection-runs/summary")
    app.dependency_overrides.clear()
    assert resp.status_code == 200


async def test_collector_sources_browse_allowed_for_viewer() -> None:
    """viewer 仍可浏览数据源列表（查询工作台/维度映射源选择器；org 收敛+脱敏）。"""
    with patch(
        "app.api.collector.CollectorService",
        lambda db: MagicMock(list_sources=AsyncMock(return_value=([], 0))),
    ):
        async with _client(_make_user("viewer")) as c:
            resp = await c.get("/api/v1/data-sources")
    app.dependency_overrides.clear()
    assert resp.status_code == 200


async def test_consistency_stats_denied_for_viewer() -> None:
    """viewer 无 metric:create：口径一致率/部门间冲突统计被拒。"""
    async with _client(_make_user("viewer")) as c:
        resp = await c.get("/api/v1/metric-definitions/consistency/stats")
    app.dependency_overrides.clear()
    assert resp.status_code == 403


async def test_consistency_stats_allowed_for_metric_owner() -> None:
    """metric_owner 有 metric:create：放行（mock 冲突仓库返回值）。"""
    with patch(
        "app.api.metrics.ConflictRepository",
        lambda db: MagicMock(consistency_stats=AsyncMock(return_value={"total": 0})),
    ):
        async with _client(_make_user("metric_owner")) as c:
            resp = await c.get("/api/v1/metric-definitions/consistency/stats")
    app.dependency_overrides.clear()
    assert resp.status_code == 200


async def test_lineage_edges_denied_for_viewer() -> None:
    """viewer 无 lineage:view：血缘图/边直调被拒。"""
    async with _client(_make_user("viewer")) as c:
        resp = await c.get("/api/v1/lineage/edges?limit=10")
    app.dependency_overrides.clear()
    assert resp.status_code == 403


async def test_lineage_edges_allowed_for_analyst() -> None:
    """analyst 有 lineage:view：放行（mock 服务返回值）。"""
    svc = MagicMock(
        node_meta=AsyncMock(return_value=[]),
        list_edges=AsyncMock(return_value=[]),
    )
    with patch("app.api.lineage.LineageService", lambda *a, **kw: svc):
        async with _client(_make_user("analyst")) as c:
            resp = await c.get("/api/v1/lineage/edges?node=metric:sales_gmv_day&limit=10")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
