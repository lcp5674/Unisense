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
        id=1, role="platform_admin"
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
