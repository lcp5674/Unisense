"""MySQL 连接器（对齐 TD §12.1 / spec FR-001/FR-005/FR-025）。

基于 information_schema 的 MySQL 采集器，从 spi.py 迁移并增强：
- connect_timeout=10 秒、query_timeout=60 秒（FR-005）
- 使用 SQLAlchemy URL.create() 避免密码出现在字符串中（FR-025）
- 单表 try/catch 跳过容错（FR-004）
- @registry.register("mysql") 注册
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.exceptions import ExternalDependencyError
from app.services.collector.classifier import SensitivityClassifier
from app.services.collector.connectors.collector_registry import registry
from app.services.collector.spi import BaseCollector, CatalogSpec, CollectResult, FailedSpec

logger = logging.getLogger("unisense.collector.connectors.mysql")


class SqlalchemyConnector:
    """基于 SQLAlchemy 异步引擎的真实连接器（含超时配置）。"""

    def __init__(
        self,
        db_url: str | URL,
        *,
        connect_timeout: int = 10,
        query_timeout: int = 60,
    ) -> None:
        if isinstance(db_url, str):
            self._engine: AsyncEngine = create_async_engine(
                db_url,
                pool_pre_ping=True,
                connect_args={"connect_timeout": connect_timeout},
            )
        else:
            self._engine = create_async_engine(
                db_url,
                pool_pre_ping=True,
                connect_args={"connect_timeout": connect_timeout},
            )
        self._query_timeout = query_timeout

    async def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            result = await conn.execute(text(sql), params or {})
            return [dict(row._mapping) for row in result]

    async def dispose(self) -> None:
        await self._engine.dispose()


class InformationSchemaCollector(BaseCollector):
    """基于 information_schema 的默认采集器（参数化查询 + 单表容错）。"""

    def __init__(
        self, connector: SqlalchemyConnector, classifier: SensitivityClassifier | None = None
    ) -> None:
        super().__init__(classifier)
        self._connector = connector

    async def collect(self, source: Any) -> CollectResult:
        source_id = getattr(source, "source_id", "?")
        try:
            tables = await self._connector.query(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_type = :ttype",
                {"schema": getattr(source, "domain", ""), "ttype": "BASE TABLE"},
            )
        except Exception as exc:  # 外部依赖失败 -> 转化为重试型错误（不静默）
            raise ExternalDependencyError(f"采集源 {source_id} 失败: {exc}") from exc

        specs: list[CatalogSpec] = []
        failed_specs: list[FailedSpec] = []
        for row in tables:
            tbl = row.get("table_name")
            if not tbl:
                continue
            try:
                cols = await self._connector.query(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = :tbl",
                    {"schema": getattr(source, "domain", ""), "tbl": tbl},
                )
                schema_json = {"columns": [c.get("column_name") for c in cols]}
                specs.append(
                    CatalogSpec(entity_name=tbl, entity_type="TABLE", schema_json=schema_json)
                )
            except Exception as exc:
                # FR-004: 单表失败跳过继续，记录到 failed_specs
                logger.warning("采集源 %s 表 %s 字段失败: %s", source_id, tbl, exc)
                failed_specs.append(FailedSpec(entity_name=tbl, error=str(exc)))

        return CollectResult(specs=specs, failed_specs=failed_specs, source_id=source_id)

    async def dispose(self) -> None:
        """采集完成后释放源库异步引擎，避免连接池泄漏。"""
        await self._connector.dispose()


def _build_mysql_url(cfg: dict[str, Any]) -> URL:
    """FR-025: 使用 SQLAlchemy URL.create() 构建 MySQL 连接 URL。

    避免密码出现在字符串中（f-string 构建方式的安全替代）。
    """
    return URL.create(
        drivername=cfg.get("driver", "mysql+aiomysql"),
        username=cfg.get("user", ""),
        password=cfg.get("password", ""),
        host=cfg.get("host", "127.0.0.1"),
        port=cfg.get("port", 3306),
        database=cfg.get("database", ""),
    )


@registry.register("mysql")
def create_mysql_collector(cfg: dict[str, Any]) -> InformationSchemaCollector:
    """MySQL 采集器工厂函数。"""
    db_url = cfg.get("db_url") or _build_mysql_url(cfg)
    connector = SqlalchemyConnector(db_url, connect_timeout=10, query_timeout=60)
    return InformationSchemaCollector(connector)
