"""资产地图服务（TD §12.11 / FR-18）。

P2 增强：
- get_graph: Neo4j Cypher 图谱查询返回节点+边
- get_heatmap: 聚合分桶返回敏感分布热力数据
- get_owner_view: 按责任人聚合资产统计

P3: 继承 BaseService Protocol。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.resilience import CircuitBreaker
from app.services.assetmap.repository import AssetMapRepository

logger = logging.getLogger(__name__)

# Neo4j 调用熔断器
_NEO4J_BREAKER = CircuitBreaker(failure_threshold=5, reset_timeout=30.0)


class AssetMapService(BaseService):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self._session = session
        self._repo = AssetMapRepository(session)

    async def catalog_summary(self) -> dict[str, Any]:
        return await self._repo.catalog_summary()

    async def classification_summary(self) -> dict[str, Any]:
        return await self._repo.classification_summary()

    async def metric_summary(self) -> dict[str, Any]:
        return await self._repo.metric_summary()

    async def list_tables(
        self, source_id: str | None, sensitivity: str | None, limit: int
    ) -> list[dict[str, Any]]:
        rows = await self._repo.list_tables(source_id, sensitivity, limit)
        # assetmap T-2: 经 to_dict 剔除敏感字段（connection_config 等）
        return [r.to_dict() for r in rows]

    async def orphan_assets(self) -> list[dict[str, Any]]:
        rows = await self._repo.orphan_assets()
        return [r.to_dict() for r in rows]

    # ----------------------------------------------------------------
    # P2 Enhancement: 图谱 / 热力 / 责任人视图
    # ----------------------------------------------------------------

    async def get_graph(
        self,
        domain: str | None = None,
        depth: int = 3,
        pii_only: bool = False,
    ) -> dict[str, Any]:
        """Neo4j Cypher 图谱查询，返回节点+边。

        当 Neo4j 不可用或熔断打开时降级为 MySQL 血缘边拼接。

        Args:
            domain: 按域过滤（None 表示全部）。
            depth: 图遍历深度（默认 3）。
            pii_only: 仅返回含 PII 标记的节点。
        """
        # 尝试 Neo4j
        result = await self._get_graph_neo4j(domain, depth, pii_only)
        if result is not None:
            return result

        # 降级：从 MySQL lineage_edge + metric 拼接
        return await self._get_graph_mysql(domain, pii_only)

    async def _get_graph_neo4j(
        self, domain: str | None, depth: int, pii_only: bool
    ) -> dict[str, Any] | None:
        """通过 Neo4j Cypher 查询图谱。"""
        if not _NEO4J_BREAKER.allow():
            logger.warning("assetmap_neo4j_breaker_open")
            return None

        try:
            from neo4j import AsyncGraphDatabase

            from app.core.config import settings

            if not settings.neo4j_url or not settings.neo4j_password:
                return None

            driver = AsyncGraphDatabase.driver(
                settings.neo4j_url,
                auth=(settings.neo4j_user, settings.neo4j_password),
            )
            async with driver.session() as session:
                # 构建动态 Cypher
                match_clause = "MATCH (n:Asset)"
                where_parts: list[str] = []
                if domain:
                    where_parts.append("n.domain = $domain")
                if pii_only:
                    where_parts.append("n.pii = true")
                where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

                # 简化查询：取节点 + 关系
                node_query = (
                    match_clause + where_clause
                    + " RETURN n.id AS id, n.type AS type, n.label AS label,"
                    + " n.pii AS pii, n.domain AS domain, n.owner AS owner LIMIT 500"
                )
                edge_query = (
                    "MATCH (a:Asset)-[r:DERIVED_FROM|LINEAGE_UP"
                    "|LINEAGE_DOWN|CONSUMED_BY]->(b:Asset)"
                )
                if domain:
                    edge_query += " WHERE a.domain = $domain OR b.domain = $domain"
                edge_query += " RETURN a.id AS source, b.id AS target, type(r) AS type LIMIT 1000"

                params: dict[str, Any] = {}
                if domain:
                    params["domain"] = domain

                nodes_result = await session.run(node_query, params)
                nodes = []
                async for record in nodes_result:
                    nodes.append({
                        "id": record["id"],
                        "type": record["type"] or "unknown",
                        "label": record["label"] or "",
                        "pii": bool(record["pii"]),
                        "domain": record["domain"],
                        "owner": record["owner"],
                    })

                edges_result = await session.run(edge_query, params)
                edges = []
                async for record in edges_result:
                    edges.append({
                        "source": record["source"],
                        "target": record["target"],
                        "type": record["type"],
                    })

                _NEO4J_BREAKER.record_success()
                return {"nodes": nodes, "edges": edges}
        except Exception as exc:
            _NEO4J_BREAKER.record_failure()
            logger.warning("assetmap_neo4j_query_failed: %s", exc)
            return None

    async def _get_graph_mysql(
        self, domain: str | None, pii_only: bool
    ) -> dict[str, Any]:
        """降级：从 MySQL lineage_edge + metric 拼接图谱。"""
        nodes, edges = await self._repo.graph_from_mysql(domain, pii_only)
        return {"nodes": nodes, "edges": edges}

    async def get_heatmap(self, dimension: str = "domain") -> dict[str, Any]:
        """聚合分桶返回敏感分布热力数据。

        Args:
            dimension: 聚合维度（domain / sensitivity / owner / dw_layer）。
        """
        return await self._repo.heatmap_aggregation(dimension)

    async def get_owner_view(self, owner_id: int) -> dict[str, Any]:
        """按责任人聚合资产统计。

        Returns:
            按责任人分组的指标/目录/PII 统计。
        """
        return await self._repo.owner_aggregation(owner_id)
