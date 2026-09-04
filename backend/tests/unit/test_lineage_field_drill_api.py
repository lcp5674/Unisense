"""血缘字段级钻取 API 测试（方案 B：表级血缘 → 字段级子图）。

覆盖：/lineage/field-drill 组装节点/边/明细、源列空降级行排除、空表返回空子图。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app


def _mapping(
    source_table: str,
    source_column: str | None,
    target_table: str,
    target_column: str,
    expression: str | None = None,
    confidence: float = 1.0,
    provenance: str = "dp_sql",
) -> SimpleNamespace:
    """构造字段映射行（模拟 LineageFieldMapping ORM 对象访问属性）。"""
    return SimpleNamespace(
        source_table=source_table,
        source_column=source_column,
        target_table=target_table,
        target_column=target_column,
        expression=expression,
        confidence=confidence,
        provenance=provenance,
        task_id=1,
        step_id=2,
    )


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """覆盖 DB 会话与当前用户（平台管理员）。"""

    async def fake_db() -> AsyncIterator[MagicMock]:
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock())
        session.commit = AsyncMock()
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        org_id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
        domains_all=lambda: [],
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_field_drill_returns_subgraph_and_mappings(client: httpx.AsyncClient) -> None:
    """命中该表作为源/目标的列映射：节点去重、边 DERIVED_FROM、明细带表达式。"""
    rows = [
        _mapping(
            "ods.a",
            "id",
            "dwd.b",
            "id",
        ),
        _mapping(
            "ods.a",
            "name",
            "dwd.b",
            "name",
            expression="COALESCE(name,'-')",
        ),
        # dwd.b 作为源 → 下游 dws.c（该表下游也纳入钻取）
        _mapping("dwd.b", "cnt", "dws.c", "cnt"),
    ]
    with patch("app.api.lineage.DpLineageRepository") as repo_cls:
        repo = repo_cls.return_value
        repo.list_field_mappings_by_table = AsyncMock(return_value=rows)
        resp = await client.get("/api/v1/lineage/field-drill?table=dwd.b")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["table"] == "dwd.b"
    ids = {n["id"] for n in data["nodes"]}
    assert "field:ods.a.id" in ids
    assert "field:dwd.b.id" in ids
    assert "field:dwd.b.cnt" in ids
    assert "field:dws.c.cnt" in ids
    # 每行映射一条 DERIVED_FROM 字段边
    assert len(data["edges"]) == 3
    assert all(e["type"] == "DERIVED_FROM" for e in data["edges"])
    # 明细带表达式/来源/任务
    assert len(data["mappings"]) == 3
    expr = next(m for m in data["mappings"] if m["target_column"] == "name")
    assert expr["expression"] == "COALESCE(name,'-')"
    assert expr["provenance"] == "dp_sql"


async def test_field_drill_skips_degraded_placeholder_rows(
    client: httpx.AsyncClient,
) -> None:
    """降级占位（source_column=None）行不入字段图（由表级血缘承载，不伪造映射）。"""
    rows = [
        _mapping("ods.a", "id", "dwd.b", "id"),
        _mapping("ods.a", None, "dwd.b", "cnt", expression="count(1)"),
    ]
    with patch("app.api.lineage.DpLineageRepository") as repo_cls:
        repo = repo_cls.return_value
        repo.list_field_mappings_by_table = AsyncMock(return_value=rows)
        resp = await client.get("/api/v1/lineage/field-drill?table=dwd.b")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["nodes"]) == 2  # 仅 ods.a.id / dwd.b.id
    assert len(data["edges"]) == 1
    assert len(data["mappings"]) == 1


async def test_field_drill_empty_table_returns_empty_subgraph(
    client: httpx.AsyncClient,
) -> None:
    """无字段映射的表：返回空子图（前端据此展示「无字段级血缘」）。"""
    with patch("app.api.lineage.DpLineageRepository") as repo_cls:
        repo = repo_cls.return_value
        repo.list_field_mappings_by_table = AsyncMock(return_value=[])
        resp = await client.get("/api/v1/lineage/field-drill?table=ods.no_mapping")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["table"] == "ods.no_mapping"
    assert data["nodes"] == []
    assert data["edges"] == []
    assert data["mappings"] == []


async def test_field_impact_returns_mappings_and_nodes(
    client: httpx.AsyncClient,
) -> None:
    """字段级影响分析：透传 node/direction/max_hops/limit，返回字段映射边与节点元数据。"""
    from app.services.lineage.schemas import FieldImpactResponse

    fake = FieldImpactResponse(
        node="table:dwd.b",
        direction="downstream",
        total=2,
        items=[
            {
                "id": 21,
                "source_table": "dwd.b",
                "source_column": "id",
                "target_table": "dws.c",
                "target_column": "id",
                "source_node": "field:dwd.b.id",
                "target_node": "field:dws.c.id",
                "expression": None,
                "confidence": 1.0,
                "provenance": "dp_sql",
                "hops": 1,
            },
            {
                "id": 22,
                "source_table": "dws.c",
                "source_column": "id",
                "target_table": "ads.d",
                "target_column": "id",
                "source_node": "field:dws.c.id",
                "target_node": "field:ads.d.id",
                "expression": None,
                "confidence": 1.0,
                "provenance": "dp_sql",
                "hops": 2,
            },
        ],
        nodes=[
            {"id": "field:dwd.b.id", "type": "field", "label": "dwd.b.id"},
            {"id": "field:dws.c.id", "type": "field", "label": "dws.c.id"},
        ],
    )
    with patch(
        "app.services.lineage.service.LineageService.field_impact",
        new=AsyncMock(return_value=fake),
    ):
        resp = await client.get(
            "/api/v1/lineage/field-impact?node=table:dwd.b&direction=downstream&max_hops=3"
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["node"] == "table:dwd.b"
    assert data["total"] == 2
    assert data["items"][0]["source_node"] == "field:dwd.b.id"
    assert data["items"][1]["hops"] == 2
    assert data["nodes"][0]["id"] == "field:dwd.b.id"


async def test_field_impact_rejects_field_without_permission(
    client: httpx.AsyncClient,
) -> None:
    """读角色之外的账号（viewer）调 field-impact 被 403（_READ_ROLES 门禁）。"""
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=9,
        org_id=1,
        role="viewer",
        roles_all=lambda: ["viewer"],
        has_role=lambda r: r == "viewer",
        domains_all=lambda: [],
    )
    resp = await client.get(
        "/api/v1/lineage/field-impact?node=table:dwd.b&direction=downstream"
    )
    assert resp.status_code == 403
    app.dependency_overrides.clear()
