"""资产地图服务（TD §12.11 / FR-18）。

P2 增强：
- get_graph: Neo4j Cypher 图谱查询返回节点+边
- get_heatmap: 聚合分桶返回敏感分布热力数据
- get_owner_view: 按责任人聚合资产统计

P3: 继承 BaseService Protocol。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.exceptions import BusinessError, NotFoundError, UnisenseError
from app.core.resilience import CircuitBreaker
from app.services.assetmap.repository import AssetMapRepository

logger = logging.getLogger(__name__)

#: 同步导出最大行数（P1 加固）：PII 盘点 CSV 为同步生成，大库无界拼接会 OOM；
#: 超量时前端按筛选收敛后重导。与平台批量标准一致。
_MAX_EXPORT_ROWS = 5000

#: 行业分级模板（PII 合规增强 C：个保法敏感个人信息 / 金融行业 / 标准分级）。
#: ``sensitive_categories`` 为该模板下须升级为 PII 的字段类别集合；
#: 应用时命中这些类别的资产（且当前非 PII）升级为 PII。
PII_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "pipil-sensitive",
        "name": "个保法·敏感个人信息",
        "description": "《个保法》第28条敏感个人信息：生物识别、医疗健康、金融账户、"
        "行踪轨迹一律判 PII",
        "sensitive_categories": ["BIOMETRIC", "HEALTH", "FINANCIAL", "GPS"],
    },
    {
        "id": "financial-industry",
        "name": "金融行业分级",
        "description": "金融数据分级：银行卡、账户余额等强敏感判 PII，其余按内置规则",
        "sensitive_categories": ["BANK_CARD", "FINANCIAL"],
    },
    {
        "id": "standard",
        "name": "标准分级（内置规则）",
        "description": "沿用内置规则引擎判定，不额外升级",
        "sensitive_categories": [],
    },
)

# Neo4j 调用熔断器
_NEO4J_BREAKER = CircuitBreaker(failure_threshold=5, reset_timeout=30.0)

# 热力聚合合法维度（Repository 对未知维度静默回退到 domain，Service 层须拒绝，
# 否则会以非法键缓存 domain 数据造成结果与请求不一致）。
_HEATMAP_DIMENSIONS = {"domain", "sensitivity", "owner", "dw_layer"}

# 聚合结果缓存：catalog/metric/classification/heatmap/health/pii 均为低频变化的
# 全表聚合，加 cache-aside 短 TTL 缓存避免每次请求全表扫描（大规模优化，TD §13 perf）。
_CACHE_TTL = 30  # 秒
_CACHE_PREFIX = "assetmap:agg:"
_CACHE_BREAKER = CircuitBreaker(failure_threshold=5, reset_timeout=30.0)


async def _agg_cache_get(key: str) -> Any | None:
    """聚合结果缓存读取（best-effort：Redis 不可用/熔断打开/坏数据均回源）。"""
    if not _CACHE_BREAKER.allow():
        return None
    try:
        from app.db.redis import get_redis

        raw = await get_redis().get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - 缓存降级，不阻断主链路
        _CACHE_BREAKER.record_failure()
        logger.warning("assetmap_agg_cache_get_failed: %s", exc)
        return None


async def _agg_cache_set(key: str, value: Any) -> None:
    """聚合结果缓存写入（best-effort）。熔断打开时跳过写，防雪崩。"""
    if not _CACHE_BREAKER.allow():
        return
    try:
        from app.db.redis import get_redis

        await get_redis().set(
            key, json.dumps(value, ensure_ascii=False, default=str), ex=_CACHE_TTL
        )
        _CACHE_BREAKER.record_success()
    except Exception as exc:  # noqa: BLE001
        _CACHE_BREAKER.record_failure()
        logger.warning("assetmap_agg_cache_set_failed: %s", exc)


async def _agg_cached(name: str, loader: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """cache-aside 通用封装：读缓存命中即返，未命中回源并写缓存。"""
    key = f"{_CACHE_PREFIX}{name}"
    cached = await _agg_cache_get(key)
    if cached is not None:
        return cast("dict[str, Any]", cached)
    data = await loader()
    await _agg_cache_set(key, data)
    return data


async def _agg_cache_invalidate() -> None:
    """写操作后主动失效聚合缓存（P2-4）：避免 summary/health 在 30s TTL 内陈旧。

    用 SCAN 按前缀删除全部聚合 key，同时覆盖动态 key（如 owner_aggregation 按
    owner_id 区分）。best-effort：Redis 不可用/熔断打开时静默跳过，下次 TTL 自然过期。
    """
    if not _CACHE_BREAKER.allow():
        return
    try:
        from app.db.redis import get_redis

        redis = get_redis()
        cursor = 0
        deleted = 0
        while True:
            cursor, keys = await redis.scan(cursor, match=f"{_CACHE_PREFIX}*", count=200)
            if keys:
                deleted += await redis.delete(*keys)
            if cursor == 0:
                break
        if deleted:
            logger.info("assetmap_agg_cache_invalidated", keys=deleted)
    except Exception as exc:  # noqa: BLE001 - 缓存失效失败不阻断写主链路
        _CACHE_BREAKER.record_failure()
        logger.warning("assetmap_agg_cache_invalidate_failed: %s", exc)


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
            # 故障容错：外部调用必须设超时，防止 Neo4j 无响应时 session.run 无限挂起
            # （熔断只在抛异常时触发，悬挂连接不抛异常，故须靠连接超时兜底）。
            connection_timeout=5.0,
            connection_acquisition_timeout=5.0,
            max_connection_pool_size=10,
        )
    return _NEO4J_DRIVER


def _close_neo4j_driver() -> None:
    """关闭 Neo4j driver（进程退出/测试收尾时调用）。"""
    global _NEO4J_DRIVER
    if _NEO4J_DRIVER is not None:
        _NEO4J_DRIVER.close()
        _NEO4J_DRIVER = None


class AssetMapService(BaseService):
    def __init__(self, session: AsyncSession, org_id: int | None = None) -> None:
        super().__init__(session)
        self._session = session
        self._org_id = org_id  # 多租户隔离：数据源作用域读取按组织过滤（None=平台视角不过滤）
        self._repo = AssetMapRepository(session)

    async def catalog_summary(self) -> dict[str, Any]:
        return await _agg_cached("catalog_summary", self._repo.catalog_summary)

    async def classification_summary(self) -> dict[str, Any]:
        return await _agg_cached("classification_summary", self._repo.classification_summary)

    async def metric_summary(self) -> dict[str, Any]:
        return await _agg_cached("metric_summary", self._repo.metric_summary)

    async def metric_dimension_summary(self) -> dict[str, Any]:
        """指标体系聚合：指标多维分布 + PII 合规率（概览 Tab 指标体系区块数据源）。"""
        return await _agg_cached(
            "metric_dimension_summary", self._repo.metric_dimension_summary
        )

    async def list_tables(
        self,
        source_id: str | None,
        sensitivity: str | None,
        limit: int,
        domain: str | None = None,
        owner_id: int | None = None,
        schema_status: str | None = None,
        keyword: str | None = None,
        database: str | None = None,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """数据表目录多维度过滤（服务端分页：返回 ``(items, total)``）。

        P2-1：repository 返回 ``(rows, total)``（真实总数 + offset 分页），此处
        解包并富化；API 层据此做服务端分页，前端不再一次拉 200 静默截断。
        """
        rows, total = await self._repo.list_tables(
            source_id,
            sensitivity,
            limit,
            domain=domain,
            owner_id=owner_id,
            schema_status=schema_status,
            keyword=keyword,
            database=database,
            org_id=self._org_id,
            offset=offset,
        )
        # assetmap T-2: 经 to_dict 剔除敏感字段（connection_config 等）
        items = [r.to_dict() for r in rows]
        # 生产化补充：源名称 / 业务域 / 责任人名（列表与下钻明细可读）
        return await self._repo.enrich_catalog_items(items), int(total)

    async def orphan_assets(
        self,
        keyword: str | None = None,
        source_id: str | None = None,
        domain: str | None = None,
        entity_type: str | None = None,
        sensitivity: str | None = None,
        schema_status: str | None = None,
        database: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """孤儿资产多维度过滤（服务端分页：返回 ``(items, total)``）。"""
        rows, total = await self._repo.orphan_assets(
            keyword=keyword,
            source_id=source_id,
            domain=domain,
            entity_type=entity_type,
            sensitivity=sensitivity,
            schema_status=schema_status,
            database=database,
            limit=limit,
            org_id=self._org_id,
            offset=offset,
        )
        items = [r.to_dict() for r in rows]
        return await self._repo.enrich_catalog_items(items), int(total)

    async def get_entity_detail(self, entity_id: int) -> dict[str, Any] | None:
        """资产实体详情：元数据 + 敏感度 + PII + 血缘边数（TD §12.11 流程 #5）。"""
        return await self._repo.get_entity_detail(entity_id, org_id=self._org_id)

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

        # 降级：从 MySQL lineage_edge + metric 拼接（按 depth 收敛，避免大图一团乱麻）
        return await self._get_graph_mysql(domain, pii_only, depth)

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
                # 节点：从指标种子出发按 depth 无向展开（与 MySQL 降级路径 BFS 语义一致），
                # 避免"无论 depth 多大都返回全量 500 节点挤成一团"。depth=1 仅指标+直连表，
                # 越深展开越多中间表。指标节点优先排序保证指标总在节点集内。
                seed_where = " seed.domain = $domain" if domain else ""
                node_where: list[str] = []
                if domain:
                    node_where.append("n.domain = $domain")
                if pii_only:
                    node_where.append("n.pii = true")
                node_where_clause = (" WHERE " + " AND ".join(node_where)) if node_where else ""
                node_query = (
                    "MATCH (seed:Asset {type: 'metric'})"
                    + (f" WHERE{seed_where}" if seed_where else "")
                    + " WITH collect(seed) AS seeds UNWIND seeds AS seed"
                    + f" MATCH (seed)-[rels:LINEAGE*0..{int(depth)}]-(n:Asset)"
                    + node_where_clause
                    + " RETURN DISTINCT n.id AS id, n.type AS type, n.label AS label,"
                    + " n.pii AS pii, n.domain AS domain, n.owner AS owner"
                    + " ORDER BY CASE n.type WHEN 'metric' THEN 0"
                    + " WHEN 'table' THEN 1 ELSE 2 END"
                    + " LIMIT 500"
                )

                # 边：两端均在被保留的收敛节点集内（自包含子图，消除悬空边——
                # 与 MySQL 路径一致，边数与图实际渲染完全对应）。
                edge_query = (
                    "MATCH (a:Asset)-[r:LINEAGE]->(b:Asset)"
                    " WHERE a.id IN $node_ids AND b.id IN $node_ids"
                    " RETURN DISTINCT a.id AS source, b.id AS target,"
                    " r.type AS type LIMIT 1000"
                )

                params: dict[str, Any] = {}
                if domain:
                    params["domain"] = domain

                nodes_result = await session.run(node_query, params)
                nodes = []
                async for record in nodes_result:
                    nodes.append(
                        {
                            "id": record["id"],
                            "type": record["type"] or "unknown",
                            "label": record["label"] or "",
                            "pii": bool(record["pii"]),
                            "domain": record["domain"],
                            "owner": record["owner"],
                        }
                    )

                # 无指标种子（图数据未就绪）→ 返回空图，避免全量散点
                if not nodes:
                    _NEO4J_BREAKER.record_success()
                    return {"nodes": [], "edges": []}

                # 表节点富集 entity_id（与 MySQL 降级路径一致）：Neo4j 节点本身不带
                # db_catalog 主键，需按 entity_name 批量回查目录，使点击表节点可打开
                # 实体详情抽屉（否则前端拿到 entity_id=None → 提示"暂不支持查看详情"）。
                table_names = [n["id"].split(":", 1)[1] for n in nodes if n["type"] == "table"]
                if table_names:
                    id_map = await self._repo.catalog_id_by_names(table_names)
                    for n in nodes:
                        if n["type"] == "table":
                            n["entity_id"] = id_map.get(n["id"].split(":", 1)[1])

                edge_params: dict[str, Any] = {"node_ids": [n["id"] for n in nodes]}
                edges_result = await session.run(edge_query, edge_params)
                edges = []
                async for record in edges_result:
                    edges.append(
                        {
                            "source": record["source"],
                            "target": record["target"],
                            "type": record["type"],
                        }
                    )

                # Neo4j 数据未就绪（节点仅导入 id、缺 label/type 等属性）时回退
                # MySQL 完整图谱，避免前端拿到满屏 unknown/空标签的孤立散点。
                if nodes and not any(n["label"] for n in nodes):
                    _NEO4J_BREAKER.record_failure()
                    logger.warning("assetmap_neo4j_nodes_missing_labels_fallback_mysql")
                    return None
                _NEO4J_BREAKER.record_success()
                return {"nodes": nodes, "edges": edges}
        except Exception as exc:
            _NEO4J_BREAKER.record_failure()
            logger.warning("assetmap_neo4j_query_failed: %s", exc)
            return None

    async def _get_graph_mysql(
        self, domain: str | None, pii_only: bool, depth: int | None = None
    ) -> dict[str, Any]:
        """降级：从 MySQL lineage_edge + metric 拼接图谱（按 depth 收敛规模）。"""
        nodes, edges = await self._repo.graph_from_mysql(domain, pii_only, depth)
        return {"nodes": nodes, "edges": edges}

    async def get_heatmap(self, dimension: str = "domain") -> dict[str, Any]:
        """聚合分桶返回敏感分布热力数据。

        Args:
            dimension: 聚合维度（domain / sensitivity / owner / dw_layer）。

        Raises:
            UnisenseError: 非法聚合维度（防止未知维度静默回退到 domain
                并在错误缓存键下缓存 domain 数据造成结果与请求不一致）。
        """
        if dimension not in _HEATMAP_DIMENSIONS:
            raise UnisenseError(
                f"非法的热力聚合维度: {dimension}",
                error_code="INVALID_HEATMAP_DIMENSION",
            )
        return await _agg_cached(
            f"heatmap:{dimension}",
            lambda: self._repo.heatmap_aggregation(dimension),
        )

    async def heatmap_matrix(self, asset_type: str = "catalog") -> dict[str, Any]:
        """二维热力矩阵：业务域 × 敏感级别（前端真热力图数据源）。

        Args:
            asset_type: 资产视角（catalog=目录资产 / metric=指标资产），
                并入缓存键避免两视角串数据。
        """
        return await _agg_cached(
            f"heatmap-matrix:{asset_type}",
            lambda: self._repo.heatmap_matrix(asset_type=asset_type),
        )

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
        return await self._repo.search_assets(q, entity_type, limit, org_id=self._org_id)

    async def health_summary(self) -> dict[str, Any]:
        """资产健康视图：源健康/schema 不完整/孤儿/陈旧资产。"""
        # 缓存键含 org_id：多租户下健康度须按组织隔离，防跨组织缓存串读（P1 加固）
        return await _agg_cached(
            f"health_summary:{self._org_id or 'all'}",
            lambda: self._repo.health_summary(org_id=self._org_id),
        )

    async def pii_overview(self) -> dict[str, Any]:
        """PII 合规资产视图：按敏感级/域聚合 PII 资产。"""
        return await _agg_cached(
            f"pii_overview:{self._org_id or 'all'}",
            lambda: self._repo.pii_overview(org_id=self._org_id),
        )

    async def recent_changes(self, days: int = 7, limit: int = 50) -> dict[str, Any]:
        """变更追踪流：最近 N 天新增/变更的目录与指标。"""
        return await self._repo.recent_changes(days, limit)

    async def my_assets(self, owner_id: int, limit: int = 50) -> dict[str, Any]:
        """我的资产：当前用户负责的目录与指标。"""
        return await self._repo.my_assets(owner_id, limit)

    async def export_tables(
        self,
        source_id: str | None,
        sensitivity: str | None,
        domain: str | None = None,
        owner_id: int | None = None,
        schema_status: str | None = None,
        keyword: str | None = None,
    ) -> list[dict[str, Any]]:
        """导出目录资产（表/视图）为字典列表，供 CSV 序列化（与列表同过滤条件）。"""
        rows, _total = await self._repo.list_tables(
            source_id,
            sensitivity,
            limit=5000,
            domain=domain,
            owner_id=owner_id,
            schema_status=schema_status,
            keyword=keyword,
            org_id=self._org_id,
        )
        return [r.to_dict() for r in rows]

    # ----------------------------------------------------------------
    # 写能力（FR-18 资产工作台）：认领/转让、重分类、批量
    # ----------------------------------------------------------------

    async def assign_owner(self, entity_id: int, owner_id: int | None) -> dict[str, Any]:
        """认领/转让归属（owner_id=None 解除归属）。"""
        entity = await self._repo.get_catalog_entity(entity_id)
        if entity is None:
            raise NotFoundError(f"资产不存在或已删除: {entity_id}", ctx={"entity_id": entity_id})
        if owner_id is not None and not await self._repo.user_exists(owner_id):
            raise NotFoundError(f"目标用户不存在: {owner_id}", ctx={"owner_id": owner_id})
        # 无变化短路：owner 未变不写库、不 bump row_version，避免人为乐观锁冲突
        if entity.owner_id == owner_id:
            return {"entity_id": entity.id, "owner_id": entity.owner_id}
        updated = await self._repo.assign_owner(entity, owner_id)
        await _agg_cache_invalidate()
        return {"entity_id": updated.id, "owner_id": updated.owner_id}

    async def reclassify_sensitivity(self, entity_id: int, level: str) -> dict[str, Any]:
        """重分类敏感级（level 由 schema 校验为枚举值）。"""
        entity = await self._repo.get_catalog_entity(entity_id)
        if entity is None:
            raise NotFoundError(f"资产不存在或已删除: {entity_id}", ctx={"entity_id": entity_id})
        # 无变化短路：敏感级未变不写库、不 bump row_version（同 assign_owner）
        if entity.sensitivity_level == level:
            return {"entity_id": entity.id, "sensitivity_level": entity.sensitivity_level}
        updated = await self._repo.reclassify_sensitivity(entity, level)
        await _agg_cache_invalidate()
        return {"entity_id": updated.id, "sensitivity_level": updated.sensitivity_level}

    async def batch_assign_owner(
        self, entity_ids: list[int], owner_id: int | None
    ) -> dict[str, Any]:
        """批量认领/转让归属（同事务，API 层统一 commit）。"""
        if owner_id is not None and not await self._repo.user_exists(owner_id):
            raise NotFoundError(f"目标用户不存在: {owner_id}", ctx={"owner_id": owner_id})
        entities = await self._repo.list_catalog_entities(entity_ids)
        if not entities:
            raise NotFoundError("指定实体均不存在或已删除")
        # 仅更新 owner 实际变化的实体：已是该 owner 的行跳过，避免无谓 bump row_version
        targets = [e for e in entities if e.owner_id != owner_id]
        affected = 0
        if targets:
            affected = await self._repo.batch_assign_owner(targets, owner_id)
            await _agg_cache_invalidate()
        return {"affected": affected, "owner_id": owner_id, "total": len(entity_ids)}

    async def batch_reclassify(self, entity_ids: list[int], level: str) -> dict[str, Any]:
        """批量重分类敏感级（同事务，API 层统一 commit）。"""
        entities = await self._repo.list_catalog_entities(entity_ids)
        if not entities:
            raise NotFoundError("指定实体均不存在或已删除")
        # 仅更新敏感级实际变化的实体（同 batch_assign_owner 短路语义）
        targets = [e for e in entities if e.sensitivity_level != level]
        affected = 0
        if targets:
            affected = await self._repo.batch_reclassify(targets, level)
            await _agg_cache_invalidate()
        return {"affected": affected, "sensitivity_level": level, "total": len(entity_ids)}

    # ----------------------------------------------------------------
    # 写能力（PII 合规增强）：表级复核 / 脱敏策略 / 字段误报标注 / 保留期 / 模板
    # ----------------------------------------------------------------

    async def list_pii_assets(
        self,
        *,
        keyword: str | None = None,
        source_id: str | None = None,
        domain: str | None = None,
        owner_id: int | None = None,
        review_status: str | None = None,
        category: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """PII 资产明细列表（分页 + 筛选，PII 合规 Tab 数据源）。"""
        items, total = await self._repo.list_pii_assets(
            keyword=keyword,
            source_id=source_id,
            domain=domain,
            owner_id=owner_id,
            review_status=review_status,
            category=category,
            page=page,
            page_size=page_size,
            org_id=self._org_id,
        )
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    async def review_catalog(
        self, entity_id: int, decision: str, reviewer_id: int
    ) -> dict[str, Any]:
        """表级 PII 合规复核（APPROVE/REJECT）。

        禁自审：资产责任人不得复核本人负责的资产（职责分离，对齐指标级
        ``MetricService.review_compliance`` 的 SELF_REVIEW_BLOCKED）。
        """
        entity = await self._repo.get_catalog_entity(entity_id)
        if entity is None:
            raise NotFoundError(f"资产不存在或已删除: {entity_id}", ctx={"entity_id": entity_id})
        if entity.owner_id is not None and entity.owner_id == reviewer_id:
            raise BusinessError(
                "资产责任人不得复核本人负责的资产（职责分离）",
                error_code="SELF_REVIEW_BLOCKED",
                ctx={"entity_id": entity_id, "owner_id": entity.owner_id},
            )
        updated = await self._repo.review_catalog(entity, decision, reviewer_id)
        await _agg_cache_invalidate()
        return {
            "entity_id": updated.id,
            "decision": decision,
            "compliance_reviewed": bool(updated.compliance_reviewed),
            "masking_policy": updated.masking_policy,
        }

    async def set_masking_policy(self, entity_id: int, policy: str) -> dict[str, Any]:
        """设置资产脱敏策略（none/mask/hash/deny）。"""
        entity = await self._repo.get_catalog_entity(entity_id)
        if entity is None:
            raise NotFoundError(f"资产不存在或已删除: {entity_id}", ctx={"entity_id": entity_id})
        updated = await self._repo.set_masking_policy(entity, policy)
        await _agg_cache_invalidate()
        return {"entity_id": updated.id, "masking_policy": updated.masking_policy}

    async def upsert_pii_override(
        self,
        entity_id: int,
        column: str,
        suppressed: bool,
        reason: str | None,
        actor_id: int,
    ) -> dict[str, Any]:
        """字段级人工标注（误报反馈/人工确认，upsert 语义）。"""
        entity = await self._repo.get_catalog_entity(entity_id)
        if entity is None:
            raise NotFoundError(f"资产不存在或已删除: {entity_id}", ctx={"entity_id": entity_id})
        row = await self._repo.upsert_pii_override(
            entity_id, column, suppressed, reason, actor_id
        )
        await _agg_cache_invalidate()
        return {
            "catalog_id": entity_id,
            "column": column,
            "suppressed": bool(row.suppressed),
            "reason": row.reason,
        }

    async def delete_pii_override(self, entity_id: int, column: str) -> dict[str, Any]:
        """撤销字段级人工标注（恢复规则引擎判定）。"""
        entity = await self._repo.get_catalog_entity(entity_id)
        if entity is None:
            raise NotFoundError(f"资产不存在或已删除: {entity_id}", ctx={"entity_id": entity_id})
        removed = await self._repo.delete_pii_override(entity_id, column)
        await _agg_cache_invalidate()
        return {"catalog_id": entity_id, "column": column, "removed": removed}

    async def set_retention(
        self, entity_id: int, retention_days: int | None, legal_basis: str | None
    ) -> dict[str, Any]:
        """设置资产保留期与合法性基础。"""
        entity = await self._repo.get_catalog_entity(entity_id)
        if entity is None:
            raise NotFoundError(f"资产不存在或已删除: {entity_id}", ctx={"entity_id": entity_id})
        updated = await self._repo.set_retention(entity, retention_days, legal_basis)
        await _agg_cache_invalidate()
        return {
            "entity_id": updated.id,
            "retention_days": updated.retention_days,
            "legal_basis": updated.legal_basis,
            "retention_expires_at": updated.retention_expires_at,
        }

    async def pii_templates(self) -> list[dict[str, Any]]:
        """行业分级模板列表（PII 合规盘点与批量升级）。"""
        return [
            {
                "id": t["id"],
                "name": t["name"],
                "description": t["description"],
                "sensitive_categories": t["sensitive_categories"],
            }
            for t in PII_TEMPLATES
        ]

    async def apply_pii_template(
        self,
        template_id: str,
        *,
        catalog_ids: list[int] | None,
        source_id: str | None,
        all_pii: bool,
    ) -> dict[str, Any]:
        """应用行业分级模板：按字段类别升级资产敏感级。

        幂等：已为 PII 的资产不再重复标记（changed=False）。
        """
        template = next((t for t in PII_TEMPLATES if t["id"] == template_id), None)
        if template is None:
            raise NotFoundError(f"模板不存在: {template_id}", ctx={"template_id": template_id})
        entities = await self._repo.list_catalog_ids_for_scope(
            catalog_ids=catalog_ids, source_id=source_id, all_pii=all_pii
        )
        if not entities:
            return {"template_id": template_id, "applied": 0, "changed": 0, "items": []}
        results: list[dict[str, Any]] = []
        changed = 0
        for entity in entities:
            res = await self._repo.apply_sensitivity_template(entity, template)
            if res["changed"]:
                changed += 1
            results.append(res)
        await _agg_cache_invalidate()
        return {
            "template_id": template_id,
            "applied": len(results),
            "changed": changed,
            "items": results,
        }

    async def export_pii_rows(
        self,
        *,
        keyword: str | None = None,
        source_id: str | None = None,
        domain: str | None = None,
        owner_id: int | None = None,
        review_status: str | None = None,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """PII 合规盘点导出数据（分页拉全量，供 CSV 序列化）。

        P1：导出上限与平台批量标准一致（5000 行），防大库无界内存拼接 OOM；
        超量时前端按筛选收敛后重导（导出接口是同步 CSV 生成，不做流式）。
        """
        all_items: list[dict[str, Any]] = []
        page = 1
        while True:
            items, total = await self._repo.list_pii_assets(
                keyword=keyword,
                source_id=source_id,
                domain=domain,
                owner_id=owner_id,
                review_status=review_status,
                category=category,
                page=page,
                page_size=500,
                org_id=self._org_id,
            )
            all_items.extend(items)
            if len(all_items) >= total or not items or len(all_items) >= _MAX_EXPORT_ROWS:
                break
            page += 1
        return all_items[:_MAX_EXPORT_ROWS]
