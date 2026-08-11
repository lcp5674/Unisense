"""冲突领域混沌测试（对齐 gateways chaos）。

覆盖：① 通知/治理服务不可达时事件 best-effort 降级，check/arbitrate 仍成功；
② 服务层外部依赖异常时 API 返回 503（熔断/降级信号）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import conflict as conflict_api
from app.api import deps
from app.core.exceptions import ExternalDependencyError
from app.main import app
from app.models.conflict import Conflict


class _RaisingEvents:
    async def publish(self, event: dict) -> None:
        raise RuntimeError("notify 不可达（模拟）")


class _FakeRepo:
    def __init__(self) -> None:
        self.rows: list[Conflict] = []
        self._seq = 0

    async def create(self, c: Conflict) -> Conflict:
        self._seq += 1
        c.id = self._seq
        self.rows.append(c)
        return c

    async def get_by_conflict_id(self, cid: str):
        for r in self.rows:
            if r.conflict_id == cid:
                return r
        return None

    async def list_conflicts(self, *a, **k):
        return self.rows, len(self.rows)

    async def update_status(self, c, *a, **k):
        return c

    async def create_ruling(self, r):
        return r

    async def get_rulings(self, cid):
        return []


async def test_check_degrades_when_notify_fails():
    from app.services.conflict.schemas import MetricInput
    from app.services.conflict.service import ConflictService

    svc = ConflictService(db=object(), events=_RaisingEvents())
    svc._repo = _FakeRepo()
    result = await svc.check(
        MetricInput(metric_code="gmv_total", domain="sales", definition="sum(amount)"),
        [MetricInput(metric_code="gmv_total", domain="finance", definition="sum(price)")],
    )
    # 事件失败不向上抛，硬冲突仍被正确识别与落库
    assert result.blocked is True
    assert len(svc._repo.rows) == 1


class _BoomService:
    def __init__(self, db, events=None, llm=None) -> None:
        pass

    async def arbitrate(self, cid, req):
        raise ExternalDependencyError("rule service 不可达")


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

    def _owner_user() -> MagicMock:
        return MagicMock(id=11, role="compliance_officer")

    app.dependency_overrides[deps.get_current_user] = _owner_user
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_arbitrate_returns_503_on_external_failure(owner_client, monkeypatch):
    monkeypatch.setattr(conflict_api, "ConflictService", _BoomService)
    resp = await owner_client.post(
        "/api/v1/conflicts/CF-X/arbitrate",
        json={"decision": "merge", "arbitrator_id": 11, "reason": "x"},
    )
    # 外部依赖失败时返回 503（ErrorHandlerMiddleware 将 ExternalDependencyError 映射到 503）
    assert resp.status_code == 503
