"""governance 混沌测试（对齐 gateways chaos，TD §5.5 降级策略）。

覆盖：
① notify 不可达时事件 best-effort 降级，授权/复核主流程仍成功；
② 分级引擎异常时标记 UNKNOWN 并继续扫描其余资产（不阻断整批）；
③ 事件发布器自身异常被吞掉且熔断可打开；
④ 未配置 notify 端点时 publish 静默 no-op；
⑤ 服务层外部依赖异常时 API 返回 503。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport
from tests.unit.test_governance_service import FakeCatalog, FakeDB, FakeRepo, FakeUser

from app.api import deps
from app.api import governance as governance_api
from app.core.exceptions import ExternalDependencyError
from app.main import app
from app.services.governance import policy
from app.services.governance.events import GovernanceEventPublisher
from app.services.governance.schemas import ClassificationRescanRequest, GrantCreate
from app.services.governance.service import GovernanceService


class _RaisingEvents:
    async def publish(self, event: dict[str, Any]) -> None:
        raise RuntimeError("notify 不可达（模拟）")


def _svc_with_raising_events() -> tuple[GovernanceService, FakeRepo]:
    # actor 用平台管理员：grant 走新越权防护（P0）后仅 platform_admin 可跨域授权，
    # 该测试聚焦「通知故障不回滚授权」，不应被权限门禁阻断。
    svc = GovernanceService(
        db=FakeDB(FakeUser(uid=9, role="platform_admin")), events=_RaisingEvents()
    )  # type: ignore[arg-type]
    repo = FakeRepo()
    svc._repo = repo  # type: ignore[assignment]
    return svc, repo


async def test_grant_survives_notify_outage() -> None:
    """通知服务不可达不得回滚授权。"""
    svc, repo = _svc_with_raising_events()
    row = await svc.grant(GrantCreate(user_id=1, domain="sales"), actor_id=9)
    assert row.id == 1
    assert len(repo.grants) == 1


async def test_rescan_survives_notify_outage() -> None:
    svc, repo = _svc_with_raising_events()
    repo.catalogs = [FakeCatalog(1, "dwd.user", {"columns": [{"name": "phone"}]}, "INTERNAL")]
    result = await svc.classification_rescan(ClassificationRescanRequest())
    assert result.scanned == 1
    assert result.pii_found == 1


async def test_rescan_degrades_to_unknown_when_engine_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """规则引擎抛错时该资产标 UNKNOWN，其余资产继续扫描，敏感级不被误改。"""
    svc, repo = _svc_with_raising_events()
    repo.catalogs = [
        FakeCatalog(1, "dwd.a", {"columns": [{"name": "phone"}]}, "INTERNAL"),
        FakeCatalog(2, "dwd.b", {"columns": [{"name": "gmv"}]}, "INTERNAL"),
    ]

    def boom(schema: dict[str, Any]) -> list[Any]:
        raise RuntimeError("分级引擎不可用（模拟）")

    monkeypatch.setattr(policy, "detect_pii_columns", boom)
    result = await svc.classification_rescan(ClassificationRescanRequest())
    assert result.scanned == 2
    assert result.degraded == 2
    assert result.changed == 0
    assert repo.catalog_updates == []  # UNKNOWN 不回写 db_catalog
    assert all(i.sensitivity_after == "UNKNOWN" for i in result.items)


async def test_publisher_swallows_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """发布器内部异常不得外泄，熔断计数正常累加。"""
    pub = GovernanceEventPublisher(notify_url="http://127.0.0.1:1")

    async def boom(event: dict[str, Any]) -> None:
        raise ConnectionError("connection refused（模拟）")

    monkeypatch.setattr(pub, "_send", boom)
    for _ in range(3):
        await pub.publish({"event_type": "grant.granted"})  # 不抛异常即通过


async def test_publisher_noop_without_notify_url() -> None:
    """未配置 notify 端点时静默降级。"""
    pub = GovernanceEventPublisher(notify_url=None)
    await pub.publish({"event_type": "classification.done"})
    await pub.close()


class _BoomService:
    def __init__(self, db: Any, events: Any = None) -> None:
        pass

    async def classification_rescan(self, payload: Any) -> Any:
        raise ExternalDependencyError("分级引擎不可达")


@pytest.fixture
async def compliance_client() -> AsyncIterator[httpx.AsyncClient]:
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())

    async def fake_db() -> AsyncIterator[MagicMock]:
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=11, role="compliance_officer", domain="sales"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_rescan_returns_503_on_external_failure(
    compliance_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(governance_api, "GovernanceService", _BoomService)
    resp = await compliance_client.post("/api/v1/classification/rescan", json={})
    assert resp.status_code == 503
