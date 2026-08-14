"""资产地图 Repository 单测（补齐覆盖率 + 生产级缺口回归）。

覆盖：
- get_entity_detail：命中（含 lineage_count / schema 摘要 / PII 判定）、未命中
- _summarize_schema：fields / columns / 非 dict 退化
- graph_from_mysql：域过滤精确匹配（非 contains 子串）、节点 ID 统一 metric: 前缀、
  无展示节点时返回空边、pii_only 过滤
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.models.data_source import DBCatalog
from app.services.assetmap.repository import AssetMapRepository


def _session() -> MagicMock:
    s = MagicMock()
    s.execute = AsyncMock()
    s.flush = AsyncMock()
    s.add = MagicMock()
    return s


class TestGetEntityDetail:
    def _cat(self, **kw) -> SimpleNamespace:
        base = {
            "id": 1,
            "entity_name": "catalog.db.t",
            "entity_type": "table",
            "source_id": "s1",
            "sensitivity_level": "PII",
            "owner_id": 5,
            "schema_incomplete": False,
            "content_signature": "abc",
            "schema_json": {"fields": [{"name": "id", "type": "BIGINT", "comment": "主键"}]},
            "created_at": None,
            "updated_at": None,
        }
        base.update(kw)
        return SimpleNamespace(**base)

    def _edge(self) -> SimpleNamespace:
        return SimpleNamespace(
            source_node="table:src",
            target_node="catalog.db.t",
            edge_type="DERIVED_FROM",
            granularity="L2",
            confidence=0.9,
            provenance="manual",
        )

    def _related_metric(self) -> SimpleNamespace:
        return SimpleNamespace(target_node="metric:gmv", edge_type="DERIVED_FROM")

    async def test_found_with_lineage_and_schema_summary(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r1 = MagicMock()
        r1.scalar_one_or_none.return_value = self._cat()
        r2 = MagicMock()
        r2.all.return_value = [self._edge()]
        r3 = MagicMock()
        r3.all.return_value = [self._related_metric()]
        r4 = MagicMock()
        r4.first.return_value = SimpleNamespace(
            health_status="healthy", last_health_check=None, name="s1"
        )
        # column_descriptions 查询：本用例 schema_summary 为 list，get_entity_detail
        # 会再执行一次 ColumnDescription 查询（并行会话新增，测试需同步 mock）
        r5 = MagicMock()
        r5.scalars.return_value.all.return_value = []
        s.execute = AsyncMock(side_effect=[r1, r2, r3, r4, r5])

        out = await repo.get_entity_detail(1)

        assert out is not None
        assert out["entity_name"] == "catalog.db.t"
        assert out["entity_type"] == "table"
        assert out["source_id"] == "s1"
        assert out["sensitivity_level"] == "PII"
        assert out["owner_id"] == 5
        assert out["pii_flag"] is True
        assert out["lineage_count"] == 1
        assert out["lineage_edges"][0]["edge_type"] == "DERIVED_FROM"
        assert out["lineage_edges"][0]["confidence"] == 0.9
        assert out["related_metrics"][0]["metric_node"] == "metric:gmv"
        assert out["source_health"]["health_status"] == "healthy"
        assert out["schema_summary"] == [
            {
                "name": "id",
                "type": "BIGINT",
                "comment": "主键",
                # 无独立描述记录但有原始 comment → 取 comment、来源 schema（并行会话
                # 新增的 _merge_descriptions 行为）
                "description": "主键",
                "description_source": "schema",
            }
        ]
        # 敏感字段绝不外泄
        assert out["etl_sql"] is None

    async def test_not_found_returns_none(self) -> None:
        s = _session()
        r = MagicMock()
        r.scalar_one_or_none.return_value = None
        s.execute = AsyncMock(return_value=r)

        out = await AssetMapRepository(s).get_entity_detail(999)

        assert out is None
        s.execute.assert_awaited_once()

    async def test_pii_flag_false_for_internal(self) -> None:
        s = _session()
        r1 = MagicMock()
        r1.scalar_one_or_none.return_value = self._cat(
            id=2,
            entity_name="catalog.db.u",
            sensitivity_level="INTERNAL",
            owner_id=None,
            schema_incomplete=True,
            content_signature=None,
            schema_json={},
        )
        r2 = MagicMock()
        r2.all.return_value = []
        r3 = MagicMock()
        r3.all.return_value = []
        r4 = MagicMock()
        r4.first.return_value = None
        # schema_json={} 经 _summarize_schema 仍返回空 list，同样触发
        # ColumnDescription 查询（并行会话新增）
        r5 = MagicMock()
        r5.scalars.return_value.all.return_value = []
        s.execute = AsyncMock(side_effect=[r1, r2, r3, r4, r5])

        out = await AssetMapRepository(s).get_entity_detail(2)

        assert out is not None
        assert out["pii_flag"] is False
        assert out["lineage_count"] == 0
        assert out["source_health"]["health_status"] == "unknown"
        assert out["owner_id"] is None


class TestSummarizeSchema:
    def test_fields_list(self) -> None:
        out = AssetMapRepository._summarize_schema(
            {"fields": [{"name": "a", "type": "INT", "comment": None}]}
        )
        assert out == [{"name": "a", "type": "INT", "comment": None}]

    def test_columns_list(self) -> None:
        out = AssetMapRepository._summarize_schema(
            {"columns": [{"column": "b", "data_type": "VARCHAR"}]}
        )
        assert out == [{"name": "b", "type": "VARCHAR", "comment": None}]

    def test_non_dict_returns_none(self) -> None:
        assert AssetMapRepository._summarize_schema("raw") is None
        assert AssetMapRepository._summarize_schema(None) is None

    def test_non_list_fields_returns_raw(self) -> None:
        out = AssetMapRepository._summarize_schema({"fields": {"a": "INT"}})
        assert out == {"fields": {"a": "INT"}}


class TestGraphFromMysql:
    def _metric(self, code: str, domain: str, pii: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            metric_code=code,
            domain=domain,
            pii_flag=pii,
            owner_id=1,
            status="PUBLISHED",
        )

    def _catalog(
        self,
        entity_name: str,
        entity_type: str = "TABLE",
        sens: str = "INTERNAL",
        domain: str | None = "sales",
        cid: int = 101,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=cid,
            entity_name=entity_name,
            entity_type=entity_type,
            sensitivity_level=sens,
            owner_id=2,
            domain=domain,
        )

    def _edge(self, source: str, target: str, edge_type: str = "DERIVED_FROM") -> SimpleNamespace:
        return SimpleNamespace(source_node=source, target_node=target, edge_type=edge_type)

    def _empty_rows(self) -> MagicMock:
        r = MagicMock()
        r.all.return_value = []
        return r

    def _lineage_names(self, names: list[str] | None = None) -> MagicMock:
        """血缘边引用的表名查询 mock（scalars().all() 返回完整 ``table:`` 节点）。"""
        r = MagicMock()
        r.scalars.return_value.all.return_value = names or [
            "table:sales.ods",
            "table:sales.dwd",
        ]
        return r

    async def test_node_id_uses_metric_prefix_and_precise_domain_filter(self) -> None:
        """域过滤必须是精确集合匹配（IN），不得再用 contains 子串匹配。"""
        s = _session()
        repo = AssetMapRepository(s)
        r_metrics = MagicMock()
        r_metrics.all.return_value = [self._metric("sales_gmv_amount_day", "sales")]
        r_edges = MagicMock()
        r_edges.all.return_value = [self._edge("table:sales.ods", "metric:sales_gmv_amount_day")]
        s.execute = AsyncMock(
            side_effect=[r_metrics, self._lineage_names(), self._empty_rows(), r_edges]
        )

        nodes, edges = await repo.graph_from_mysql(domain="sales", pii_only=False)

        assert nodes[0]["id"] == "metric:sales_gmv_amount_day"
        assert nodes[0]["label"] == "sales_gmv_amount_day"
        assert len(edges) == 1
        edge_stmt = s.execute.call_args_list[3].args[0]
        compiled = str(edge_stmt.compile(compile_kwargs={"literal_binds": True}))
        # 精确匹配：IN 集合，非 LIKE/contains 子串
        assert "metric:sales_gmv_amount_day" in compiled
        assert "contains" not in compiled.lower()
        assert "LIKE" not in compiled.upper()

    async def test_edge_filter_uses_in_set_excluding_other_domains(self) -> None:
        """边的 IN 集合只含域内节点：fin 端点不会被包含（消除子串误匹配）。

        mock 无法模拟 SQL 过滤，故断言编译后的 SQL：IN 集合精确列出域内节点，
        不含 fin 节点，且无 LIKE/contains。
        """
        s = _session()
        repo = AssetMapRepository(s)
        r_metrics = MagicMock()
        r_metrics.all.return_value = [self._metric("sales_gmv_amount_day", "sales")]
        r_edges = MagicMock()
        r_edges.all.return_value = [self._edge("table:fin.raw", "metric:fin_cost")]
        s.execute = AsyncMock(
            side_effect=[r_metrics, self._lineage_names(), self._empty_rows(), r_edges]
        )

        await repo.graph_from_mysql(domain="sales", pii_only=False)

        edge_stmt = s.execute.call_args_list[3].args[0]
        compiled = str(edge_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "metric:sales_gmv_amount_day" in compiled
        assert "metric:fin_cost" not in compiled
        assert "table:fin.raw" not in compiled
        assert "contains" not in compiled.lower()
        assert "LIKE" not in compiled.upper()

    async def test_no_nodes_returns_no_edges(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r_metrics = MagicMock()
        r_metrics.all.return_value = []
        s.execute = AsyncMock(side_effect=[r_metrics, self._lineage_names(), self._empty_rows()])

        nodes, edges = await repo.graph_from_mysql(domain="sales", pii_only=False)

        assert nodes == []
        assert edges == []
        assert s.execute.await_count == 3

    async def test_pii_only_filters_metric_stmt(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r_metrics = MagicMock()
        r_metrics.all.return_value = [self._metric("sales_pii", "sales", pii=True)]
        r_edges = MagicMock()
        r_edges.all.return_value = [self._edge("table:sales.ods", "metric:sales_pii")]
        s.execute = AsyncMock(
            side_effect=[r_metrics, self._lineage_names(), self._empty_rows(), r_edges]
        )

        nodes, edges = await repo.graph_from_mysql(domain=None, pii_only=True)

        assert [n["id"] for n in nodes] == ["metric:sales_pii"]
        assert len(edges) == 1
        metric_stmt = s.execute.call_args_list[0].args[0]
        compiled = str(metric_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "pii_flag" in compiled

    async def test_catalog_nodes_included_with_domain_inheritance(self) -> None:
        """表/视图节点并入图，域从 data_source 继承、PII 由敏感级判定。"""
        s = _session()
        repo = AssetMapRepository(s)
        r_metrics = MagicMock()
        r_metrics.all.return_value = [self._metric("sales_gmv_amount_day", "sales")]
        r_catalog = MagicMock()
        r_catalog.all.return_value = [
            self._catalog("sales.ods", sens="PII"),
            self._catalog("sales.dwd", entity_type="VIEW", sens="INTERNAL"),
        ]
        r_edges = MagicMock()
        r_edges.all.return_value = [self._edge("table:sales.ods", "metric:sales_gmv_amount_day")]
        s.execute = AsyncMock(
            side_effect=[r_metrics, self._lineage_names(), r_catalog, r_edges]
        )

        nodes, edges = await repo.graph_from_mysql(domain="sales", pii_only=False)

        ids = [n["id"] for n in nodes]
        assert "table:sales.ods" in ids
        assert "table:sales.dwd" in ids
        table_node = next(n for n in nodes if n["id"] == "table:sales.ods")
        assert table_node["type"] == "table"
        assert table_node["domain"] == "sales"
        assert table_node["pii"] is True
        assert table_node["entity_id"] == 101
        view_node = next(n for n in nodes if n["id"] == "table:sales.dwd")
        assert view_node["pii"] is False
        assert len(edges) == 1
        # catalog 查询含 entity_type 过滤、data_source 域继承 join、血缘表优先与已删源排除
        catalog_stmt = s.execute.call_args_list[2].args[0]
        compiled = str(catalog_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "TABLE" in compiled
        assert "JOIN" in compiled.upper()
        assert "sales.ods" in compiled
        # 边的 IN 集合同时含表节点
        edge_stmt = s.execute.call_args_list[3].args[0]
        edge_sql = str(edge_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "table:sales.ods" in edge_sql

    async def test_field_nodes_extracted_from_edges(self) -> None:
        """血缘边引用的字段节点并入图，域继承对端表。"""
        s = _session()
        repo = AssetMapRepository(s)
        r_metrics = MagicMock()
        r_metrics.all.return_value = [self._metric("sales_gmv_amount_day", "sales")]
        r_catalog = MagicMock()
        r_catalog.all.return_value = [self._catalog("sales.ods", sens="PII")]
        r_edges = MagicMock()
        r_edges.all.return_value = [
            self._edge("table:sales.ods", "field:sales.ods.amount"),
            self._edge("field:sales.ods.amount", "metric:sales_gmv_amount_day"),
        ]
        s.execute = AsyncMock(
            side_effect=[r_metrics, self._lineage_names(), r_catalog, r_edges]
        )

        nodes, _ = await repo.graph_from_mysql(domain="sales", pii_only=False)

        field_node = next(n for n in nodes if n["id"] == "field:sales.ods.amount")
        assert field_node["type"] == "field"
        assert field_node["label"] == "sales.ods.amount"
        assert field_node["domain"] == "sales"
        assert field_node["pii"] is False

    async def test_pii_only_excludes_field_nodes(self) -> None:
        """PII 视图不展示字段节点（字段级 PII 无法从血缘边判定）。"""
        s = _session()
        repo = AssetMapRepository(s)
        r_metrics = MagicMock()
        r_metrics.all.return_value = [self._metric("sales_pii", "sales", pii=True)]
        r_catalog = MagicMock()
        r_catalog.all.return_value = [self._catalog("sales.ods", sens="PII")]
        r_edges = MagicMock()
        r_edges.all.return_value = [self._edge("field:sales.ods.amount", "metric:sales_pii")]
        s.execute = AsyncMock(
            side_effect=[r_metrics, self._lineage_names(), r_catalog, r_edges]
        )

        nodes, _ = await repo.graph_from_mysql(domain=None, pii_only=True)

        ids = [n["id"] for n in nodes]
        assert "field:sales.ods.amount" not in ids
        assert "metric:sales_pii" in ids
        assert "table:sales.ods" in ids


class TestListTablesAndOrphans:
    async def test_list_tables_with_filters(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.scalars.return_value.all.return_value = [
            SimpleNamespace(id=1, entity_name="catalog.db.t")
        ]
        s.execute = AsyncMock(return_value=r)

        rows = await repo.list_tables(source_id="s1", sensitivity="PII", limit=50)

        assert len(rows) == 1
        stmt = s.execute.call_args_list[0].args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "entity_type" in compiled
        assert "source_id" in compiled
        assert "sensitivity_level" in compiled

    async def test_list_tables_without_filters(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.scalars.return_value.all.return_value = []
        s.execute = AsyncMock(return_value=r)

        rows = await repo.list_tables(None, None, 100)

        assert rows == []

    async def test_orphan_assets(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.scalars.return_value.all.return_value = [SimpleNamespace(id=1, owner_id=None)]
        s.execute = AsyncMock(return_value=r)

        rows = await repo.orphan_assets()

        assert len(rows) == 1
        stmt = s.execute.call_args_list[0].args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "owner_id IS NULL" in compiled


class TestAggregations:
    async def test_catalog_summary(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r_total = MagicMock()
        r_total.scalar.return_value = 5
        r_by_type = MagicMock()
        r_by_type.all.return_value = [("table", 3), ("field", 2)]
        r_by_sens = MagicMock()
        r_by_sens.all.return_value = [("PII", 2)]
        r_orphan = MagicMock()
        r_orphan.scalar.return_value = 1
        s.execute = AsyncMock(side_effect=[r_total, r_by_type, r_by_sens, r_orphan])

        out = await repo.catalog_summary()

        assert out["total"] == 5
        assert out["by_entity_type"] == {"table": 3, "field": 2}
        assert out["by_sensitivity"] == {"PII": 2}
        assert out["orphan_assets"] == 1

    async def test_heatmap_default_domain(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.all.return_value = [("sales", 3, 1)]
        s.execute = AsyncMock(return_value=r)

        out = await repo.heatmap_aggregation("domain")

        assert out["dimension"] == "domain"
        assert out["buckets"] == [{"key": "sales", "total": 3, "pii_count": 1}]

    async def test_heatmap_sensitivity(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.all.return_value = [("PII", 2)]
        s.execute = AsyncMock(return_value=r)

        out = await repo.heatmap_aggregation("sensitivity")

        assert out["dimension"] == "sensitivity"
        assert out["buckets"] == [{"key": "PII", "count": 2}]

    async def test_heatmap_owner_and_dw_layer(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.all.return_value = [("1", 3, 1)]
        s.execute = AsyncMock(return_value=r)
        out = await repo.heatmap_aggregation("owner")
        assert out["buckets"] == [{"key": "1", "total": 3, "pii_count": 1}]

        r2 = MagicMock()
        r2.all.return_value = [("DWD", 2)]
        s.execute = AsyncMock(return_value=r2)
        out2 = await repo.heatmap_aggregation("dw_layer")
        assert out2["buckets"] == [{"key": "DWD", "count": 2}]

    async def test_owner_aggregation(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r_stats = MagicMock()
        r_stats.one.return_value = SimpleNamespace(total=4, published=2, draft=1, pii_count=1)
        r_domain = MagicMock()
        r_domain.all.return_value = [("sales", 4)]
        r_catalog = MagicMock()
        r_catalog.scalar.return_value = 7
        s.execute = AsyncMock(side_effect=[r_stats, r_domain, r_catalog])

        out = await repo.owner_aggregation(owner_id=9)

        assert out["owner_id"] == 9
        assert out["metrics"]["total"] == 4
        assert out["metrics"]["published"] == 2
        assert out["metrics"]["by_domain"] == {"sales": 4}
        assert out["catalogs"]["total"] == 7

    async def test_heatmap_matrix(self) -> None:
        """二维热力矩阵：域 × 敏感级别聚合，含 join/group_by 与 PII 判定。"""
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.all.return_value = [
            ("sales", "PII", 3),
            ("sales", "INTERNAL", 2),
            ("finance", "PUBLIC", 1),
        ]
        s.execute = AsyncMock(return_value=r)

        out = await repo.heatmap_matrix()

        assert out["columns"] == ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", "NEEDS_REVIEW"]
        assert out["cells"] == [
            {"domain": "sales", "sensitivity": "PII", "count": 3, "pii_count": 3},
            {"domain": "sales", "sensitivity": "INTERNAL", "count": 2, "pii_count": 0},
            {"domain": "finance", "sensitivity": "PUBLIC", "count": 1, "pii_count": 0},
        ]
        stmt = s.execute.call_args_list[0].args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "JOIN" in compiled.upper()
        assert "GROUP BY" in compiled.upper()
        assert "db_catalog" in compiled.lower()

    async def test_heatmap_matrix_metric_view(self) -> None:
        """指标视角热力矩阵：按 domain × pii_flag 聚合，列 = INTERNAL/PII。"""
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.all.return_value = [
            ("sales", True, 3),
            ("sales", False, 2),
            ("finance", False, 1),
        ]
        s.execute = AsyncMock(return_value=r)

        out = await repo.heatmap_matrix(asset_type="metric")

        assert out["columns"] == ["INTERNAL", "PII"]
        assert out["cells"] == [
            {"domain": "sales", "sensitivity": "PII", "count": 3, "pii_count": 3},
            {"domain": "sales", "sensitivity": "INTERNAL", "count": 2, "pii_count": 0},
            {"domain": "finance", "sensitivity": "INTERNAL", "count": 1, "pii_count": 0},
        ]
        stmt = s.execute.call_args_list[0].args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "pii_flag" in compiled.lower()
        assert "metric" in compiled.lower()


class TestEscapeLike:
    def test_escapes_wildcards(self) -> None:
        assert AssetMapRepository._escape_like("100%_ok") == "100\\%\\_ok"
        assert AssetMapRepository._escape_like("a\\b") == "a\\\\b"

    def test_plain_text_unchanged(self) -> None:
        assert AssetMapRepository._escape_like("sales") == "sales"


class TestSearchAssets:
    def _catalog(self, name: str = "catalog.db.orders") -> SimpleNamespace:
        return SimpleNamespace(
            id=1,
            entity_name=name,
            entity_type="table",
            sensitivity_level="INTERNAL",
            owner_id=2,
        )

    def _metric(self, code: str = "sales_gmv_amount_day") -> SimpleNamespace:
        return SimpleNamespace(
            id=3,
            metric_code=code,
            name="GMV",
            pii_flag=False,
            domain="sales",
            owner_id=2,
            status="PUBLISHED",
        )

    async def test_search_both_types(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r_cat = MagicMock()
        r_cat.scalars.return_value.all.return_value = [self._catalog()]
        r_met = MagicMock()
        r_met.scalars.return_value.all.return_value = [self._metric()]
        s.execute = AsyncMock(side_effect=[r_cat, r_met])

        out = await repo.search_assets("sales", None, 20)

        assert len(out) == 2
        assert out[0]["type"] == "catalog"
        assert out[1]["type"] == "metric"
        assert out[1]["sensitivity_level"] == "INTERNAL"
        # 查询含转义后的 LIKE
        cat_stmt = s.execute.call_args_list[0].args[0]
        compiled = str(cat_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "LIKE" in compiled.upper()

    async def test_search_metric_only(self) -> None:
        """限定 metric 时只查指标，不查目录（分流查询）。"""
        s = _session()
        repo = AssetMapRepository(s)
        r_met = MagicMock()
        r_met.scalars.return_value.all.return_value = [self._metric()]
        s.execute = AsyncMock(return_value=r_met)

        out = await repo.search_assets("sales", "metric", 20)

        assert len(out) == 1
        assert out[0]["type"] == "metric"
        # 只执行一次查询（指标）
        s.execute.assert_awaited_once()

    async def test_search_table_only_skips_metrics(self) -> None:
        """限定 table 时只查目录，不查指标。"""
        s = _session()
        repo = AssetMapRepository(s)
        r_cat = MagicMock()
        r_cat.scalars.return_value.all.return_value = [self._catalog()]
        s.execute = AsyncMock(return_value=r_cat)

        out = await repo.search_assets("orders", "table", 20)

        assert len(out) == 1
        assert out[0]["type"] == "catalog"
        s.execute.assert_awaited_once()

    async def test_search_pii_metric_flagged(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r_cat = MagicMock()
        r_cat.scalars.return_value.all.return_value = []
        r_met = MagicMock()
        r_met.scalars.return_value.all.return_value = [
            SimpleNamespace(
                id=1,
                metric_code="sales_user_phone",
                name="手机号",
                pii_flag=True,
                domain="sales",
                owner_id=2,
                status="PUBLISHED",
            )
        ]
        s.execute = AsyncMock(side_effect=[r_cat, r_met])

        out = await repo.search_assets("phone", None, 20)

        assert out[0]["sensitivity_level"] == "PII"

    async def test_search_blank_returns_empty(self) -> None:
        s = _session()
        out = await AssetMapRepository(s).search_assets("   ", None, 20)
        assert out == []
        s.execute.assert_not_awaited()


class TestHealthSummary:
    async def test_aggregates_all(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r_unhealthy = MagicMock()
        r_unhealthy.all.return_value = [
            SimpleNamespace(source_id="s1", name="bad", health_status="unhealthy")
        ]
        r_incomplete = MagicMock()
        r_incomplete.all.return_value = [SimpleNamespace(id=1, entity_name="t", source_id="s1")]
        r_orphan = MagicMock()
        r_orphan.scalar.return_value = 2
        r_stale = MagicMock()
        r_stale.all.return_value = [SimpleNamespace(id=2, entity_name="old", updated_at=None)]
        s.execute = AsyncMock(side_effect=[r_unhealthy, r_incomplete, r_orphan, r_stale])

        out = await repo.health_summary()

        assert out["unhealthy_sources"][0]["source_id"] == "s1"
        assert out["schema_incomplete"][0]["entity_name"] == "t"
        assert out["orphan_assets"] == 2
        assert len(out["stale_assets"]) == 1
        assert out["stale_days"] == 7


class TestPiiOverview:
    async def test_aggregates(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r_sens = MagicMock()
        r_sens.all.return_value = [("PII", 3)]
        r_domain = MagicMock()
        r_domain.all.return_value = [("sales", 2)]
        s.execute = AsyncMock(side_effect=[r_sens, r_domain])

        out = await repo.pii_overview()

        assert out["pii_catalog_count"] == 3
        assert out["pii_metric_count"] == 2
        assert out["by_sensitivity"] == {"PII": 3}
        assert out["by_domain"] == {"sales": 2}


class TestRecentChanges:
    def _catalog_row(self) -> SimpleNamespace:
        return SimpleNamespace(
            id=1,
            entity_name="catalog.db.t",
            entity_type="table",
            sensitivity_level="INTERNAL",
            owner_id=2,
            source_id="s1",
            updated_at=None,
        )

    def _metric_row(self) -> SimpleNamespace:
        return SimpleNamespace(
            metric_code="sales_gmv_amount_day",
            name="GMV",
            status="PUBLISHED",
            domain="sales",
            pii_flag=False,
            updated_at=None,
        )

    async def test_recent_catalogs_and_metrics(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r_cat = MagicMock()
        r_cat.all.return_value = [self._catalog_row()]
        r_met = MagicMock()
        r_met.all.return_value = [self._metric_row()]
        s.execute = AsyncMock(side_effect=[r_cat, r_met])

        out = await repo.recent_changes(days=7, limit=50)

        assert out["days"] == 7
        assert out["catalogs"][0]["entity_name"] == "catalog.db.t"
        assert out["metrics"][0]["metric_code"] == "sales_gmv_amount_day"
        assert out["metrics"][0]["pii_flag"] is False


class TestMyAssets:
    def _catalog_row(self) -> SimpleNamespace:
        return SimpleNamespace(
            id=1,
            entity_name="catalog.db.t",
            entity_type="table",
            sensitivity_level="PII",
            source_id="s1",
        )

    def _metric_row(self) -> SimpleNamespace:
        return SimpleNamespace(
            metric_code="sales_gmv_amount_day",
            name="GMV",
            status="PUBLISHED",
            domain="sales",
            pii_flag=True,
        )

    async def test_my_assets(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r_cat = MagicMock()
        r_cat.all.return_value = [self._catalog_row()]
        r_met = MagicMock()
        r_met.all.return_value = [self._metric_row()]
        s.execute = AsyncMock(side_effect=[r_cat, r_met])

        out = await repo.my_assets(owner_id=2, limit=50)

        assert out["owner_id"] == 2
        assert out["catalogs"][0]["sensitivity_level"] == "PII"
        assert out["metrics"][0]["pii_flag"] is True
        cat_stmt = s.execute.call_args_list[0].args[0]
        compiled = str(cat_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "owner_id" in compiled


class TestWriteOps:
    """写能力（FR-18）：认领/转让、重分类、批量——同事务 flush，API 层 commit。"""

    async def test_get_catalog_entity_found(self) -> None:
        s = _session()
        row = SimpleNamespace(id=1, entity_name="catalog.sales.orders", deleted_at=None)
        res = MagicMock()
        res.scalar_one_or_none.return_value = row
        s.execute.return_value = res
        repo = AssetMapRepository(s)
        out = await repo.get_catalog_entity(1)
        assert out is row
        stmt = s.execute.call_args.args[0]
        assert "deleted_at IS NULL" in str(stmt)

    async def test_get_catalog_entity_missing(self) -> None:
        s = _session()
        res = MagicMock()
        res.scalar_one_or_none.return_value = None
        s.execute.return_value = res
        repo = AssetMapRepository(s)
        assert await repo.get_catalog_entity(999) is None

    async def test_list_catalog_entities_empty(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        assert await repo.list_catalog_entities([]) == []
        s.execute.assert_not_awaited()

    async def test_list_catalog_entities_preserves_order(self) -> None:
        """批量获取按入参顺序排序（供批量操作按请求顺序落库）。"""
        s = _session()
        a = SimpleNamespace(id=3, deleted_at=None)
        b = SimpleNamespace(id=1, deleted_at=None)
        # execute 返回普通 MagicMock（非 AsyncMock），scalars().all() 是同步链
        res = MagicMock()
        res.scalars.return_value.all.return_value = [a, b]
        s.execute.return_value = res
        repo = AssetMapRepository(s)
        out = await repo.list_catalog_entities([1, 3])
        assert [r.id for r in out] == [1, 3]  # 按入参顺序，而非 DB 返回序
        stmt = s.execute.call_args.args[0]
        assert "IN" in str(stmt) and "deleted_at IS NULL" in str(stmt)

    async def test_user_exists_true(self) -> None:
        s = _session()
        res = MagicMock()
        res.first.return_value = (1,)
        s.execute.return_value = res
        repo = AssetMapRepository(s)
        assert await repo.user_exists(9) is True
        stmt = s.execute.call_args.args[0]
        assert "deleted_at IS NULL" in str(stmt)

    async def test_user_exists_false(self) -> None:
        s = _session()
        res = MagicMock()
        res.first.return_value = None
        s.execute.return_value = res
        repo = AssetMapRepository(s)
        assert await repo.user_exists(999) is False

    async def test_assign_owner(self) -> None:
        s = _session()
        entity = DBCatalog(id=1, entity_name="catalog.sales.orders", owner_id=None)
        repo = AssetMapRepository(s)
        out = await repo.assign_owner(entity, owner_id=9)
        assert out is entity
        assert entity.owner_id == 9
        s.add.assert_called_once_with(entity)
        s.flush.assert_awaited_once()

    async def test_assign_owner_release(self) -> None:
        s = _session()
        entity = DBCatalog(id=1, entity_name="catalog.sales.orders", owner_id=9)
        repo = AssetMapRepository(s)
        await repo.assign_owner(entity, owner_id=None)
        assert entity.owner_id is None

    async def test_reclassify_sensitivity(self) -> None:
        s = _session()
        entity = DBCatalog(id=1, entity_name="catalog.sales.orders", sensitivity_level="INTERNAL")
        repo = AssetMapRepository(s)
        out = await repo.reclassify_sensitivity(entity, "PII")
        assert out is entity
        assert entity.sensitivity_level == "PII"
        s.flush.assert_awaited_once()

    async def test_batch_assign_owner(self) -> None:
        s = _session()
        entities = [
            DBCatalog(id=1, entity_name="catalog.sales.orders"),
            DBCatalog(id=2, entity_name="catalog.sales.items"),
        ]
        repo = AssetMapRepository(s)
        affected = await repo.batch_assign_owner(entities, owner_id=9)
        assert affected == 2
        assert all(e.owner_id == 9 for e in entities)
        assert s.add.call_count == 2
        s.flush.assert_awaited_once()

    async def test_batch_reclassify(self) -> None:
        s = _session()
        entities = [DBCatalog(id=1, entity_name="catalog.sales.orders")]
        repo = AssetMapRepository(s)
        affected = await repo.batch_reclassify(entities, "CONFIDENTIAL")
        assert affected == 1
        assert entities[0].sensitivity_level == "CONFIDENTIAL"
        s.flush.assert_awaited_once()
