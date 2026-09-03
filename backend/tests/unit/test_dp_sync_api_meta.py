"""dp 血缘同步元数据/排除预览 API 测试。

覆盖：/meta 无配置返回内置+reason、有配置但源不可达降级、/exclude-preview
参数校验错误、源不可达明确信号、命中统计（含正则非法逐条报告）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.api import lineage_dp_sync as dp_api
from app.main import app


class _FakeDpCollector:
    """假 dp 连接器（query 返回 out_table 行）。"""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.disposed = False

    async def query(self, sql: str, params: dict | None = None):
        return list(self.rows)

    async def dispose(self) -> None:
        self.disposed = True


@pytest.fixture
async def dp_client() -> AsyncIterator[httpx.AsyncClient]:
    """覆盖 DB 会话与当前用户（平台管理员）。"""

    async def fake_db() -> AsyncIterator[MagicMock]:
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock())
        session.commit = AsyncMock()
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        org_id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_meta_without_config_returns_builtin(dp_client: httpx.AsyncClient) -> None:
    """未配置 dp 源：/meta 返回内置类型 + not_configured reason（不编造枚举）。"""
    with patch.object(
        dp_api.DpLineageRepository, "get_config", new=AsyncMock(return_value=None)
    ):
        resp = await dp_client.get("/api/v1/lineage/dp-sync/meta")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["reachable"] is False
    assert data["reason"] == "not_configured"
    assert [t["value"] for t in data["task_types"]] == [1]
    assert data["task_types"][0]["label"] == "SQL 任务"
    assert [s["value"] for s in data["step_types"]] == [2, 7]
    assert any("tmp_" in p for p in data["exclude_defaults"])


async def test_meta_unreachable_degrades_gracefully(
    dp_client: httpx.AsyncClient,
) -> None:
    """配置了 dp 源但不可达：/meta 返回内置 + 明确 reason（不 500）。"""
    cfg = SimpleNamespace(source_id="dp", schema_name="dp_stable")
    with (
        patch.object(
            dp_api.DpLineageRepository, "get_config", new=AsyncMock(return_value=cfg)
        ),
        patch.object(dp_api, "_collector_factory", new=_unreachable_factory),
    ):
        resp = await dp_client.get("/api/v1/lineage/dp-sync/meta")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["reachable"] is False
    assert "不可达" in data["reason"]
    assert [t["value"] for t in data["task_types"]] == [1]


async def test_exclude_preview_requires_source(dp_client: httpx.AsyncClient) -> None:
    """未配置 source_id 且未传：/exclude-preview 返回干净 VALIDATION_ERROR。"""
    with patch.object(
        dp_api.DpLineageRepository, "get_config", new=AsyncMock(return_value=None)
    ):
        resp = await dp_client.post(
            "/api/v1/lineage/dp-sync/exclude-preview",
            json={"patterns": [r"(^|\.)tmp_"]},
        )
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "source_id" in body["message"]


async def test_exclude_preview_unreachable(dp_client: httpx.AsyncClient) -> None:
    """dp 源不可达：明确 SOURCE_UNREACHABLE 信号而非 500。"""
    cfg = SimpleNamespace(source_id="dp", schema_name="dp_stable")
    with (
        patch.object(
            dp_api.DpLineageRepository, "get_config", new=AsyncMock(return_value=cfg)
        ),
        patch.object(dp_api, "_collector_factory", new=_unreachable_factory),
    ):
        resp = await dp_client.post(
            "/api/v1/lineage/dp-sync/exclude-preview",
            json={"patterns": [r"(^|\.)tmp_"]},
        )
    body = resp.json()
    assert body["code"] == "SOURCE_UNREACHABLE"
    assert body["data"]["reachable"] is False


async def test_exclude_preview_counts_and_samples(
    dp_client: httpx.AsyncClient,
) -> None:
    """命中统计：对 out_table 全集统计匹配表数与样例；正则非法逐条报告。"""
    cfg = SimpleNamespace(source_id="dp", schema_name="dp_stable")
    collector = _FakeDpCollector(
        [{"t": "wedw_dwd.dp_out"}, {"t": "wedw_dwd.tmp_x"}, {"t": "wedw_ods.tbl_bak"}]
    )

    async def factory(db):
        async def fetch(source_id):
            return collector

        return fetch

    with (
        patch.object(
            dp_api.DpLineageRepository, "get_config", new=AsyncMock(return_value=cfg)
        ),
        patch.object(dp_api, "_collector_factory", new=factory),
    ):
        resp = await dp_client.post(
            "/api/v1/lineage/dp-sync/exclude-preview",
            json={"patterns": [r"(^|\.)tmp_", r"_bak$", "("]},
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["reachable"] is True
    assert data["total"] == 3
    assert data["matched"] == 2  # tmp_x + tbl_bak
    assert {s["table"] for s in data["samples"]} == {
        "wedw_dwd.tmp_x",
        "wedw_ods.tbl_bak",
    }
    assert len(data["invalid_patterns"]) == 1
    assert data["invalid_patterns"][0]["pattern"] == "("
    assert collector.disposed is True


async def _unreachable_factory(db):
    """构造「dp 源不可达」的 fetch_collector。"""

    async def fetch(source_id):
        raise RuntimeError("Can't connect to dp")

    return fetch
