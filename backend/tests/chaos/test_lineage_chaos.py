"""血缘领域混沌/韧性测试（对齐 gateways chaos）。

覆盖：Neo4j 图存储（降级）宕机 -> 核心解析链路仍 200；事件发布熔断降级
不阻断主流程（fallback）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.api import lineage as lineage_api
from app.core.exceptions import ExternalDependencyError
from app.main import app
from app.services.lineage import service as lineage_service
from app.services.lineage.events import LineageEventPublisher
from app.services.lineage.service import LineageService


@pytest.fixture
async def owner_client():
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=5, role="metric_owner")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


class _DownGraph:
    """模拟 Neo4j 不可用，图写降级。"""

    async def write_edges(self, edges) -> bool:
        return False


async def test_parse_200_when_graph_down(owner_client, monkeypatch):
    """Neo4j 图存储宕机 -> 解析链路降级仍 200（核心可用）。"""

    def _fake_svc(db, **kw):
        return LineageService(db, graph=_DownGraph(), events=None)

    monkeypatch.setattr(lineage_api, "LineageService", _fake_svc)
    resp = await owner_client.post(
        "/api/v1/lineage/parse",
        json={"sql": "INSERT INTO t SELECT a.id FROM a"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200  # 图存储降级不阻断主流程


async def test_event_publisher_circuit_degradation():
    """Redis 持续失败 -> 熔断打开，发布降级返回 False（不阻断）。"""

    class _BoomRedis:
        async def publish(self, channel: str, message: str) -> None:
            raise RuntimeError("redis down")

    publisher = LineageEventPublisher(_BoomRedis())  # type: ignore[arg-type]
    ok = await publisher.publish("lineage_parsed", {"table_edges": 1})
    assert ok is False  # fallback：发布失败不影响主流程


async def test_parse_external_failure_returns_503(owner_client, monkeypatch):
    """解析依赖外部 SQLGlot 抛错 -> 返回 503（重试型），不静默 200。"""

    def _boom(_type, _dialect):
        raise ExternalDependencyError("解析引擎不可用")

    monkeypatch.setattr(lineage_service, "extract_table_lineage", _boom)
    resp = await owner_client.post(
        "/api/v1/lineage/parse",
        json={"sql": "INSERT INTO t SELECT a.id FROM a"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 503
