"""指标目录增强的后端契约测试。

覆盖本次前端「生产级检索」配套的后端改动：
1. /auth/users 只读用户列表（Owner 责任链渲染，不暴露 email/password_hash）
2. /audit 支持 entity_id 过滤（变更审计时间线）
3. /metric-definitions 列表排序（sort_by/sort_order 白名单防注入）
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from app.models.audit import AuditLog
from app.models.user import User
from app.services.semantic.repository import MetricRepository


@pytest.fixture
async def users_client() -> AsyncIterator[httpx.AsyncClient]:
    """覆盖 DB 会话与当前用户依赖（任意登录角色）。"""

    async def fake_db():
        session = MagicMock()
        rows = [
            User(
                id=1,
                org_id=1,
                username="admin",
                email="admin@x.com",
                password_hash="hash1",
                display_name="管理员",
                role="platform_admin",
                domain=None,
                status="active",
            ),
            User(
                id=2,
                org_id=1,
                username="owner",
                email="owner@x.com",
                password_hash="hash2",
                display_name="指标负责人",
                role="metric_owner",
                domain="sales",
                status="active",
            ),
        ]
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        session.execute = AsyncMock(return_value=result)
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="viewer", roles_all=lambda: ["viewer"], has_role=lambda r: r == "viewer"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_list_users_returns_brief_without_sensitive_fields(
    users_client: httpx.AsyncClient,
) -> None:
    resp = await users_client.get("/api/v1/auth/users")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 2
    assert data[0]["username"] == "admin"
    assert data[0]["role"] == "platform_admin"
    assert data[1]["display_name"] == "指标负责人"
    # 绝不暴露 email / password_hash
    assert "email" not in data[0]
    assert "password_hash" not in data[0]
    assert "email" not in data[1]


async def test_list_users_filters_by_role(users_client: httpx.AsyncClient) -> None:
    resp = await users_client.get("/api/v1/auth/users?role=metric_owner")
    assert resp.status_code == 200


# ---- audit entity_id 过滤 ----


@pytest.fixture
async def audit_entity_client() -> AsyncIterator[httpx.AsyncClient]:
    async def fake_db():
        session = MagicMock()
        # 新版查询：count（scalar_one）+ 主查询（join User 后返回 (log, display_name) 元组行）
        count_result = MagicMock()
        count_result.scalar_one.return_value = 1
        rows = [
            (
                AuditLog(
                    id=1,
                    actor_id=1,
                    action="CREATE",
                    entity_type="metric_definition",
                    entity_id="sales_gmv_sum_d",
                    ip="x",
                    trace_id="t",
                    pii_access=False,
                ),
                "管理员",
            ),
        ]
        rows_result = MagicMock()
        rows_result.all.return_value = rows
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_audit_accepts_entity_id_filter(audit_entity_client: httpx.AsyncClient) -> None:
    resp = await audit_entity_client.get("/api/v1/audit?entity_id=sales_gmv_sum_d")
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert items and items[0]["entity_id"] == "sales_gmv_sum_d"


# ---- repository 排序白名单 ----


def _mk_session() -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.scalar.return_value = 1
    result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    return session


async def test_list_metrics_honors_sort_by_version_asc() -> None:
    db = _mk_session()
    repo = MetricRepository(db)
    await repo.list_metrics(sort_by="version", sort_order="asc", offset=0, limit=10)
    # 第二次 execute 是列表查询，含 ORDER BY version ASC
    stmt = db.execute.await_args_list[1].args[0]
    sql = str(stmt.compile())
    assert "ORDER BY metric.version" in sql
    assert "ASC" in sql


async def test_list_metrics_falls_back_to_updated_at_on_bad_sort() -> None:
    db = _mk_session()
    repo = MetricRepository(db)
    await repo.list_metrics(sort_by="evil_col", sort_order="desc", offset=0, limit=10)
    stmt = db.execute.await_args_list[1].args[0]
    sql = str(stmt.compile())
    # 非法字段回退 updated_at（白名单防注入）
    assert "ORDER BY metric.updated_at" in sql
    assert "evil_col" not in sql


# ---- 列表接口健康度回填（目录页"健康"列）----


def _metric_snapshot() -> SimpleNamespace:
    """构造一个可通过 MetricResponse.model_validate 的最小指标对象。"""
    return SimpleNamespace(
        id=1,
        metric_code="sales_gmv_sum_d",
        name="销售 GMV",
        domain="sales",
        type="atomic",
        granularity="day",
        # OneData 原子层：关联逻辑度量（度量目录）
        measure_id=1,
        unit="元",
        currency=None,
        aggregation="SUM",
        time_semantics="PERIOD",
        freshness="T1",
        sla=None,
        dw_layer="DWS",
        metric_tier="T1",
        serving_mode="BATCH_ONLY",
        additivity="ADDITIVE",
        non_additive_dimensions=None,
        definition_json={"expression": "sum(gmv)"},
        version=1,
        row_version=1,
        status="PUBLISHED",
        owner_id=1,
        backup_owner_id=None,
        approver_id=None,
        submitted_by=None,
        pii_flag=False,
        compliance_reviewed=True,
        effective_version=1,
        consumption_guide=None,
        successor_code=None,
        deprecated_at=None,
        sunset_until=None,
        emergency_publish=False,
        emergency_reason=None,
        gray_tenant_ids=None,
        pending_conflict=False,
        pending_conflict_detail=None,
        pending_version=False,
        created_at=datetime(2026, 8, 1),
        updated_at=datetime(2026, 8, 2),
    )


async def test_list_metrics_enriches_health_score_from_table() -> None:
    """GET /metric-definitions 应经 metric_health_score 批量回填 health_score/level。"""

    async def fake_db():
        session = MagicMock()

        async def fake_execute(statement, *args, **kwargs):
            sql = str(statement.compile(dialect=None))
            result = MagicMock()
            if "pending_version_confirmation" in sql:
                result.scalars.return_value.all.return_value = []
            elif "metric_health_score" in sql:
                # 模拟 SQLAlchemy Row（支持属性访问）
                result.all.return_value = [SimpleNamespace(metric_id=1, score=78, level="GOOD")]
            else:
                result.scalars.return_value.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=fake_execute)
        session.commit = AsyncMock()
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="viewer", roles_all=lambda: ["viewer"], has_role=lambda r: r == "viewer"
    )
    with patch(
        "app.api.metrics.MetricService.list_metrics",
        new=AsyncMock(return_value=([_metric_snapshot()], 1)),
    ):
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/api/v1/metric-definitions")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    item = resp.json()["data"]["items"][0]
    assert item["health_score"] == 78
    assert item["health_level"] == "GOOD"
