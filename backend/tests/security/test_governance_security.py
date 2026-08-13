"""governance 安全测试（对齐 gateways security_reverse，TD §13）。

覆盖：
① 授权/复核/重扫端点的 RBAC 写闸门（非授权角色 403）；
② 列表端点 SQL 注入守卫（400）；
③ 授权范围为空即拒绝（防止一次性全量放权）；
④ 非管理员访问授权列表被强制收敛到本人；
⑤ 非管理员越权查询他人权限被改写为本人。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from app.models.erasure import ErasureRequest, ErasureStatus
from app.services.governance.schemas import GrantListParams, PermissionCheckResult
from app.services.governance.service import GovernanceService


def _session() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())
    return session


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
async def owner_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(9, "metric_owner"):
        yield c


@pytest.fixture
async def analyst_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(10, "analyst"):
        yield c


@pytest.fixture
async def admin_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(1, "platform_admin"):
        yield c


_GRANT_BODY = {"user_id": 2, "domain": "sales", "grant_type": "READ"}


async def test_create_grant_requires_admin_403(owner_client: httpx.AsyncClient) -> None:
    resp = await owner_client.post("/api/v1/grants", json=_GRANT_BODY)
    assert resp.status_code == 403


async def test_batch_grant_requires_admin_403(owner_client: httpx.AsyncClient) -> None:
    resp = await owner_client.post("/api/v1/grants/batch", json={"items": [_GRANT_BODY]})
    assert resp.status_code == 403


async def test_dry_run_requires_admin_403(analyst_client: httpx.AsyncClient) -> None:
    resp = await analyst_client.post("/api/v1/grants/batch/dry-run", json={"items": [_GRANT_BODY]})
    assert resp.status_code == 403


async def test_revoke_requires_scope_403_for_other_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回收范围校验：非管理员越权回收他人授权被拒绝（403）。

    单条回收端点已放宽至全体已登录用户，实际范围由服务层 ``_assert_revoke_scope``
    收敛；此处用真实 Grant 替身绕过 MagicMock 的状态检查，使范围校验得以触发。
    """
    from app.models.governance import Grant, GrantStatus, GrantType
    from app.services.governance.repository import GovernanceRepository
    from app.services.governance.service import GovernanceService

    grant = Grant(
        id=1, user_id=2, domain="sales", grant_type=GrantType.READ, status=GrantStatus.ACTIVE
    )

    class _StubUser:
        id = 10
        role = "analyst"
        domain = "sales"

    async def fake_get(self: GovernanceRepository, gid: int) -> Grant:
        return grant

    async def fake_set(
        self: GovernanceRepository, g: Grant, s: GrantStatus, _r: str | None = None
    ) -> Grant:
        g.status = s
        return g

    async def fake_ensure_user(self: GovernanceService, uid: int) -> _StubUser:
        return _StubUser()

    monkeypatch.setattr(GovernanceRepository, "get_grant", fake_get)
    monkeypatch.setattr(GovernanceRepository, "set_grant_status", fake_set)
    monkeypatch.setattr(GovernanceService, "_ensure_user_exists", fake_ensure_user)
    async for c in _client(10, "analyst"):
        resp = await c.delete("/api/v1/grants/1")
        assert resp.status_code == 403


async def test_pii_review_requires_compliance_role_403(owner_client: httpx.AsyncClient) -> None:
    """PII 复核必须由合规官执行，指标 Owner 无权自审。"""
    resp = await owner_client.post(
        "/api/v1/pii/review",
        json={"metric_code": "m1", "decision": "APPROVE", "comment": "放行"},
    )
    assert resp.status_code == 403


async def test_rescan_requires_compliance_role_403(analyst_client: httpx.AsyncClient) -> None:
    resp = await analyst_client.post("/api/v1/classification/rescan", json={})
    assert resp.status_code == 403


async def test_role_create_requires_platform_admin_403(owner_client: httpx.AsyncClient) -> None:
    resp = await owner_client.post("/api/v1/roles", json={"name": "viewer"})
    assert resp.status_code == 403


async def test_grants_list_rejects_sql_injection_400(analyst_client: httpx.AsyncClient) -> None:
    resp = await analyst_client.get("/api/v1/grants", params={"domain": "' OR '1'='1"})
    assert resp.status_code == 400


async def test_empty_scope_grant_rejected_422(admin_client: httpx.AsyncClient) -> None:
    """domain 与 metric_whitelist 同时为空 → 等价于全量放权，必须拒绝。"""
    resp = await admin_client.post("/api/v1/grants", json={"user_id": 2, "grant_type": "READ"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_non_admin_grants_list_forced_to_self(monkeypatch: pytest.MonkeyPatch) -> None:
    """越权读防护：非管理员查询授权列表时 user_id 被强制改写为本人。"""
    captured: dict[str, Any] = {}

    async def fake_list(self: GovernanceService, params: GrantListParams) -> tuple[list[Any], int]:
        captured["user_id"] = params.user_id
        return [], 0

    monkeypatch.setattr(GovernanceService, "list_grants", fake_list)
    async for c in _client(10, "analyst"):
        resp = await c.get("/api/v1/grants", params={"user_id": 999})
        assert resp.status_code == 200
        assert captured["user_id"] == 10


async def test_non_admin_permission_check_forced_to_self(monkeypatch: pytest.MonkeyPatch) -> None:
    """非管理员不得代查他人权限，请求主体被改写为自身。"""
    captured: dict[str, Any] = {}

    async def fake_check(self: GovernanceService, req: Any) -> PermissionCheckResult:
        captured["user_id"] = req.user_id
        return PermissionCheckResult(allow=False, reason="stub")

    monkeypatch.setattr(GovernanceService, "check_permission", fake_check)
    async for c in _client(10, "analyst"):
        resp = await c.post(
            "/api/v1/permissions/check",
            json={"user_id": 999, "action": "read", "domain": "sales"},
        )
        assert resp.status_code == 200
        assert captured["user_id"] == 10


async def test_batch_size_capped_422(admin_client: httpx.AsyncClient) -> None:
    """批量条目上限 200，超限直接拒绝（防止一次性放权面过大）。"""
    resp = await admin_client.post("/api/v1/grants/batch", json={"items": [_GRANT_BODY] * 201})
    assert resp.status_code == 422


# ---------------------------------------------------------------- erasure (D9)


async def test_erasure_requires_compliance_role_403(owner_client: httpx.AsyncClient) -> None:
    """被遗忘权仅合规官可发起，指标 Owner 无权（R7-09③ 门禁）。"""
    resp = await owner_client.post(
        "/api/v1/erasure", json={"subject_user_id": 42, "reason": "GDPR"}
    )
    assert resp.status_code == 403


async def test_erasure_requires_compliance_role_403_analyst(
    analyst_client: httpx.AsyncClient,
) -> None:
    resp = await analyst_client.post("/api/v1/erasure", json={"subject_user_id": 42})
    assert resp.status_code == 403


async def test_erasure_compliance_role_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合规官发起被遗忘权：调用 service 并回显去标识化台账。"""
    fake = ErasureRequest(
        subject_user_id=42,
        requested_by=3,
        status=ErasureStatus.COMPLETED,
        token="ANONYMIZED_deadbeefcafe1234",
        affected_rows=2,
        reason="GDPR",
    )
    fake.created_at = datetime.now(UTC)

    async def fake_exec(
        self: GovernanceService, subject_user_id: int, operator_id: int, reason: str | None = None
    ) -> ErasureRequest:
        return fake

    monkeypatch.setattr(GovernanceService, "execute_erasure", fake_exec)
    async for c in _client(3, "compliance_officer"):
        resp = await c.post("/api/v1/erasure", json={"subject_user_id": 42, "reason": "GDPR"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == "OK"
        data = body["data"]
        assert data["subject_user_id"] == 42
        assert data["status"] == "COMPLETED"
        assert data["affected_rows"] == 2
        assert data["token_prefix"] == "ANONYMIZED_d"


async def test_erasure_rejects_invalid_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """subject_user_id <= 0 触发参数校验（422）。"""

    async def fake_exec(
        self: GovernanceService, subject_user_id: int, operator_id: int, reason: str | None = None
    ) -> ErasureRequest:
        return ErasureRequest(
            subject_user_id=subject_user_id,
            requested_by=3,
            status=ErasureStatus.COMPLETED,
            token="x",
            affected_rows=0,
        )

    monkeypatch.setattr(GovernanceService, "execute_erasure", fake_exec)
    async for c in _client(3, "compliance_officer"):
        resp = await c.post("/api/v1/erasure", json={"subject_user_id": 0})
        assert resp.status_code == 422
