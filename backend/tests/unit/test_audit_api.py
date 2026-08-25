"""审计查询 API 单测（补齐覆盖率 + enrich 中文化）。

针对 api/audit.py 覆盖：
1. 列表查询（含 actor/entity/trace_id/PII 过滤）
2. enrich：action_desc（中文描述）与 actor_display（操作人姓名）
3. 精确计数（count 子查询）
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from app.models.audit import AuditLog


def _make_log(action: str = "CREATE", entity_type: str = "metric_definition") -> AuditLog:
    return AuditLog(
        id=1,
        actor_id=1,
        action=action,
        entity_type=entity_type,
        entity_id="sales_gmv_amount_daily",
        ip="127.0.0.1",
        trace_id="t1",
        pii_access=False,
    )


@pytest.fixture
async def audit_client() -> AsyncIterator[httpx.AsyncClient]:
    """覆盖 DB 会话与当前用户依赖（平台管理员）。

    新版查询为 select(AuditLog, User.display_name).join(...)：
    session.execute 先执行 count（scalar_one），再执行主查询（.all() 返回元组行）。
    """

    async def fake_db():
        session = MagicMock()
        count_result = MagicMock()
        count_result.scalar_one.return_value = 1
        rows_result = MagicMock()
        rows_result.all.return_value = [(_make_log(), "平台管理员")]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_list_audit_logs(audit_client: httpx.AsyncClient) -> None:
    resp = await audit_client.get("/api/v1/audit")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 1
    item = data["items"][0]
    assert item["entity_type"] == "metric_definition"
    assert item["entity_id"] == "sales_gmv_amount_daily"
    # enrich 中文化：中文描述 + 操作人姓名
    assert item["action_desc"] == "创建了指标定义"
    assert item["actor_display"] == "平台管理员"


async def test_list_audit_logs_with_filters(audit_client: httpx.AsyncClient) -> None:
    resp = await audit_client.get(
        "/api/v1/audit",
        params={"actor_id": 1, "entity_type": "metric", "pii_access": "false"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["page"] == 1
    assert resp.json()["data"]["page_size"] == 20


async def test_list_audit_logs_with_actor_keyword(audit_client: httpx.AsyncClient) -> None:
    """操作人姓名/用户名模糊搜索（企业级检索：记姓名而非数字 ID）。"""
    resp = await audit_client.get("/api/v1/audit", params={"actor_keyword": "管理员"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["actor_display"] == "平台管理员"


async def test_list_audit_logs_with_trace_id_filter(audit_client: httpx.AsyncClient) -> None:
    """trace_id 过滤（跨服务链路追踪入口）。"""
    resp = await audit_client.get("/api/v1/audit", params={"trace_id_filter": "t1"})
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 1


# ------------------------------------------------------------------- 导出（合规留档）


def _export_session() -> MagicMock:
    """export 端点专用 mock：单次主查询 + write_audit(仅 add) + commit。"""
    session = MagicMock()
    rows_result = MagicMock()
    rows_result.all.return_value = [(_make_log(), "平台管理员")]
    session.execute = AsyncMock(return_value=rows_result)
    session.add = MagicMock()
    session.commit = AsyncMock()
    return session


async def test_export_audit_csv() -> None:
    """CSV 导出：text/csv + UTF-8 BOM + 列头与中文 action_desc。"""
    session = _export_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/v1/audit/export")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert resp.text.startswith("\ufeff")  # BOM（Excel 打开不乱码）
    assert "action_desc" in resp.text
    assert "创建了指标定义" in resp.text
    # 导出动作本身落审计
    entry = session.add.call_args.args[0]
    assert entry.action == "audit.export"


async def test_export_audit_json() -> None:
    """JSON 导出：application/json + 数组含 enrich 字段。"""
    session = _export_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/v1/audit/export", params={"format": "json"})
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    records = resp.json()
    assert isinstance(records, list)
    assert records[0]["entity_type"] == "metric_definition"
    assert records[0]["action_desc"] == "创建了指标定义"


async def test_export_audit_limited() -> None:
    """导出上限：limit 参数生效（请求带 limit=1 仍成功）。"""
    session = _export_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/v1/audit/export", params={"format": "json", "limit": 1})
    app.dependency_overrides.clear()
    assert resp.status_code == 200


async def test_export_audit_viewer_forbidden() -> None:
    """viewer 无审计读权限 → 403。"""
    session = _export_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=9, role="viewer", roles_all=lambda: ["viewer"], has_role=lambda r: r == "viewer"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/v1/audit/export")
    app.dependency_overrides.clear()
    assert resp.status_code == 403


class TestClientIp:
    """client_ip 分支覆盖（audit.py 15-22）：None 请求 / X-Forwarded-For 拆分 / 直连地址。"""

    def test_client_ip_none_request(self) -> None:
        """request 为 None 时返回空串。"""
        from app.core.audit import client_ip

        assert client_ip(None) == ""

    def test_client_ip_single_forwarded(self, monkeypatch) -> None:
        """直连 IP 在 trusted 白名单时，信任 X-Forwarded-For 单 IP。"""
        from app.core.audit import client_ip
        from app.core.config import settings

        monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.1")
        req = MagicMock()
        req.client = MagicMock()
        req.client.host = "127.0.0.1"
        req.headers = {"X-Forwarded-For": "203.0.113.7"}
        assert client_ip(req) == "203.0.113.7"

    def test_client_ip_multi_forwarded_takes_first(self, monkeypatch) -> None:
        """trusted 代理后的 XFF 链：reversed 取第一个非 trusted IP（去空白）。"""
        from app.core.audit import client_ip
        from app.core.config import settings

        monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.1,10.0.0.1")
        req = MagicMock()
        req.client = MagicMock()
        req.client.host = "10.0.0.1"
        req.headers = {"X-Forwarded-For": "203.0.113.9, 127.0.0.1"}
        assert client_ip(req) == "203.0.113.9"

    def test_client_ip_ignores_forged_xff(self, monkeypatch) -> None:
        """SEC-03 防伪造：直连 IP 不在 trusted 白名单时忽略 XFF，使用直连地址。"""
        from app.core.audit import client_ip
        from app.core.config import settings

        monkeypatch.setattr(settings, "trusted_proxies", "")
        req = MagicMock()
        req.client = MagicMock()
        req.client.host = "127.0.0.1"
        req.headers = {"X-Forwarded-For": "203.0.113.7"}
        assert client_ip(req) == "127.0.0.1"

    def test_client_ip_no_forwarded_uses_direct(self) -> None:
        """无 X-Forwarded-For 时回退直连地址。"""
        from app.core.audit import client_ip

        req = MagicMock()
        req.headers = {}
        req.client = MagicMock()
        req.client.host = "127.0.0.1"
        assert client_ip(req) == "127.0.0.1"

    def test_client_ip_no_forwarded_no_client(self) -> None:
        """既无 X-Forwarded-For 也无 client 时返回空串。"""
        from app.core.audit import client_ip

        req = MagicMock()
        req.headers = {}
        req.client = None
        assert client_ip(req) == ""


class TestWriteAudit:
    """write_audit 落库断言（audit.py 25-48）：仅 add、不 commit、字段完整。"""

    async def test_write_audit_adds_entry_with_all_fields(self) -> None:
        from app.core.audit import write_audit

        session = MagicMock()
        await write_audit(
            session,
            actor_id=7,
            action="conflict.arbitrate",
            entity_type="conflict",
            entity_id="conflict-42",
            detail={"decision": "alias", "canonical": "c"},
            ip="203.0.113.7",
            trace_id="trace-abc",
            pii_access=True,
        )
        session.add.assert_called_once()
        entry = session.add.call_args.args[0]
        assert isinstance(entry, AuditLog)
        assert entry.actor_id == 7
        assert entry.action == "conflict.arbitrate"
        assert entry.entity_type == "conflict"
        assert entry.entity_id == "conflict-42"
        assert entry.detail_json == {"decision": "alias", "canonical": "c"}
        assert entry.ip == "203.0.113.7"
        assert entry.trace_id == "trace-abc"
        assert entry.pii_access is True
        # 仅 add，不 commit（由调用方控制事务边界）
        session.commit.assert_not_called()

    async def test_write_audit_defaults(self) -> None:
        from app.core.audit import write_audit

        session = MagicMock()
        await write_audit(
            session,
            actor_id=1,
            action="CREATE",
            entity_type="metric_definition",
            entity_id=99,
            detail={},
        )
        entry = session.add.call_args.args[0]
        assert entry.entity_id == "99"  # int 转为 str
        assert entry.ip == ""
        assert entry.trace_id == ""
        assert entry.pii_access is False

    async def test_write_audit_truncates_oversized_entity_id(self) -> None:
        """超长 entity_id（如 维度code:成员code 拼接）截断到列宽 64，审计不阻断业务。"""
        from app.core.audit import write_audit

        session = MagicMock()
        long_id = "sales_e2e_channel_dimension:" + "x" * 100
        await write_audit(
            session,
            actor_id=1,
            action="DIMENSION_MEMBER_CREATE",
            entity_type="dimension_member",
            entity_id=long_id,
            detail={},
        )
        entry = session.add.call_args.args[0]
        assert len(entry.entity_id) == 64
        assert entry.entity_id.startswith("sales_e2e_channel_dimension:")
