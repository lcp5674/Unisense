"""资产地图 Repository 单测（补齐覆盖率 + 生产级缺口回归）。

覆盖：
- get_entity_detail：命中（含 lineage_count / schema 摘要 / PII 判定）、未命中
- _summarize_schema：fields / columns / 非 dict 退化
- graph_from_mysql：域过滤精确匹配（非 contains 子串）、节点 ID 统一 metric: 前缀、
  无展示节点时返回空边、pii_only 过滤
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

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
            # 表级业务描述（治理补全，TD §12.1）
            "description": None,
            "description_source": None,
            "description_updated_at": None,
            # PII 合规增强：表级复核/脱敏/保留期
            "compliance_reviewed": False,
            "compliance_reviewed_by": None,
            "compliance_reviewed_at": None,
            "masking_policy": None,
            "retention_days": None,
            "legal_basis": None,
            "retention_expires_at": None,
            "retention_notified_at": None,
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
        # 业务域（经 data_source 继承）：get_entity_detail 新增 _source_domain 查询
        r5 = MagicMock()
        r5.first.return_value = ("sales",)
        # column_descriptions 查询：本用例 schema_summary 为 list，get_entity_detail
        # 会再执行一次 ColumnDescription 查询（并行会话新增，测试需同步 mock）
        r6 = MagicMock()
        r6.scalars.return_value.all.return_value = []
        # 责任人展示名：_owner_display_name 查询（owner_id=5）
        r7 = MagicMock()
        r7.first.return_value = ("李四", "lisi")
        # PII 合规增强：classification 明细查询（无记录 → None，走实时检测）
        r8 = MagicMock()
        r8.scalar_one_or_none.return_value = None
        # PII 合规增强：字段级人工标注查询（无标注）
        r9 = MagicMock()
        r9.scalars.return_value.all.return_value = []
        # 执行顺序：pii_fields 计算（classification → override）在 owner_name 之前
        s.execute = AsyncMock(side_effect=[r1, r2, r3, r4, r5, r6, r8, r9, r7])

        out = await repo.get_entity_detail(1)

        assert out is not None
        assert out["entity_name"] == "catalog.db.t"
        assert out["entity_type"] == "table"
        assert out["source_id"] == "s1"
        assert out["source_name"] == "s1"
        assert out["domain"] == "sales"
        assert out["sensitivity_level"] == "PII"
        assert out["owner_id"] == 5
        assert out["owner_name"] == "李四"
        assert out["column_count"] == 1
        # 行视图样本（mock 未构造 sample_rows → None，不参与详情展示）
        assert out["sample_rows"] is None
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
                # 脱敏样本值（未采样时为 None）
                "sample": None,
                # 样本命中的敏感类别（未采样时为 None）
                "sample_rule": None,
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
        # 业务域（经 data_source 继承）：get_entity_detail 新增 _source_domain 查询
        r5 = MagicMock()
        r5.first.return_value = ("finance",)
        # schema_json={} 经 _summarize_schema 仍返回空 list，同样触发
        # ColumnDescription 查询（并行会话新增）
        r6 = MagicMock()
        r6.scalars.return_value.all.return_value = []
        # PII 合规增强：字段级人工标注查询（INTERNAL 无 PII 明细，仅查标注表）
        r7 = MagicMock()
        r7.scalars.return_value.all.return_value = []
        s.execute = AsyncMock(side_effect=[r1, r2, r3, r4, r5, r6, r7])

        out = await AssetMapRepository(s).get_entity_detail(2)

        assert out is not None
        assert out["pii_flag"] is False
        assert out["lineage_count"] == 0
        assert out["source_health"]["health_status"] == "unknown"
        assert out["owner_id"] is None
        assert out["domain"] == "finance"
        assert out["owner_name"] is None


class TestSummarizeSchema:
    def test_fields_list(self) -> None:
        out = AssetMapRepository._summarize_schema(
            {"fields": [{"name": "a", "type": "INT", "comment": None}]}
        )
        # 脱敏样本未采样时为 None（采集侧打码后才有值）
        assert out == [
            {"name": "a", "type": "INT", "comment": None, "sample": None, "sample_rule": None}
        ]

    def test_columns_list(self) -> None:
        out = AssetMapRepository._summarize_schema(
            {"columns": [{"column": "b", "data_type": "VARCHAR"}]}
        )
        assert out == [
            {"name": "b", "type": "VARCHAR", "comment": None, "sample": None, "sample_rule": None}
        ]

    def test_sample_passthrough(self) -> None:
        """采样开启时脱敏样本值透出（已打码，非原始敏感值）。"""
        out = AssetMapRepository._summarize_schema(
            {
                "columns": [
                    {"name": "phone", "type": "VARCHAR", "comment": "手机", "sample": "138****5678"}
                ]
            }
        )
        assert out == [
            {
                "name": "phone",
                "type": "VARCHAR",
                "comment": "手机",
                "sample": "138****5678",
                "sample_rule": None,
            }
        ]

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

    async def test_depth_prunes_far_tables(self) -> None:
        """depth 收敛：从指标 BFS 按层展开，depth=1 只保留直连表，剔除中间表。"""
        s = _session()
        repo = AssetMapRepository(s)
        r_metrics = MagicMock()
        r_metrics.all.return_value = [self._metric("sales_gmv", "sales")]
        r_catalog = MagicMock()
        r_catalog.all.return_value = [
            self._catalog("sales.ods"),
            self._catalog("sales.dwd"),
            self._catalog("sales.ads"),
        ]
        # 链：metric ← ads ← dwd ← ods（血缘汇聚到指标）
        r_edges = MagicMock()
        r_edges.all.return_value = [
            self._edge("table:sales.ads", "metric:sales_gmv"),
            self._edge("table:sales.dwd", "table:sales.ads"),
            self._edge("table:sales.ods", "table:sales.dwd"),
        ]
        s.execute = AsyncMock(
            side_effect=[r_metrics, self._lineage_names(), r_catalog, r_edges]
        )

        nodes, edges = await repo.graph_from_mysql(
            domain="sales", pii_only=False, depth=1
        )

        ids = [n["id"] for n in nodes]
        assert "metric:sales_gmv" in ids
        assert "table:sales.ads" in ids  # 直连
        assert "table:sales.dwd" not in ids  # 1 层外
        assert "table:sales.ods" not in ids
        assert all(e["target"] in ids or e["source"] in ids for e in edges)
        assert len(edges) == 1  # 仅 metric↔ads 两端都在

    async def test_depth_none_keeps_all(self) -> None:
        """depth=None（默认）不过滤：全量返回。"""
        s = _session()
        repo = AssetMapRepository(s)
        r_metrics = MagicMock()
        r_metrics.all.return_value = [self._metric("sales_gmv", "sales")]
        r_catalog = MagicMock()
        r_catalog.all.return_value = [
            self._catalog("sales.ads"),
            self._catalog("sales.dwd"),
        ]
        r_edges = MagicMock()
        r_edges.all.return_value = [
            self._edge("table:sales.ads", "metric:sales_gmv"),
            self._edge("table:sales.dwd", "table:sales.ads"),
        ]
        s.execute = AsyncMock(
            side_effect=[r_metrics, self._lineage_names(), r_catalog, r_edges]
        )

        nodes, edges = await repo.graph_from_mysql(domain="sales", pii_only=False)

        ids = [n["id"] for n in nodes]
        assert "table:sales.dwd" in ids
        assert len(edges) == 2


class TestCatalogIdByNames:
    async def test_returns_name_to_id_map(self) -> None:
        s = _session()
        r = MagicMock()
        r.all.return_value = [
            SimpleNamespace(entity_name="ods_orders", id=42),
            SimpleNamespace(entity_name="dwd_order", id=43),
        ]
        s.execute = AsyncMock(return_value=r)

        out = await AssetMapRepository(s).catalog_id_by_names(
            ["ods_orders", "dwd_order", "missing_table"]
        )

        assert out == {"ods_orders": 42, "dwd_order": 43}

    async def test_empty_names_returns_empty_without_query(self) -> None:
        s = _session()
        out = await AssetMapRepository(s).catalog_id_by_names([])
        assert out == {}
        s.execute.assert_not_called()


class TestListTablesAndOrphans:
    async def test_list_tables_with_filters(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.scalar.return_value = 1
        r.scalars.return_value.all.return_value = [
            SimpleNamespace(id=1, entity_name="catalog.db.t")
        ]
        s.execute = AsyncMock(return_value=r)

        rows, total = await repo.list_tables(source_id="s1", sensitivity="PII", limit=50)

        assert len(rows) == 1
        assert total == 1
        stmt = s.execute.call_args_list[1].args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "entity_type" in compiled
        assert "source_id" in compiled
        assert "sensitivity_level" in compiled

    async def test_list_tables_without_filters(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.scalar.return_value = 0
        r.scalars.return_value.all.return_value = []
        s.execute = AsyncMock(return_value=r)

        rows, total = await repo.list_tables(None, None, 100)

        assert rows == []
        assert total == 0

    async def test_list_tables_multi_dimension_filters(self) -> None:
        """多维度过滤：责任人 / Schema 完整性 / 关键字同时生效（LIKE 转义防放大）。"""
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.scalar.return_value = 0
        r.scalars.return_value.all.return_value = []
        s.execute = AsyncMock(return_value=r)

        rows, _total = await repo.list_tables(
            None,
            None,
            100,
            owner_id=7,
            schema_status="incomplete",
            keyword="100%_表",
        )

        assert rows == []
        stmt = s.execute.call_args_list[1].args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "owner_id" in compiled and "= 7" in compiled
        assert "schema_incomplete" in compiled
        # LIKE 通配符已转义：% → /%、_ → /_，且显式 ESCAPE '/'
        assert "100/%/_表" in compiled
        assert "ESCAPE '/'" in compiled

    async def test_list_tables_domain_joins_data_source(self) -> None:
        """业务域过滤：经 data_source 继承 join（仅活跃源），SQL 含 JOIN 与 domain 条件。"""
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.scalar.return_value = 0
        r.scalars.return_value.all.return_value = []
        s.execute = AsyncMock(return_value=r)

        _rows, _total = await repo.list_tables(None, None, 100, domain="sales")

        stmt = s.execute.call_args_list[1].args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "JOIN" in compiled
        assert "data_source" in compiled
        assert "domain" in compiled
        assert "deleted_at" in compiled

    async def test_list_tables_owner_zero_means_unassigned(self) -> None:
        """owner_id=0 约定：过滤无责任人（owner_id IS NULL）的未分配表。"""
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.scalar.return_value = 0
        r.scalars.return_value.all.return_value = []
        s.execute = AsyncMock(return_value=r)

        _rows, _total = await repo.list_tables(None, None, 100, owner_id=0)

        stmt = s.execute.call_args_list[1].args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "owner_id IS NULL" in compiled

    async def test_list_tables_schema_status_complete(self) -> None:
        """Schema 完整性 complete：过滤非 schema_incomplete 行。"""
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.scalar.return_value = 0
        r.scalars.return_value.all.return_value = []
        s.execute = AsyncMock(return_value=r)

        _rows, _total = await repo.list_tables(None, None, 100, schema_status="complete")

        stmt = s.execute.call_args_list[1].args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "schema_incomplete" in compiled
        assert "false" in compiled.lower() or "0" in compiled

    async def test_list_tables_database_filter(self) -> None:
        """库名过滤：entity_name 前缀 LIKE '{db}.%'（通配符转义 + ESCAPE '/'）。"""
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.scalar.return_value = 0
        r.scalars.return_value.all.return_value = []
        s.execute = AsyncMock(return_value=r)

        _rows, _total = await repo.list_tables(None, None, 100, database="wedw_dws")

        stmt = s.execute.call_args_list[1].args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "entity_name" in compiled
        assert "LIKE" in compiled
        # ilike 编译为 lower()；_ 被转义为 /_ → wedw/_dws.%
        assert "wedw/_dws.%" in compiled
        assert "ESCAPE '/'" in compiled

    async def test_list_tables_database_filter_escapes_wildcards(self) -> None:
        """库名含 % / _ 通配符时转义，防止模糊放大（对齐采集目录 description-coverage）。"""
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.scalar.return_value = 0
        r.scalars.return_value.all.return_value = []
        s.execute = AsyncMock(return_value=r)

        _rows, _total = await repo.list_tables(None, None, 100, database="ods_%_d")

        stmt = s.execute.call_args_list[1].args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        # ods_%_d → ods/_/%/_d（% → /%、_ → /_）
        assert "ods/_/%/_d.%" in compiled
        assert "ESCAPE '/'" in compiled

    async def test_orphan_assets(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.scalar.return_value = 1
        r.scalars.return_value.all.return_value = [SimpleNamespace(id=1, owner_id=None)]
        s.execute = AsyncMock(return_value=r)

        rows, total = await repo.orphan_assets()

        assert len(rows) == 1
        assert total == 1
        stmt = s.execute.call_args_list[1].args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "owner_id IS NULL" in compiled

    async def test_orphan_assets_multi_dimension_filters(self) -> None:
        """孤儿资产多维度过滤：数据源/实体类型/敏感度/Schema/关键字同时生效。"""
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.scalar.return_value = 0
        r.scalars.return_value.all.return_value = []
        s.execute = AsyncMock(return_value=r)

        rows, _total = await repo.orphan_assets(
            keyword="ods_%",
            source_id="s1",
            entity_type="table",
            sensitivity="PII",
            schema_status="incomplete",
            limit=50,
        )

        assert rows == []
        stmt = s.execute.call_args_list[1].args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "owner_id IS NULL" in compiled
        assert "source_id" in compiled and "= 's1'" in compiled
        assert "entity_type" in compiled and "= 'table'" in compiled
        assert "sensitivity_level" in compiled and "= 'PII'" in compiled
        assert "schema_incomplete" in compiled
        # LIKE 通配符转义 + limit 收敛（ods_% → %ods/_/%%）
        assert "ods/_/%%" in compiled and "ESCAPE '/'" in compiled
        assert "LIMIT" in compiled and "50" in compiled

    async def test_orphan_assets_domain_joins_data_source(self) -> None:
        """孤儿资产业务域过滤：经 data_source 继承 JOIN（仅活跃源）。"""
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.scalar.return_value = 0
        r.scalars.return_value.all.return_value = []
        s.execute = AsyncMock(return_value=r)

        rows, _total = await repo.orphan_assets(domain="sales")

        assert rows == []
        stmt = s.execute.call_args_list[1].args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "data_source" in compiled
        assert "sales" in compiled
        assert "deleted_at IS NULL" in compiled

    async def test_orphan_assets_database_filter(self) -> None:
        """孤儿资产库名过滤：entity_name 前缀 LIKE '{db}.%'（通配符转义）。"""
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.scalar.return_value = 0
        r.scalars.return_value.all.return_value = []
        s.execute = AsyncMock(return_value=r)

        rows, _total = await repo.orphan_assets(database="wedw_dim")

        assert rows == []
        stmt = s.execute.call_args_list[1].args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "entity_name" in compiled
        assert "LIKE" in compiled
        # ilike 编译为 lower()；_ 被转义为 /_ → wedw/_dim.%
        assert "wedw/_dim.%" in compiled
        assert "ESCAPE '/'" in compiled
        # 库过滤不破坏孤儿语义（owner_id IS NULL 仍在）
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
        r_by_source = MagicMock()
        r_by_source.all.return_value = [("hive_meta", "Hive 元数据", 4), ("mysql_uni", None, 1)]
        r_by_db = MagicMock()
        r_by_db.all.return_value = [("wedw_dws", 3), ("wedw_dw", 2)]
        r_pii = MagicMock()
        r_pii.all.return_value = [
            ("PII", True, 1),
            ("PII", False, 2),
            ("CONFIDENTIAL", False, 1),
        ]
        s.execute = AsyncMock(
            side_effect=[
                r_total, r_by_type, r_by_sens, r_orphan, r_by_source, r_by_db, r_pii
            ]
        )

        out = await repo.catalog_summary()

        assert out["total"] == 5
        assert out["by_entity_type"] == {"table": 3, "field": 2}
        assert out["by_sensitivity"] == {"PII": 2}
        assert out["orphan_assets"] == 1
        assert out["by_source"] == [
            {"source_id": "hive_meta", "source_name": "Hive 元数据", "count": 4},
            {"source_id": "mysql_uni", "source_name": "mysql_uni", "count": 1},
        ]
        assert out["by_database"] == [
            {"database": "wedw_dws", "count": 3},
            {"database": "wedw_dw", "count": 2},
        ]
        # 目录资产 PII 合规：敏感 4（PII 3 / CONFIDENTIAL 1）、已复核 1、待复核 3、合规率 25%
        assert out["pii_compliance"] == {
            "sensitive_total": 4,
            "reviewed": 1,
            "pending": 3,
            "compliance_rate": 25.0,
            "by_sensitivity": {"PII": 3, "CONFIDENTIAL": 1},
        }

    async def test_catalog_summary_source_database_sql(self) -> None:
        """按数据源/库聚合的 SQL 形态：join data_source 取名称、substring_index 拆库名。"""
        s = _session()
        repo = AssetMapRepository(s)
        r = MagicMock()
        r.scalar.return_value = 0
        r.all.return_value = []
        s.execute = AsyncMock(return_value=r)

        await repo.catalog_summary()

        stmts = [
            str(a.args[0].compile(compile_kwargs={"literal_binds": True}))
            for a in s.execute.call_args_list
        ]
        # 数据源聚合：LEFT JOIN data_source、source_name 兜底、count 降序
        src_sql = stmts[4]
        assert "db_catalog" in src_sql and "data_source" in src_sql
        assert "substring_index" in stmts[5]
        assert "LIKE" in stmts[5] and "'%.%'" in stmts[5]
        assert "ORDER BY" in src_sql and "DESC" in src_sql
        # 目录资产 PII 合规聚合：敏感度 IN (PII,CONFIDENTIAL) + 未删 + 按敏感度×复核态分组
        pii_sql = stmts[6]
        assert "PII" in pii_sql and "CONFIDENTIAL" in pii_sql
        assert "compliance_reviewed" in pii_sql
        assert "GROUP BY" in pii_sql

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
        r_type = MagicMock()
        r_type.all.return_value = [("atomic", 3), ("derived", 1)]
        r_tier = MagicMock()
        r_tier.all.return_value = [("T1", 2), ("T3", 2)]
        r_pii = MagicMock()
        r_pii.scalar.return_value = 1
        r_dep = MagicMock()
        r_dep.scalar.return_value = 0
        r_cat = MagicMock()
        r_cat.all.return_value = [
            SimpleNamespace(
                id=1,
                entity_name="catalog.db.t",
                entity_type="table",
                sensitivity_level="PII",
                source_id="s1",
                updated_at=None,
            )
        ]
        r_src = MagicMock()
        r_src.all.return_value = [("s1", "Source A", "sales")]
        r_usr = MagicMock()
        r_usr.all.return_value = [(9, "Bob", "bob")]
        r_metric_codes = MagicMock()
        r_metric_codes.scalars.return_value.all.return_value = ["m1", "m2", "m3", "m4"]
        r_snap_codes = MagicMock()
        r_snap_codes.scalars.return_value.all.return_value = ["m1"]
        r_profile = MagicMock()
        r_profile.first.return_value = ("Bob", "bob", "metric_owner", "sales")
        s.execute = AsyncMock(
            side_effect=[
                r_stats,
                r_domain,
                r_type,
                r_tier,
                r_pii,
                r_dep,
                r_cat,
                r_src,
                r_usr,
                r_metric_codes,
                r_snap_codes,
                r_profile,
            ]
        )

        out = await repo.owner_aggregation(owner_id=9)

        assert out["owner_id"] == 9
        assert out["owner_name"] == "Bob"
        assert out["role"] == "metric_owner"
        assert out["domain"] == "sales"
        assert out["metrics"]["total"] == 4
        assert out["metrics"]["published"] == 2
        assert out["metrics"]["by_domain"] == {"sales": 4}
        assert out["metrics"]["by_type"] == {"atomic": 3, "derived": 1}
        assert out["metrics"]["by_metric_tier"] == {"T1": 2, "T3": 2}
        assert out["metrics"]["snapshot_covered"] == 1
        assert out["metrics"]["todo"]["pii_unreviewed"] == 1
        # 目录明细（可下钻，替代纯数字）
        assert out["catalogs"]["total"] == 1
        assert out["catalogs"]["items"][0]["entity_name"] == "catalog.db.t"
        assert out["catalogs"]["items"][0]["source_name"] == "Source A"
        assert out["catalogs"]["items"][0]["owner_name"] == "Bob"

    async def test_metric_dimension_summary(self) -> None:
        """指标体系聚合：13 类维度分布 + PII 合规率。"""
        s = _session()
        repo = AssetMapRepository(s)

        def dist(*pairs: tuple[str, int]) -> MagicMock:
            r = MagicMock()
            r.all.return_value = list(pairs)
            return r

        r_pii_total = MagicMock()
        r_pii_total.scalar.return_value = 4
        r_pii_reviewed = MagicMock()
        r_pii_reviewed.scalar.return_value = 3
        r_total = MagicMock()
        r_total.scalar.return_value = 10
        # 顺序：pii_total → pii_reviewed → metric_total → 13 类 distribution
        s.execute = AsyncMock(
            side_effect=[
                r_pii_total,
                r_pii_reviewed,
                r_total,
                dist(("atomic", 7), ("derived", 3)),  # type
                dist(("day", 6), ("month", 4)),  # granularity
                dist(("DWS", 6), ("ADS", 4)),  # dw_layer
                dist(("T1", 3), ("T2", 2), ("T3", 5)),  # tier
                dist(("CNY", 3), ("cnt", 7)),  # unit
                dist(("CNY", 3), ("USD", 1)),  # currency
                dist(("SUM", 8), ("AVG", 2)),  # aggregation
                dist(("PERIOD", 9), ("YTD", 1)),  # time_semantics
                dist(("T1", 6), ("HOURLY", 4)),  # freshness
                dist(("BATCH_ONLY", 7), ("REALTIME_ONLY", 3)),  # serving_mode
                dist(("ADDITIVE", 8), ("NON_ADDITIVE", 2)),  # additivity
                dist(("PUBLISHED", 5), ("DRAFT", 3), ("DEPRECATED", 2)),  # status
                dist(("sales", 6), ("user", 4)),  # domain
            ]
        )

        out = await repo.metric_dimension_summary()

        assert out["total"] == 10
        assert out["by_type"] == {"atomic": 7, "derived": 3}
        assert out["by_granularity"] == {"day": 6, "month": 4}
        assert out["by_dw_layer"] == {"DWS": 6, "ADS": 4}
        assert out["by_metric_tier"] == {"T1": 3, "T2": 2, "T3": 5}
        assert out["by_unit"] == {"CNY": 3, "cnt": 7}
        assert out["by_currency"] == {"CNY": 3, "USD": 1}
        assert out["by_aggregation"] == {"SUM": 8, "AVG": 2}
        assert out["by_time_semantics"] == {"PERIOD": 9, "YTD": 1}
        assert out["by_freshness"] == {"T1": 6, "HOURLY": 4}
        assert out["by_serving_mode"] == {"BATCH_ONLY": 7, "REALTIME_ONLY": 3}
        assert out["by_additivity"] == {"ADDITIVE": 8, "NON_ADDITIVE": 2}
        assert out["by_status"] == {"PUBLISHED": 5, "DRAFT": 3, "DEPRECATED": 2}
        assert out["by_domain"] == {"sales": 6, "user": 4}
        assert out["pii_compliance"]["pii_total"] == 4
        assert out["pii_compliance"]["pii_reviewed"] == 3
        assert out["pii_compliance"]["pii_unreviewed"] == 1
        assert out["pii_compliance"]["review_rate"] == 0.75
        # 合规统计必须排除 DEPRECATED：已废弃指标不参与 PII 合规分母/分子
        for i in (0, 1):
            compiled = str(
                s.execute.call_args_list[i].args[0].compile(
                    compile_kwargs={"literal_binds": True}
                )
            )
            assert "pii_flag" in compiled
            assert "'DEPRECATED'" in compiled

    async def test_metric_dimension_summary_no_pii(self) -> None:
        """无有效 PII 指标（含废弃被排除后为 0）时 review_rate=None，前端展示空态。"""
        s = _session()
        repo = AssetMapRepository(s)

        def dist(*pairs: tuple[str, int]) -> MagicMock:
            r = MagicMock()
            r.all.return_value = list(pairs)
            return r

        r_zero = MagicMock()
        r_zero.scalar.return_value = 0
        r_total = MagicMock()
        r_total.scalar.return_value = 5
        s.execute = AsyncMock(
            side_effect=[
                r_zero,
                r_zero,
                r_total,
                dist(("atomic", 5)),  # type
                dist(("day", 5)),  # granularity
                dist(("DWS", 5)),  # dw_layer
                dist(("T1", 5)),  # tier
                dist(("cnt", 5)),  # unit
                dist(("CNY", 5)),  # currency
                dist(("SUM", 5)),  # aggregation
                dist(("PERIOD", 5)),  # time_semantics
                dist(("T1", 5)),  # freshness
                dist(("BATCH_ONLY", 5)),  # serving_mode
                dist(("ADDITIVE", 5)),  # additivity
                dist(("PUBLISHED", 3), ("DRAFT", 2)),  # status（无 DEPRECATED）
                dist(("sales", 5)),  # domain
            ]
        )

        out = await repo.metric_dimension_summary()

        assert out["pii_compliance"]["pii_total"] == 0
        assert out["pii_compliance"]["pii_reviewed"] == 0
        assert out["pii_compliance"]["pii_unreviewed"] == 0
        assert out["pii_compliance"]["review_rate"] is None

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

        # 0109 枚举统一：热力图列含 NEEDS_REVIEW 与 UNKNOWN（6 值并集）
        assert out["columns"] == [
            "PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", "NEEDS_REVIEW", "UNKNOWN",
        ]
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
        assert AssetMapRepository._escape_like("100%_ok") == "100/%/_ok"
        assert AssetMapRepository._escape_like("a/b") == "a//b"

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
            source_id="s1",
            description="订单表",
            schema_json={"fields": [{"name": "order_id", "type": "bigint"}]},
            updated_at=None,
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
            type="atomic",
            granularity="day",
            # OneData 原子层：关联逻辑度量（度量目录）
            measure_id=1,
            unit="CNY",
            aggregation="SUM",
            time_semantics="PERIOD",
            freshness="T1",
            dw_layer="DWS",
            metric_tier="T1",
            additivity="ADDITIVE",
            serving_mode="BATCH_ONLY",
            description="每日成交总额",
            updated_at=None,
        )

    def _src(self) -> MagicMock:
        r = MagicMock()
        r.all.return_value = [("s1", "Source A", "sales")]
        return r

    def _usr(self) -> MagicMock:
        r = MagicMock()
        r.all.return_value = [(2, "Alice", "alice")]
        return r

    def _empty_scalars(self) -> MagicMock:
        r = MagicMock()
        r.scalars.return_value.all.return_value = []
        return r

    async def test_search_both_types(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r_cat = MagicMock()
        r_cat.scalars.return_value.all.return_value = [self._catalog()]
        r_met = MagicMock()
        r_met.scalars.return_value.all.return_value = [self._metric()]
        # catalog 表查询 → enrich(src/usr) → field 扫描(空) → metric 查询 → metric usr
        s.execute = AsyncMock(
            side_effect=[r_cat, self._src(), self._usr(), self._empty_scalars(), r_met, self._usr()]
        )

        out = await repo.search_assets("sales", None, 20)

        assert len(out) == 2
        assert out[0]["type"] == "catalog"
        assert out[0]["source_name"] == "Source A"
        assert out[0]["owner_name"] == "Alice"
        assert out[0]["column_count"] == 1
        assert out[0]["description"] == "订单表"
        assert out[1]["type"] == "metric"
        assert out[1]["metric_type"] == "atomic"
        assert out[1]["granularity"] == "day"
        assert out[1]["unit"] == "CNY"
        assert out[1]["freshness"] == "T1"
        assert out[1]["sensitivity_level"] == "INTERNAL"
        # 查询含转义后的 LIKE
        cat_stmt = s.execute.call_args_list[0].args[0]
        compiled = str(cat_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "LIKE" in compiled.upper()

    async def test_search_field_type(self) -> None:
        """限定 field 时只扫字段，返回 ``table.field`` 项（字段级搜索）。"""
        s = _session()
        repo = AssetMapRepository(s)
        cat = self._catalog()
        r_field = MagicMock()
        r_field.scalars.return_value.all.return_value = [cat]
        s.execute = AsyncMock(side_effect=[r_field, self._src(), self._usr()])

        out = await repo.search_assets("order_id", "field", 20)

        assert len(out) == 1
        assert out[0]["type"] == "field"
        assert out[0]["name"] == "catalog.db.orders.order_id"
        assert out[0]["entity_type"] == "field"

    async def test_search_metric_only(self) -> None:
        """限定 metric 时只查指标，不查目录/字段（分流查询）。"""
        s = _session()
        repo = AssetMapRepository(s)
        r_met = MagicMock()
        r_met.scalars.return_value.all.return_value = [self._metric()]
        s.execute = AsyncMock(side_effect=[r_met, self._usr()])

        out = await repo.search_assets("sales", "metric", 20)

        assert len(out) == 1
        assert out[0]["type"] == "metric"
        assert out[0]["owner_name"] == "Alice"
        # 只执行两次查询（指标 + 责任人名）
        assert s.execute.await_count == 2

    async def test_search_table_only_skips_metrics(self) -> None:
        """限定 table 时只查目录，不查指标/字段。"""
        s = _session()
        repo = AssetMapRepository(s)
        r_cat = MagicMock()
        r_cat.scalars.return_value.all.return_value = [self._catalog()]
        s.execute = AsyncMock(side_effect=[r_cat, self._src(), self._usr()])

        out = await repo.search_assets("orders", "table", 20)

        assert len(out) == 1
        assert out[0]["type"] == "catalog"
        assert s.execute.await_count == 3

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
                type="atomic",
                granularity="day",
                unit="cnt",
                aggregation="COUNT_DISTINCT",
                time_semantics="PERIOD",
                freshness="T1",
                dw_layer="DWS",
                metric_tier="T2",
                additivity="NON_ADDITIVE",
                serving_mode="BATCH_ONLY",
                description=None,
                updated_at=None,
            )
        ]
        # catalog(空) → field(空) → metric → metric usr
        s.execute = AsyncMock(
            side_effect=[r_cat, self._empty_scalars(), r_met, self._usr()]
        )

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
        # 表/字段描述体检：返回 (description, schema_json) 行
        r_desc = MagicMock()
        r_desc.all.return_value = [
            SimpleNamespace(
                description=None,
                schema_json={"fields": [{"name": "a"}, {"name": "b"}]},
            ),
            SimpleNamespace(description="有描述", schema_json={"fields": [{"name": "c"}]}),
        ]
        r_covered = MagicMock()
        r_covered.scalar.return_value = 2
        # 指标体检：PII 未复核 / 快照 codes / 无快照 rows / 废弃未替换
        r_pii = MagicMock()
        r_pii.all.return_value = [SimpleNamespace(metric_code="m1", name="M1", owner_id=1)]
        r_snap = MagicMock()
        r_snap.scalars.return_value.all.return_value = ["m1"]
        r_no_snap = MagicMock()
        r_no_snap.all.return_value = [SimpleNamespace(metric_code="m2", name="M2")]
        r_dep = MagicMock()
        r_dep.all.return_value = [SimpleNamespace(metric_code="m3", name="M3")]
        s.execute = AsyncMock(
            side_effect=[
                r_unhealthy,
                r_incomplete,
                r_orphan,
                r_stale,
                r_desc,
                r_covered,
                r_pii,
                r_snap,
                r_no_snap,
                r_dep,
            ]
        )

        out = await repo.health_summary()

        assert out["unhealthy_sources"][0]["source_id"] == "s1"
        assert out["schema_incomplete"][0]["entity_name"] == "t"
        assert out["orphan_assets"] == 2
        assert len(out["stale_assets"]) == 1
        assert out["stale_days"] == 7
        # 9 项体检
        assert len(out["checks"]) == 9
        assert out["checks"][0]["key"] == "unhealthy_sources"
        # 字段描述缺失：3 字段 - 2 已覆盖 = 1
        assert out["checks"][5]["key"] == "fields_missing_desc"
        assert out["checks"][5]["count"] == 1
        # PII 未复核 1 项 → 扣 5；废弃未替换 1 项 → 扣 3
        assert out["checks"][6]["key"] == "pii_unreviewed"
        assert out["pii_unreviewed"][0]["metric_code"] == "m1"
        assert out["metrics_without_snapshot"][0]["metric_code"] == "m2"
        assert out["deprecated_without_successor"][0]["metric_code"] == "m3"
        # 100 - 5(unhealthy) - 2(schema) - 1(stale) - 5(pii) - 2(no_snapshot) - 3(dep) = 82 → good
        assert out["score"] == 82
        assert out["level"] == "good"

    async def test_score_floor_and_levels(self) -> None:
        """评分下限 0，且分档映射正确。"""
        assert AssetMapRepository._health_level(95) == "excellent"
        assert AssetMapRepository._health_level(80) == "good"
        assert AssetMapRepository._health_level(65) == "fair"
        assert AssetMapRepository._health_level(30) == "poor"




    async def test_health_descriptions_filters_by_org(self) -> None:
        """org_id 非 None 时表/字段描述体检按组织过滤（join DataSource.org_id）。"""
        from sqlalchemy import Select

        s = _session()
        repo = AssetMapRepository(s)
        r_desc = MagicMock()
        r_desc.all.return_value = [
            SimpleNamespace(
                description=None,
                schema_json={"fields": [{"name": "a"}, {"name": "b"}]},
            )
        ]
        r_covered = MagicMock()
        r_covered.scalar.return_value = 0
        s.execute = AsyncMock(side_effect=[r_desc, r_covered])

        tables_missing, field_missing, field_total = await repo._health_descriptions(org_id=7)
        assert tables_missing == 1
        assert field_total == 2

        selects = [
            c.args[0] for c in s.execute.call_args_list if isinstance(c.args[0], Select)
        ]
        assert len(selects) == 2
        rendered = [str(x.compile(compile_kwargs={"literal_binds": True})) for x in selects]
        assert all("org_id = 7" in r for r in rendered)
class TestPiiOverview:
    async def test_aggregates(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r_sens = MagicMock()
        r_sens.all.return_value = [("PII", 3)]
        r_domain = MagicMock()
        r_domain.all.return_value = [("sales", 2)]
        # PII 合规增强：无主/待复核/已复核计数（scalar 返回数字）
        r_unowned = MagicMock()
        r_unowned.scalar.return_value = 1
        r_unreviewed = MagicMock()
        r_unreviewed.scalar.return_value = 1
        r_unreviewed_metric = MagicMock()
        r_unreviewed_metric.scalar.return_value = 1
        # 字段类别分布（scalars().all 返回空）
        r_cat = MagicMock()
        r_cat.scalars.return_value.all.return_value = []
        s.execute = AsyncMock(
            side_effect=[r_sens, r_domain, r_unowned, r_unreviewed, r_unreviewed_metric, r_cat]
        )

        out = await repo.pii_overview()

        assert out["pii_catalog_count"] == 3
        assert out["pii_metric_count"] == 2
        assert out["by_sensitivity"] == {"PII": 3}
        assert out["by_domain"] == {"sales": 2}
        # PII 合规增强扩展字段
        assert out["unowned_pii"] == 1
        assert out["unreviewed_pii"] == 2  # 1 目录 + 1 指标
        assert out["unreviewed_catalog"] == 1
        assert out["unreviewed_metric"] == 1
        assert out["reviewed_pii"] == 2  # 3 总 - 1 未复核
        assert out["by_category"] == {}


class TestRecentChanges:
    def _catalog_row(self) -> SimpleNamespace:
        return SimpleNamespace(
            id=1,
            entity_name="catalog.db.t",
            entity_type="table",
            sensitivity_level="INTERNAL",
            owner_id=2,
            source_id="s1",
            created_at=None,
            updated_at=None,
        )

    def _metric_row(self) -> SimpleNamespace:
        return SimpleNamespace(
            metric_code="sales_gmv_amount_day",
            name="GMV",
            status="PUBLISHED",
            domain="sales",
            pii_flag=False,
            version=3,
            description="每日成交总额",
            owner_id=2,
            updated_at=None,
        )

    def _src(self) -> MagicMock:
        r = MagicMock()
        r.all.return_value = [("s1", "Source A", "sales")]
        return r

    def _usr(self) -> MagicMock:
        r = MagicMock()
        r.all.return_value = [(2, "Alice", "alice")]
        return r

    async def test_recent_catalogs_and_metrics(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r_cat = MagicMock()
        r_cat.all.return_value = [self._catalog_row()]
        r_met = MagicMock()
        r_met.all.return_value = [self._metric_row()]
        r_drift = MagicMock()
        r_drift.all.return_value = []
        # catalog → enrich(src/usr) → metric → metric usr → drift
        s.execute = AsyncMock(
            side_effect=[r_cat, self._src(), self._usr(), r_met, self._usr(), r_drift]
        )

        out = await repo.recent_changes(days=7, limit=50)

        assert out["days"] == 7
        assert out["catalogs"][0]["entity_name"] == "catalog.db.t"
        assert out["catalogs"][0]["source_name"] == "Source A"
        assert out["catalogs"][0]["owner_name"] == "Alice"
        assert out["catalogs"][0]["change_type"] == "updated"
        assert out["metrics"][0]["metric_code"] == "sales_gmv_amount_day"
        assert out["metrics"][0]["pii_flag"] is False
        assert out["metrics"][0]["version"] == 3
        assert out["metrics"][0]["change_type"] == "updated"
        assert out["drift"] == []

    async def test_recent_changes_created_detection(self) -> None:
        """created_at 接近 updated_at（3s 内）→ change_type=created。"""
        s = _session()
        repo = AssetMapRepository(s)
        r_cat = MagicMock()
        r_cat.all.return_value = [
            SimpleNamespace(
                id=2,
                entity_name="catalog.db.new",
                entity_type="table",
                sensitivity_level="PII",
                owner_id=None,
                source_id=None,
                created_at=datetime(2026, 8, 1, 0, 0, 0),
                updated_at=datetime(2026, 8, 1, 0, 0, 1),
            )
        ]
        r_met = MagicMock()
        r_met.all.return_value = []
        r_drift = MagicMock()
        r_drift.all.return_value = []
        # catalog(无 source/owner → enrich 不查) → metric → drift
        s.execute = AsyncMock(side_effect=[r_cat, r_met, r_drift])

        out = await repo.recent_changes(days=7, limit=50)

        assert out["catalogs"][0]["change_type"] == "created"


class TestMyAssets:
    def _catalog_row(self) -> SimpleNamespace:
        return SimpleNamespace(
            id=1,
            entity_name="catalog.db.t",
            entity_type="table",
            sensitivity_level="PII",
            source_id="s1",
            owner_id=2,
            description="订单表",
            schema_json={"fields": [{"name": "order_id"}]},
            updated_at=None,
        )

    def _metric_row(self) -> SimpleNamespace:
        return SimpleNamespace(
            metric_code="sales_gmv_amount_day",
            name="GMV",
            status="PUBLISHED",
            domain="sales",
            pii_flag=True,
            type="atomic",
            granularity="day",
            unit="CNY",
            metric_tier="T1",
            description="每日成交总额",
            updated_at=None,
        )

    async def test_my_assets(self) -> None:
        s = _session()
        repo = AssetMapRepository(s)
        r_cat = MagicMock()
        r_cat.all.return_value = [self._catalog_row()]
        r_met = MagicMock()
        r_met.all.return_value = [self._metric_row()]
        r_src = MagicMock()
        r_src.all.return_value = [("s1", "Source A", "sales")]
        r_usr = MagicMock()
        r_usr.all.return_value = [(2, "Alice", "alice")]
        r_snap = MagicMock()
        r_snap.scalars.return_value.all.return_value = ["sales_gmv_amount_day"]
        r_claim = MagicMock()
        r_claim.scalar.return_value = 5
        # catalog → enrich(src/usr) → metric → snapshot → claimable
        s.execute = AsyncMock(
            side_effect=[r_cat, r_src, r_usr, r_met, r_snap, r_claim]
        )

        out = await repo.my_assets(owner_id=2, limit=50)

        assert out["owner_id"] == 2
        assert out["catalogs"][0]["sensitivity_level"] == "PII"
        assert out["catalogs"][0]["source_name"] == "Source A"
        assert out["catalogs"][0]["column_count"] == 1
        assert out["metrics"][0]["pii_flag"] is True
        assert out["metrics"][0]["metric_tier"] == "T1"
        # summary 统计
        assert out["summary"]["catalog_count"] == 1
        assert out["summary"]["metric_count"] == 1
        assert out["summary"]["snapshot_covered"] == 1
        assert out["summary"]["pii_count"] == 1
        # 待认领孤儿
        assert out["claimable_orphans"] == 5
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
        s.execute.return_value.rowcount = 1
        entity = DBCatalog(id=1, entity_name="catalog.sales.orders", owner_id=None, row_version=1)
        repo = AssetMapRepository(s)
        out = await repo.assign_owner(entity, owner_id=9)
        assert out is entity
        assert entity.owner_id == 9
        assert entity.row_version == 2
        stmt = s.execute.call_args.args[0]
        assert "UPDATE" in str(stmt) and "row_version" in str(stmt)
        s.flush.assert_awaited_once()

    async def test_assign_owner_release(self) -> None:
        s = _session()
        s.execute.return_value.rowcount = 1
        entity = DBCatalog(id=1, entity_name="catalog.sales.orders", owner_id=9, row_version=1)
        repo = AssetMapRepository(s)
        await repo.assign_owner(entity, owner_id=None)
        assert entity.owner_id is None

    async def test_assign_owner_conflict_raises_409(self) -> None:
        """乐观锁：版本不匹配（rowcount=0）→ ConflictError(409)，不覆盖。"""
        from app.core.exceptions import ConflictError

        s = _session()
        s.execute.return_value.rowcount = 0
        entity = DBCatalog(id=1, entity_name="catalog.sales.orders", owner_id=None, row_version=1)
        repo = AssetMapRepository(s)
        with pytest.raises(ConflictError) as exc:
            await repo.assign_owner(entity, owner_id=9)
        assert exc.value.error_code == "OPTIMISTIC_LOCK_CONFLICT"
        assert entity.owner_id is None  # 未覆盖

    async def test_reclassify_sensitivity(self) -> None:
        s = _session()
        s.execute.return_value.rowcount = 1
        entity = DBCatalog(
            id=1, entity_name="catalog.sales.orders", sensitivity_level="INTERNAL", row_version=1
        )
        repo = AssetMapRepository(s)
        out = await repo.reclassify_sensitivity(entity, "PII")
        assert out is entity
        assert entity.sensitivity_level == "PII"
        assert entity.row_version == 2
        stmt = s.execute.call_args.args[0]
        assert "UPDATE" in str(stmt) and "row_version" in str(stmt)
        s.flush.assert_awaited_once()

    async def test_batch_assign_owner(self) -> None:
        s = _session()
        s.execute.return_value.rowcount = 1
        entities = [
            DBCatalog(id=1, entity_name="catalog.sales.orders", row_version=1),
            DBCatalog(id=2, entity_name="catalog.sales.items", row_version=1),
        ]
        repo = AssetMapRepository(s)
        affected = await repo.batch_assign_owner(entities, owner_id=9)
        assert affected == 2
        assert all(e.owner_id == 9 for e in entities)
        assert all(e.row_version == 2 for e in entities)
        assert s.execute.await_count == 2  # 逐条乐观锁 UPDATE
        s.flush.assert_awaited_once()

    async def test_batch_reclassify(self) -> None:
        s = _session()
        s.execute.return_value.rowcount = 1
        entities = [DBCatalog(id=1, entity_name="catalog.sales.orders", row_version=1)]
        repo = AssetMapRepository(s)
        affected = await repo.batch_reclassify(entities, "CONFIDENTIAL")
        assert affected == 1
        assert entities[0].sensitivity_level == "CONFIDENTIAL"
        assert entities[0].row_version == 2
        s.flush.assert_awaited_once()
