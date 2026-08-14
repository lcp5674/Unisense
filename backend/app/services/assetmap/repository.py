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

from app.models.data_source import DataSource, DBCatalog
from app.models.enums import SensitivityLevelEnum
from app.models.governance import Classification
from app.models.lineage import LineageEdge
from app.models.metric import Metric
from app.models.user import User


class AssetMapRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_tables(
        self, source_id: str | None, sensitivity: str | None, limit: int
    ) -> list[DBCatalog]:
        stmt = select(DBCatalog).where(
            DBCatalog.entity_type == "table", DBCatalog.deleted_at.is_(None)
        )
        if source_id:
            stmt = stmt.where(DBCatalog.source_id == source_id)
        if sensitivity:
            stmt = stmt.where(DBCatalog.sensitivity_level == sensitivity)
        return list((await self._session.execute(stmt.limit(limit))).scalars().all())

    async def orphan_assets(self) -> list[DBCatalog]:
        stmt = select(DBCatalog).where(DBCatalog.owner_id.is_(None), DBCatalog.deleted_at.is_(None))
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

    async def _graph_catalog_nodes(
        self, pii_only: bool
    ) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
        """db_catalog 表/视图节点：id=``table:{entity_name}``（与血缘边格式对齐）。

        域从 ``data_source.domain`` 继承（db_catalog 无域字段）；PII 由
        ``sensitivity_level`` 含 "PII" 判定。返回节点列表 + {节点 id -> 域}。
        """
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
            .where(
                DBCatalog.deleted_at.is_(None),
                DBCatalog.entity_type.in_(["TABLE", "VIEW"]),
            )
        )
        if pii_only:
            catalog_stmt = catalog_stmt.where(DBCatalog.sensitivity_level.like("%PII%"))

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
        self, domain: str | None, pii_only: bool
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """从 MySQL lineage_edge + metric + db_catalog 拼接图谱数据。

        - 节点：metric（``metric:{code}``）+ db_catalog 表/视图（``table:{entity_name}``，
          与血缘边节点格式对齐）+ 血缘边引用到的字段（``field:{...}``，数量受控）。
        - 边：仅保留至少一端属于展示节点的边（**精确 IN 集合匹配**，消除 ``contains``
          子串误匹配）。
        - PII 视图：仅指标/表节点（字段级 PII 无法从血缘边判定，故不展示字段）。
        """
        metric_nodes, allowed = await self._graph_metric_nodes(domain, pii_only)
        catalog_nodes, catalog_domain = await self._graph_catalog_nodes(pii_only)
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
        return nodes + field_nodes, edges

    async def heatmap_matrix(self) -> dict[str, Any]:
        """二维热力矩阵：业务域 × 敏感级别的资产分布（db_catalog 表/视图/字段）。

        域从 ``data_source.domain`` 继承；``columns`` 固定为完整敏感级枚举，
        保证前端坐标轴稳定（空矩阵也返回全轴）。
        """
        rows = (
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
            for r in rows
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
        """按责任人聚合资产统计。"""
        # 指标统计
        metric_stats = (
            await self._session.execute(
                select(
                    func.count().label("total"),
                    func.sum(case((Metric.status == "PUBLISHED", 1), else_=0)).label("published"),
                    func.sum(case((Metric.status == "DRAFT", 1), else_=0)).label("draft"),
                    func.sum(case((Metric.pii_flag.is_(True), 1), else_=0)).label("pii_count"),
                ).where(Metric.owner_id == owner_id, Metric.deleted_at.is_(None))
            )
        ).one()

        # 域分布
        domain_rows = (
            await self._session.execute(
                select(Metric.domain, func.count())
                .where(Metric.owner_id == owner_id, Metric.deleted_at.is_(None))
                .group_by(Metric.domain)
            )
        ).all()

        # 目录统计
        catalog_count = (
            await self._session.execute(
                select(func.count())
                .select_from(DBCatalog)
                .where(DBCatalog.owner_id == owner_id, DBCatalog.deleted_at.is_(None))
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

    # ----------------------------------------------------------------
    # 产品补充（FR-18 生产化）：全局搜索 / 健康 / PII / 变更 / 我的资产
    # ----------------------------------------------------------------

    @staticmethod
    def _escape_like(text: str) -> str:
        """转义 LIKE 通配符，防止用户输入 `%`/`_` 做全表模糊放大。"""
        return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    async def search_assets(
        self, q: str, entity_type: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """全局资产搜索：目录（表/字段）+ 指标，按名称模糊匹配。

        统一返回 ``{type, id, name, sensitivity, domain, owner_id, status}`` 结构，
        供前端全局搜索框消费。LIKE 通配符已转义（防模糊放大）。
        """
        if not q.strip():
            return []
        needle = f"%{self._escape_like(q.strip())}%"
        results: list[dict[str, Any]] = []

        # 目录：entity_name 模糊匹配（表/字段）——仅当未限定类型或限定目录类型时
        if entity_type is None or entity_type in ("table", "field"):
            catalog_stmt = select(DBCatalog).where(
                DBCatalog.deleted_at.is_(None), DBCatalog.entity_name.like(needle)
            )
            if entity_type:
                catalog_stmt = catalog_stmt.where(DBCatalog.entity_type == entity_type)
            catalog_rows = (await self._session.execute(catalog_stmt.limit(limit))).scalars().all()
            for r in catalog_rows:
                results.append(
                    {
                        "type": "catalog",
                        "id": r.id,
                        "name": r.entity_name,
                        "entity_type": r.entity_type,
                        "sensitivity_level": r.sensitivity_level,
                        "domain": None,
                        "owner_id": r.owner_id,
                        "status": None,
                    }
                )

        # 指标：metric_code / name 模糊匹配（仅当未限定目录类型或限定 metric 时）
        if entity_type is None or entity_type == "metric":
            metric_stmt = select(Metric).where(
                Metric.deleted_at.is_(None),
                or_(Metric.metric_code.like(needle), Metric.name.like(needle)),
            )
            metric_rows = (await self._session.execute(metric_stmt.limit(limit))).scalars().all()
            for m in metric_rows:
                results.append(
                    {
                        "type": "metric",
                        "id": m.id,
                        "name": m.metric_code,
                        "entity_type": "metric",
                        "sensitivity_level": "PII" if m.pii_flag else "INTERNAL",
                        "domain": m.domain,
                        "owner_id": m.owner_id,
                        "status": m.status,
                    }
                )
        return results

    async def health_summary(self) -> dict[str, Any]:
        """资产健康视图：源健康、schema 不完整、孤儿、陈旧资产聚合。

        Returns:
            ``{unhealthy_sources, schema_incomplete, orphans, stale_assets, updated_at}``
        """
        # 不健康数据源
        unhealthy_rows = (
            await self._session.execute(
                select(DataSource.source_id, DataSource.name, DataSource.health_status).where(
                    DataSource.health_status == "unhealthy", DataSource.deleted_at.is_(None)
                )
            )
        ).all()
        unhealthy_sources = [
            {"source_id": r.source_id, "name": r.name, "health_status": r.health_status}
            for r in unhealthy_rows
        ]

        # schema 不完整目录
        incomplete_rows = (
            await self._session.execute(
                select(DBCatalog.id, DBCatalog.entity_name, DBCatalog.source_id)
                .where(
                    DBCatalog.deleted_at.is_(None),
                    DBCatalog.schema_incomplete.is_(True),
                )
                .limit(100)
            )
        ).all()
        schema_incomplete = [
            {"id": r.id, "entity_name": r.entity_name, "source_id": r.source_id}
            for r in incomplete_rows
        ]

        # 孤儿资产
        orphan_count = (
            await self._session.execute(
                select(func.count())
                .select_from(DBCatalog)
                .where(DBCatalog.owner_id.is_(None), DBCatalog.deleted_at.is_(None))
            )
        ).scalar() or 0

        # 陈旧资产：7 天未更新（数据源采集停滞的间接信号）
        stale_cutoff = datetime.now(UTC) - timedelta(days=7)
        stale_rows = (
            await self._session.execute(
                select(DBCatalog.id, DBCatalog.entity_name, DBCatalog.updated_at)
                .where(
                    DBCatalog.deleted_at.is_(None),
                    DBCatalog.updated_at < stale_cutoff,
                )
                .limit(100)
            )
        ).all()
        stale_assets = [
            {"id": r.id, "entity_name": r.entity_name, "updated_at": r.updated_at}
            for r in stale_rows
        ]

        return {
            "unhealthy_sources": unhealthy_sources,
            "schema_incomplete": schema_incomplete,
            "orphan_assets": int(orphan_count),
            "stale_assets": stale_assets,
            "stale_days": 7,
        }

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

        Returns:
            ``{catalogs, metrics, days}``
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)
        catalog_rows = (
            await self._session.execute(
                select(
                    DBCatalog.id,
                    DBCatalog.entity_name,
                    DBCatalog.entity_type,
                    DBCatalog.sensitivity_level,
                    DBCatalog.owner_id,
                    DBCatalog.source_id,
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
        catalogs = [
            {
                "id": r.id,
                "entity_name": r.entity_name,
                "entity_type": r.entity_type,
                "sensitivity_level": r.sensitivity_level,
                "owner_id": r.owner_id,
                "source_id": r.source_id,
                "updated_at": r.updated_at,
            }
            for r in catalog_rows
        ]

        metric_rows = (
            await self._session.execute(
                select(
                    Metric.metric_code,
                    Metric.name,
                    Metric.status,
                    Metric.domain,
                    Metric.pii_flag,
                    Metric.updated_at,
                )
                .where(Metric.deleted_at.is_(None), Metric.updated_at >= cutoff)
                .order_by(Metric.updated_at.desc())
                .limit(limit)
            )
        ).all()
        metrics = [
            {
                "metric_code": r.metric_code,
                "name": r.name,
                "status": r.status,
                "domain": r.domain,
                "pii_flag": bool(r.pii_flag),
                "updated_at": r.updated_at,
            }
            for r in metric_rows
        ]
        return {"catalogs": catalogs, "metrics": metrics, "days": days}

    async def my_assets(self, owner_id: int, limit: int) -> dict[str, Any]:
        """我的资产：当前用户负责的目录与指标（个人工作台视角）。"""
        catalog_rows = (
            await self._session.execute(
                select(
                    DBCatalog.id,
                    DBCatalog.entity_name,
                    DBCatalog.entity_type,
                    DBCatalog.sensitivity_level,
                    DBCatalog.source_id,
                )
                .where(DBCatalog.deleted_at.is_(None), DBCatalog.owner_id == owner_id)
                .limit(limit)
            )
        ).all()
        catalogs = [
            {
                "id": r.id,
                "entity_name": r.entity_name,
                "entity_type": r.entity_type,
                "sensitivity_level": r.sensitivity_level,
                "source_id": r.source_id,
            }
            for r in catalog_rows
        ]

        metric_rows = (
            await self._session.execute(
                select(
                    Metric.metric_code,
                    Metric.name,
                    Metric.status,
                    Metric.domain,
                    Metric.pii_flag,
                )
                .where(Metric.deleted_at.is_(None), Metric.owner_id == owner_id)
                .limit(limit)
            )
        ).all()
        metrics = [
            {
                "metric_code": r.metric_code,
                "name": r.name,
                "status": r.status,
                "domain": r.domain,
                "pii_flag": bool(r.pii_flag),
            }
            for r in metric_rows
        ]
        return {"owner_id": owner_id, "catalogs": catalogs, "metrics": metrics}

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
