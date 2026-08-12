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

from app.services.assetmap.repository import AssetMapRepository


def _session() -> MagicMock:
    s = MagicMock()
    s.execute = AsyncMock()
    return s


class TestGetEntityDetail:
    async def test_found_with_lineage_and_schema_summary(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        cat = SimpleNamespace(
            id=1,
            entity_name="catalog.db.t",
            entity_type="table",
            source_id="s1",
            sensitivity_level="PII",
            owner_id=5,
            schema_incomplete=False,
            content_signature="abc",
            schema_json={"fields": [{"name": "id", "type": "BIGINT", "comment": "主键"}]},
        )
        r1 = MagicMock()
        r1.scalar_one_or_none.return_value = cat
        r2 = MagicMock()
        r2.scalar.return_value = 3
        s.execute = AsyncMock(side_effect=[r1, r2])

        out = await repo.get_entity_detail(1)

        assert out is not None
        assert out["entity_name"] == "catalog.db.t"
        assert out["entity_type"] == "table"
        assert out["source_id"] == "s1"
        assert out["sensitivity_level"] == "PII"
        assert out["owner_id"] == 5
        assert out["pii_flag"] is True
        assert out["lineage_count"] == 3
        assert out["schema_summary"] == [
            {"name": "id", "type": "BIGINT", "comment": "主键"}
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
        cat = SimpleNamespace(
            id=2,
            entity_name="catalog.db.u",
            entity_type="table",
            source_id="s1",
            sensitivity_level="INTERNAL",
            owner_id=None,
            schema_incomplete=True,
            content_signature=None,
            schema_json={},
        )
        r1 = MagicMock()
        r1.scalar_one_or_none.return_value = cat
        r2 = MagicMock()
        r2.scalar.return_value = 0
        s.execute = AsyncMock(side_effect=[r1, r2])

        out = await AssetMapRepository(s).get_entity_detail(2)

        assert out is not None
        assert out["pii_flag"] is False
        assert out["lineage_count"] == 0
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

    async def test_node_id_uses_metric_prefix_and_precise_domain_filter(self) -> None:
        """域过滤必须是精确集合匹配（IN），不得再用 contains 子串匹配。"""
        s = _session()
        repo = AssetMapRepository(s)
        m = self._metric("sales_gmv_amount_day", "sales")
        r_metrics = MagicMock()
        r_metrics.all.return_value = [m]
        e = SimpleNamespace(
            source_node="table:sales.ods",
            target_node="metric:sales_gmv_amount_day",
            edge_type="DERIVED_FROM",
        )
        r_edges = MagicMock()
        r_edges.all.return_value = [e]
        s.execute = AsyncMock(side_effect=[r_metrics, r_edges])

        nodes, edges = await repo.graph_from_mysql(domain="sales", pii_only=False)

        assert nodes[0]["id"] == "metric:sales_gmv_amount_day"
        assert nodes[0]["label"] == "sales_gmv_amount_day"
        assert len(edges) == 1
        edge_stmt = s.execute.call_args_list[1].args[0]
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
        m = self._metric("sales_gmv_amount_day", "sales")
        r_metrics = MagicMock()
        r_metrics.all.return_value = [m]
        e = SimpleNamespace(
            source_node="table:fin.raw",
            target_node="metric:fin_cost",
            edge_type="DERIVED_FROM",
        )
        r_edges = MagicMock()
        r_edges.all.return_value = [e]
        s.execute = AsyncMock(side_effect=[r_metrics, r_edges])

        await repo.graph_from_mysql(domain="sales", pii_only=False)

        edge_stmt = s.execute.call_args_list[1].args[0]
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
        s.execute = AsyncMock(side_effect=[r_metrics])

        nodes, edges = await repo.graph_from_mysql(domain="sales", pii_only=False)

        assert nodes == []
        assert edges == []
        s.execute.assert_awaited_once()

    async def test_pii_only_filters_metric_stmt(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        m = self._metric("sales_pii", "sales", pii=True)
        r_metrics = MagicMock()
        r_metrics.all.return_value = [m]
        e = SimpleNamespace(
            source_node="table:sales.ods",
            target_node="metric:sales_pii",
            edge_type="DERIVED_FROM",
        )
        r_edges = MagicMock()
        r_edges.all.return_value = [e]
        s.execute = AsyncMock(side_effect=[r_metrics, r_edges])

        nodes, edges = await repo.graph_from_mysql(domain=None, pii_only=True)

        assert [n["id"] for n in nodes] == ["metric:sales_pii"]
        assert len(edges) == 1
        metric_stmt = s.execute.call_args_list[0].args[0]
        compiled = str(metric_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "pii_flag" in compiled


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
        r.scalars.return_value.all.return_value = [
            SimpleNamespace(id=1, owner_id=None)
        ]
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
        r_stats.one.return_value = SimpleNamespace(
            total=4, published=2, draft=1, pii_count=1
        )
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
