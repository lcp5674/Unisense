"""资产地图 Repository（TD §12.11 / FR-18）。

只读聚合：元数据目录（db_catalog）、分类（classification）、指标（metric）。
P2 增强：图谱降级查询、热力聚合、责任人视图。
产品补充（FR-18 生产化）：全局搜索、资产健康、PII 合规视图、变更追踪、
我的资产、详情增强（血缘边列表 + 关联指标 + 源健康/新鲜度）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.collector_models import SchemaDriftLog
from app.models.consume import MetricValueSnapshot
from app.models.data_source import ColumnDescription, DataSource, DBCatalog
from app.models.enums import SensitivityLevelEnum
from app.models.governance import Classification
from app.models.lineage import LineageEdge
from app.models.metric import Metric
from app.models.user import User


def _prune_graph_by_depth(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    depth: int,
    seed_nodes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """从指标（seed）出发沿血缘边 BFS ``depth`` 层，收敛图谱规模。

    血缘是下游汇聚到指标的有向图：指标作为 BFS 起点向上游逐层展开，
    ``depth=1`` 只保留指标与其直连表，``depth=2`` 再展开一层中间表。
    返回 (收敛后节点, 两端均在被保留节点内的边)。
    """
    seed_ids = {n["id"] for n in seed_nodes}
    adj: dict[str, set[str]] = {}
    for e in edges:
        adj.setdefault(e["source"], set()).add(e["target"])
        adj.setdefault(e["target"], set()).add(e["source"])

    # BFS 无向遍历（血缘上下游都算邻居）；visited 防环
    frontier = set(seed_ids)
    visited = set(seed_ids)
    for _ in range(depth):
        nxt: set[str] = set()
        for nid in frontier:
            nxt |= adj.get(nid, set())
        nxt -= visited
        if not nxt:
            break
        visited |= nxt
        frontier = nxt

    kept_ids = set(visited)
    pruned_nodes = [n for n in nodes if n["id"] in kept_ids]
    pruned_edges = [
        e for e in edges if e["source"] in kept_ids and e["target"] in kept_ids
    ]
    return pruned_nodes, pruned_edges


class AssetMapRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_tables(
        self,
        source_id: str | None,
        sensitivity: str | None,
        limit: int,
        domain: str | None = None,
        owner_id: int | None = None,
        schema_status: str | None = None,
        keyword: str | None = None,
    ) -> list[DBCatalog]:
        """数据表目录多维度过滤（数据表 Tab / CSV 导出共用）。

        支持：数据源 / 敏感度 / 业务域（经 data_source 继承）/ 责任人 /
        Schema 完整性（complete|incomplete）/ 关键字（表名或数据源模糊）。
        """
        stmt = select(DBCatalog).where(
            DBCatalog.entity_type == "table", DBCatalog.deleted_at.is_(None)
        )
        if source_id:
            stmt = stmt.where(DBCatalog.source_id == source_id)
        if sensitivity:
            stmt = stmt.where(DBCatalog.sensitivity_level == sensitivity)
        if owner_id == 0:
            # 约定：owner_id=0 表示「无责任人」（未分配，孤儿表）
            stmt = stmt.where(DBCatalog.owner_id.is_(None))
        elif owner_id is not None:
            stmt = stmt.where(DBCatalog.owner_id == owner_id)
        if schema_status == "incomplete":
            stmt = stmt.where(DBCatalog.schema_incomplete.is_(True))
        elif schema_status == "complete":
            stmt = stmt.where(DBCatalog.schema_incomplete.is_(False))
        if domain:
            # db_catalog 无 domain 列，经数据源继承过滤（仅活跃源归属明确）
            stmt = stmt.join(DataSource, DataSource.source_id == DBCatalog.source_id).where(
                DataSource.deleted_at.is_(None),
                DataSource.domain == domain,
            )
        if keyword:
            # LIKE 通配符转义（对齐 collector.list_catalogs：% / _ 须转义防模糊放大）
            escaped = keyword.replace("/", "//").replace("%", "/%").replace("_", "/_")
            stmt = stmt.where(
                or_(
                    DBCatalog.entity_name.ilike(f"%{escaped}%", escape="/"),
                    DBCatalog.source_id.ilike(f"%{escaped}%", escape="/"),
                )
            )
        return list((await self._session.execute(stmt.limit(limit))).scalars().all())

    async def orphan_assets(
        self,
        keyword: str | None = None,
        source_id: str | None = None,
        domain: str | None = None,
        entity_type: str | None = None,
        sensitivity: str | None = None,
        schema_status: str | None = None,
        limit: int = 200,
    ) -> list[DBCatalog]:
        """孤儿资产（无责任人）多维度过滤，镜像 ``list_tables``。

        支持：关键字 / 数据源 / 业务域（经 data_source 继承）/ 实体类型 /
        敏感度 / Schema 完整性（complete|incomplete）。无参调用返回全部
        （概览下钻「孤儿资产明细」兼容）。
        """
        stmt = select(DBCatalog).where(
            DBCatalog.owner_id.is_(None), DBCatalog.deleted_at.is_(None)
        )
        if source_id:
            stmt = stmt.where(DBCatalog.source_id == source_id)
        if entity_type:
            stmt = stmt.where(DBCatalog.entity_type == entity_type)
        if sensitivity:
            stmt = stmt.where(DBCatalog.sensitivity_level == sensitivity)
        if schema_status == "incomplete":
            stmt = stmt.where(DBCatalog.schema_incomplete.is_(True))
        elif schema_status == "complete":
            stmt = stmt.where(DBCatalog.schema_incomplete.is_(False))
        if domain:
            # db_catalog 无 domain 列，经数据源继承过滤（仅活跃源归属明确）
            stmt = stmt.join(DataSource, DataSource.source_id == DBCatalog.source_id).where(
                DataSource.deleted_at.is_(None),
                DataSource.domain == domain,
            )
        if keyword:
            # LIKE 通配符转义（对齐 collector.list_catalogs：% / _ 须转义防模糊放大）
            escaped = keyword.replace("/", "//").replace("%", "/%").replace("_", "/_")
            stmt = stmt.where(
                or_(
                    DBCatalog.entity_name.ilike(f"%{escaped}%", escape="/"),
                    DBCatalog.source_id.ilike(f"%{escaped}%", escape="/"),
                )
            )
        return list((await self._session.execute(stmt.limit(limit))).scalars().all())

    @staticmethod
    def _summarize_schema(schema_json: Any) -> Any:
        """将 schema_json 压缩为可读摘要（字段名/类型/注释列表）。

        不直接返回原始 schema_json：其字段可能含敏感细节，摘要仅暴露
        字段级元数据，满足资产地图详情展示（TD §12.11 流程 #5）。
        """
        if not isinstance(schema_json, dict):
            return None
        fields = schema_json.get("fields") or schema_json.get("columns") or []
        if isinstance(fields, list):
            summary: list[dict[str, Any]] = []
            for f in fields:
                if isinstance(f, dict):
                    summary.append(
                        {
                            "name": f.get("name") or f.get("column"),
                            "type": f.get("type") or f.get("data_type"),
                            "comment": f.get("comment"),
                        }
                    )
                else:
                    summary.append({"name": str(f)})
            return summary
        return schema_json

    @staticmethod
    def _merge_descriptions(
        summary: list[dict[str, Any]],
        descriptions: Sequence[ColumnDescription],
    ) -> list[dict[str, Any]]:
        """将 column_descriptions 按 manual>llm>schema 优先级合并到 schema_summary。

        Args:
            summary: _summarize_schema 的输出列表。
            descriptions: column_descriptions 表记录。

        Returns:
            增强后的 summary，每条字段增加 description 和 description_source。
        """
        desc_map: dict[str, ColumnDescription] = {d.column_name: d for d in descriptions}
        for field in summary:
            col_name = field.get("name")
            if col_name and col_name in desc_map:
                d = desc_map[col_name]
                field["description"] = d.description
                field["description_source"] = d.source
            elif field.get("comment"):
                # 无独立描述记录，但有原始 comment → 使用 schema_json 原始 comment
                field["description"] = field["comment"]
                field["description_source"] = "schema"
            else:
                field["description"] = None
                field["description_source"] = None
        return summary

    async def get_entity_detail(self, entity_id: int) -> dict[str, Any] | None:
        """资产实体详情：元数据 + 敏感度 + PII + 血缘边列表 + 关联指标 + 源健康/新鲜度。

        Args:
            entity_id: db_catalog 主键。

        Returns:
            详情字典；实体不存在或已删除返回 ``None``。
        """
        row = (
            await self._session.execute(
                select(DBCatalog).where(DBCatalog.id == entity_id, DBCatalog.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if row is None:
            return None

        variants = self._lineage_variants(row.entity_name)
        lineage_edges = await self._lineage_edges_for(variants, limit=50)
        lineage_count = len(lineage_edges)

        # 关联指标：血缘下游指向 metric: 前缀的节点（指标级血缘）
        related_metrics = await self._related_metrics_for(variants)

        # 源健康/新鲜度：关联 data_source 的最后健康检查与健康状态
        source_health = await self._source_health(row.source_id)
        # 业务域：db_catalog 无 domain 列，经 data_source 继承（生产详情展示）
        domain = await self._source_domain(row.source_id)

        sens = (row.sensitivity_level or "").upper()

        # 查询 column_descriptions 并合并到 schema_summary
        schema_summary = self._summarize_schema(row.schema_json)
        if isinstance(schema_summary, list):
            descriptions = await self._session.execute(
                select(ColumnDescription).where(
                    ColumnDescription.catalog_id == row.id,
                    ColumnDescription.deleted_at.is_(None),
                )
            )
            desc_list = descriptions.scalars().all()
            schema_summary = self._merge_descriptions(schema_summary, desc_list)

        return {
            "id": row.id,
            "entity_name": row.entity_name,
            "entity_type": row.entity_type,
            "source_id": row.source_id,
            "source_name": (source_health or {}).get("source_name"),
            "domain": domain,
            "sensitivity_level": row.sensitivity_level,
            "owner_id": row.owner_id,
            # 责任人展示名（display_name 优先，缺省回退 username）——生产场景需可读
            "owner_name": await self._owner_display_name(row.owner_id),
            "column_count": len(schema_summary) if isinstance(schema_summary, list) else None,
            "schema_incomplete": row.schema_incomplete,
            "content_signature": row.content_signature,
            "schema_summary": schema_summary,
            # 表级业务描述（治理补全，TD §12.1）
            "description": row.description,
            "description_source": row.description_source,
            "description_updated_at": row.description_updated_at,
            "lineage_count": int(lineage_count),
            "lineage_edges": lineage_edges,
            "related_metrics": related_metrics,
            "source_health": source_health,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "pii_flag": "PII" in sens,
            # etl_sql 属敏感字段（可能内嵌连接串），详情接口不返回
            "etl_sql": None,
        }

    @staticmethod
    def _lineage_variants(entity_name: str) -> list[str]:
        """实体名的血缘节点编码形态（裸名/table:/field: 前缀）。"""
        return [entity_name, f"table:{entity_name}", f"field:{entity_name}"]

    async def _lineage_edges_for(self, variants: list[str], limit: int) -> list[dict[str, Any]]:
        """查询与某实体相关的血缘边明细（含类型/粒度/置信度/来源）。"""
        rows = (
            await self._session.execute(
                select(
                    LineageEdge.source_node,
                    LineageEdge.target_node,
                    LineageEdge.edge_type,
                    LineageEdge.granularity,
                    LineageEdge.confidence,
                    LineageEdge.provenance,
                )
                .where(
                    LineageEdge.deleted_at.is_(None),
                    or_(
                        LineageEdge.source_node.in_(variants),
                        LineageEdge.target_node.in_(variants),
                    ),
                )
                .limit(limit)
            )
        ).all()
        return [
            {
                "source": r.source_node,
                "target": r.target_node,
                "edge_type": r.edge_type,
                "granularity": r.granularity,
                "confidence": float(r.confidence or 0),
                "provenance": r.provenance,
            }
            for r in rows
        ]

    async def _related_metrics_for(self, variants: list[str]) -> list[dict[str, Any]]:
        """查询血缘下游指向该实体的关联指标（metric: 前缀节点）。"""
        rows = (
            await self._session.execute(
                select(LineageEdge.target_node, LineageEdge.edge_type)
                .where(
                    LineageEdge.deleted_at.is_(None),
                    LineageEdge.source_node.in_(variants),
                    LineageEdge.target_node.like("metric:%"),
                )
                .limit(50)
            )
        ).all()
        return [
            {
                "metric_node": r.target_node,
                "edge_type": r.edge_type,
            }
            for r in rows
        ]

    async def _source_health(self, source_id: str) -> dict[str, Any]:
        """查询数据源健康状态与最近健康检查时间（无则返回 unknown）。"""
        row = (
            await self._session.execute(
                select(
                    DataSource.health_status, DataSource.last_health_check, DataSource.name
                ).where(DataSource.source_id == source_id)
            )
        ).first()
        if row is None:
            return {"health_status": "unknown", "last_health_check": None, "source_name": None}
        return {
            "health_status": row.health_status,
            "last_health_check": row.last_health_check,
            "source_name": row.name,
        }

    async def _source_domain(self, source_id: str) -> str | None:
        """数据源所属业务域（db_catalog 无 domain 列，经 data_source 继承）。"""
        row = (
            await self._session.execute(
                select(DataSource.domain).where(
                    DataSource.source_id == source_id, DataSource.deleted_at.is_(None)
                )
            )
        ).first()
        return row[0] if row else None

    async def _owner_display_name(self, owner_id: int | None) -> str | None:
        """责任人可读名（display_name 优先，缺省回退 username）；无归属返回 None。"""
        if owner_id is None:
            return None
        row = (
            await self._session.execute(
                select(User.display_name, User.username).where(User.id == owner_id)
            )
        ).first()
        if row is None:
            return None
        return row[0] or row[1] or None

    async def enrich_catalog_items(
        self, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """批量给目录条目补源名称/业务域/责任人名（列表下钻与详情展示用，幂等）。"""
        if not items:
            return items
        source_ids = {it.get("source_id") for it in items if it.get("source_id")}
        owner_ids = {it.get("owner_id") for it in items if it.get("owner_id") is not None}
        src_map: dict[str, tuple[str | None, str | None]] = {}
        if source_ids:
            src_rows = (
                await self._session.execute(
                    select(DataSource.source_id, DataSource.name, DataSource.domain).where(
                        DataSource.source_id.in_(source_ids)
                    )
                )
            ).all()
            src_map = {r[0]: (r[1], r[2]) for r in src_rows}
        usr_map: dict[int, str] = {}
        if owner_ids:
            usr_rows = (
                await self._session.execute(
                    select(User.id, User.display_name, User.username).where(
                        User.id.in_(owner_ids)
                    )
                )
            ).all()
            usr_map = {r[0]: (r[1] or r[2]) for r in usr_rows}
        for it in items:
            sid = it.get("source_id")
            name, domain = src_map.get(sid, (None, None)) if sid is not None else (None, None)
            if it.get("source_name") is None:
                it["source_name"] = name
            if it.get("domain") is None:
                it["domain"] = domain
            oid = it.get("owner_id")
            if oid is not None and it.get("owner_name") is None:
                it["owner_name"] = usr_map.get(oid)
        return items

    async def catalog_summary(self) -> dict[str, Any]:
        total = (
            await self._session.execute(
                select(func.count()).select_from(DBCatalog).where(DBCatalog.deleted_at.is_(None))
            )
        ).scalar() or 0
        by_type = (
            await self._session.execute(
                select(DBCatalog.entity_type, func.count())
                .where(DBCatalog.deleted_at.is_(None))
                .group_by(DBCatalog.entity_type)
            )
        ).all()
        by_sens = (
            await self._session.execute(
                select(DBCatalog.sensitivity_level, func.count())
                .where(DBCatalog.deleted_at.is_(None))
                .group_by(DBCatalog.sensitivity_level)
            )
        ).all()
        orphans = (
            await self._session.execute(
                select(func.count())
                .select_from(DBCatalog)
                .where(DBCatalog.owner_id.is_(None), DBCatalog.deleted_at.is_(None))
            )
        ).scalar() or 0
        return {
            "total": total,
            "by_entity_type": dict(cast("Sequence[tuple[Any, Any]]", by_type)),
            "by_sensitivity": dict(cast("Sequence[tuple[Any, Any]]", by_sens)),
            "orphan_assets": orphans,
        }

    async def classification_summary(self) -> dict[str, Any]:
        rows = (
            await self._session.execute(
                select(Classification.sensitivity_level, func.count()).group_by(
                    Classification.sensitivity_level
                )
            )
        ).all()
        return {"by_sensitivity": dict(cast("Sequence[tuple[Any, Any]]", rows))}

    async def metric_summary(self) -> dict[str, Any]:
        by_domain = (
            await self._session.execute(
                select(Metric.domain, func.count())
                .where(Metric.deleted_at.is_(None))
                .group_by(Metric.domain)
            )
        ).all()
        by_status = (
            await self._session.execute(
                select(Metric.status, func.count())
                .where(Metric.deleted_at.is_(None))
                .group_by(Metric.status)
            )
        ).all()
        return {
            "by_domain": dict(cast("Sequence[tuple[Any, Any]]", by_domain)),
            "by_status": dict(cast("Sequence[tuple[Any, Any]]", by_status)),
        }

    async def _metric_distribution(self, column: Any) -> dict[str, int]:
        """按指定列聚合指标分布（null 值归入 ``__null__``，前端可显式展示）。"""
        rows = (
            await self._session.execute(
                select(column, func.count())
                .where(Metric.deleted_at.is_(None))
                .group_by(column)
            )
        ).all()
        out: dict[str, int] = {}
        for key, cnt in rows:
            out[str(key) if key is not None else "__null__"] = int(cnt or 0)
        return out

    async def metric_dimension_summary(self) -> dict[str, Any]:
        """指标体系聚合：指标多维分布 + PII 合规率。

        13 类维度：类型/粒度/分层/分级/单位/币种/聚合/时间语义/新鲜度/服务模式/可加性/状态/域。
        复用 SQL GROUP BY，与热力聚合同源（TD §12.11），避免指标体系口径漂移。
        """
        # 合规率：已复核 PII 指标 / 全部 PII 指标
        pii_total = (
            await self._session.execute(
                select(func.count())
                .select_from(Metric)
                .where(Metric.deleted_at.is_(None), Metric.pii_flag.is_(True))
            )
        ).scalar() or 0
        pii_reviewed = (
            await self._session.execute(
                select(func.count())
                .select_from(Metric)
                .where(
                    Metric.deleted_at.is_(None),
                    Metric.pii_flag.is_(True),
                    Metric.compliance_reviewed.is_(True),
                )
            )
        ).scalar() or 0
        metric_total = (
            await self._session.execute(
                select(func.count()).select_from(Metric).where(Metric.deleted_at.is_(None))
            )
        ).scalar() or 0

        return {
            "by_type": await self._metric_distribution(Metric.type),
            "by_granularity": await self._metric_distribution(Metric.granularity),
            "by_dw_layer": await self._metric_distribution(Metric.dw_layer),
            "by_metric_tier": await self._metric_distribution(Metric.metric_tier),
            "by_unit": await self._metric_distribution(Metric.unit),
            "by_currency": await self._metric_distribution(Metric.currency),
            "by_aggregation": await self._metric_distribution(Metric.aggregation),
            "by_time_semantics": await self._metric_distribution(Metric.time_semantics),
            "by_freshness": await self._metric_distribution(Metric.freshness),
            "by_serving_mode": await self._metric_distribution(Metric.serving_mode),
            "by_additivity": await self._metric_distribution(Metric.additivity),
            "by_status": await self._metric_distribution(Metric.status),
            "by_domain": await self._metric_distribution(Metric.domain),
            "pii_compliance": {
                "pii_total": int(pii_total),
                "pii_reviewed": int(pii_reviewed),
                "pii_unreviewed": int(pii_total - pii_reviewed),
                "review_rate": round(float(pii_reviewed) / pii_total, 4) if pii_total else 0.0,
            },
            "total": int(metric_total),
        }


    # ----------------------------------------------------------------
    # P2 Enhancement: 图谱降级、热力聚合、责任人视图
    # ----------------------------------------------------------------

    # 图谱表/视图节点上限（力导向图可读性：节点过多会失去地图形态）
    _GRAPH_CATALOG_LIMIT = 200

    async def _graph_metric_nodes(
        self, domain: str | None, pii_only: bool
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """metric 节点：id=``metric:{code}``，按域/PII 过滤。"""
        metric_stmt = select(
            Metric.metric_code,
            Metric.domain,
            Metric.pii_flag,
            Metric.owner_id,
            Metric.status,
        ).where(Metric.deleted_at.is_(None))
        if domain:
            metric_stmt = metric_stmt.where(Metric.domain == domain)
        if pii_only:
            metric_stmt = metric_stmt.where(Metric.pii_flag.is_(True))

        rows = (await self._session.execute(metric_stmt)).all()
        nodes: list[dict[str, Any]] = []
        allowed: set[str] = set()
        seen: set[str] = set()
        for row in rows:
            node_id = f"metric:{row.metric_code}"
            if node_id in seen:
                continue
            seen.add(node_id)
            allowed.add(node_id)
            nodes.append(
                {
                    "id": node_id,
                    "type": "metric",
                    "label": row.metric_code,
                    "pii": bool(row.pii_flag),
                    "domain": row.domain,
                    "owner": str(row.owner_id) if row.owner_id else None,
                }
            )
        return nodes, allowed

    async def _graph_lineage_table_names(self) -> set[str]:
        """血缘边引用的表/视图名集合（``table:`` 前缀节点），用于优先让业务表进图。

        若直接全量取 db_catalog 表并按插入序 LIMIT，会混入已删除探针源的系统表，
        导致 catalog 节点与血缘边（业务表）无法匹配、图退化为孤立散点。
        """
        rows = (
            (
                await self._session.execute(
                    select(LineageEdge.source_node).where(
                        LineageEdge.deleted_at.is_(None),
                        LineageEdge.source_node.like("table:%"),
                    )
                )
            )
            .scalars()
            .all()
        )
        return {r.split(":", 1)[1] for r in rows}

    async def _graph_catalog_nodes(
        self, pii_only: bool, lineage_tables: set[str]
    ) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
        """db_catalog 表/视图节点：id=``table:{entity_name}``（与血缘边格式对齐）。

        优先血缘边引用的表（``lineage_tables``）并排除已删除数据源，保证节点与边
        连通、图呈现真实血缘结构；域从 ``data_source.domain`` 继承（db_catalog 无
        域字段）；PII 由 ``sensitivity_level`` 含 "PII" 判定。
        """
        filters: list[Any] = [
            DBCatalog.deleted_at.is_(None),
            DBCatalog.entity_type.in_(["TABLE", "VIEW"]),
            DataSource.deleted_at.is_(None),
        ]
        if lineage_tables:
            filters.append(DBCatalog.entity_name.in_(lineage_tables))
        if pii_only:
            filters.append(DBCatalog.sensitivity_level.like("%PII%"))

        catalog_stmt = (
            select(
                DBCatalog.id,
                DBCatalog.entity_name,
                DBCatalog.entity_type,
                DBCatalog.sensitivity_level,
                DBCatalog.owner_id,
                DataSource.domain,
            )
            .join(DataSource, DataSource.source_id == DBCatalog.source_id)
            .where(*filters)
        )
        rows = (await self._session.execute(catalog_stmt.limit(self._GRAPH_CATALOG_LIMIT))).all()
        nodes: list[dict[str, Any]] = []
        domain_by_id: dict[str, str | None] = {}
        seen: set[str] = set()
        for row in rows:
            node_id = f"table:{row.entity_name}"
            if node_id in seen:
                continue
            seen.add(node_id)
            domain_by_id[node_id] = row.domain
            nodes.append(
                {
                    "id": node_id,
                    "type": "table",
                    "label": row.entity_name,
                    "entity_id": row.id,
                    "pii": bool(row.sensitivity_level and "PII" in row.sensitivity_level),
                    "domain": row.domain,
                    "owner": str(row.owner_id) if row.owner_id else None,
                }
            )
        return nodes, domain_by_id

    async def graph_from_mysql(
        self, domain: str | None, pii_only: bool, depth: int | None = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """从 MySQL lineage_edge + metric + db_catalog 拼接图谱数据。

        - 节点：metric（``metric:{code}``）+ db_catalog 表/视图（``table:{entity_name}``，
          与血缘边节点格式对齐）+ 血缘边引用到的字段（``field:{...}``，数量受控）。
        - 边：仅保留至少一端属于展示节点的边（**精确 IN 集合匹配**，消除 ``contains``
          子串误匹配）。
        - PII 视图：仅指标/表节点（字段级 PII 无法从血缘边判定，故不展示字段）。
        - ``depth``：从指标出发沿血缘边 BFS 收敛（None=全量不过滤）。值越小图越聚焦：
          depth=1 仅指标与其直连表，depth=2 展开一层中间表，以此类推——避免
          "节点很多时一团乱麻"，同时保留血缘语义（指标是下游汇聚点）。
        """
        metric_nodes, allowed = await self._graph_metric_nodes(domain, pii_only)
        lineage_tables = await self._graph_lineage_table_names()
        catalog_nodes, catalog_domain = await self._graph_catalog_nodes(pii_only, lineage_tables)
        allowed.update(catalog_domain)
        nodes: list[dict[str, Any]] = metric_nodes + catalog_nodes

        if not allowed:
            # 无展示节点则无有效边
            return nodes, []

        edge_stmt = select(
            LineageEdge.source_node,
            LineageEdge.target_node,
            LineageEdge.edge_type,
        ).where(
            LineageEdge.deleted_at.is_(None),
            or_(
                LineageEdge.source_node.in_(allowed),
                LineageEdge.target_node.in_(allowed),
            ),
        )
        edge_rows = (await self._session.execute(edge_stmt.limit(1000))).all()

        # 血缘边引用的字段节点（数量受控）：域继承对端表/视图
        field_seen: set[str] = set()
        field_nodes: list[dict[str, Any]] = []
        for row in edge_rows:
            for node_id in (row.source_node, row.target_node):
                if not node_id.startswith("field:") or node_id in field_seen:
                    continue
                if pii_only:
                    # 字段级 PII 无法从血缘边判定，PII 视图不展示字段节点
                    continue
                field_seen.add(node_id)
                other = row.target_node if node_id == row.source_node else row.source_node
                field_nodes.append(
                    {
                        "id": node_id,
                        "type": "field",
                        "label": node_id.split(":", 1)[1],
                        "pii": False,
                        "domain": catalog_domain.get(other),
                        "owner": None,
                    }
                )

        edges = [
            {
                "source": str(row.source_node),
                "target": str(row.target_node),
                "type": str(row.edge_type),
            }
            for row in edge_rows
        ]
        if depth is not None and depth > 0:
            nodes, edges = _prune_graph_by_depth(
                nodes + field_nodes, edges, depth, metric_nodes
            )
            return nodes, edges
        return nodes + field_nodes, edges

    async def heatmap_matrix(self, asset_type: str = "catalog") -> dict[str, Any]:
        """二维热力矩阵：业务域 × 敏感级别的资产分布。

        Args:
            asset_type: 资产视角。``catalog``=目录资产（db_catalog 表/视图/字段，
                域从 ``data_source.domain`` 继承）；``metric``=指标资产
                （metric.pii_flag → PII / 内部 两列）。

        ``columns`` 固定为完整敏感级枚举（catalog）或 PII/内部（metric），
        保证前端坐标轴稳定（空矩阵也返回全轴）。
        """
        if asset_type == "metric":
            rows = (
                await self._session.execute(
                    select(
                        Metric.domain,
                        Metric.pii_flag,
                        func.count().label("total"),
                    )
                    .where(Metric.deleted_at.is_(None))
                    .group_by(Metric.domain, Metric.pii_flag)
                )
            ).all()
            cells = [
                {
                    "domain": r[0],
                    "sensitivity": "PII" if r[1] else "INTERNAL",
                    "count": r[2],
                    "pii_count": r[2] if r[1] else 0,
                }
                for r in rows
            ]
            return {"cells": cells, "columns": ["INTERNAL", "PII"]}
        catalog_rows = (
            await self._session.execute(
                select(
                    DataSource.domain,
                    DBCatalog.sensitivity_level,
                    func.count().label("total"),
                )
                .join(DataSource, DataSource.source_id == DBCatalog.source_id)
                .where(DBCatalog.deleted_at.is_(None))
                .group_by(DataSource.domain, DBCatalog.sensitivity_level)
            )
        ).all()
        cells = [
            {
                "domain": r[0],
                "sensitivity": r[1],
                "count": r[2],
                "pii_count": r[2] if (r[1] and "PII" in r[1]) else 0,
            }
            for r in catalog_rows
        ]
        return {
            "cells": cells,
            "columns": [e.value for e in SensitivityLevelEnum],
        }

    async def heatmap_aggregation(self, dimension: str) -> dict[str, Any]:
        """按维度聚合返回热力桶数据。

        Args:
            dimension: 聚合维度 domain / sensitivity / owner / dw_layer。
        """
        if dimension == "sensitivity":
            rows = (
                await self._session.execute(
                    select(DBCatalog.sensitivity_level, func.count())
                    .where(DBCatalog.deleted_at.is_(None))
                    .group_by(DBCatalog.sensitivity_level)
                )
            ).all()
            buckets = [{"key": r[0], "count": r[1]} for r in rows]
        elif dimension == "owner":
            rows = (
                await self._session.execute(
                    select(
                        Metric.owner_id,
                        func.count().label("total"),
                        func.sum(case((Metric.pii_flag.is_(True), 1), else_=0)).label("pii_count"),
                    )
                    .where(Metric.deleted_at.is_(None))
                    .group_by(Metric.owner_id)
                )
            ).all()
            buckets = [{"key": str(r[0]), "total": r[1], "pii_count": int(r[2] or 0)} for r in rows]
        elif dimension == "dw_layer":
            rows = (
                await self._session.execute(
                    select(Metric.dw_layer, func.count())
                    .where(Metric.deleted_at.is_(None))
                    .group_by(Metric.dw_layer)
                )
            ).all()
            buckets = [{"key": r[0], "count": r[1]} for r in rows]
        else:
            # 默认按 domain 聚合
            rows = (
                await self._session.execute(
                    select(
                        Metric.domain,
                        func.count().label("total"),
                        func.sum(case((Metric.pii_flag.is_(True), 1), else_=0)).label("pii_count"),
                    )
                    .where(Metric.deleted_at.is_(None))
                    .group_by(Metric.domain)
                )
            ).all()
            buckets = [{"key": r[0], "total": r[1], "pii_count": int(r[2] or 0)} for r in rows]

        return {"dimension": dimension, "buckets": buckets}

    async def owner_aggregation(self, owner_id: int) -> dict[str, Any]:
        """按责任人聚合资产统计（指标多维度分布 + 目录明细 + 待办）。

        Returns:
            ``{owner_id, metrics:{total,published,draft,pii_count,by_domain,
            by_type,by_metric_tier,snapshot_covered,todo}, catalogs:{total,
            items:[...]}}``。``catalogs.items`` 为目录明细（可下钻），替代纯数字。
        """
        metric_stats = await self._owner_metric_stats(owner_id)
        by_domain = await self._owner_distribution(owner_id, Metric.domain)
        by_type = await self._owner_distribution(owner_id, Metric.type)
        by_tier = await self._owner_distribution(owner_id, Metric.metric_tier)
        todo = await self._owner_todo(owner_id)
        catalogs = await self._owner_catalog_items(owner_id)
        snapshot_covered = await self._owner_snapshot_covered(owner_id)
        # 责任人档案：姓名/角色/所属域（真实姓名优先，回退 username）
        owner_profile = await self._owner_profile(owner_id)

        return {
            "owner_id": owner_id,
            "owner_name": owner_profile[0],
            "role": owner_profile[1],
            "domain": owner_profile[2],
            "metrics": {
                "total": metric_stats.total or 0,
                "published": int(metric_stats.published or 0),
                "draft": int(metric_stats.draft or 0),
                "pii_count": int(metric_stats.pii_count or 0),
                "by_domain": dict(cast("Sequence[tuple[Any, Any]]", by_domain)),
                "by_type": dict(cast("Sequence[tuple[Any, Any]]", by_type)),
                "by_metric_tier": dict(cast("Sequence[tuple[Any, Any]]", by_tier)),
                "snapshot_covered": snapshot_covered,
                "todo": todo,
            },
            "catalogs": {"total": len(catalogs), "items": catalogs},
        }

    async def _owner_profile(self, owner_id: int) -> tuple[str | None, str | None, str | None]:
        """责任人档案：``(display_name|username, role, domain)``；
        用户不存在返回 ``(None, None, None)``。"""
        row = (
            await self._session.execute(
                select(User.display_name, User.username, User.role, User.domain).where(
                    User.id == owner_id
                )
            )
        ).first()
        if row is None:
            return None, None, None
        name = row[0] or row[1] or None
        return name, row[2], row[3]

    async def _owner_metric_stats(self, owner_id: int) -> Any:
        """责任人指标核心统计（总量/发布/草稿/PII）。"""
        return (
            await self._session.execute(
                select(
                    func.count().label("total"),
                    func.sum(case((Metric.status == "PUBLISHED", 1), else_=0)).label("published"),
                    func.sum(case((Metric.status == "DRAFT", 1), else_=0)).label("draft"),
                    func.sum(case((Metric.pii_flag.is_(True), 1), else_=0)).label("pii_count"),
                ).where(Metric.owner_id == owner_id, Metric.deleted_at.is_(None))
            )
        ).one()

    async def _owner_distribution(self, owner_id: int, column: Any) -> list[Any]:
        """责任人指标按列分布（域/类型/分级）。"""
        rows = (
            await self._session.execute(
                select(column, func.count())
                .where(Metric.owner_id == owner_id, Metric.deleted_at.is_(None))
                .group_by(column)
            )
        ).all()
        return list(rows)

    async def _owner_todo(self, owner_id: int) -> dict[str, Any]:
        """责任人待办：PII 未复核、废弃未替换、无快照指标数。"""
        unreviewed = (
            await self._session.execute(
                select(func.count())
                .select_from(Metric)
                .where(
                    Metric.owner_id == owner_id,
                    Metric.deleted_at.is_(None),
                    Metric.pii_flag.is_(True),
                    Metric.compliance_reviewed.is_(False),
                )
            )
        ).scalar() or 0
        deprecated_orphan = (
            await self._session.execute(
                select(func.count())
                .select_from(Metric)
                .where(
                    Metric.owner_id == owner_id,
                    Metric.deleted_at.is_(None),
                    Metric.status == "DEPRECATED",
                    Metric.successor_code.is_(None),
                )
            )
        ).scalar() or 0
        return {
            "pii_unreviewed": int(unreviewed),
            "deprecated_without_successor": int(deprecated_orphan),
        }

    async def _owner_catalog_items(self, owner_id: int) -> list[dict[str, Any]]:
        """责任人目录明细（entity_name/类型/敏感度/源/更新时间，可下钻）。"""
        rows = (
            await self._session.execute(
                select(
                    DBCatalog.id,
                    DBCatalog.entity_name,
                    DBCatalog.entity_type,
                    DBCatalog.sensitivity_level,
                    DBCatalog.source_id,
                    DBCatalog.updated_at,
                )
                .where(DBCatalog.owner_id == owner_id, DBCatalog.deleted_at.is_(None))
                .limit(100)
            )
        ).all()
        items = [
            {
                "id": r.id,
                "entity_name": r.entity_name,
                "entity_type": r.entity_type,
                "sensitivity_level": r.sensitivity_level,
                "source_id": r.source_id,
                "owner_id": owner_id,
                "updated_at": r.updated_at,
            }
            for r in rows
        ]
        return await self.enrich_catalog_items(items)

    async def _owner_snapshot_covered(self, owner_id: int) -> int:
        """责任人指标中有快照的数量（覆盖度分子）。"""
        codes = set(
            (
                await self._session.execute(
                    select(Metric.metric_code).where(
                        Metric.owner_id == owner_id, Metric.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        if not codes:
            return 0
        covered = set(
            (
                await self._session.execute(
                    select(MetricValueSnapshot.metric_code)
                    .where(MetricValueSnapshot.metric_code.in_(codes))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        return len(covered)

    # ----------------------------------------------------------------
    # 产品补充（FR-18 生产化）：全局搜索 / 健康 / PII / 变更 / 我的资产
    # ----------------------------------------------------------------

    @staticmethod
    def _escape_like(text: str) -> str:
        """转义 LIKE 通配符，防止用户输入 `%`/`_` 做全表模糊放大。

        修复前：转义为 \\% 但调用方 like() 无 escape 参数不生成 ESCAPE 子句，
        MySQL 默认把 \\ 当普通字符、%/_ 仍当通配符 → 转义实际失效。
        现用 / 作转义符（转义 //、/% 和 /_），配合 like(..., escape="/")。
        """
        return text.replace("/", "//").replace("%", "/%").replace("_", "/_")

    async def search_assets(
        self, q: str, entity_type: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """全局资产搜索：表/字段/指标三级，返回完整信息（源/责任人/口径/描述）。

        Returns:
            统一 ``{type, id, name, entity_type, sensitivity_level, domain, owner_id,
            owner_name, source_id, source_name, description, updated_at, status}``
            结构；metric 额外带 type/granularity/unit/aggregation/freshness/
            metric_tier/dw_layer。LIKE 通配符已转义（防模糊放大）。
        """
        if not q.strip():
            return []
        needle = f"%{self._escape_like(q.strip())}%"
        results: list[dict[str, Any]] = []
        want_table = entity_type is None or entity_type in ("table", "view")
        want_field = entity_type is None or entity_type == "field"
        want_metric = entity_type is None or entity_type == "metric"

        # 表级（entity_name 模糊）
        if want_table:
            results.extend(await self._search_catalog_tables(needle, entity_type, limit))
        # 字段级（schema_json 字段名模糊）
        if want_field:
            results.extend(await self._search_fields(q, limit))
        # 指标级（metric_code / name 模糊）
        if want_metric:
            results.extend(await self._search_metrics(needle, limit))
        return results

    async def _search_catalog_tables(
        self, needle: str, entity_type: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """表/视图级搜索结果（含源/责任人/描述/字段数富集）。"""
        stmt = select(DBCatalog).where(
            DBCatalog.deleted_at.is_(None), DBCatalog.entity_name.like(needle, escape="/")
        )
        if entity_type:
            stmt = stmt.where(DBCatalog.entity_type == entity_type)
        rows = (await self._session.execute(stmt.limit(limit))).scalars().all()
        items: list[dict[str, Any]] = []
        for r in rows:
            schema = r.schema_json if isinstance(r.schema_json, dict) else {}
            fields = schema.get("fields") or schema.get("columns") or []
            items.append(
                {
                    "type": "catalog",
                    "id": r.id,
                    "name": r.entity_name,
                    "entity_type": r.entity_type,
                    "sensitivity_level": r.sensitivity_level,
                    "domain": None,
                    "owner_id": r.owner_id,
                    "source_id": r.source_id,
                    "description": r.description,
                    "column_count": len(fields) if isinstance(fields, list) else None,
                    "updated_at": r.updated_at,
                    "status": None,
                }
            )
        return await self.enrich_catalog_items(items)

    async def _search_fields(self, q: str, limit: int) -> list[dict[str, Any]]:
        """字段级搜索结果：扫 schema_json 字段名，返回 ``{table}.{field}`` 项。

        字段名匹配用原始关键词（不转义 LIKE 通配符），因为这里走内存包含判断
        而非 SQL LIKE——``_escape_like`` 会把 `_` 转成 `\\_` 导致匹配失败。
        """
        q_lower = q.strip().lower()
        if not q_lower:
            return []
        rows = (
            await self._session.execute(
                select(DBCatalog).where(DBCatalog.deleted_at.is_(None)).limit(1000)
            )
        ).scalars().all()
        results: list[dict[str, Any]] = []
        for r in rows:
            schema = r.schema_json if isinstance(r.schema_json, dict) else {}
            fields = schema.get("fields") or schema.get("columns") or []
            if not isinstance(fields, list):
                continue
            for f in fields:
                if not isinstance(f, dict):
                    continue
                col = str(f.get("name") or f.get("column") or "")
                if col and q_lower in col.lower():
                    results.append(
                        {
                            "type": "field",
                            "id": r.id,
                            "name": f"{r.entity_name}.{col}",
                            "entity_type": "field",
                            "sensitivity_level": r.sensitivity_level,
                            "domain": None,
                            "owner_id": r.owner_id,
                            "source_id": r.source_id,
                            "description": f.get("comment"),
                            "column_count": None,
                            "updated_at": r.updated_at,
                            "status": None,
                        }
                    )
                    if len(results) >= limit:
                        return await self.enrich_catalog_items(results)
        return await self.enrich_catalog_items(results)

    async def _search_metrics(self, needle: str, limit: int) -> list[dict[str, Any]]:
        """指标级搜索结果（含治理一等字段：类型/粒度/单位/聚合/新鲜度/分级/分层）。"""
        stmt = select(Metric).where(
            Metric.deleted_at.is_(None),
            or_(Metric.metric_code.like(needle, escape="/"), Metric.name.like(needle, escape="/")),
        )
        rows = (await self._session.execute(stmt.limit(limit))).scalars().all()
        owner_ids = {m.owner_id for m in rows if m.owner_id is not None}
        usr_map: dict[int, str] = {}
        if owner_ids:
            usr_rows = (
                await self._session.execute(
                    select(User.id, User.display_name, User.username).where(
                        User.id.in_(owner_ids)
                    )
                )
            ).all()
            usr_map = {r[0]: (r[1] or r[2]) for r in usr_rows}
        return [
            {
                "type": "metric",
                "id": m.id,
                "name": m.metric_code,
                "entity_type": "metric",
                "sensitivity_level": "PII" if m.pii_flag else "INTERNAL",
                "domain": m.domain,
                "owner_id": m.owner_id,
                "owner_name": usr_map.get(m.owner_id),
                "status": m.status,
                "metric_type": m.type,
                "granularity": m.granularity,
                "unit": m.unit,
                "aggregation": m.aggregation,
                "time_semantics": m.time_semantics,
                "freshness": m.freshness,
                "dw_layer": m.dw_layer,
                "metric_tier": m.metric_tier,
                "additivity": m.additivity,
                "serving_mode": m.serving_mode,
                "description": m.description,
                "updated_at": m.updated_at,
            }
            for m in rows
        ]

    async def health_summary(self) -> dict[str, Any]:
        """资产健康视图：9 项体检 + 健康评分。

        Returns:
            ``{score, level, checks, unhealthy_sources, schema_incomplete,
            orphan_assets, stale_assets, stale_days}``。
            ``checks`` 为逐项体检明细（name/count/deduct/details），前端据此渲染
            健康报告与下钻。评分规则见 ``_health_level``。
        """
        checks: list[dict[str, Any]] = []
        score = 100

        # 体检 1：不健康数据源
        unhealthy = await self._health_unhealthy_sources()
        score -= min(len(unhealthy) * 5, 15)
        checks.append({"key": "unhealthy_sources", "count": len(unhealthy), "deduct": 0})

        # 体检 2：schema 不完整目录
        incomplete = await self._health_schema_incomplete()
        score -= min(len(incomplete) * 2, 10)
        checks.append({"key": "schema_incomplete", "count": len(incomplete), "deduct": 0})

        # 体检 3：孤儿资产
        orphan_count = (
            await self._session.execute(
                select(func.count())
                .select_from(DBCatalog)
                .where(DBCatalog.owner_id.is_(None), DBCatalog.deleted_at.is_(None))
            )
        ).scalar() or 0
        score -= min(int(orphan_count) // 10, 10)
        checks.append({"key": "orphan_assets", "count": int(orphan_count), "deduct": 0})

        # 体检 4：陈旧资产（7 天未更新）
        stale_days = 7
        stale = await self._health_stale_assets(stale_days)
        score -= min(len(stale), 10)
        checks.append({"key": "stale_assets", "count": len(stale), "deduct": 0})

        # 体检 5/6：表描述缺失 / 字段描述缺失
        desc_missing, field_missing, field_total = await self._health_descriptions()
        score -= min(desc_missing // 10, 10)
        checks.append({"key": "tables_missing_desc", "count": desc_missing, "deduct": 0})
        score -= min(field_missing // 100, 10)
        checks.append(
            {
                "key": "fields_missing_desc",
                "count": field_missing,
                "field_total": field_total,
                "deduct": 0,
            }
        )

        # 体检 7/8/9：PII 未复核 / 无快照 / 废弃未替换
        pii_unreviewed, no_snapshot, deprecated_orphan = await self._health_metric_checks()
        score -= min(len(pii_unreviewed) * 5, 15)
        checks.append({"key": "pii_unreviewed", "count": len(pii_unreviewed), "deduct": 0})
        score -= min(len(no_snapshot) * 2, 10)
        checks.append(
            {"key": "metrics_without_snapshot", "count": len(no_snapshot), "deduct": 0}
        )
        score -= min(len(deprecated_orphan) * 3, 10)
        checks.append(
            {
                "key": "deprecated_without_successor",
                "count": len(deprecated_orphan),
                "deduct": 0,
            }
        )

        return {
            "score": max(score, 0),
            "level": self._health_level(score),
            "checks": checks,
            "unhealthy_sources": unhealthy,
            "schema_incomplete": incomplete,
            "orphan_assets": int(orphan_count),
            "stale_assets": stale,
            "stale_days": stale_days,
            "pii_unreviewed": pii_unreviewed,
            "metrics_without_snapshot": no_snapshot,
            "deprecated_without_successor": deprecated_orphan,
        }

    @staticmethod
    def _health_level(score: int) -> str:
        """健康评分分档：>=90 优 / >=75 良 / >=60 中 / <60 差。"""
        if score >= 90:
            return "excellent"
        if score >= 75:
            return "good"
        if score >= 60:
            return "fair"
        return "poor"

    async def _health_unhealthy_sources(self) -> list[dict[str, Any]]:
        """体检 1：健康状态为 unhealthy 的数据源列表。"""
        rows = (
            await self._session.execute(
                select(DataSource.source_id, DataSource.name, DataSource.health_status).where(
                    DataSource.health_status == "unhealthy", DataSource.deleted_at.is_(None)
                )
            )
        ).all()
        return [
            {"source_id": r.source_id, "name": r.name, "health_status": r.health_status}
            for r in rows
        ]

    async def _health_schema_incomplete(self) -> list[dict[str, Any]]:
        """体检 2：schema 不完整（缺列元数据）的目录列表。"""
        rows = (
            await self._session.execute(
                select(DBCatalog.id, DBCatalog.entity_name, DBCatalog.source_id)
                .where(
                    DBCatalog.deleted_at.is_(None),
                    DBCatalog.schema_incomplete.is_(True),
                )
                .limit(100)
            )
        ).all()
        return [
            {"id": r.id, "entity_name": r.entity_name, "source_id": r.source_id}
            for r in rows
        ]

    async def _health_stale_assets(self, days: int) -> list[dict[str, Any]]:
        """体检 4：N 天未更新的陈旧目录资产（数据源采集停滞信号）。"""
        stale_cutoff = datetime.now(UTC) - timedelta(days=days)
        rows = (
            await self._session.execute(
                select(DBCatalog.id, DBCatalog.entity_name, DBCatalog.updated_at)
                .where(
                    DBCatalog.deleted_at.is_(None),
                    DBCatalog.updated_at < stale_cutoff,
                )
                .limit(100)
            )
        ).all()
        return [
            {"id": r.id, "entity_name": r.entity_name, "updated_at": r.updated_at}
            for r in rows
        ]

    async def _health_descriptions(self) -> tuple[int, int, int]:
        """体检 5/6：表描述缺失数 / 字段描述缺失数 / 字段总数。

        表级：``db_catalog.description`` 为空；字段级：schema_json 字段总数减去
        column_descriptions 已覆盖数（一次全表扫描，30s 缓存兜底性能）。
        """
        rows = (
            await self._session.execute(
                select(DBCatalog.description, DBCatalog.schema_json).where(
                    DBCatalog.deleted_at.is_(None)
                )
            )
        ).all()
        tables_missing = sum(1 for r in rows if not r.description)
        field_total = 0
        for r in rows:
            schema = r.schema_json if isinstance(r.schema_json, dict) else {}
            fields = schema.get("fields") or schema.get("columns") or []
            if isinstance(fields, list):
                field_total += len(fields)
        covered = (
            await self._session.execute(
                select(func.count()).select_from(ColumnDescription).where(
                    ColumnDescription.deleted_at.is_(None)
                )
            )
        ).scalar() or 0
        return tables_missing, max(field_total - int(covered), 0), int(field_total)

    async def _health_metric_checks(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """体检 7/8/9：PII 未复核 / 无快照 / 废弃未替换指标列表。

        无快照判断：以存在任何快照记录的指标码集合为基准，未命中的视为无快照。
        """
        pii_unreviewed_rows = (
            await self._session.execute(
                select(Metric.metric_code, Metric.name, Metric.owner_id)
                .where(
                    Metric.deleted_at.is_(None),
                    Metric.pii_flag.is_(True),
                    Metric.compliance_reviewed.is_(False),
                )
                .limit(100)
            )
        ).all()
        pii_unreviewed = [
            {"metric_code": r.metric_code, "name": r.name, "owner_id": r.owner_id}
            for r in pii_unreviewed_rows
        ]

        snapshot_codes = set(
            (
                await self._session.execute(
                    select(MetricValueSnapshot.metric_code).distinct()
                )
            )
            .scalars()
            .all()
        )
        no_snapshot_rows = (
            await self._session.execute(
                select(Metric.metric_code, Metric.name)
                .where(Metric.deleted_at.is_(None))
                .limit(500)
            )
        ).all()
        no_snapshot = [
            {"metric_code": r.metric_code, "name": r.name}
            for r in no_snapshot_rows
            if r.metric_code not in snapshot_codes
        ]

        deprecated_rows = (
            await self._session.execute(
                select(Metric.metric_code, Metric.name)
                .where(
                    Metric.deleted_at.is_(None),
                    Metric.status == "DEPRECATED",
                    Metric.successor_code.is_(None),
                )
                .limit(100)
            )
        ).all()
        deprecated_orphan = [
            {"metric_code": r.metric_code, "name": r.name} for r in deprecated_rows
        ]
        return pii_unreviewed, no_snapshot, deprecated_orphan


    async def pii_overview(self) -> dict[str, Any]:
        """PII 合规资产视图：按敏感级/域聚合 PII 资产。

        Returns:
            ``{by_sensitivity, by_domain, pii_metric_count, pii_catalog_count}``
        """
        # 目录 PII 分布（敏感级含 PII）
        sens_rows = (
            await self._session.execute(
                select(DBCatalog.sensitivity_level, func.count())
                .where(DBCatalog.deleted_at.is_(None), DBCatalog.sensitivity_level.like("%PII%"))
                .group_by(DBCatalog.sensitivity_level)
            )
        ).all()
        pii_catalog_count = sum(int(r[1] or 0) for r in sens_rows)

        # 指标 PII 按域分布
        domain_rows = (
            await self._session.execute(
                select(Metric.domain, func.count())
                .where(Metric.deleted_at.is_(None), Metric.pii_flag.is_(True))
                .group_by(Metric.domain)
            )
        ).all()
        pii_metric_count = sum(int(r[1] or 0) for r in domain_rows)

        return {
            "by_sensitivity": dict(cast("Sequence[tuple[Any, Any]]", sens_rows)),
            "by_domain": dict(cast("Sequence[tuple[Any, Any]]", domain_rows)),
            "pii_metric_count": int(pii_metric_count),
            "pii_catalog_count": int(pii_catalog_count),
        }

    async def recent_changes(self, days: int, limit: int) -> dict[str, Any]:
        """变更追踪流：最近 N 天新增/变更的目录与指标。

        富化：目录带 created_at 推断 ``change_type``（created/updated）+ 源/责任人名；
        指标带版本号/描述/状态推断变更类型；接入 ``schema_drift_log`` 变更内容
        （列增删/类型变更 diff）。

        Returns:
            ``{catalogs, metrics, drift, days}``
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)
        catalogs = await self._recent_catalog_changes(cutoff, limit)
        metrics = await self._recent_metric_changes(cutoff, limit)
        drift = await self._recent_drift(cutoff, limit)
        return {"catalogs": catalogs, "metrics": metrics, "drift": drift, "days": days}

    async def _recent_catalog_changes(
        self, cutoff: datetime, limit: int
    ) -> list[dict[str, Any]]:
        """最近变更的目录资产（created/updated 由 created_at vs updated_at 推断）。"""
        rows = (
            await self._session.execute(
                select(
                    DBCatalog.id,
                    DBCatalog.entity_name,
                    DBCatalog.entity_type,
                    DBCatalog.sensitivity_level,
                    DBCatalog.owner_id,
                    DBCatalog.source_id,
                    DBCatalog.created_at,
                    DBCatalog.updated_at,
                )
                .where(
                    DBCatalog.deleted_at.is_(None),
                    DBCatalog.updated_at >= cutoff,
                )
                .order_by(DBCatalog.updated_at.desc())
                .limit(limit)
            )
        ).all()
        items = [
            {
                "id": r.id,
                "entity_name": r.entity_name,
                "entity_type": r.entity_type,
                "sensitivity_level": r.sensitivity_level,
                "owner_id": r.owner_id,
                "source_id": r.source_id,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
                # 变更类型：创建时间接近更新时间（3s 内）视为新增，否则为更新
                "change_type": (
                    "created"
                    if r.created_at
                    and r.updated_at
                    and abs((r.updated_at - r.created_at).total_seconds()) < 3
                    else "updated"
                ),
            }
            for r in rows
        ]
        return await self.enrich_catalog_items(items)

    async def _recent_metric_changes(
        self, cutoff: datetime, limit: int
    ) -> list[dict[str, Any]]:
        """最近变更的指标（change_type 由状态机推断：废弃/新增/更新）。"""
        rows = (
            await self._session.execute(
                select(
                    Metric.metric_code,
                    Metric.name,
                    Metric.status,
                    Metric.domain,
                    Metric.pii_flag,
                    Metric.version,
                    Metric.description,
                    Metric.owner_id,
                    Metric.updated_at,
                )
                .where(Metric.deleted_at.is_(None), Metric.updated_at >= cutoff)
                .order_by(Metric.updated_at.desc())
                .limit(limit)
            )
        ).all()
        owner_ids = {r.owner_id for r in rows if r.owner_id is not None}
        usr_map: dict[int, str] = {}
        if owner_ids:
            usr_rows = (
                await self._session.execute(
                    select(User.id, User.display_name, User.username).where(
                        User.id.in_(owner_ids)
                    )
                )
            ).all()
            usr_map = {r[0]: (r[1] or r[2]) for r in usr_rows}
        return [
            {
                "metric_code": r.metric_code,
                "name": r.name,
                "status": r.status,
                "domain": r.domain,
                "pii_flag": bool(r.pii_flag),
                "version": r.version,
                "description": r.description,
                "owner_id": r.owner_id,
                "owner_name": usr_map.get(r.owner_id),
                "change_type": (
                    "deprecated"
                    if r.status == "DEPRECATED"
                    else "created"
                    if r.version == 1
                    else "updated"
                ),
                "updated_at": r.updated_at,
            }
            for r in rows
        ]

    async def _recent_drift(self, cutoff: datetime, limit: int) -> list[dict[str, Any]]:
        """最近 schema 漂移记录（列增删/类型变更 diff，TD §12.1 变更审计）。"""
        rows = (
            await self._session.execute(
                select(
                    SchemaDriftLog.id,
                    SchemaDriftLog.source_id,
                    SchemaDriftLog.entity_name,
                    SchemaDriftLog.change_type,
                    SchemaDriftLog.diff_json,
                    SchemaDriftLog.created_at,
                )
                .where(SchemaDriftLog.created_at >= cutoff)
                .order_by(SchemaDriftLog.created_at.desc())
                .limit(limit)
            )
        ).all()
        return [
            {
                "id": r.id,
                "source_id": r.source_id,
                "entity_name": r.entity_name,
                "change_type": r.change_type,
                "diff_json": r.diff_json,
                "created_at": r.created_at,
            }
            for r in rows
        ]

    async def my_assets(self, owner_id: int, limit: int) -> dict[str, Any]:
        """我的资产：当前用户负责的目录与指标（个人工作台视角）。

        Returns:
            ``{owner_id, catalogs, metrics, summary, claimable_orphans}``。
            ``summary`` 含目录/指标/草稿/PII/快照覆盖统计；``claimable_orphans``
            为全局待认领孤儿数（无主资产归属引导）。
        """
        catalogs = await self._my_catalog_items(owner_id, limit)
        metrics = await self._my_metric_items(owner_id, limit)

        draft_count = sum(1 for m in metrics if m["status"] == "DRAFT")
        pii_count = sum(1 for m in metrics if m["pii_flag"])
        snapshot_count = await self._my_snapshot_count(metrics)
        claimable = (
            await self._session.execute(
                select(func.count())
                .select_from(DBCatalog)
                .where(DBCatalog.owner_id.is_(None), DBCatalog.deleted_at.is_(None))
            )
        ).scalar() or 0

        return {
            "owner_id": owner_id,
            "catalogs": catalogs,
            "metrics": metrics,
            "summary": {
                "catalog_count": len(catalogs),
                "metric_count": len(metrics),
                "draft_count": draft_count,
                "pii_count": pii_count,
                "snapshot_covered": snapshot_count,
                "snapshot_total": len(metrics),
            },
            "claimable_orphans": int(claimable),
        }

    async def _my_catalog_items(
        self, owner_id: int, limit: int
    ) -> list[dict[str, Any]]:
        """我的目录资产（含描述/字段数/更新时间 + 源/责任人名）。"""
        rows = (
            await self._session.execute(
                select(
                    DBCatalog.id,
                    DBCatalog.entity_name,
                    DBCatalog.entity_type,
                    DBCatalog.sensitivity_level,
                    DBCatalog.source_id,
                    DBCatalog.owner_id,
                    DBCatalog.description,
                    DBCatalog.schema_json,
                    DBCatalog.updated_at,
                )
                .where(DBCatalog.deleted_at.is_(None), DBCatalog.owner_id == owner_id)
                .limit(limit)
            )
        ).all()
        items = []
        for r in rows:
            schema = r.schema_json if isinstance(r.schema_json, dict) else {}
            fields = schema.get("fields") or schema.get("columns") or []
            items.append(
                {
                    "id": r.id,
                    "entity_name": r.entity_name,
                    "entity_type": r.entity_type,
                    "sensitivity_level": r.sensitivity_level,
                    "source_id": r.source_id,
                    "owner_id": r.owner_id,
                    "description": r.description,
                    "column_count": len(fields) if isinstance(fields, list) else None,
                    "updated_at": r.updated_at,
                }
            )
        return await self.enrich_catalog_items(items)

    async def _my_metric_items(
        self, owner_id: int, limit: int
    ) -> list[dict[str, Any]]:
        """我的指标资产（含治理一等字段 + 描述 + 快照覆盖标记）。"""
        rows = (
            await self._session.execute(
                select(
                    Metric.metric_code,
                    Metric.name,
                    Metric.status,
                    Metric.domain,
                    Metric.pii_flag,
                    Metric.type,
                    Metric.granularity,
                    Metric.unit,
                    Metric.metric_tier,
                    Metric.description,
                    Metric.updated_at,
                )
                .where(Metric.deleted_at.is_(None), Metric.owner_id == owner_id)
                .limit(limit)
            )
        ).all()
        return [
            {
                "metric_code": r.metric_code,
                "name": r.name,
                "status": r.status,
                "domain": r.domain,
                "pii_flag": bool(r.pii_flag),
                "type": r.type,
                "granularity": r.granularity,
                "unit": r.unit,
                "metric_tier": r.metric_tier,
                "description": r.description,
                "updated_at": r.updated_at,
            }
            for r in rows
        ]

    async def _my_snapshot_count(self, metrics: list[dict[str, Any]]) -> int:
        """我的指标中有快照记录的数量（快照覆盖度）。"""
        codes = [m["metric_code"] for m in metrics]
        if not codes:
            return 0
        covered = set(
            (
                await self._session.execute(
                    select(MetricValueSnapshot.metric_code)
                    .where(MetricValueSnapshot.metric_code.in_(codes))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        return len(covered)

    # ----------------------------------------------------------------
    # 写能力（FR-18 资产工作台）：认领/转让归属、敏感级重分类、批量操作
    # 全部写操作仅由 platform_admin/domain_admin 触发（API 层 RBAC），
    # 且落审计（API 层 write_audit），此处只做数据变更与 flush。
    # ----------------------------------------------------------------

    async def get_catalog_entity(self, entity_id: int) -> DBCatalog | None:
        """按 id 获取未删除的目录资产。"""
        return (
            await self._session.execute(
                select(DBCatalog).where(DBCatalog.id == entity_id, DBCatalog.deleted_at.is_(None))
            )
        ).scalar_one_or_none()

    async def catalog_id_by_names(self, names: list[str]) -> dict[str, int]:
        """按 entity_name 批量查未删除的表/视图主键（图谱表节点 entity_id 富集用）。

        返回 ``{entity_name: id}``；不在目录中（未采集/已删除）的名称不出现在结果里。
        """
        if not names:
            return {}
        rows = (
            await self._session.execute(
                select(DBCatalog.id, DBCatalog.entity_name).where(
                    DBCatalog.entity_name.in_(names),
                    DBCatalog.deleted_at.is_(None),
                    DBCatalog.entity_type.in_(["TABLE", "VIEW"]),
                )
            )
        ).all()
        return {row.entity_name: row.id for row in rows}

    async def list_catalog_entities(self, entity_ids: list[int]) -> list[DBCatalog]:
        """按 id 批量获取未删除的目录资产（保持入参顺序，供批量操作）。"""
        if not entity_ids:
            return []
        rows = (
            (
                await self._session.execute(
                    select(DBCatalog).where(
                        DBCatalog.id.in_(entity_ids), DBCatalog.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        order = {eid: idx for idx, eid in enumerate(entity_ids)}
        return sorted(rows, key=lambda r: order.get(r.id, len(order)))

    async def user_exists(self, user_id: int) -> bool:
        """校验用户存在且未删除（owner 指派目标）。"""
        return (
            await self._session.execute(
                select(User.id).where(User.id == user_id, User.deleted_at.is_(None))
            )
        ).first() is not None

    async def assign_owner(self, entity: DBCatalog, owner_id: int | None) -> DBCatalog:
        """认领/转让归属（owner_id=None 表示解除归属回到孤儿池）。"""
        entity.owner_id = owner_id
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def reclassify_sensitivity(self, entity: DBCatalog, level: str) -> DBCatalog:
        """重分类敏感级（仅允许枚举值，校验在 service/API 层）。"""
        entity.sensitivity_level = level
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def batch_assign_owner(self, entities: Sequence[DBCatalog], owner_id: int | None) -> int:
        """批量认领/转让归属，返回受影响数量（同事务 flush，API 层统一 commit）。"""
        for e in entities:
            e.owner_id = owner_id
            self._session.add(e)
        await self._session.flush()
        return len(entities)

    async def batch_reclassify(self, entities: Sequence[DBCatalog], level: str) -> int:
        """批量重分类敏感级，返回受影响数量。"""
        for e in entities:
            e.sensitivity_level = level
            self._session.add(e)
        await self._session.flush()
        return len(entities)
