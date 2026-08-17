"""血缘领域安全反向测试（对齐 gateways security_reverse）。

覆盖：普通用户调写接口 -> 403 FORBIDDEN；SQL 注入 fuzz（注入）-> 被拦截
（INJECTION_DETECTED）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from app.services.lineage.schemas import LineageParseBatchResponse, LineageParseResponse


@pytest.fixture
async def analyst_client():
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=5, role="analyst")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def owner_client():
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=5, role="metric_owner")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, session
    app.dependency_overrides.clear()


async def test_analyst_cannot_parse_lineage_403(analyst_client):
    resp = await analyst_client.post(
        "/api/v1/lineage/parse",
        json={"sql": "INSERT INTO t SELECT a.id FROM a"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 403
    assert "FORBIDDEN" in resp.text


async def test_injection_keyword_blocked_on_impact(owner_client):
    client, _ = owner_client
    resp = await client.get(
        "/api/v1/lineage/impact?node=' OR 1=1 -- ",
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 400
    assert "INJECTION_DETECTED" in resp.text  # 注入关键字被守卫拦截


async def test_parse_accepts_legit_sql_with_comments_and_union(owner_client, monkeypatch):
    """/parse 的 sql 字段是待解析文本（仅经 sqlglot 纯函数解析），合法 SQL 含
    -- 注释 / /* */ 块注释 / UNION ALL 不应再被注入守卫误伤为 400。"""
    client, _ = owner_client
    svc = AsyncMock()
    svc.parse_and_store.return_value = LineageParseResponse(
        table_edges=2, field_edges=0, graph_written=False
    )
    monkeypatch.setattr("app.api.lineage._svc", lambda db: svc)
    resp = await client.post(
        "/api/v1/lineage/parse",
        json={
            "sql": (
                "SELECT u.id, o.amount FROM db1.users u -- 取用户与订单\n"
                "JOIN db2.orders o /* +SET_VAR(enable_vectorized_engine=false) */\n"
                "ON u.id = o.uid\n"
                "UNION ALL SELECT id, amount FROM db3.archive"
            ),
            "dialect": "doris",
        },
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["table_edges"] == 2
    svc.parse_and_store.assert_awaited_once()


async def test_parse_still_blocks_injection_in_other_fields(owner_client, monkeypatch):
    """豁免仅作用于 sql 字段；provenance 等其他字段命中注入仍应 400 拦截。"""
    client, _ = owner_client
    svc = AsyncMock()
    monkeypatch.setattr("app.api.lineage._svc", lambda db: svc)
    resp = await client.post(
        "/api/v1/lineage/parse",
        json={"sql": "SELECT 1", "provenance": "x'; drop table users--"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 400
    assert "INJECTION_DETECTED" in resp.text
    svc.parse_and_store.assert_not_awaited()


async def test_parse_batch_accepts_legit_sql_in_statements_and_text(owner_client, monkeypatch):
    """/parse-batch 的 statements/text 字段与 /parse 的 sql 同理是待解析 SQL 文本：
    含 -- 注释 / UNION ALL 的合法 SQL 不应被注入守卫误伤为 400。"""
    client, _ = owner_client
    svc = AsyncMock()
    svc.parse_batch.return_value = LineageParseBatchResponse(
        total_statements=2,
        succeeded=2,
        failed=0,
        total_edges=3,
        added=3,
        updated=0,
        skipped=0,
        graph_written=False,
        statements=[],
    )
    monkeypatch.setattr("app.api.lineage._svc", lambda db: svc)
    # statements 数组：第一条含 -- 注释，第二条含 UNION ALL
    resp = await client.post(
        "/api/v1/lineage/parse-batch",
        json={
            "dialect": "doris",
            "statements": [
                "INSERT INTO dws.t SELECT u.id FROM db1.users u -- 上游用户",
                "INSERT INTO dws.t SELECT id FROM db1.a UNION ALL SELECT id FROM db2.b",
            ],
        },
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["total_edges"] == 3
    svc.parse_batch.assert_awaited_once()
    # text 多语句文本块（含分号多语句 + 注释）同样豁免
    svc.reset_mock()
    resp = await client.post(
        "/api/v1/lineage/parse-batch",
        json={
            "dialect": "hive",
            "text": (
                "-- 批量 ETL\nINSERT INTO t1 SELECT id FROM s1; INSERT INTO t2 SELECT id FROM s2"
            ),
        },
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200
    svc.parse_batch.assert_awaited_once()


async def test_parse_batch_still_blocks_injection_in_other_fields(owner_client, monkeypatch):
    """豁免仅作用于 statements/text；provenance 等其他字段命中注入仍 400 拦截。"""
    client, _ = owner_client
    svc = AsyncMock()
    monkeypatch.setattr("app.api.lineage._svc", lambda db: svc)
    resp = await client.post(
        "/api/v1/lineage/parse-batch",
        json={
            "dialect": "mysql",
            "statements": ["SELECT 1"],
            "provenance": "x'; drop table users--",
        },
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 400
    assert "INJECTION_DETECTED" in resp.text
    svc.parse_batch.assert_not_awaited()
