"""assetmap 安全测试（对齐 gateways security_reverse，TD §13）。

覆盖读端点 RBAC/注入守卫 + 写能力端点（认领/重分类/批量）RBAC 闸门、
成功路径与 404/422 边界（FR-18 资产工作台）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from app.services.assetmap.service import AssetMapService


def _session() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    s.commit = AsyncMock()
    s.rollback = AsyncMock()
    s.flush = AsyncMock()
    s.refresh = AsyncMock()
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


@pytest.fixture
async def writer_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(9, "metric_owner"):
        yield c


@pytest.fixture
async def reader_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(11, "viewer"):
        yield c


async def test_summary_blocks_sql_injection_400(writer_client: httpx.AsyncClient) -> None:
    """资产地图读端点 SQL 注入被守卫拦截。"""
    resp = await writer_client.get("/api/v1/assetmap/summary", params={"status": "' OR '1'='1"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "INJECTION_DETECTED"


async def test_summary_returns_200_for_reader(
    reader_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已认证读者可正常读取资产地图。"""

    async def fake(self: AssetMapService) -> dict:
        return {"total": 0, "by_domain": {}, "by_tier": {}, "by_layer": {}}

    monkeypatch.setattr(AssetMapService, "catalog_summary", fake)
    resp = await reader_client.get("/api/v1/assetmap/summary")
    assert resp.status_code == 200


async def test_tables_blocks_unauthorized_role_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLAT-1: 不在 _READ_ROLES 中的角色被 RBAC 闸门拦截。"""
    from app.api import deps

    async def fake_db():
        yield MagicMock()

    app.dependency_overrides[deps.get_db_session] = fake_db
    # 角色 "guest" 不在 _READ_ROLES 白名单内
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=99, role="guest", domain="sales"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/assetmap/tables")
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


# ---- 产品补充端点（FR-18 生产化）----


async def test_search_returns_200(
    reader_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全局搜索端点对已认证读者返回 200。"""

    async def fake(self: AssetMapService, q: str, entity_type=None, limit: int = 20) -> list:
        return [{"type": "metric", "id": 1, "name": "sales_gmv_amount_day"}]

    monkeypatch.setattr(AssetMapService, "search_assets", fake)
    resp = await reader_client.get("/api/v1/assetmap/search", params={"q": "sales"})
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["type"] == "metric"


async def test_search_blocks_injection_400(writer_client: httpx.AsyncClient) -> None:
    """搜索关键词注入被守卫拦截。"""
    resp = await writer_client.get("/api/v1/assetmap/search", params={"q": "' OR '1'='1"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "INJECTION_DETECTED"


async def test_health_pii_changes_my_assets_200(
    reader_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """健康/PII/变更/我的资产四个新端点对读者返回 200。"""

    async def fake_health(self: AssetMapService) -> dict:
        return {
            "unhealthy_sources": [],
            "schema_incomplete": [],
            "orphan_assets": 0,
            "stale_assets": [],
            "stale_days": 7,
        }

    async def fake_pii(self: AssetMapService) -> dict:
        return {
            "by_sensitivity": {},
            "by_domain": {},
            "pii_metric_count": 0,
            "pii_catalog_count": 0,
        }

    async def fake_changes(self: AssetMapService, days: int = 7, limit: int = 50) -> dict:
        return {"catalogs": [], "metrics": [], "days": days}

    async def fake_mine(self: AssetMapService, owner_id: int, limit: int = 50) -> dict:
        return {"owner_id": owner_id, "catalogs": [], "metrics": []}

    monkeypatch.setattr(AssetMapService, "health_summary", fake_health)
    monkeypatch.setattr(AssetMapService, "pii_overview", fake_pii)
    monkeypatch.setattr(AssetMapService, "recent_changes", fake_changes)
    monkeypatch.setattr(AssetMapService, "my_assets", fake_mine)

    assert (await reader_client.get("/api/v1/assetmap/health")).status_code == 200
    assert (await reader_client.get("/api/v1/assetmap/pii")).status_code == 200
    assert (await reader_client.get("/api/v1/assetmap/changes?days=7")).status_code == 200
    assert (await reader_client.get("/api/v1/assetmap/my-assets")).status_code == 200


async def test_export_csv_returns_csv(
    reader_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CSV 导出端点返回 text/csv 与表头。"""
    from app.models.data_source import DBCatalog

    async def fake_export(self: AssetMapService, source_id, sensitivity, **kwargs) -> list[dict]:
        # export_tables 后续演进新增 domain/owner_id/schema_status/keyword 过滤参数（**kwargs 兼容）
        row = DBCatalog(
            source_id="s", entity_name="catalog.db.t", entity_type="table", schema_json={}
        )
        d = row.to_dict()
        d["sensitivity_level"] = "INTERNAL"
        return [d]

    monkeypatch.setattr(AssetMapService, "export_tables", fake_export)
    resp = await reader_client.get("/api/v1/assetmap/export.csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "entity_name" in resp.text
    assert "catalog.db.t" in resp.text


# ---- 写能力端点（FR-18 资产工作台）：认领/重分类/批量——RBAC 闸门 ----
# _WRITE_ROLES = (platform_admin, domain_admin)；viewer 等非写角色须 403。


@pytest.fixture
async def admin_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(1, "platform_admin"):
        yield c


async def test_assign_owner_blocks_viewer_403(reader_client: httpx.AsyncClient) -> None:
    """非写角色（viewer）调用认领端点 → 403。"""
    resp = await reader_client.post("/api/v1/assetmap/entities/1/owner", json={"owner_id": 9})
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_assign_owner_success_admin(
    admin_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """platform_admin 认领归属 → 200，且写入审计（PLAT-3 原子提交）。"""

    async def fake(self: AssetMapService, entity_id: int, owner_id: int | None) -> dict:
        return {"entity_id": entity_id, "owner_id": owner_id}

    monkeypatch.setattr(AssetMapService, "assign_owner", fake)
    resp = await admin_client.post("/api/v1/assetmap/entities/1/owner", json={"owner_id": 9})
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["entity_id"] == 1
    assert body["owner_id"] == 9


async def test_assign_owner_entity_missing_404(
    admin_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """资产不存在 → 404（service 抛 NotFoundError 透传）。"""
    from app.core.exceptions import NotFoundError

    async def fake(self: AssetMapService, entity_id: int, owner_id: int | None) -> dict:
        raise NotFoundError(f"资产不存在: {entity_id}")

    monkeypatch.setattr(AssetMapService, "assign_owner", fake)
    resp = await admin_client.post("/api/v1/assetmap/entities/999/owner", json={"owner_id": 9})
    assert resp.status_code == 404


async def test_reclassify_blocks_viewer_403(reader_client: httpx.AsyncClient) -> None:
    """非写角色（viewer）调用重分类端点 → 403。"""
    resp = await reader_client.post(
        "/api/v1/assetmap/entities/1/sensitivity", json={"sensitivity_level": "PII"}
    )
    assert resp.status_code == 403


async def test_reclassify_success_admin(
    admin_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """platform_admin 重分类敏感级 → 200。"""

    async def fake(self: AssetMapService, entity_id: int, level: str) -> dict:
        return {"entity_id": entity_id, "sensitivity_level": level}

    monkeypatch.setattr(AssetMapService, "reclassify_sensitivity", fake)
    resp = await admin_client.post(
        "/api/v1/assetmap/entities/1/sensitivity", json={"sensitivity_level": "PII"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["sensitivity_level"] == "PII"


async def test_batch_owner_blocks_viewer_403(reader_client: httpx.AsyncClient) -> None:
    """非写角色（viewer）调用批量认领 → 403。"""
    resp = await reader_client.post(
        "/api/v1/assetmap/batch-owner", json={"entity_ids": [1, 2], "owner_id": 9}
    )
    assert resp.status_code == 403


async def test_batch_reclassify_success_admin(
    admin_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """platform_admin 批量重分类 → 200，且返回受影响数。"""

    async def fake(self: AssetMapService, entity_ids: list[int], level: str) -> dict:
        return {"affected": len(entity_ids), "sensitivity_level": level, "total": len(entity_ids)}

    monkeypatch.setattr(AssetMapService, "batch_reclassify", fake)
    resp = await admin_client.post(
        "/api/v1/assetmap/batch-sensitivity",
        json={"entity_ids": [1, 2], "sensitivity_level": "CONFIDENTIAL"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["affected"] == 2
    assert resp.json()["data"]["sensitivity_level"] == "CONFIDENTIAL"


async def test_batch_owner_invalid_payload_422(
    admin_client: httpx.AsyncClient,
) -> None:
    """批量认领空列表 → 422（schema min_length 校验）。"""
    resp = await admin_client.post(
        "/api/v1/assetmap/batch-owner", json={"entity_ids": [], "owner_id": 9}
    )
    assert resp.status_code == 422
