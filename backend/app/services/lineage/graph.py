"""血缘图存储客户端（Neo4j，可选依赖，best-effort 降级）。

Neo4j 用于影响分析的可视化与图遍历；MySQL 为权威边存储。
当 Neo4j 未配置或不可达时，写图静默降级（返回 False），不影响主流程。
"""

from __future__ import annotations

import contextlib
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("unisense.lineage.graph")


class LineageGraphClient:
    """Neo4j 血缘图客户端（惰性连接，可降级）。"""

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
        """将血缘边写入 Neo4j（MERGE 节点与关系）。

        Args:
            edges: ``(source_node, target_node, edge_type)`` 三元组列表。

        Returns:
            写入成功返回 True；未配置/不可达/异常时返回 False（降级）。
        """
        if not self._uri:
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
                for src, tgt, etype in edges:
                    await session.run(
                        "MERGE (s:Asset {id:$s}) "
                        "MERGE (t:Asset {id:$t}) "
                        "MERGE (s)-[:LINEAGE {type:$e}]->(t)",
                        s=src,
                        t=tgt,
                        e=etype,
                    )
            return True
        except Exception as exc:  # 图存储不可达，降级
            logger.warning("lineage_graph_write_failed", error=str(exc))
            return False

    async def dispose(self) -> None:
        """关闭驱动连接。"""
        if self._driver is not None:
            with contextlib.suppress(Exception):
                await self._driver.close()
            self._driver = None
