"""语义领域安全逆向测试（对齐 DEV_GUIDE §9 安全 / TD §13）。

覆盖：
1. 普通用户调用写接口 -> 403 FORBIDDEN（RBAC 反向校验）
2. SQL 注入 fuzz -> 被守卫拦截（INJECTION_DETECTED）
3. PII 指标读取 -> 审计落库且含 data_classification=PII
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport
from tests.conftest import make_create_payload

from app.api import deps
from app.main import app
from app.models.audit import AuditLog
from app.models.metric import Metric
from app.models.user import User
from app.services.semantic.service import MetricService


def _normal_user() -> MagicMock:
    user = MagicMock(spec=User)
    user.id = 5
    user.org_id = 1
    user.role = "analyst"  # 非写角色
    return user


def _make_metric(code: str = "m", pii: bool = False) -> Metric:
    now = datetime(2026, 1, 1, 0, 0, 0)
    return Metric(
        id=1,
        metric_code=code,
        name="测试指标",
        domain="finance",
        type="atomic",
        granularity="day",
        unit="次",
        currency=None,
        aggregation="SUM",
        time_semantics="PERIOD",
        freshness="T1",
        sla=None,
        dw_layer="ADS",
        metric_tier="T3",
        serving_mode="BATCH_ONLY",
        additivity="ADDITIVE",
        non_additive_dimensions=None,
        definition_json={"expression": "sum(x)"},
        version=1,
        row_version=1,
        status="DRAFT",
        owner_id=1,
        backup_owner_id=None,
        pii_flag=pii,
        compliance_reviewed=False,
        effective_version=None,
        consumption_guide=None,
        successor_code=None,
        deprecated_at=None,
        sunset_until=None,
        emergency_publish=False,
        emergency_reason=None,
        gray_tenant_ids=None,
        pending_conflict=False,
        pending_conflict_detail=None,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
async def security_session() -> AsyncGenerator[MagicMock, None]:
    """提供可控的 MagicMock 会话并覆盖依赖。"""
    session: MagicMock = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock()

    async def fake_db() -> AsyncGenerator[MagicMock, None]:
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = _normal_user
    yield session
    app.dependency_overrides.clear()


async def test_normal_user_calls_admin_endpoint_returns_403(security_session):
    # 普通用户 token 调管理员接口 -> 403
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/metric-definitions",
            json=make_create_payload(),
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"  # RBAC 反向拦截


async def test_sql_injection_fuzz_blocked(security_session):
    # SQL 注入 fuzz -> 被拦截
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/metric-definitions?keyword=x%27%20OR%20%271%27%3D%271",
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 400
    assert "INJECTION_DETECTED" in resp.text  # 注入被守卫识别并拦截


async def test_pii_access_writes_audit_with_data_classification(security_session):
    # PII 访问 -> 审计含 data_classification=PII
    session = security_session
    # 使 get_by_code 返回带 PII 标记的真实指标（经缓存读路径 + 校验）
    result = MagicMock()
    result.scalar_one_or_none.return_value = _make_metric(code="fin_pii_daily", pii=True)
    session.execute.return_value = result
    session.add = MagicMock()
    session.commit = AsyncMock()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(
            "/api/v1/metric-definitions/fin_pii_daily",
            headers={"Authorization": "Bearer x"},
        )
    # 无论业务返回如何，审计必须先写入
    audits = [c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], AuditLog)]
    assert audits, "PII 指标读取必须写入审计记录"
    assert audits[0].pii_access is True
    # 审计详情明确标注 PII 数据分级（审计含 data_classification=PII）
    assert audits[0].detail_json.get("data_classification") == "PII"


async def test_list_pii_metrics_writes_bulk_audit(security_session, monkeypatch):
    # 列表返回 PII 指标 -> 写一条批量 PII 访问审计（闭合列表批量暴露 PII 漏洞）
    session = security_session
    pii_a = _make_metric(code="fin_pii_a", pii=True)
    pii_b = _make_metric(code="fin_pii_b", pii=True)
    normal = _make_metric(code="metric_x", pii=False)

    async def fake_list(self, params):
        return [pii_a, pii_b, normal], 3

    monkeypatch.setattr(MetricService, "list_metrics", fake_list)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/metric-definitions?page=1&page_size=10",
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 200
    audits = [c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], AuditLog)]
    assert audits, "列表返回 PII 指标必须写入批量审计记录"
    assert audits[0].pii_access is True
    detail = audits[0].detail_json
    assert detail.get("data_classification") == "PII"
    assert detail.get("count") == 2
    assert set(detail.get("codes")) == {"fin_pii_a", "fin_pii_b"}
