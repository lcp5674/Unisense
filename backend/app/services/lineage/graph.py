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

    async def upsert_assets(self, assets: list[dict[str, Any]]) -> bool:
        """批量 upsert 资产节点展示属性（MERGE + SET，幂等），带熔断保护。

        资产地图 Neo4j 路径的节点属性补全：血缘导入脚本只写节点 ``id`` 与关系，
        展示所需 ``type/label/domain/pii/owner`` 由此方法从 MySQL 资产元数据同步。

        Args:
            assets: ``{"id": str, "type": str, "label": str, "pii": bool,
                "domain": str|None, "owner": str|None}`` 列表；id 必填，其余可缺省。

        Returns:
            全部写入成功返回 True；未配置/不可达/熔断时返回 False（降级）。
        """
        if not self._uri or not assets:
            return False
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
            rows = [
                {
                    "id": a["id"],
                    "type": a.get("type"),
                    "label": a.get("label"),
                    "pii": bool(a.get("pii")),
                    "domain": a.get("domain"),
                    "owner": a.get("owner"),
                }
                for a in assets
            ]
            async with self._driver.session() as session:
                for i in range(0, len(rows), _WRITE_BATCH_SIZE):
                    await session.run(
                        "UNWIND $rows AS row "
                        "MERGE (n:Asset {id: row.id}) "
                        "SET n.type = row.type, n.label = row.label, "
                        "n.pii = row.pii, n.domain = row.domain, n.owner = row.owner",
                        rows=rows[i : i + _WRITE_BATCH_SIZE],
                    )
            neo4j_breaker.record_success()
            return True
        except Exception as exc:  # 图存储不可达，降级
            neo4j_breaker.record_failure()
            logger.warning("lineage_graph_upsert_failed", error=str(exc))
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

    async def list_asset_ids(self, limit: int = 100000) -> list[str]:
        """列出图内全部 Asset 节点 id（对账/同步用）。

        供定时对账任务（``neo4j_sync.sync_neo4j_assets_task``）枚举图现状，
        以决定节点属性补全范围与指标边表端存在性。

        Returns:
            Asset 节点 id 列表；未配置/不可达/熔断时返回空列表（调用方降级）。
        """
        if not self._uri:
            return []
        if not neo4j_breaker.allow():
            logger.warning("lineage_graph_breaker_open")
            return []
        try:
            from neo4j import AsyncGraphDatabase
        except Exception:  # pragma: no cover - 依赖缺失时降级
            return []
        try:
            if self._driver is None:
                self._driver = AsyncGraphDatabase.driver(
                    self._uri, auth=(self._user, self._password)
                )
            ids: list[str] = []
            async with self._driver.session() as session:
                result = await session.run(
                    "MATCH (n:Asset) RETURN n.id AS id LIMIT $limit", limit=limit
                )
                async for record in result:
                    ids.append(record["id"])
            neo4j_breaker.record_success()
            return ids
        except Exception as exc:  # 图存储不可达，降级
            neo4j_breaker.record_failure()
            logger.warning("lineage_graph_list_ids_failed", error=str(exc))
            return []

    async def delete_edges(self, edges: list[tuple[str, str, str]]) -> bool:
        """从 Neo4j 删除血缘边（确认失效边时同步图存储，best-effort）。

        Args:
            edges: ``(source_node, target_node, edge_type)`` 三元组列表。

        Returns:
            全部删除成功返回 True；未配置/不可达/熔断时返回 False（降级，不影响
            MySQL 权威删除——图读路径对已删边回退 MySQL，行为一致）。
        """
        if not self._uri or not edges:
            return False
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
            rows = [{"src": s, "tgt": t, "etype": e} for s, t, e in edges]
            async with self._driver.session() as session:
                for i in range(0, len(rows), _WRITE_BATCH_SIZE):
                    await session.run(
                        "UNWIND $rows AS row "
                        "MATCH (s:Asset {id:row.src})"
                        "-[r:LINEAGE {type:row.etype}]->(t:Asset {id:row.tgt}) "
                        "DELETE r",
                        rows=rows[i : i + _WRITE_BATCH_SIZE],
                    )
            neo4j_breaker.record_success()
            return True
        except Exception as exc:  # 图存储不可达，降级
            neo4j_breaker.record_failure()
            logger.warning("lineage_graph_delete_failed", error=str(exc))
            return False

    async def dispose(self) -> None:
        """关闭驱动连接。"""
        if self._driver is not None:
            with contextlib.suppress(Exception):
                await self._driver.close()
            self._driver = None

    async def query_paths(
        self, source: str, target: str, max_hops: int = 5, limit: int = 50
    ) -> list[tuple[list[str], list[tuple[str, str, str]]]] | None:
        """图路径查询（P3）：``source`` → ``target`` 的全部有向路径（Neo4j 读路径）。

        Args:
            source: 起点节点 id。
            target: 终点节点 id。
            max_hops: 最大跳数（可变长关系上界；小于 1 返回空列表）。
            limit: 返回路径条数上限。

        Returns:
            每条路径 ``(nodes, edges)``：``nodes`` 为 ``[source, ..., target]`` 节点
            id 序列，``edges`` 为 ``(src, tgt, edge_type)`` 边序列；未配置 URI /
            熔断 / 不可达时返回 None（调用方降级 MySQL DFS）。
        """
        if max_hops < 1:
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

        # 可变长关系上界必须是字面量（Neo4j 禁止参数），max_hops 由服务层校验无注入面。
        hops = int(max_hops)
        query = (
            f"MATCH p = (a:Asset {{id:$source}})-[:LINEAGE*1..{hops}]->"
            "(b:Asset {id:$target}) "
            "RETURN [n IN nodes(p) | n.id] AS nodes, "
            "[r IN relationships(p) | "
            "{src: startNode(r).id, tgt: endNode(r).id, type: r.type}] AS edges "
            "LIMIT $limit"
        )
        try:
            if self._driver is None:
                self._driver = AsyncGraphDatabase.driver(
                    self._uri, auth=(self._user, self._password)
                )
            paths: list[tuple[list[str], list[tuple[str, str, str]]]] = []
            async with self._driver.session() as session:
                records = await session.run(query, source=source, target=target, limit=limit)
                async for record in records:
                    nodes = list(record["nodes"])
                    edges = [(r["src"], r["tgt"], r["type"]) for r in record["edges"]]
                    paths.append((nodes, edges))
                    if len(paths) >= limit:
                        break
            neo4j_breaker.record_success()
            return paths
        except Exception as exc:  # 图存储不可达，降级 MySQL
            neo4j_breaker.record_failure()
            logger.warning("lineage_graph_paths_failed", error=str(exc))
            return None

    async def query_terminals(
        self, node: str, max_hops: int = 5, limit: int = 100
    ) -> list[tuple[str, list[str]]] | None:
        """图终止节点查询（P3 断链定位）：从 ``node`` 下游可达的无下游死端（Neo4j）。

        Args:
            node: 起点节点 id。
            max_hops: 最大跳数（可变长关系上界；小于 1 返回空列表）。
            limit: 返回终止节点数上限。

        Returns:
            ``[(terminal_node, path_nodes)]``，``path_nodes`` 为 ``shortestPath``
            的最短节点序列（含起点）；未配置 / 熔断 / 不可达时返回 None（调用方
            降级 MySQL DFS）。
        """
        if max_hops < 1:
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

        hops = int(max_hops)
        query = (
            f"MATCH p = shortestPath((s:Asset {{id:$node}})"
            f"-[:LINEAGE*1..{hops}]->(t:Asset)) "
            "WHERE NOT (t)-[:LINEAGE]->(:Asset) "
            "RETURN t.id AS terminal, [n IN nodes(p) | n.id] AS path "
            "LIMIT $limit"
        )
        try:
            if self._driver is None:
                self._driver = AsyncGraphDatabase.driver(
                    self._uri, auth=(self._user, self._password)
                )
            terminals: list[tuple[str, list[str]]] = []
            async with self._driver.session() as session:
                records = await session.run(query, node=node, limit=limit)
                async for record in records:
                    terminals.append((record["terminal"], list(record["path"])))
                    if len(terminals) >= limit:
                        break
            neo4j_breaker.record_success()
            return terminals
        except Exception as exc:  # 图存储不可达，降级 MySQL
            neo4j_breaker.record_failure()
            logger.warning("lineage_graph_terminals_failed", error=str(exc))
            return None

    async def count_edges(self) -> int | None:
        """统计图内血缘边总数（P2 健康度「图-库对账偏差」维度原料）。

        Returns:
            图内 ``LINEAGE`` 关系总数；未配置 / 熔断 / 不可达时返回 None
            （健康度对该维度降级为 unknown，不参与总分）。
        """
        if not self._uri:
            return None
        if not neo4j_breaker.allow():
            logger.warning("lineage_graph_breaker_open")
            return None
        try:
            from neo4j import AsyncGraphDatabase
        except Exception:  # pragma: no cover - 依赖缺失时降级
            return None
        try:
            if self._driver is None:
                self._driver = AsyncGraphDatabase.driver(
                    self._uri, auth=(self._user, self._password)
                )
            async with self._driver.session() as session:
                record = await session.run("MATCH ()-[r:LINEAGE]->() RETURN count(r) AS n")
                row = await record.single()
                count = int(row["n"]) if row is not None else 0
            neo4j_breaker.record_success()
            return count
        except Exception as exc:  # 图存储不可达，降级
            neo4j_breaker.record_failure()
            logger.warning("lineage_graph_count_failed", error=str(exc))
            return None
