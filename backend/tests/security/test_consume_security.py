"""consume 安全反向测试（对齐 gateways security_reverse，TD §12.6 / §15.4）。

双视角审查发现的 3 处 High 缺口修复后的回归锁定：
① 普通用户 token 调管理员接口 -> 403（FORBIDDEN，RBAC 写闸门）；
② SQL 注入 fuzz -> 被拦截（INJECTION_DETECTED，API 层 guard 纵深防御）；
③ PII 访问 -> 审计含 data_classification=PII（TD §15.4 数据分级留痕）；
④ 跨域 / PII 越权 -> fail-closed 拒绝（FORBIDDEN_DOMAIN / FORBIDDEN_PII）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.api.consume import get_consume_or_internal_user
from app.core.error_codes import ErrorCode
from app.core.exceptions import BusinessError
from app.core.security import hash_password
from app.main import app
from app.models.consume import ApiClient, ApiClientStatus
from app.models.metric import Metric
from app.services.consume.schemas import QueryRequest, QueryResponse
from app.services.consume.service import ConsumeService


def _api_client(
    whitelist=("M1",), scope_domain: str | None = None, daily_quota: int = 100_000
) -> ApiClient:
    c = ApiClient()
    c.client_id = "acme"
    c.client_secret_ref = hash_password("s3cr3t")
    c.status = ApiClientStatus.ACTIVE
    c.metric_whitelist = list(whitelist) if whitelist else None
    c.scope_domain = scope_domain
    c.qps = 100
    c.daily_quota = daily_quota
    c.created_by = 1
    return c


def _metric(code: str = "M1", domain: str = "sales", pii: bool = False) -> Metric:
    m = Metric()
    m.metric_code = code
    m.status = "PUBLISHED"
    m.owner_org = 1
    m.domain = domain
    m.definition_json = {
        "expression": "SUM(x)",
        "dependencies": ["fct_order"],
        "dimensions": ["region"],
        "grain": "day",
        "unit": "yuan",
        "pii": pii,
    }
    return m


async def test_admin_endpoint_requires_admin_role_403() -> None:
    """普通用户 token 调管理员接口 -> 403（FORBIDDEN）。"""

    async def fake_db():
        session = MagicMock()
        session.commit = AsyncMock()
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="metric_owner", domain="sales", roles_all=lambda: ["metric_owner"]
    )
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/consume/api-clients",
                json={"client_id": "x", "secret": "y", "scope_domain": "sales"},
                headers={"Authorization": "Bearer t"},
            )
        assert r.status_code == 403
        assert r.json()["code"] == "FORBIDDEN"
    finally:
        app.dependency_overrides.clear()


async def test_query_sql_injection_blocked_400() -> None:
    """SQL 注入 fuzz -> 被拦截（INJECTION_DETECTED，纵深防御）。"""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/api/v1/consume/query/dry-run",
            json={"metric_code": "M1", "date_range": "2024'; DROP TABLE metric; --"},
            headers={"X-Api-Key": "acme:s3cr3t"},
        )
    assert r.status_code == 400
    assert r.json()["code"] == "INJECTION_DETECTED"


async def test_pii_access_audit_contains_data_classification(monkeypatch) -> None:
    """PII 指标消费后，审计必须标注 data_classification=PII（对齐 TD §15.4）。"""
    captured: dict = {}

    async def spy_write_audit(
        db: object,
        *,
        actor_id: object,
        action: object,
        entity_type: object,
        entity_id: object,
        detail: dict[str, object],
        trace_id: object,
        pii_access: bool = False,
    ) -> None:
        captured["detail"] = detail
        captured["pii_access"] = pii_access

    async def fake_db():
        session = MagicMock()
        session.commit = AsyncMock()
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[get_consume_or_internal_user] = lambda: _api_client(whitelist=["M1"])
    monkeypatch.setattr(
        ConsumeService,
        "execute_query",
        AsyncMock(return_value=QueryResponse(metric_code="M1", meta={"pii": True})),
    )
    monkeypatch.setattr("app.api.consume.write_audit", spy_write_audit)
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/consume/query",
                json={"metric_code": "M1", "date_range": "2024-01~2024-03"},
                headers={"X-Api-Key": "acme:s3cr3t"},
            )
        assert r.status_code == 200
        # PII 访问 -> 审计含 data_classification=PII
        assert captured["pii_access"] is True
        assert captured["detail"]["data_classification"] == "PII"
    finally:
        app.dependency_overrides.clear()


async def test_cross_domain_and_pii_fail_closed() -> None:
    """双视角审查发现的两处 High 越权缺口：跨域与 PII 必须 fail-closed 拒绝。"""
    # 1) 跨域：scope_domain=finance 但指标在 sales 域 → FORBIDDEN_DOMAIN
    svc = ConsumeService(None)
    svc._get_metric = AsyncMock(return_value=_metric(domain="sales"))
    with pytest.raises(BusinessError) as exc:
        await svc.dry_run_query(
            QueryRequest(metric_code="M1", date_range=""),
            _api_client(scope_domain="finance"),
        )
    assert exc.value.error_code == ErrorCode.FORBIDDEN_DOMAIN

    # 2) PII：域内全量授权（白名单空）不能隐式访问 PII 指标 → FORBIDDEN_PII
    svc2 = ConsumeService(None)
    svc2._get_metric = AsyncMock(return_value=_metric(pii=True))
    with pytest.raises(BusinessError) as exc2:
        await svc2.dry_run_query(
            QueryRequest(metric_code="M1", date_range=""),
            _api_client(whitelist=None),
        )
    assert exc2.value.error_code == ErrorCode.FORBIDDEN_PII


async def test_internal_query_rejects_viewer_403() -> None:
    """内部查询端点（/consume/metrics/{code}/query）RBAC 对齐 query:execute：
    viewer/analyst 无执行权限（仅登录态+PDP 数据闸门不足以支撑特权执行+快照写副作用），
    须经 consume 客户端令牌通道消费数据。"""
    app.dependency_overrides[deps.get_db_session] = _fake_db_session()
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=2, role="viewer", domain="sales", roles_all=lambda: ["viewer"]
    )
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/consume/metrics/M1/query",
                json={"metric_code": "M1", "date_range": "2024-01~2024-03"},
                headers={"Authorization": "Bearer t"},
            )
        assert r.status_code == 403
        assert r.json()["code"] == "FORBIDDEN"
    finally:
        app.dependency_overrides.clear()


async def test_internal_query_allowed_for_metric_owner(monkeypatch) -> None:
    """metric_owner（前端 query:execute 默认基线角色）可执行内部查询。"""
    app.dependency_overrides[deps.get_db_session] = _fake_db_session()
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=3, role="metric_owner", domain="sales", roles_all=lambda: ["metric_owner"]
    )
    monkeypatch.setattr(
        ConsumeService,
        "execute_query",
        AsyncMock(return_value=QueryResponse(metric_code="M1", meta={})),
    )
    monkeypatch.setattr("app.api.consume.write_audit", AsyncMock())
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/consume/metrics/M1/query",
                json={"metric_code": "M1", "date_range": "2024-01~2024-03"},
                headers={"Authorization": "Bearer t"},
            )
        assert r.status_code == 200
    finally:
        app.dependency_overrides.clear()


def _fake_db_session():
    """内部查询端点测试用的假 DB session（mock 会话避免真实连接）。"""

    async def fake_db():
        session = MagicMock()
        session.commit = AsyncMock()
        yield session

    return fake_db


async def test_issue_token_expire_minutes_parameterized(monkeypatch) -> None:
    """签发令牌有效期参数化：默认 60、自定义透传 create_access_token、超范围 422。"""
    captured: dict = {}

    def fake_create_access_token(**kwargs):
        captured["expire_minutes"] = kwargs.get("expire_minutes")
        return "token-xyz"

    app.dependency_overrides[deps.get_db_session] = _fake_db_session()
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="platform_admin", domain=None, roles_all=lambda: ["platform_admin"]
    )
    monkeypatch.setattr("app.api.consume.create_access_token", fake_create_access_token)
    monkeypatch.setattr("app.api.consume.write_audit", AsyncMock())
    monkeypatch.setattr(
        "app.api.consume.ApiClientRepo.get_by_client_id",
        AsyncMock(return_value=_api_client()),
    )
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            # 未传 body → 默认 60 分钟（向后兼容）
            r = await c.post(
                "/api/v1/consume/api-clients/acme/token",
                headers={"Authorization": "Bearer t"},
            )
            assert r.status_code == 200
            assert captured["expire_minutes"] == 60
            # 自定义有效期 → 透传 create_access_token
            r = await c.post(
                "/api/v1/consume/api-clients/acme/token",
                json={"expire_minutes": 240},
                headers={"Authorization": "Bearer t"},
            )
            assert r.status_code == 200
            assert captured["expire_minutes"] == 240
            # 超范围（>1440）→ 422
            r = await c.post(
                "/api/v1/consume/api-clients/acme/token",
                json={"expire_minutes": 5000},
                headers={"Authorization": "Bearer t"},
            )
            assert r.status_code == 422
            # 下限（<5）→ 422
            r = await c.post(
                "/api/v1/consume/api-clients/acme/token",
                json={"expire_minutes": 1},
                headers={"Authorization": "Bearer t"},
            )
            assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()


def _fake_admin_db():
    """管理端点测试用假 DB session（含 flush/commit）。"""

    async def fake_db():
        session = MagicMock()
        session.flush = AsyncMock()
        session.commit = AsyncMock()
        yield session

    return fake_db


def _admin_user() -> MagicMock:
    return MagicMock(id=1, role="platform_admin", domain=None, roles_all=lambda: ["platform_admin"])


async def test_update_api_client_ok(monkeypatch) -> None:
    """编辑接入方：PUT 更新授权域/配额透传，审计落库。"""
    client = _api_client(scope_domain="sales")
    app.dependency_overrides[deps.get_db_session] = _fake_admin_db()
    app.dependency_overrides[deps.get_current_user] = _admin_user
    monkeypatch.setattr("app.api.consume.write_audit", AsyncMock())
    monkeypatch.setattr(
        "app.api.consume.ApiClientRepo.get_by_client_id", AsyncMock(return_value=client)
    )
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.put(
                "/api/v1/consume/api-clients/acme",
                json={"scope_domain": "finance", "qps": 50, "daily_quota": 200000},
                headers={"Authorization": "Bearer t"},
            )
        assert r.status_code == 200
        assert r.json()["data"]["scope_domain"] == "finance"
        # 行对象已被就地更新（mock 引用同一对象）
        assert client.scope_domain == "finance"
        assert client.qps == 50
        assert client.daily_quota == 200000
    finally:
        app.dependency_overrides.clear()


async def test_update_api_client_clear_to_empty_422(monkeypatch) -> None:
    """R8（审查修复）：编辑时**显式**把授权域与白名单清成双空 → 422 CLIENT_SCOPE_EMPTY。

    防把已有授权清成双空（= 任意非 PII 指标全量消费权）；纯配额修改不受影响。
    """
    client = _api_client(scope_domain="sales")  # 默认白名单 ("M1",)，非双空
    app.dependency_overrides[deps.get_db_session] = _fake_admin_db()
    app.dependency_overrides[deps.get_current_user] = _admin_user
    monkeypatch.setattr("app.api.consume.write_audit", AsyncMock())
    monkeypatch.setattr(
        "app.api.consume.ApiClientRepo.get_by_client_id", AsyncMock(return_value=client)
    )
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.put(
                "/api/v1/consume/api-clients/acme",
                json={"scope_domain": "", "metric_whitelist": [], "qps": 50},
                headers={"Authorization": "Bearer t"},
            )
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "CLIENT_SCOPE_EMPTY"
    finally:
        app.dependency_overrides.clear()


async def test_update_api_client_keep_historical_empty_ok(monkeypatch) -> None:
    """R8：历史双空客户端仅改配额（不传授权字段）→ 200，保留存量双空（仅治理页警告）。"""
    client = _api_client(whitelist=None, scope_domain=None)  # 双空历史客户端
    app.dependency_overrides[deps.get_db_session] = _fake_admin_db()
    app.dependency_overrides[deps.get_current_user] = _admin_user
    monkeypatch.setattr("app.api.consume.write_audit", AsyncMock())
    monkeypatch.setattr(
        "app.api.consume.ApiClientRepo.get_by_client_id", AsyncMock(return_value=client)
    )
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.put(
                "/api/v1/consume/api-clients/acme",
                json={"qps": 30, "daily_quota": 50000},
                headers={"Authorization": "Bearer t"},
            )
        assert r.status_code == 200, r.text
        assert r.json()["data"]["qps"] == 30
        # 双空保留
        assert client.scope_domain is None
        assert client.metric_whitelist is None
    finally:
        app.dependency_overrides.clear()


async def test_api_client_status_toggle(monkeypatch) -> None:
    """停用/启用接入方：PATCH status 透传并落审计。"""
    client = _api_client()
    app.dependency_overrides[deps.get_db_session] = _fake_admin_db()
    app.dependency_overrides[deps.get_current_user] = _admin_user
    monkeypatch.setattr("app.api.consume.write_audit", AsyncMock())
    monkeypatch.setattr(
        "app.api.consume.ApiClientRepo.get_by_client_id", AsyncMock(return_value=client)
    )
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.patch(
                "/api/v1/consume/api-clients/acme/status",
                json={"status": "REVOKED"},
                headers={"Authorization": "Bearer t"},
            )
        assert r.status_code == 200
        assert client.status == ApiClientStatus.REVOKED
    finally:
        app.dependency_overrides.clear()


async def test_api_client_delete_soft(monkeypatch) -> None:
    """删除接入方：软删（deleted_at 置位 + REVOKED），返回 deleted=true。"""
    client = _api_client()
    app.dependency_overrides[deps.get_db_session] = _fake_admin_db()
    app.dependency_overrides[deps.get_current_user] = _admin_user
    monkeypatch.setattr("app.api.consume.write_audit", AsyncMock())
    monkeypatch.setattr(
        "app.api.consume.ApiClientRepo.get_by_client_id", AsyncMock(return_value=client)
    )
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.delete(
                "/api/v1/consume/api-clients/acme", headers={"Authorization": "Bearer t"}
            )
        assert r.status_code == 200
        assert r.json()["data"] == {"deleted": True}
        assert client.status == ApiClientStatus.REVOKED
        assert client.deleted_at is not None
    finally:
        app.dependency_overrides.clear()


async def test_api_client_batch_mixed(monkeypatch) -> None:
    """批量操作：存在与不存在混合，逐条容错返回 ok/fail 计数。"""
    existing = _api_client()
    app.dependency_overrides[deps.get_db_session] = _fake_admin_db()
    app.dependency_overrides[deps.get_current_user] = _admin_user
    monkeypatch.setattr("app.api.consume.write_audit", AsyncMock())
    monkeypatch.setattr(
        "app.api.consume.ApiClientRepo.get_many",
        AsyncMock(return_value=[existing]),
    )
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/consume/api-clients/batch",
                json={"action": "disable", "client_ids": ["acme", "ghost"]},
                headers={"Authorization": "Bearer t"},
            )
        body = r.json()
        assert r.status_code == 200
        assert body["data"]["ok_count"] == 1
        assert body["data"]["fail_count"] == 1
        assert existing.status == ApiClientStatus.REVOKED
    finally:
        app.dependency_overrides.clear()


async def test_api_client_manage_forbidden_for_non_admin(monkeypatch) -> None:
    """非平台/域管理员调管理端点 → 403（RBAC 写闸门）。"""
    app.dependency_overrides[deps.get_db_session] = _fake_admin_db()
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=2, role="metric_owner", domain="sales", roles_all=lambda: ["metric_owner"]
    )
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.put(
                "/api/v1/consume/api-clients/acme",
                json={"scope_domain": "finance"},
                headers={"Authorization": "Bearer t"},
            )
        assert r.status_code == 403
        assert r.json()["code"] == "FORBIDDEN"
    finally:
        app.dependency_overrides.clear()


async def test_create_client_duplicate_id_conflict_409() -> None:
    """显式指定已存在（含软删）client_id → 409 而非 IntegrityError 500（B3 修复）。

    修复前：仅自动生成路径做冲突检测，用户显式传重复 client_id 时直插唯一键
    → IntegrityError → 通用 500；软删客户保留原 client_id 占位唯一键，恢复/重建受阻。
    """
    async def fake_db():
        session = MagicMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.flush = AsyncMock()
        existing = MagicMock()
        existing.client_id = "acme"
        session.execute = AsyncMock(return_value=MagicMock())
        session.execute.return_value.scalar_one_or_none.return_value = existing
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="platform_admin", domain=None, roles_all=lambda: ["platform_admin"]
    )
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/consume/api-clients",
                json={"client_id": "acme", "secret": "secret123", "scope_domain": "sales"},
                headers={"Authorization": "Bearer t"},
            )
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "CLIENT_ID_CONFLICT"
    finally:
        app.dependency_overrides.clear()


async def test_create_client_unique_id_ok() -> None:
    """显式指定唯一 client_id → 正常创建 200（预检不误杀）。"""
    async def fake_db():
        session = MagicMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.flush = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock())
        session.execute.return_value.scalar_one_or_none.return_value = None
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="platform_admin", domain=None, roles_all=lambda: ["platform_admin"]
    )
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/consume/api-clients",
                json={"client_id": "brand_new", "secret": "secret123", "scope_domain": "sales"},
                headers={"Authorization": "Bearer t"},
            )
        assert r.status_code == 200, r.text
        assert r.json()["data"]["client_id"] == "brand_new"
    finally:
        app.dependency_overrides.clear()


async def test_create_client_empty_scope_422() -> None:
    """R8（审查修复）：scope_domain 与 metric_whitelist 双空 → 422（最小授权范围）。

    双空时接入方四级闸门四个条件全不触发，将获得任意非 PII 指标的全量消费权
    （越权缺口）。创建时强制至少其一，杜绝管理员漏配即获得超出预期的消费权。
    """
    async def fake_db():
        session = MagicMock()
        session.commit = AsyncMock()
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="platform_admin", domain=None, roles_all=lambda: ["platform_admin"]
    )
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/consume/api-clients",
                json={"client_id": "acme2", "secret": "secret123"},
                headers={"Authorization": "Bearer t"},
            )
        assert r.status_code == 422, r.text
        assert "最小授权范围" in r.text
    finally:
        app.dependency_overrides.clear()


async def test_issue_token_scope_ownership(monkeypatch) -> None:
    """S3（审查修复）：签发令牌属主校验——domain_admin 仅可签本域接入方；
    不限域平台级接入方仅 platform_admin 可签；org_id 取签发人组织而非 created_by。"""
    captured: dict = {}

    def fake_create_access_token(**kwargs):
        captured["org_id"] = kwargs.get("org_id")
        return "token-xyz"

    app.dependency_overrides[deps.get_db_session] = _fake_db_session()
    monkeypatch.setattr("app.api.consume.create_access_token", fake_create_access_token)
    monkeypatch.setattr("app.api.consume.write_audit", AsyncMock())

    async def _issue(role: str, user_domain: str | None, scope_domain: str | None) -> int:
        app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
            id=2,
            role=role,
            domain=user_domain,
            org_id=9,
            roles_all=lambda: [role],
        )
        monkeypatch.setattr(
            "app.api.consume.ApiClientRepo.get_by_client_id",
            AsyncMock(return_value=_api_client(scope_domain=scope_domain)),
        )
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/consume/api-clients/acme/token",
                headers={"Authorization": "Bearer t"},
            )
            return r.status_code

    try:
        # 1) domain_admin 签本域接入方 → 放行，org_id=9（签发人组织）
        assert await _issue("domain_admin", "acme", "acme") == 200
        assert captured["org_id"] == 9
        # 2) domain_admin 签其他域接入方 → 403
        assert await _issue("domain_admin", "other", "acme") == 403
        # 3) domain_admin 签不限域平台级接入方 → 403
        assert await _issue("domain_admin", "acme", None) == 403
        # 4) platform_admin 签不限域接入方 → 放行
        assert await _issue("platform_admin", None, None) == 200
    finally:
        app.dependency_overrides.clear()
