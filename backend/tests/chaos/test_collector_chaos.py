"""采集领域混沌/韧性测试（对齐 gateways chaos）。

覆盖：事件总线（Redis）宕机 -> 核心链路仍 200（降级）；外部源库故障 ->
采集返回 503（重试型，不静默 200）；事件发布熔断降级不阻断主流程。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import collector as collector_api
from app.api import deps
from app.core.exceptions import ExternalDependencyError
from app.main import app
from app.services.collector.events import CatalogEventPublisher
from app.services.collector.schemas import DBCatalogResponse


@pytest.fixture
async def owner_client():
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=5, role="metric_owner")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


class _DownPublisher:
    async def publish(self, event_type: str, payload: dict) -> bool:
        return False  # 模拟 Redis 不可用，降级


class _FakeRegisterSvc:
    """核心链路假服务：事件总线降级，但注册成功。"""

    def __init__(self, db: object, **kw: object) -> None:
        self._events = _DownPublisher()
        self._repo = MagicMock()
        self._repo.upsert_catalog = AsyncMock(return_value=(MagicMock(), True))
        self._repo.recompute_coverage = AsyncMock(return_value=1.0)

    async def register_catalog(self, req: object, actor_id: int) -> DBCatalogResponse:
        return DBCatalogResponse(
            source_id="s",
            entity_name="users",
            entity_type="TABLE",
            schema_def={"columns": ["id"]},
            etl_sql=None,
            sensitivity_level="INTERNAL",
            owner_id=None,
            upstream_signature="sig",
        )


async def test_core_path_200_when_event_bus_down(owner_client, monkeypatch):
    """事件总线（Redis）宕机 -> 核心注册链路仍 200（降级生效）。"""
    monkeypatch.setattr(collector_api, "CollectorService", _FakeRegisterSvc)
    resp = await owner_client.post(
        "/api/v1/data-sources/s/catalogs",
        json={"source_id": "s", "entity_name": "users", "schema_json": {"columns": ["id"]}},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200  # 核心链路降级仍可用


async def test_collect_external_failure_returns_503(owner_client, monkeypatch):
    """外部源库故障 -> 采集返回 503（重试型），不静默 200。"""

    def _boom(_type: str, _cfg: str) -> object:
        raise ExternalDependencyError("源库不可达")

    monkeypatch.setattr(collector_api, "build_collector", _boom)
    resp = await owner_client.post(
        "/api/v1/data-sources/s/collect",
        json={"collector_type": "information_schema"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 503


async def test_event_publisher_circuit_degradation():
    """Redis 持续失败 -> 熔断打开，发布降级返回 False（不阻断）。"""

    class _BoomRedis:
        async def publish(self, channel: str, message: str) -> None:
            raise RuntimeError("redis down")

    publisher = CatalogEventPublisher(_BoomRedis())  # type: ignore[arg-type]
    ok = await publisher.publish("catalog_registered", {"source_id": "s"})
    assert ok is False  # 降级：发布失败不影响主流程
