"""批量推断历史 API 单测（服务端持久化，跨设备/团队可见）。

覆盖 POST 写入+裁剪+审计、GET 列表、DELETE 清空当前用户自己的记录。
仅覆盖 http 路由层行为，DB 以 FakeSession（记录 add/flush）+ patch 注入，
无外部依赖（对齐 test_collector_api.py 风格）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import Update

from app.api import deps
from app.main import app


class FakeSession:
    """最小可测 session：记录 add/flush 行为，execute 按语句类型返回可控结果。"""

    def __init__(self) -> None:
        self.added: list = []
        self.committed = False
        self.executed: list = []
        self.keep_ids: list = [1]

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        for i, r in enumerate(self.added, start=1):
            if getattr(r, "id", None) is None:
                r.id = i

    async def execute(self, stmt):
        self.executed.append(stmt)
        # update（裁剪/清空）返回可控 rowcount；select 按语句特征返回可控结果
        if isinstance(stmt, Update):
            return SimpleNamespace(rowcount=0)
        result = MagicMock()
        if self._is_id_select(stmt):
            # keep_ids 物化查询（select 单列 id）返回可控 id 列表
            result.scalars.return_value.all.return_value = self.keep_ids
        else:
            # 实体列表查询（select 整个实体）返回空
            result.scalars.return_value.all.return_value = []
        return result

    @staticmethod
    def _is_id_select(stmt) -> bool:
        cols = list(getattr(stmt, "selected_columns", []))
        return (
            len(cols) == 1
            and cols[0].name == "id"
            and getattr(cols[0].table, "name", None) == "batch_infer_history"
        )

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _row) -> None:
        pass

    async def rollback(self) -> None:
        pass


@pytest.fixture
async def history_client() -> AsyncIterator[tuple[httpx.AsyncClient, dict]]:
    holder: dict = {}

    async def fake_db() -> AsyncIterator[FakeSession]:
        session = FakeSession()
        holder["session"] = session
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=7,
        role="platform_admin",
        username="admin_lcp",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, holder
    app.dependency_overrides.clear()


async def test_create_history_persists_and_audits(
    history_client: tuple[httpx.AsyncClient, dict],
) -> None:
    """POST /batch-infer-history 写入一条记录：返回结构、add 落库、审计留痕。"""
    client, _ = history_client
    with patch("app.api.collector.write_audit", new_callable=AsyncMock) as mock_audit:
        resp = await client.post(
            "/api/v1/catalogs/batch-infer-history",
            json={
                "tables": [
                    {"catalog_id": 1, "entity_name": "fact_sales"},
                    {"catalog_id": 2, "entity_name": "dim_customer"},
                ],
                "done": 1,
                "failed": 1,
                "cancelled": 0,
                "added": 12,
                "elapsed": 34,
                "failed_tables": [{"catalog_id": 2, "entity_name": "dim_customer"}],
            },
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["id"] == 1
    assert data["actor_name"] == "admin_lcp"
    assert data["done"] == 1
    assert data["failed"] == 1
    assert data["added"] == 12
    assert data["elapsed"] == 34
    assert data["failed_tables"] == [{"catalog_id": 2, "entity_name": "dim_customer"}]
    assert len(data["tables"]) == 2
    # 审计留痕（治理动作可追溯）
    mock_audit.assert_awaited_once()
    kwargs = mock_audit.await_args.kwargs
    assert kwargs["action"] == "catalog.batch_infer_history"
    assert kwargs["actor_id"] == 7


async def test_create_history_rejects_empty_tables(
    history_client: tuple[httpx.AsyncClient, dict],
) -> None:
    """POST 表集为空时 422（tables min_length=1）。"""
    client, _ = history_client
    resp = await client.post(
        "/api/v1/catalogs/batch-infer-history",
        json={
            "tables": [],
            "done": 0,
            "failed": 0,
            "cancelled": 0,
            "added": 0,
            "elapsed": 0,
        },
    )
    assert resp.status_code == 422


async def test_create_history_trims_with_id_list_not_subquery(
    history_client: tuple[httpx.AsyncClient, dict],
) -> None:
    """裁剪必须用物化 id 列表而非 NOT IN (SELECT ... LIMIT) 子查询（MySQL 1235 不支持）。"""
    client, holder = history_client
    with patch("app.api.collector.write_audit", new_callable=AsyncMock):
        resp = await client.post(
            "/api/v1/catalogs/batch-infer-history",
            json={
                "tables": [{"catalog_id": 1, "entity_name": "fact_sales"}],
                "done": 1,
                "failed": 0,
                "cancelled": 0,
                "added": 5,
                "elapsed": 9,
                "failed_tables": [],
            },
        )
    assert resp.status_code == 200
    # 裁剪 Update 语句编译后不得含 LIMIT（避免 NOT IN 子查询触发 MySQL 1235）
    prune_stmt = next(
        s for s in holder["session"].executed if isinstance(s, Update)
    )
    assert "LIMIT" not in str(prune_stmt).upper()


async def test_create_history_skips_prune_when_no_keep_ids(
    history_client: tuple[httpx.AsyncClient, dict],
) -> None:
    """keep_ids 为空时跳过裁剪更新（not_in(空) 会渲染为永真条件导致全表软删）。"""
    client, holder = history_client
    # 先触发一次请求让 fake_db 创建 session，再置空 keep_ids 并清空已记录语句
    await client.get("/api/v1/catalogs/batch-infer-history")
    session = holder["session"]
    session.keep_ids = []
    session.executed.clear()
    with patch("app.api.collector.write_audit", new_callable=AsyncMock):
        resp = await client.post(
            "/api/v1/catalogs/batch-infer-history",
            json={
                "tables": [{"catalog_id": 1, "entity_name": "fact_sales"}],
                "done": 1,
                "failed": 0,
                "cancelled": 0,
                "added": 5,
                "elapsed": 9,
                "failed_tables": [],
            },
        )
    assert resp.status_code == 200
    assert not any(isinstance(s, Update) for s in session.executed)


async def test_list_history_returns_entries(
    history_client: tuple[httpx.AsyncClient, dict],
) -> None:
    """GET /batch-infer-history 返回列表（fake 无数据时为空数组）。"""
    client, _ = history_client
    resp = await client.get("/api/v1/catalogs/batch-infer-history")
    assert resp.status_code == 200
    assert resp.json()["data"] == []


async def test_clear_history_soft_deletes_own_and_audits(
    history_client: tuple[httpx.AsyncClient, dict],
) -> None:
    """DELETE /batch-infer-history 清空当前用户自己的记录并审计。"""
    client, _ = history_client
    with patch("app.api.collector.write_audit", new_callable=AsyncMock) as mock_audit:
        resp = await client.delete("/api/v1/catalogs/batch-infer-history")
    assert resp.status_code == 200
    assert resp.json()["data"]["cleared"] == 0
    mock_audit.assert_awaited_once()
    assert mock_audit.await_args.kwargs["action"] == "catalog.batch_infer_history_clear"
