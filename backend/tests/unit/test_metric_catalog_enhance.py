"""指标目录增强的后端契约测试。

覆盖本次前端「生产级检索」配套的后端改动：
1. /auth/users 只读用户列表（Owner 责任链渲染，不暴露 email/password_hash）
2. /audit 支持 entity_id 过滤（变更审计时间线）
3. /metric-definitions 列表排序（sort_by/sort_order 白名单防注入）
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

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
            User(id=1, org_id=1, username="admin", email="admin@x.com",
                 password_hash="hash1", display_name="管理员",
                 role="platform_admin", domain=None, status="active"),
            User(id=2, org_id=1, username="owner", email="owner@x.com",
                 password_hash="hash2", display_name="指标负责人",
                 role="metric_owner", domain="sales", status="active"),
        ]
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        session.execute = AsyncMock(return_value=result)
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="viewer"
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
                AuditLog(id=1, actor_id=1, action="CREATE", entity_type="metric_definition",
                         entity_id="sales_gmv_sum_d", ip="x", trace_id="t", pii_access=False),
                "管理员",
            ),
        ]
        rows_result = MagicMock()
        rows_result.all.return_value = rows
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="platform_admin"
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
