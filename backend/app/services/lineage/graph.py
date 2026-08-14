"""血缘图存储客户端（Neo4j，可选依赖，best-effort 降级）。

Neo4j 用于影响分析的可视化与图遍历；MySQL 为权威边存储。
当 Neo4j 未配置或不可达时，写入静默降级（返回 False），读取返回 None
（调用方回退 MySQL 影响分析），均不影响主流程。

P2: 集成全局 neo4j_breaker 熔断器，连续失败后自动熔断，半开窗口探测恢复。
P3: 新增读路径 ``query_impact``，图遍历走 Cypher，熔断/不可达时降级 MySQL。
"""

from __future__ import annotations

import contextlib
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.resilience import neo4j_breaker

logger = get_logger("unisense.lineage.graph")

# 批量写图时分批大小（防单请求过大；语义与逐条 MERGE 等价）
_WRITE_BATCH_SIZE = 2000


class LineageGraphClient:
    """Neo4j 血缘图客户端（惰性连接，可降级，带熔断保护）。"""

    def __init__(
        self,
        uri: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        self._uri = uri or settings.neo4j_url
        self._user = user or settings.neo4j_user
        self._password = password if password is not None else settings.neo4j_password
        self._driver: Any = None

    async def write_edges(self, edges: list[tuple[str, str, str]]) -> bool:
        """将血缘边写入 Neo4j（UNWIND 批量 MERGE 节点与关系），带熔断保护。

        Args:
            edges: ``(source_node, target_node, edge_type)`` 三元组列表。

        Returns:
            写入成功返回 True；未配置/不可达/熔断时返回 False（降级）。
        """
        if not self._uri:
            return False

        # 熔断检查
        if not neo4j_breaker.allow():
            logger.warning("lineage_graph_breaker_open")
            return False

        try:
            from neo4j import AsyncGraphDatabase
        except Exception:  # pragma: no cover - 依赖缺失时降级
            return False
        try:
            if self._driver is None:
                self._driver = AsyncGraphDatabase.driver(
                    self._uri, auth=(self._user, self._password)
                )
            async with self._driver.session() as session:
                # UNWIND 批量 MERGE（幂等，语义与逐条 MERGE 等价）：大批量
                # （如脚本导入上万条边）一次提交性能远优于逐条 session.run。
                # 分批防单请求过大；每批内任意一条失败即整批报错降级。
                rows = [{"src": s, "tgt": t, "etype": e} for s, t, e in edges]
                for i in range(0, len(rows), _WRITE_BATCH_SIZE):
                    await session.run(
                        "UNWIND $rows AS row "
                        "MERGE (s:Asset {id:row.src}) "
                        "MERGE (t:Asset {id:row.tgt}) "
                        "MERGE (s)-[:LINEAGE {type:row.etype}]->(t)",
                        rows=rows[i : i + _WRITE_BATCH_SIZE],
                    )
            neo4j_breaker.record_success()
            return True
        except Exception as exc:  # 图存储不可达，降级
            neo4j_breaker.record_failure()
            logger.warning("lineage_graph_write_failed", error=str(exc))
            return False

    async def query_impact(
        self,
        node: str,
        direction: str = "downstream",
        max_hops: int = 5,
        max_edges: int = 5000,
    ) -> list[tuple[str, str, str]] | None:
        """图遍历影响分析（Neo4j 读路径），失败时返回 None 供调用方降级 MySQL。

        Args:
            node: 起点资产节点 id（如 ``table:db.x`` / ``field:db.t.c``）。
            direction: ``downstream``（下游/目标）、``upstream``（上游/源）、
                ``both``（双向）；未知取值按 ``downstream`` 处理。
            max_hops: 最大遍历跳数（可变长关系上界；小于 1 时返回空列表）。
            max_edges: 返回边数上限（Cypher LIMIT + Python 端兜底截断）。

        Returns:
            ``(source_node, target_node, edge_type)`` 边列表（edge_type 取关系的
            ``type`` 属性，现为 ``DERIVED_FROM``）；未配置 URI / 熔断打开 /
            Neo4j 不可达时返回 None（调用方降级到 MySQL 影响分析）。
        """
        if max_hops < 1 or max_edges < 1:
            return []
        if not self._uri:
            return None
        if not neo4j_breaker.allow():
            logger.warning("lineage_graph_breaker_open")
            return None
        try:
            from neo4j import AsyncGraphDatabase
        except Exception:  # pragma: no cover - 依赖缺失时降级
            return None

        # Neo4j 禁止在 MATCH pattern 中用参数作为可变长度关系上界（必须字面量）。
        # max_hops 由服务层校验（1..10），无注入面，直接字面量插值。
        hops = int(max_hops)
        pattern = f"(s:Asset {{id:$node}})-[:LINEAGE*1..{hops}]->(t:Asset)"
        if direction == "upstream":
            pattern = f"(s:Asset {{id:$node}})<-[:LINEAGE*1..{hops}]-(t:Asset)"
        elif direction == "both":
            pattern = f"(s:Asset {{id:$node}})-[:LINEAGE*1..{hops}]-(t:Asset)"
        query = (
            f"MATCH p={pattern} "
            "UNWIND relationships(p) AS r "
            "RETURN DISTINCT startNode(r).id AS src, endNode(r).id AS tgt, r.type AS edge_type "
            "LIMIT $max_edges"
        )
        try:
            if self._driver is None:
                self._driver = AsyncGraphDatabase.driver(
                    self._uri, auth=(self._user, self._password)
                )
            edges: list[tuple[str, str, str]] = []
            async with self._driver.session() as session:
                records = await session.run(query, node=node, max_edges=max_edges)
                async for record in records:
                    edges.append((record["src"], record["tgt"], record["edge_type"]))
                    if len(edges) >= max_edges:
                        break
            neo4j_breaker.record_success()
            return edges
        except Exception as exc:  # 图存储不可达，降级 MySQL
            neo4j_breaker.record_failure()
            logger.warning("lineage_graph_query_failed", error=str(exc))
            return None

    async def dispose(self) -> None:
        """关闭驱动连接。"""
        if self._driver is not None:
            with contextlib.suppress(Exception):
                await self._driver.close()
            self._driver = None
