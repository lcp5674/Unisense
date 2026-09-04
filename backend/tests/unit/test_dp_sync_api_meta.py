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
    assert [t["value"] for t in data["task_types"]] == [0, 1, 3, 4, 10, 15]
    assert {t["value"]: t["label"] for t in data["task_types"]}[1] == "数据抽取（SQL 加工）"
    assert [s["value"] for s in data["step_types"]] == [2, 3, 4, 5, 6, 7, 9, 15]
    assert {s["value"]: s["label"] for s in data["step_types"]}[7] == "Hive/Spark SQL"
    assert any("tmp_" in p for p in data["exclude_defaults"])


async def test_meta_unreachable_degrades_gracefully(
    dp_client: httpx.AsyncClient,
) -> None:
    """配置了 dp 源但不可达：/meta 返回内置 + 明确 reason（不 500）。"""
    cfg = SimpleNamespace(
        source_id="dp",
        schema_name="dp_stable",
        task_table="dispatch_task",
        step_table="dispatch_task_step",
    )
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
    assert [t["value"] for t in data["task_types"]] == [0, 1, 3, 4, 10, 15]
    assert all("未识别" not in t["label"] for t in data["task_types"])


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
    cfg = SimpleNamespace(
        source_id="dp",
        schema_name="dp_stable",
        task_table="dispatch_task",
        step_table="dispatch_task_step",
    )
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
    cfg = SimpleNamespace(
        source_id="dp",
        schema_name="dp_stable",
        task_table="dispatch_task",
        step_table="dispatch_task_step",
    )
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


async def test_resolve_ticket_unknown_resolution_returns_validation(
    dp_client: httpx.AsyncClient,
) -> None:
    """未知裁决方式（service 抛 ValueError）应返回 VALIDATION_ERROR 而非 500。

    回归：此前 API 只 catch LookupError，ValueError 逃逸成 500（P0-2）。
    """
    from app.services.lineage import dp_sync_service as dp_svc_mod

    with patch.object(
        dp_svc_mod.DpSyncService,
        "resolve_ticket",
        new=AsyncMock(
            side_effect=ValueError("未知裁决方式: nuke")
        ),
    ):
        resp = await dp_client.post(
            "/api/v1/lineage/dp-sync/tickets/1/resolve",
            json={"resolution": "nuke"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "未知裁决方式" in body["message"]


async def test_update_config_rejects_invalid_ident(
    dp_client: httpx.AsyncClient,
) -> None:
    """保存配置时非法 schema/表名标识符 → VALIDATION_ERROR（注入面防御）。

    回归（P2-9）：配置表名经 f-string 拼 SQL，此前未在保存时校验。
    """
    cfg = SimpleNamespace(
        source_id="dp",
        schema_name="dp_stable",
        task_table="dispatch_task",
        step_table="dispatch_task_step",
    )
    cfg.id = 1
    cfg.to_dict = lambda: {"source_id": "dp"}
    with patch.object(
        dp_api.DpLineageRepository,
        "get_config",
        new=AsyncMock(side_effect=[cfg, cfg]),
    ):
        resp = await dp_client.put(
            "/api/v1/lineage/dp-sync/config",
            json={"task_table": "dispatch_task; DROP TABLE x"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "合法标识符" in body["message"]


async def test_exclude_preview_rejects_invalid_schema(
    dp_client: httpx.AsyncClient,
) -> None:
    """排除预览非法 schema → VALIDATION_ERROR（不拼进 SQL）。"""
    resp = await dp_client.post(
        "/api/v1/lineage/dp-sync/exclude-preview",
        json={"source_id": "dp", "schema_name": "dp_stable; DROP", "patterns": ["tmp"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "合法标识符" in body["message"]


async def test_resolve_llm_disabled_endpoint(dp_client: httpx.AsyncClient) -> None:
    """一键处置 LLM 关闭期单端点：调用 service 并返回计数、写审计。"""
    with (
        patch.object(
            dp_api.DpSyncService,
            "resolve_llm_disabled_tickets",
            new=AsyncMock(return_value={"resolved": 3, "failed": 0, "skipped": 2}),
        ),
        patch.object(dp_api, "write_audit", new=AsyncMock()) as audit,
    ):
        resp = await dp_client.post("/api/v1/lineage/dp-sync/resolve-llm-disabled")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["resolved"] == 3
    assert data["failed"] == 0
    assert data["skipped"] == 2
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "dp_sync.resolve_llm_disabled"


async def test_config_poll_interval_out_of_range_rejected(
    dp_client: httpx.AsyncClient,
) -> None:
    """poll_interval_minutes 超出 1~1440 → VALIDATION_ERROR，且不落库。"""
    cfg = MagicMock(id=1)
    update_cfg = AsyncMock()
    with (
        patch.object(dp_api.DpLineageRepository, "get_config", new=AsyncMock(return_value=cfg)),
        patch.object(dp_api.DpLineageRepository, "update_config", new=update_cfg),
        patch.object(dp_api, "write_audit", new=AsyncMock()),
    ):
        for bad in (0, -5, 1441, "abc"):
            resp = await dp_client.put(
                "/api/v1/lineage/dp-sync/config",
                json={"poll_interval_minutes": bad},
            )
            assert resp.status_code == 200
            assert resp.json()["code"] == "VALIDATION_ERROR", bad
    update_cfg.assert_not_awaited()


async def test_config_poll_interval_upper_bound_accepted(
    dp_client: httpx.AsyncClient,
) -> None:
    """1440（24 小时）为合法上界 → 正常落库。"""
    cfg = MagicMock(id=1)
    cfg.to_dict = MagicMock(return_value={"id": 1, "poll_interval_minutes": 1440})
    update_cfg = AsyncMock()
    with (
        patch.object(dp_api.DpLineageRepository, "get_config", new=AsyncMock(return_value=cfg)),
        patch.object(dp_api.DpLineageRepository, "update_config", new=update_cfg),
        patch.object(dp_api, "write_audit", new=AsyncMock()),
    ):
        resp = await dp_client.put(
            "/api/v1/lineage/dp-sync/config",
            json={"poll_interval_minutes": 1440},
        )
    assert resp.status_code == 200
    assert resp.json()["code"] == "OK"
    update_cfg.assert_awaited_once()
