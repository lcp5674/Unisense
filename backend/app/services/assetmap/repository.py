"""资产地图 Repository（TD §12.11 / FR-18）。

只读聚合：元数据目录（db_catalog）、分类（classification）、指标（metric）。
P2 增强：图谱降级查询、热力聚合、责任人视图。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_source import DBCatalog
from app.models.governance import Classification
from app.models.lineage import LineageEdge
from app.models.metric import Metric


class AssetMapRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_tables(
        self, source_id: str | None, sensitivity: str | None, limit: int
    ) -> list[DBCatalog]:
        stmt = select(DBCatalog).where(DBCatalog.entity_type == "table")
        if source_id:
            stmt = stmt.where(DBCatalog.source_id == source_id)
        if sensitivity:
            stmt = stmt.where(DBCatalog.sensitivity_level == sensitivity)
        return list((await self._session.execute(stmt.limit(limit))).scalars().all())

    async def orphan_assets(self) -> list[DBCatalog]:
        stmt = select(DBCatalog).where(DBCatalog.owner_id.is_(None))
        return list((await self._session.execute(stmt)).scalars().all())

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

    async def get_entity_detail(self, entity_id: int) -> dict[str, Any] | None:
        """资产实体详情：元数据 + 敏感度 + PII + 血缘边数。

        Args:
            entity_id: db_catalog 主键。

        Returns:
            详情字典；实体不存在或已删除返回 ``None``。
        """
        row = (
            await self._session.execute(
                select(DBCatalog).where(
                    DBCatalog.id == entity_id, DBCatalog.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None

        # 血缘边数：实体名匹配 lineage 节点的多种编码形态
        variants = [row.entity_name, f"table:{row.entity_name}", f"field:{row.entity_name}"]
        lineage_count = (
            await self._session.execute(
                select(func.count())
                .select_from(LineageEdge)
                .where(
                    LineageEdge.deleted_at.is_(None),
                    or_(
                        LineageEdge.source_node.in_(variants),
                        LineageEdge.target_node.in_(variants),
                    ),
                )
            )
        ).scalar() or 0
        sens = (row.sensitivity_level or "").upper()
        return {
            "id": row.id,
            "entity_name": row.entity_name,
            "entity_type": row.entity_type,
            "source_id": row.source_id,
            "sensitivity_level": row.sensitivity_level,
            "owner_id": row.owner_id,
            "schema_incomplete": row.schema_incomplete,
            "content_signature": row.content_signature,
            "schema_summary": self._summarize_schema(row.schema_json),
            "lineage_count": int(lineage_count),
            "pii_flag": "PII" in sens,
            # etl_sql 属敏感字段（可能内嵌连接串），详情接口不返回
            "etl_sql": None,
        }

    async def catalog_summary(self) -> dict[str, Any]:
        total = (
            await self._session.execute(select(func.count()).select_from(DBCatalog))
        ).scalar() or 0
        by_type = (
            await self._session.execute(
                select(DBCatalog.entity_type, func.count()).group_by(DBCatalog.entity_type)
            )
        ).all()
        by_sens = (
            await self._session.execute(
                select(DBCatalog.sensitivity_level, func.count()).group_by(
                    DBCatalog.sensitivity_level
                )
            )
        ).all()
        orphans = (
            await self._session.execute(
                select(func.count()).select_from(DBCatalog).where(DBCatalog.owner_id.is_(None))
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
            await self._session.execute(select(Metric.domain, func.count()).group_by(Metric.domain))
        ).all()
        by_status = (
            await self._session.execute(select(Metric.status, func.count()).group_by(Metric.status))
        ).all()
        return {
            "by_domain": dict(cast("Sequence[tuple[Any, Any]]", by_domain)),
            "by_status": dict(cast("Sequence[tuple[Any, Any]]", by_status)),
        }

    # ----------------------------------------------------------------
    # P2 Enhancement: 图谱降级、热力聚合、责任人视图
    # ----------------------------------------------------------------

    async def graph_from_mysql(
        self, domain: str | None, pii_only: bool
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """从 MySQL lineage_edge + metric 拼接图谱数据。

        - 节点：域内/全部 metric 节点，id 统一为 ``metric:{code}``（与血缘边端点格式一致，
          避免图节点与边端点断裂）。
        - 边：仅保留至少一端属于展示节点的边（**精确集合匹配**，消除原 ``contains``
          子串误匹配，如 domain="s" 误命中所有含 "s" 的节点）。
        """
        # 节点：metric 表
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

        metric_rows = (await self._session.execute(metric_stmt)).all()
        nodes: list[dict[str, Any]] = []
        allowed: set[str] = set()
        seen_ids: set[str] = set()
        for row in metric_rows:
            node_id = f"metric:{row.metric_code}"
            if node_id in seen_ids:
                continue
            seen_ids.add(node_id)
            allowed.add(node_id)
            nodes.append({
                "id": node_id,
                "type": "metric",
                "label": row.metric_code,
                "pii": bool(row.pii_flag),
                "domain": row.domain,
                "owner": str(row.owner_id) if row.owner_id else None,
            })

        # 边：仅保留至少一端属于展示节点的边（精确匹配）
        edge_stmt = select(
            LineageEdge.source_node,
            LineageEdge.target_node,
            LineageEdge.edge_type,
        ).where(LineageEdge.deleted_at.is_(None))
        if allowed:
            edge_stmt = edge_stmt.where(
                or_(
                    LineageEdge.source_node.in_(allowed),
                    LineageEdge.target_node.in_(allowed),
                )
            )
        else:
            # 无展示节点则无有效边
            return nodes, []

        edge_rows = (await self._session.execute(edge_stmt.limit(1000))).all()
        edges: list[dict[str, Any]] = []
        for edge_row in edge_rows:
            edges.append({
                "source": str(edge_row.source_node),
                "target": str(edge_row.target_node),
                "type": str(edge_row.edge_type),
            })

        return nodes, edges

    async def heatmap_aggregation(self, dimension: str) -> dict[str, Any]:
        """按维度聚合返回热力桶数据。

        Args:
            dimension: 聚合维度 domain / sensitivity / owner / dw_layer。
        """
        if dimension == "sensitivity":
            rows = (
                await self._session.execute(
                    select(DBCatalog.sensitivity_level, func.count())
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
                    ).group_by(Metric.owner_id)
                )
            ).all()
            buckets = [{"key": str(r[0]), "total": r[1], "pii_count": int(r[2] or 0)} for r in rows]
        elif dimension == "dw_layer":
            rows = (
                await self._session.execute(
                    select(Metric.dw_layer, func.count())
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
                    ).group_by(Metric.domain)
                )
            ).all()
            buckets = [{"key": r[0], "total": r[1], "pii_count": int(r[2] or 0)} for r in rows]

        return {"dimension": dimension, "buckets": buckets}

    async def owner_aggregation(self, owner_id: int) -> dict[str, Any]:
        """按责任人聚合资产统计。"""
        # 指标统计
        metric_stats = (
            await self._session.execute(
                select(
                    func.count().label("total"),
                    func.sum(case((Metric.status == "PUBLISHED", 1), else_=0)).label("published"),
                    func.sum(case((Metric.status == "DRAFT", 1), else_=0)).label("draft"),
                    func.sum(case((Metric.pii_flag.is_(True), 1), else_=0)).label("pii_count"),
                ).where(Metric.owner_id == owner_id)
            )
        ).one()

        # 域分布
        domain_rows = (
            await self._session.execute(
                select(Metric.domain, func.count())
                .where(Metric.owner_id == owner_id)
                .group_by(Metric.domain)
            )
        ).all()

        # 目录统计
        catalog_count = (
            await self._session.execute(
                select(func.count()).select_from(DBCatalog).where(DBCatalog.owner_id == owner_id)
            )
        ).scalar() or 0

        return {
            "owner_id": owner_id,
            "metrics": {
                "total": metric_stats.total or 0,
                "published": int(metric_stats.published or 0),
                "draft": int(metric_stats.draft or 0),
                "pii_count": int(metric_stats.pii_count or 0),
                "by_domain": dict(cast("Sequence[tuple[Any, Any]]", domain_rows)),
            },
            "catalogs": {"total": catalog_count},
        }
