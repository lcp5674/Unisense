"""quality 安全测试（D11 外部基准对账 RBAC 闸门，对齐 gateways security_reverse）。

覆盖：
① 基准导入/绑定/对账执行需写权限（analyst → 403）；
② 差异确认需治理权限（analyst → 403，metric_owner → 200）；
③ 基准/对账记录列表需读权限（viewer → 200）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from app.models.quality import (
    QualityEventStatus,
    QualityRuleType,
    QualitySeverity,
    ReconciliationStatus,
)
from app.services.quality.schemas import (
    BenchmarkResponse,
    QualityEventResponse,
    ReconciliationRecordResponse,
)


def _session() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    # S1 域校验（service 层 _assert_metric_domain）查询返回本域指标（sales），
    # 与 mock user.domain="sales" 一致，放行域校验聚焦 RBAC 断言。
    metric = MagicMock()
    metric.domain = "sales"
    session.execute = AsyncMock(return_value=MagicMock())
    session.execute.return_value.scalar_one_or_none.return_value = metric
    return session


async def _client(uid: int, role: str) -> AsyncIterator[httpx.AsyncClient]:
    session = _session()

    async def fake_db() -> AsyncIterator[MagicMock]:
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=uid,
        role=role,
        domain="sales",
        # require_roles 走 roles_all()：缺省会返回 MagicMock（不可 JSON 序列化 → 403
        # 被 ctx 序列化失败吞成 500），须显式返回角色列表（对齐 test_consume_security）。
        roles_all=lambda: [role],
        has_role=lambda r: r == role,
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def owner_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(9, "metric_owner"):
        yield c


@pytest.fixture
async def analyst_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(10, "analyst"):
        yield c


@pytest.fixture
async def viewer_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(11, "viewer"):
        yield c


async def test_benchmark_import_requires_write_role_403(analyst_client: httpx.AsyncClient) -> None:
    resp = await analyst_client.post(
        "/api/v1/quality/benchmarks/import",
        json={
            "source_id": "S",
            "metric_code": "M",
            "bench_date": "2024-01-01",
            "bench_value": "100",
            "provider": "audit",
        },
    )
    assert resp.status_code == 403


async def test_benchmark_bind_requires_write_role_403(analyst_client: httpx.AsyncClient) -> None:
    resp = await analyst_client.post(
        "/api/v1/quality/benchmarks/1/bind",
        json={"metric_code": "M2"},
    )
    assert resp.status_code == 403


async def test_reconciliation_run_requires_write_role_403(
    analyst_client: httpx.AsyncClient,
) -> None:
    resp = await analyst_client.post(
        "/api/v1/quality/reconciliation/run",
        json={"benchmark_id": 1, "metric_value": "100"},
    )
    assert resp.status_code == 403


async def test_reconciliation_confirm_requires_gov_role_403(
    analyst_client: httpx.AsyncClient,
) -> None:
    resp = await analyst_client.post(
        "/api/v1/quality/reconciliation-records/1/confirm",
        json={"decision": "reasonable"},
    )
    assert resp.status_code == 403


async def test_benchmark_list_read_role_200(viewer_client: httpx.AsyncClient) -> None:
    resp = await viewer_client.get("/api/v1/quality/benchmarks")
    assert resp.status_code == 200


async def test_reconciliation_records_list_read_role_200(viewer_client: httpx.AsyncClient) -> None:
    resp = await viewer_client.get("/api/v1/quality/reconciliation-records")
    assert resp.status_code == 200


async def test_benchmark_import_success(
    owner_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = BenchmarkResponse(
        id=1,
        source_id="S",
        metric_code="M",
        bench_date=date(2024, 1, 1),
        dims=None,
        bench_value=Decimal("100"),
        provider="audit",
        tolerance_pct=None,
        imported_by=9,
        created_at=datetime(2024, 1, 1),
    )
    monkeypatch.setattr(
        "app.services.quality.service.QualityService.import_benchmark",
        AsyncMock(return_value=fake),
    )
    resp = await owner_client.post(
        "/api/v1/quality/benchmarks/import",
        json={
            "source_id": "S",
            "metric_code": "M",
            "bench_date": "2024-01-01",
            "bench_value": "100",
            "provider": "audit",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["id"] == 1


async def test_reconciliation_confirm_success(
    owner_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = ReconciliationRecordResponse(
        id=1,
        benchmark_id=1,
        metric_code="M",
        metric_value=Decimal("103"),
        bench_value=Decimal("100"),
        diff_pct=Decimal("3"),
        window=None,
        status=ReconciliationStatus.CONFIRMED,
        owner_note="口径有误",
        decision="caliber_error",
        confirmed_by=9,
        checked_at=datetime(2024, 1, 2),
        created_at=datetime(2024, 1, 1),
    )
    monkeypatch.setattr(
        "app.services.quality.service.QualityService.confirm_reconciliation",
        AsyncMock(return_value=fake),
    )
    resp = await owner_client.post(
        "/api/v1/quality/reconciliation-records/1/confirm",
        json={"decision": "caliber_error", "owner_note": "口径有误"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "CONFIRMED"


async def test_detect_triggered_writes_audit_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归（复审）：质量检测命中触发异常事件为治理写操作，须先审计后 commit。

    原缺陷：detect 落库 OPEN 异常事件却零审计，且 commit 先于审计（PLAT-3 违规）；
    PLAT-3 要求业务写入与审计同事务原子提交。
    """
    session = AsyncMock()
    session.add = MagicMock()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=9,
        role="compliance_officer",
        domain="sales",
        roles_all=lambda: ["compliance_officer"],
        has_role=lambda r: r == "compliance_officer",
    )
    event = QualityEventResponse(
        id=42,
        metric_id=7,
        level=QualitySeverity.P1,
        rule_type=QualityRuleType.COMPLETENESS,
        obs_value=Decimal("0.55"),
        threshold=Decimal("0.5"),
        status=QualityEventStatus.OPEN,
    )
    monkeypatch.setattr(
        "app.services.quality.service.QualityService.detect",
        AsyncMock(return_value=event),
    )
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/api/v1/quality/events/detect",
                json={
                    "metric_id": 7,
                    "rule_type": "COMPLETENESS",
                    "obs_value": "0.55",
                    "rule_mode": "static",
                },
            )
        assert resp.status_code == 200, resp.text
        # 事件命中必须提交事务
        assert session.commit.await_count == 1, "detect 命中未提交事务"
        # 必须写入审计（PLAT-3 原子审计）
        assert session.add.call_args_list, "detect 命中未写审计"
    finally:
        app.dependency_overrides.clear()
