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
from app.services.global_search.repository import (
    GlobalSearchRepository,
    _escape_like,
    _expand_keywords,
)
from app.services.global_search.service import GlobalSearchService


def _session() -> MagicMock:
    s = MagicMock()
    s.execute = AsyncMock()
    return s


def _disabled_es() -> MagicMock:
    """ES 禁用客户端：本组单测专注 MySQL 路径，显式关闭 ES 保结果确定性（不连本地 ES）。"""
    es = MagicMock()
    es.enabled = False
    return es


def _repo(s: MagicMock) -> GlobalSearchRepository:
    return GlobalSearchRepository(s, es_client=_disabled_es())


def _rows_result(*rows: object) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value.all.return_value = list(rows)
    return r


def _all_result(*rows: object) -> MagicMock:
    """供 select(Metric, match_reason) 行查询使用（走 result.all() 而非 scalars）。"""
    r = MagicMock()
    r.all.return_value = list(rows)
    return r


def _metric(code: str = "sales_gmv_day", name: str = "每日GMV") -> SimpleNamespace:
    return SimpleNamespace(
        id=1, metric_code=code, name=name, domain="sales", status="PUBLISHED", pii_flag=False
    )


def _dimension(code: str = "region") -> SimpleNamespace:
    return SimpleNamespace(id=2, dim_code=code, name="区域", domain="sales", status="PUBLISHED")


def _term(code: str = "gmv") -> SimpleNamespace:
    return SimpleNamespace(
        id=3, term_code=code, name="成交总额", domain="sales", status="PUBLISHED"
    )


def _template(code: str = "tpl_sales_orders") -> SimpleNamespace:
    return SimpleNamespace(
        id=4, code=code, name="订单模板", domain="sales", description="订单指标", is_active=True
    )


def _source(source_id: str = "mysql_finance") -> SimpleNamespace:
    return SimpleNamespace(
        id=5,
        source_id=source_id,
        name="财务MySQL",
        domain="finance",
        health_status="healthy",
        source_type="mysql",
        description="财务结算库",
    )


def _catalog(
    entity_name: str = "finance.dwd_order", columns: list[dict] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        id=6,
        entity_name=entity_name,
        source_id="mysql_finance",
        entity_type="TABLE",
        sensitivity_level="INTERNAL",
        description="销售订单明细表",
        schema_json={"columns": columns or []},
    )


def _domain(code: str = "sales") -> SimpleNamespace:
    return SimpleNamespace(id=7, code=code, name="销售域", level=1, status="active")


def _measure(code: str = "pay_amt") -> SimpleNamespace:
    return SimpleNamespace(
        id=8, measure_code=code, name="支付金额", domain="sales", status="PUBLISHED"
    )


class TestEscapeLike:
    def test_escapes_wildcards(self) -> None:
        assert _escape_like("a%b_c") == "a/%b/_c"
        assert _escape_like("a/b") == "a//b"
        assert _escape_like("plain") == "plain"


class TestGlobalSearchRepository:
    async def test_search_groups_each_type(self) -> None:
        """9 类资源各自查询，按类型分组返回。"""
        s = _session()
        repo = _repo(s)
        s.execute = AsyncMock(
            side_effect=[
                _all_result((_metric(), "field")),  # metric（行含 match_reason）
                _rows_result(_dimension()),  # dimension
                _rows_result(_term()),  # term
                _rows_result(_template()),  # template
                _rows_result(_source()),  # data_source
                _rows_result(_catalog()),  # catalog
                _rows_result(),  # field（本次无命中）
                _rows_result(_domain()),  # subject_domain
                _rows_result(_measure()),  # measure（度量目录）
            ]
        )

        groups = await repo.search("sales", limit=5)

        assert groups["metric"][0]["type"] == "metric"
        assert groups["metric"][0]["code"] == "sales_gmv_day"
        assert groups["metric"][0]["match_reason"] == "field"
        assert groups["dimension"][0]["code"] == "region"
        assert groups["term"][0]["code"] == "gmv"
        assert groups["template"][0]["code"] == "tpl_sales_orders"
        assert groups["template"][0]["status"] == "ACTIVE"
        assert groups["data_source"][0]["code"] == "mysql_finance"
        assert groups["catalog"][0]["code"] == "finance.dwd_order"
        assert groups["field"] == []
        assert groups["subject_domain"][0]["code"] == "sales"
        assert groups["measure"][0]["code"] == "pay_amt"
        assert groups["measure"][0]["type"] == "measure"
        # 9 类各执行一次查询
        assert s.execute.await_count == 9

    async def test_search_blank_returns_all_empty(self) -> None:
        """空/纯空白关键词不触发任何查询，返回全空分组。"""
        s = _session()
        groups = await _repo(s).search("   ", limit=5)
        assert all(v == [] for v in groups.values())
        s.execute.assert_not_awaited()

    async def test_field_search_extracts_matching_column(self) -> None:
        """字段级搜索：schema_json 中列名命中关键词 → 返回独立 field 条目（含表名）。"""
        s = _session()
        repo = _repo(s)
        s.execute = AsyncMock(
            return_value=_rows_result(
                _catalog(
                    "finance.dwd_order",
                    columns=[
                        {"name": "order_id", "type": "bigint"},
                        {"name": "buyer_phone", "type": "varchar"},
                    ],
                ),
                _catalog("finance.dwd_pay", columns=[{"name": "pay_id", "type": "bigint"}]),
            )
        )

        fields = await repo._search_fields("phone", limit=5)

        assert len(fields) == 1
        assert fields[0]["type"] == "field"
        assert fields[0]["code"] == "buyer_phone"
        assert fields[0]["table_name"] == "finance.dwd_order"
        assert fields[0]["source_id"] == "mysql_finance"

    async def test_field_search_blank_returns_empty(self) -> None:
        s = _session()
        assert await _repo(s)._search_fields("", 5) == []
        s.execute.assert_not_awaited()

    async def test_search_escapes_like_wildcards(self) -> None:
        """LIKE 通配符被转义，防模糊放大。"""
        s = _session()
        repo = _repo(s)
        s.execute = AsyncMock(side_effect=[_rows_result()] * 9)
        await repo.search("100%", limit=5)
        metric_stmt = s.execute.call_args_list[0].args[0]
        compiled = str(metric_stmt.compile(compile_kwargs={"literal_binds": True}))
        # 转义符为 /：% 转义为 /% （100% → 100/%），并生成 ESCAPE '/' 子句
        assert "/%" in compiled
        assert "ESCAPE '/'" in compiled

    async def test_metric_synonym_hit_reports_match_reason(self) -> None:
        """指标经关联逻辑度量同义词命中 → match_reason=synonym（供前端"您是不是想找…"提示）。"""
        s = _session()
        repo = _repo(s)
        s.execute = AsyncMock(return_value=_all_result((_metric(), "synonym")))

        items = await repo._search_metrics(["%pay%"], limit=5)

        assert items[0]["code"] == "sales_gmv_day"
        assert items[0]["match_reason"] == "synonym"
        assert items[0]["type"] == "metric"

    async def test_metric_synonyms_join_in_sql(self) -> None:
        """指标检索 SQL 外连接 measure_catalog，同义词列参与 LIKE 匹配（参数化 + 转义）。"""
        s = _session()
        repo = _repo(s)
        s.execute = AsyncMock(return_value=_all_result())

        await repo.search("支付", limit=5)
        metric_stmt = s.execute.call_args_list[0].args[0]
        compiled = str(metric_stmt.compile(compile_kwargs={"literal_binds": True}))

        assert "measure_catalog" in compiled
        assert "synonyms" in compiled.lower()
        assert "ESCAPE '/'" in compiled
        # 直接命中优先级高于同义词：CASE 中 field 标签先于 synonym 标签
        assert compiled.index("'field'") < compiled.index("'synonym'")

    async def test_metric_synonyms_case_prioritizes_direct_match(self) -> None:
        """同义词与直接字段都命中时 match_reason=field，避免"同义词匹配"标签噪音。"""
        s = _session()
        repo = _repo(s)
        s.execute = AsyncMock(return_value=_all_result((_metric(), "field")))

        items = await repo._search_metrics(["%sales_gmv%"], limit=5)

        assert items[0]["code"] == "sales_gmv_day"
        assert items[0]["match_reason"] == "field"

    async def test_term_search_matches_synonyms(self) -> None:
        """术语同义词命中：term.synonyms（JSON）粗匹配参与检索。"""
        s = _session()
        repo = _repo(s)
        s.execute = AsyncMock(side_effect=[_rows_result()] * 9)

        await repo.search("成交", limit=5)
        # 第 3 次执行为 term 查询（9 类顺序：metric/dimension/term/...）
        term_stmt = s.execute.call_args_list[2].args[0]
        compiled = str(term_stmt.compile(compile_kwargs={"literal_binds": True}))

        assert "synonyms" in compiled.lower()
        assert "ESCAPE '/'" in compiled
        assert "成交" in compiled

    # ---- 描述类字段全覆盖（TD§12.1）：表/数据源/主题域描述、术语边界、字段注释 ----

    async def test_catalog_search_matches_description(self) -> None:
        """采集目录表级业务描述纳入检索（搜中文描述找到英文表名）。"""
        s = _session()
        repo = _repo(s)
        s.execute = AsyncMock(return_value=_rows_result(_catalog()))

        items = await repo._search_catalogs(["%订单%"], limit=5)

        assert items[0]["code"] == "finance.dwd_order"
        stmt = s.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "description" in compiled.lower()
        assert "销售订单明细表" in compiled or "%订单%" in compiled

    async def test_data_source_search_matches_description(self) -> None:
        """数据源用途描述纳入检索。"""
        s = _session()
        repo = _repo(s)
        s.execute = AsyncMock(return_value=_rows_result(_source()))

        items = await repo._search_data_sources(["%结算%"], limit=5)

        assert items[0]["code"] == "mysql_finance"
        stmt = s.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "description" in compiled.lower()

    async def test_subject_domain_search_matches_description(self) -> None:
        """主题域描述纳入检索。"""
        s = _session()
        repo = _repo(s)
        s.execute = AsyncMock(return_value=_rows_result(_domain()))

        items = await repo._search_subject_domains(["%销售%"], limit=5)

        assert items[0]["code"] == "sales"
        stmt = s.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "description" in compiled.lower()

    async def test_term_search_matches_boundary(self) -> None:
        """术语边界说明（boundary）纳入检索。"""
        s = _session()
        repo = _repo(s)
        s.execute = AsyncMock(side_effect=[_rows_result()] * 9)

        await repo.search("口径", limit=5)
        term_stmt = s.execute.call_args_list[2].args[0]
        compiled = str(term_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "boundary" in compiled.lower()

    async def test_field_search_matches_column_comment(self) -> None:
        """字段注释（col.comment）命中返回该列——修复此前注释命中被内存精筛丢弃。"""
        s = _session()
        repo = _repo(s)
        s.execute = AsyncMock(
            return_value=_rows_result(
                _catalog(
                    "finance.dwd_order",
                    columns=[{"name": "order_id", "type": "bigint", "comment": "订单ID"}],
                )
            )
        )

        fields = await repo._search_fields("订单ID", limit=5)

        assert len(fields) == 1
        assert fields[0]["code"] == "order_id"
        assert fields[0]["table_name"] == "finance.dwd_order"

    # ---- 度量目录（FR-18 覆盖度量目录模块，新增 measure 分组）----

    async def test_measure_search(self) -> None:
        """度量目录按编码/名称/描述/口径/同义词检索，独立 measure 分组。"""
        s = _session()
        repo = _repo(s)
        s.execute = AsyncMock(return_value=_rows_result(_measure()))

        items = await repo._search_measures(["%支付%"], limit=5)

        assert items[0]["type"] == "measure"
        assert items[0]["code"] == "pay_amt"
        assert items[0]["name"] == "支付金额"
        assert items[0]["status"] == "PUBLISHED"

    async def test_measure_search_sql_includes_description_fields(self) -> None:
        """度量目录 SQL 覆盖编码/名称/描述/统计口径/源头系统/同义词。"""
        s = _session()
        repo = _repo(s)
        s.execute = AsyncMock(return_value=_rows_result())

        await repo._search_measures(["%pay%"], limit=5)
        stmt = s.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
        for col in ("measure_code", "description", "stat_caliber", "source_system", "synonyms"):
            assert col in compiled, f"度量目录检索缺少列 {col}"

    # ---- 中英同义词扩展（search/synonyms.py，双向）----

    async def test_synonym_expansion_chinese_to_english(self) -> None:
        """搜中文"订单" → LIKE 候选含英文 order/sales_order（命中英文表名）。"""
        s = _session()
        repo = _repo(s)
        s.execute = AsyncMock(return_value=_rows_result())

        await repo._search_catalogs(["%订单%", "%order%", "%sales_order%"], limit=5)
        stmt = s.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "%order%" in compiled
        assert "%sales_order%" in compiled

    def test_expand_keywords_chinese_to_english(self) -> None:
        assert _expand_keywords("订单") == ["订单", "order", "sales_order"]
        assert _expand_keywords("order") == ["order", "订单"]
        assert _expand_keywords("未知词") == ["未知词"]
        assert _expand_keywords("  ") == []

    # ---- ES 检索接线（TD §1.3：ES 优先，降级 MySQL LIKE）----

    def _enabled_es(self, hits: list[dict]) -> MagicMock:
        es = MagicMock()
        es.enabled = True
        es.search = AsyncMock(return_value={"hits": {"hits": hits}})
        return es

    async def test_metric_search_uses_es_when_enabled(self) -> None:
        """ES 启用时指标检索走 ES（multi_match），返回结构对齐 MySQL 路径。"""
        s = _session()
        es = self._enabled_es(
            [
                {
                    "_source": {
                        "id": 1,
                        "metric_code": "sales_gmv_day",
                        "name": "销售GMV",
                        "domain": "sales",
                        "status": "PUBLISHED",
                        "pii_flag": False,
                    }
                }
            ]
        )
        repo = GlobalSearchRepository(s, es_client=es)
        items = await repo._search_metrics(["%gmv%"], limit=5, raw_q="gmv")
        assert items[0]["code"] == "sales_gmv_day"
        assert items[0]["match_reason"] == "field"
        es.search.assert_awaited_once()
        s.execute.assert_not_awaited()  # ES 命中不再走 MySQL

    async def test_term_search_uses_es_when_enabled(self) -> None:
        """ES 启用时术语检索走 ES。"""
        s = _session()
        es = self._enabled_es(
            [
                {
                    "_source": {
                        "id": 2,
                        "term_code": "gmv",
                        "name": "成交总额",
                        "domain": "sales",
                        "status": "ACTIVE",
                    }
                }
            ]
        )
        repo = GlobalSearchRepository(s, es_client=es)
        items = await repo._search_terms(["%gmv%"], limit=5, raw_q="gmv")
        assert items[0]["code"] == "gmv"
        es.search.assert_awaited_once()
        s.execute.assert_not_awaited()

    async def test_term_es_search_uses_definition_field(self) -> None:
        """术语 ES fields 用 definition（term_idx 实际字段名），修复 description 不匹配 bug。"""
        s = _session()
        es = self._enabled_es([])  # 零命中 → 降级 MySQL，但 es.search 调用已被断言
        repo = GlobalSearchRepository(s, es_client=es)
        s.execute = AsyncMock(return_value=_rows_result())

        await repo._search_terms(["%gmv%"], limit=5, raw_q="gmv")

        body = es.search.call_args.args[1]
        fields = body["query"]["multi_match"]["fields"]
        assert "definition" in fields
        assert "description" not in fields
        # metric 路径仍用 description
        await repo._search_metrics(["%gmv%"], limit=5, raw_q="gmv")
        body_metric = es.search.call_args.args[1]
        fields_metric = body_metric["query"]["multi_match"]["fields"]
        assert "description" in fields_metric

    async def test_es_search_fallback_to_mysql(self) -> None:
        """ES 异常（down/熔断）时降级 MySQL LIKE，不阻断检索。"""
        s = _session()
        es = MagicMock()
        es.enabled = True
        es.search = AsyncMock(side_effect=RuntimeError("es down"))
        repo = GlobalSearchRepository(s, es_client=es)
        s.execute = AsyncMock(return_value=_all_result((_metric(), "field")))
        items = await repo._search_metrics(["%gmv%"], limit=5, raw_q="gmv")
        assert items[0]["code"] == "sales_gmv_day"
        s.execute.assert_awaited()

    async def test_es_zero_hits_fallbacks_to_mysql(self) -> None:
        """ES 零命中降级 MySQL（空结果不应短路成空列表）。"""
        s = _session()
        es = self._enabled_es([])
        repo = GlobalSearchRepository(s, es_client=es)
        s.execute = AsyncMock(return_value=_all_result((_metric(), "field")))
        items = await repo._search_metrics(["%gmv%"], limit=5, raw_q="gmv")
        assert items[0]["code"] == "sales_gmv_day"
        s.execute.assert_awaited()

    async def test_es_disabled_skips_es(self) -> None:
        """ES 未配置/未启用（enabled=False）直接走 MySQL，不发起 ES 调用。"""
        s = _session()
        es = MagicMock()
        es.enabled = False
        es.search = AsyncMock()
        repo = GlobalSearchRepository(s, es_client=es)
        s.execute = AsyncMock(return_value=_all_result((_metric(), "field")))
        items = await repo._search_metrics(["%gmv%"], limit=5, raw_q="gmv")
        assert items[0]["code"] == "sales_gmv_day"
        es.search.assert_not_awaited()


class TestGlobalSearchService:
    async def test_service_delegates_to_repo(self) -> None:
        s = _session()
        svc = GlobalSearchService(s)
        svc._repo = MagicMock()
        svc._repo.search = AsyncMock(return_value={"metric": [{"type": "metric"}]})

        out = await svc.search("sales", limit=5)

        assert out["metric"][0]["type"] == "metric"
        svc._repo.search.assert_awaited_once_with(
            "sales", 5, visible_actor_id=None, visible_role=None
        )


@pytest.fixture
async def search_client() -> AsyncIterator[httpx.AsyncClient]:
    """覆盖 DB 会话与当前用户依赖（读角色 viewer）。"""

    async def fake_db():
        session = MagicMock()
        session.execute = AsyncMock(side_effect=[MagicMock()] * 9)
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="viewer", roles_all=lambda: ["viewer"], has_role=lambda r: r == "viewer"
    )
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
        assert "measure" in data["groups"]

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


class TestGlobalSearchVisibility:
    """D-1 读路径行级隔离：全局搜索不得经侧门检索他人未发布资产。"""

    def _render(self, cond: object) -> str:
        """渲染 SQL 表达式为字面值（in_ 不展开需 compile literal_binds）。"""
        from sqlalchemy.dialects import mysql
        return str(cond.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))

    async def test_visibility_conditions_non_admin(self) -> None:
        """非管理角色返回 OR 可见性条件（公开状态 + 本人负责）。"""
        repo = _repo(_session())
        cond = repo._visibility_conditions(visible_actor_id=7, visible_role="viewer")
        assert cond is not None
        rendered = self._render(cond)
        assert "PUBLISHED" in rendered and "EXPERIMENTAL" in rendered and "DEPRECATED" in rendered
        assert "owner_id" in rendered and "backup_owner_id" in rendered

    async def test_visibility_conditions_admin(self) -> None:
        """管理角色不加过滤（platform_admin/domain_admin 可检索全部）。"""
        repo = _repo(_session())
        assert repo._visibility_conditions(7, "platform_admin") is None
        assert repo._visibility_conditions(7, "domain_admin") is None
        assert repo._visibility_conditions(None, None) is None

    async def test_visibility_conditions_reviewer_extra_review(self) -> None:
        """reviewer 额外放行 REVIEW（评审工作台需看待审项）。"""
        repo = _repo(_session())
        cond = self._render(repo._visibility_conditions(7, "reviewer"))
        assert "REVIEW" in cond

    async def test_es_search_applies_visibility_filter(self) -> None:
        """非管理角色 ES 查询用 bool.filter 收敛可见范围（与 MySQL 同语义）。"""
        s = _session()
        es = MagicMock()
        es.enabled = True
        es.search = AsyncMock(return_value={"hits": {"hits": []}})
        repo = GlobalSearchRepository(s, es_client=es)
        await repo._es_search_assets(
            "metric", "gmv", 5, visible_actor_id=7, visible_role="viewer"
        )
        body = es.search.call_args.args[1]
        q = body["query"]
        assert q["bool"]["must"]["multi_match"]["fields"] == [
            "code^3", "name^2", "description", "synonyms",
        ]
        filter_clause = q["bool"]["filter"][0]["bool"]["should"]
        statuses = {t for c in filter_clause for t in c.get("terms", {}).get("status", [])}
        assert {"PUBLISHED", "EXPERIMENTAL", "DEPRECATED"} <= statuses
        assert {"term": {"owner_id": 7}} in filter_clause
        assert {"term": {"backup_owner_id": 7}} in filter_clause

    async def test_es_search_no_filter_for_admin(self) -> None:
        """管理角色 ES 查询不加可见性 filter（保持 multi_match 顶层结构）。"""
        s = _session()
        es = MagicMock()
        es.enabled = True
        es.search = AsyncMock(return_value={"hits": {"hits": []}})
        repo = GlobalSearchRepository(s, es_client=es)
        await repo._es_search_assets(
            "metric", "gmv", 5, visible_actor_id=7, visible_role="platform_admin"
        )
        body = es.search.call_args.args[1]
        assert "bool" not in body["query"]
        assert "multi_match" in body["query"]


class TestGlobalSearchVisibility:
    """全局搜索读路径行级隔离（越权审查修复）：维度/术语分支透传可见性。"""

    async def test_dimension_term_search_applies_visibility(self) -> None:
        """非管理角色搜索：dimension/term 查询携带可见性过滤（防搜索侧门窥探草稿）。"""
        s = _session()
        repo = _repo(s)
        s.execute = AsyncMock(
            side_effect=[
                _all_result(),  # metric（无命中）
                _rows_result(),  # dimension（无命中）
                _rows_result(),  # term（无命中）
                _rows_result(),  # template
                _rows_result(),  # data_source
                _rows_result(),  # catalog
                _rows_result(),  # field
                _rows_result(),  # subject_domain
                _rows_result(),  # measure
            ]
        )
        await repo.search(
            "sales",
            limit=5,
            visible_actor_id=9,
            visible_role="metric_owner",
        )
        dim_sql = str(
            s.execute.call_args_list[1][0][0].compile(
                compile_kwargs={"literal_binds": True}
            )
        )
        term_sql = str(
            s.execute.call_args_list[2][0][0].compile(
                compile_kwargs={"literal_binds": True}
            )
        )
        assert "dimension.status IN ('PUBLISHED', 'DEPRECATED')" in dim_sql
        assert "dimension.owner_id = 9" in dim_sql
        assert "term.status IN ('PUBLISHED', 'DEPRECATED')" in term_sql
        assert "term.owner_id = 9" in term_sql

    async def test_search_admin_no_visibility(self) -> None:
        """管理角色搜索不加可见性过滤（治理视角全量）。"""
        s = _session()
        repo = _repo(s)
        s.execute = AsyncMock(
            side_effect=[
                _all_result(),
                _rows_result(),
                _rows_result(),
                _rows_result(),
                _rows_result(),
                _rows_result(),
                _rows_result(),
                _rows_result(),
                _rows_result(),
            ]
        )
        await repo.search("sales", limit=5, visible_actor_id=1, visible_role="platform_admin")
        dim_sql = str(s.execute.call_args_list[1][0][0].compile())
        term_sql = str(s.execute.call_args_list[2][0][0].compile())
        assert "IN ('PUBLISHED', 'DEPRECATED')" not in dim_sql
        assert "IN ('PUBLISHED', 'DEPRECATED')" not in term_sql
