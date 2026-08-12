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

# Neo4j 异步 driver 单例：惰性创建并复用，避免每请求新建连接池导致泄漏（P2-1）。
_NEO4J_DRIVER: Any | None = None


def _get_neo4j_driver() -> Any:
    """惰性创建并复用 Neo4j 异步 driver。"""
    global _NEO4J_DRIVER
    if _NEO4J_DRIVER is None:
        from neo4j import AsyncGraphDatabase

        from app.core.config import settings

        _NEO4J_DRIVER = AsyncGraphDatabase.driver(
            settings.neo4j_url,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
    return _NEO4J_DRIVER


def _close_neo4j_driver() -> None:
    """关闭 Neo4j driver（进程退出/测试收尾时调用）。"""
    global _NEO4J_DRIVER
    if _NEO4J_DRIVER is not None:
        _NEO4J_DRIVER.close()
        _NEO4J_DRIVER = None


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

    async def get_entity_detail(self, entity_id: int) -> dict[str, Any] | None:
        """资产实体详情：元数据 + 敏感度 + PII + 血缘边数（TD §12.11 流程 #5）。"""
        return await self._repo.get_entity_detail(entity_id)

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
            from app.core.config import settings

            if not settings.neo4j_url or not settings.neo4j_password:
                return None

            driver = _get_neo4j_driver()
            async with driver.session() as session:
                # 构建动态 Cypher
                match_clause = "MATCH (n:Asset)"
                where_parts: list[str] = []
                if domain:
                    where_parts.append("n.domain = $domain")
                if pii_only:
                    where_parts.append("n.pii = true")
                where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

                node_query = (
                    match_clause + where_clause
                    + " RETURN n.id AS id, n.type AS type, n.label AS label,"
                    + " n.pii AS pii, n.domain AS domain, n.owner AS owner LIMIT 500"
                )

                # 边：可变长关系按 depth 限跳（Neo4j pattern 不支持参数作长度上界，
                # 故以字面量插值；depth 由 API 约束 ge=1 le=10 为安全整数）。
                # UNWIND relationships(p) 逐跳展开，返回每条实际关系边。
                edge_query = (
                    "MATCH p=(a:Asset)-[rels:DERIVED_FROM|LINEAGE_UP"
                    f"|LINEAGE_DOWN|CONSUMED_BY*1..{int(depth)}]->(b:Asset)"
                )
                edge_where: list[str] = []
                if domain:
                    edge_where.append("(a.domain = $domain OR b.domain = $domain)")
                if pii_only:
                    edge_where.append("(a.pii = true AND b.pii = true)")
                if edge_where:
                    edge_query += " WHERE " + " AND ".join(edge_where)
                edge_query += (
                    " UNWIND relationships(p) AS r"
                    " RETURN DISTINCT startNode(r).id AS source,"
                    " endNode(r).id AS target, type(r) AS type LIMIT 1000"
                )

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

    # ----------------------------------------------------------------
    # 产品补充（FR-18 生产化）：全局搜索 / 健康 / PII / 变更 / 我的资产
    # ----------------------------------------------------------------

    async def search_assets(
        self, q: str, entity_type: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """全局资产搜索：目录 + 指标统一结果。"""
        return await self._repo.search_assets(q, entity_type, limit)

    async def health_summary(self) -> dict[str, Any]:
        """资产健康视图：源健康/schema 不完整/孤儿/陈旧资产。"""
        return await self._repo.health_summary()

    async def pii_overview(self) -> dict[str, Any]:
        """PII 合规资产视图：按敏感级/域聚合 PII 资产。"""
        return await self._repo.pii_overview()

    async def recent_changes(self, days: int = 7, limit: int = 50) -> dict[str, Any]:
        """变更追踪流：最近 N 天新增/变更的目录与指标。"""
        return await self._repo.recent_changes(days, limit)

    async def my_assets(self, owner_id: int, limit: int = 50) -> dict[str, Any]:
        """我的资产：当前用户负责的目录与指标。"""
        return await self._repo.my_assets(owner_id, limit)

    async def export_tables(
        self, source_id: str | None, sensitivity: str | None
    ) -> list[dict[str, Any]]:
        """导出目录资产（表/视图）为字典列表，供 CSV 序列化。"""
        rows = await self._repo.list_tables(source_id, sensitivity, limit=5000)
        return [r.to_dict() for r in rows]
