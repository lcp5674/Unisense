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
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=1, role="platform_admin")
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
            action="CONFLICT_ARBITRATE",
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
        assert entry.action == "CONFLICT_ARBITRATE"
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
