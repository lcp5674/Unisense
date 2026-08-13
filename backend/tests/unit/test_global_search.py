"""全局聚合搜索单测（app/services/global_search + app/api/search.py）。

覆盖：
1. GlobalSearchRepository.search：8 类资源分组返回、字段级列名精确提取、空关键词
2. GlobalSearchService.search：透传编排
3. GET /api/v1/search 端点：统一信封 + RBAC 读角色 + 注入守卫
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from app.services.global_search.repository import GlobalSearchRepository, _escape_like
from app.services.global_search.service import GlobalSearchService


def _session() -> MagicMock:
    s = MagicMock()
    s.execute = AsyncMock()
    return s


def _rows_result(*rows: object) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value.all.return_value = list(rows)
    return r


def _metric(code: str = "sales_gmv_day", name: str = "每日GMV") -> SimpleNamespace:
    return SimpleNamespace(id=1, metric_code=code, name=name, domain="sales",
                           status="PUBLISHED", pii_flag=False)


def _dimension(code: str = "region") -> SimpleNamespace:
    return SimpleNamespace(id=2, dim_code=code, name="区域", domain="sales",
                           status="PUBLISHED")


def _term(code: str = "gmv") -> SimpleNamespace:
    return SimpleNamespace(id=3, term_code=code, name="成交总额", domain="sales",
                           status="PUBLISHED")


def _template(code: str = "tpl_sales_orders") -> SimpleNamespace:
    return SimpleNamespace(id=4, code=code, name="订单模板", domain="sales",
                           description="订单指标", is_active=True)


def _source(source_id: str = "mysql_finance") -> SimpleNamespace:
    return SimpleNamespace(id=5, source_id=source_id, name="财务MySQL",
                           domain="finance", health_status="healthy", source_type="mysql")


def _catalog(
    entity_name: str = "finance.dwd_order", columns: list[dict] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(id=6, entity_name=entity_name, source_id="mysql_finance",
                           entity_type="TABLE", sensitivity_level="INTERNAL",
                           schema_json={"columns": columns or []})


def _domain(code: str = "sales") -> SimpleNamespace:
    return SimpleNamespace(id=7, code=code, name="销售域", level=1, status="active")


class TestEscapeLike:
    def test_escapes_wildcards(self) -> None:
        assert _escape_like("a%b_c") == "a\\%b\\_c"
        assert _escape_like("plain") == "plain"


class TestGlobalSearchRepository:
    async def test_search_groups_each_type(self) -> None:
        """8 类资源各自查询，按类型分组返回。"""
        s = _session()
        repo = GlobalSearchRepository(s)
        s.execute = AsyncMock(side_effect=[
            _rows_result(_metric()),          # metric
            _rows_result(_dimension()),       # dimension
            _rows_result(_term()),            # term
            _rows_result(_template()),        # template
            _rows_result(_source()),          # data_source
            _rows_result(_catalog()),         # catalog
            _rows_result(),                   # field（本次无命中）
            _rows_result(_domain()),          # subject_domain
        ])

        groups = await repo.search("sales", limit=5)

        assert groups["metric"][0]["type"] == "metric"
        assert groups["metric"][0]["code"] == "sales_gmv_day"
        assert groups["dimension"][0]["code"] == "region"
        assert groups["term"][0]["code"] == "gmv"
        assert groups["template"][0]["code"] == "tpl_sales_orders"
        assert groups["template"][0]["status"] == "ACTIVE"
        assert groups["data_source"][0]["code"] == "mysql_finance"
        assert groups["catalog"][0]["code"] == "finance.dwd_order"
        assert groups["field"] == []
        assert groups["subject_domain"][0]["code"] == "sales"
        # 8 类各执行一次查询
        assert s.execute.await_count == 8

    async def test_search_blank_returns_all_empty(self) -> None:
        """空/纯空白关键词不触发任何查询，返回全空分组。"""
        s = _session()
        groups = await GlobalSearchRepository(s).search("   ", limit=5)
        assert all(v == [] for v in groups.values())
        s.execute.assert_not_awaited()

    async def test_field_search_extracts_matching_column(self) -> None:
        """字段级搜索：schema_json 中列名命中关键词 → 返回独立 field 条目（含表名）。"""
        s = _session()
        repo = GlobalSearchRepository(s)
        s.execute = AsyncMock(return_value=_rows_result(
            _catalog("finance.dwd_order", columns=[
                {"name": "order_id", "type": "bigint"},
                {"name": "buyer_phone", "type": "varchar"},
            ]),
            _catalog("finance.dwd_pay", columns=[{"name": "pay_id", "type": "bigint"}]),
        ))

        fields = await repo._search_fields("phone", limit=5)

        assert len(fields) == 1
        assert fields[0]["type"] == "field"
        assert fields[0]["code"] == "buyer_phone"
        assert fields[0]["table_name"] == "finance.dwd_order"
        assert fields[0]["source_id"] == "mysql_finance"

    async def test_field_search_blank_returns_empty(self) -> None:
        s = _session()
        assert await GlobalSearchRepository(s)._search_fields("", 5) == []
        s.execute.assert_not_awaited()

    async def test_search_escapes_like_wildcards(self) -> None:
        """LIKE 通配符被转义，防模糊放大。"""
        s = _session()
        repo = GlobalSearchRepository(s)
        s.execute = AsyncMock(side_effect=[_rows_result()] * 8)
        await repo.search("100%", limit=5)
        metric_stmt = s.execute.call_args_list[0].args[0]
        compiled = str(metric_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "\\%" in compiled


class TestGlobalSearchService:
    async def test_service_delegates_to_repo(self) -> None:
        s = _session()
        svc = GlobalSearchService(s)
        svc._repo = MagicMock()
        svc._repo.search = AsyncMock(return_value={"metric": [{"type": "metric"}]})

        out = await svc.search("sales", limit=5)

        assert out["metric"][0]["type"] == "metric"
        svc._repo.search.assert_awaited_once_with("sales", 5)


@pytest.fixture
async def search_client() -> AsyncIterator[httpx.AsyncClient]:
    """覆盖 DB 会话与当前用户依赖（读角色 viewer）。"""

    async def fake_db():
        session = MagicMock()
        session.execute = AsyncMock(side_effect=[MagicMock()] * 8)
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=1, role="viewer")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


class TestGlobalSearchAPI:
    async def test_search_returns_grouped_envelope(self, search_client: httpx.AsyncClient) -> None:
        resp = await search_client.get("/api/v1/search", params={"q": "sales"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == "OK"
        data = body["data"]
        assert "groups" in data
        assert "total" in data
        assert "metric" in data["groups"]
        assert "field" in data["groups"]
        assert "subject_domain" in data["groups"]

    async def test_search_blank_returns_422(self, search_client: httpx.AsyncClient) -> None:
        resp = await search_client.get("/api/v1/search", params={"q": ""})
        assert resp.status_code == 422

    async def test_search_forbidden_for_unauthenticated(self) -> None:
        """无登录用户（require_roles 兜底）应 403/401，不泄露数据。"""
        app.dependency_overrides[deps.get_db_session] = lambda: None
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/api/v1/search", params={"q": "sales"})
        app.dependency_overrides.clear()
        assert resp.status_code in (401, 403)
