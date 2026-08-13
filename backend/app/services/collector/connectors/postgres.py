"""PostgreSQL 连接器（对齐 TD §12.1 / spec FR-001）。

通过 information_schema 查询 PostgreSQL 表和字段信息。
- URL: postgresql+asyncpg://
- 单表 try/catch 跳过容错
- 生产语义（FR-030）：table_schema 取 connection_config.schema（默认 public），
  不再误用业务 domain 作为 schema
- @registry.register("postgres") 注册
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy.engine import URL

from app.core.exceptions import ExternalDependencyError
from app.services.collector.classifier import SensitivityClassifier
from app.services.collector.connectors.collector_registry import registry
from app.services.collector.spi import (
    BaseCollector,
    CatalogSpec,
    CollectResult,
    FailedSpec,
    ProbeResult,
)

if TYPE_CHECKING:
    from app.services.collector.connectors.mysql import SqlalchemyConnector

logger = logging.getLogger("unisense.collector.connectors.postgres")


class PostgresCollector(BaseCollector):
    """PostgreSQL 采集器：查询 information_schema.tables + information_schema.columns。"""

    # PostgreSQL 系统 schema（全库枚举时排除）
    _EXCLUDE_SCHEMAS = frozenset(
        {
            "pg_catalog",
            "information_schema",
            "pg_toast",
            "pg_temp_1",
            "pg_toast_temp_1",
            "pg_toast_temp_2",
        }
    )

    def __init__(
        self,
        connector: SqlalchemyConnector,
        classifier: SensitivityClassifier | None = None,
        *,
        schema: str | None = "public",
    ) -> None:
        super().__init__(classifier)
        self._connector = connector
        # P1-2: schema 为空 → 枚举该实例下全部非系统 schema（与 MySQL 空 database 语义对齐）
        self._schema = (schema or "").strip() or None

    async def _list_schemas(self) -> list[str]:
        rows = await self._connector.query(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('pg_catalog','information_schema','pg_toast',"
            "'pg_temp_1','pg_toast_temp_1','pg_toast_temp_2') "
            "ORDER BY schema_name"
        )
        names: list[str] = []
        for row in rows:
            name = row.get("schema_name")
            if name and name not in self._EXCLUDE_SCHEMAS:
                names.append(str(name))
        return names

    async def collect(self, source: Any) -> CollectResult:
        source_id = getattr(source, "source_id", "?")

        try:
            if self._schema:
                schemas = [self._schema]
            else:
                schemas = await self._list_schemas()
        except Exception as exc:
            raise ExternalDependencyError(f"采集源 {source_id} 失败: {exc}") from exc

        specs: list[CatalogSpec] = []
        failed_specs: list[FailedSpec] = []
        for schema in schemas:
            try:
                tables = await self._connector.query(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :schema AND table_type = :ttype",
                    {"schema": schema, "ttype": "BASE TABLE"},
                )
            except Exception as exc:
                logger.warning("采集源 %s 库 %s 表列表失败: %s", source_id, schema, exc)
                continue

            for row in tables:
                tbl = row.get("table_name")
                if not tbl:
                    continue
                entity_name = f"{schema}.{tbl}" if not self._schema else tbl
                try:
                    cols = await self._connector.query(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema = :schema AND table_name = :tbl "
                        "ORDER BY ordinal_position",
                        {"schema": schema, "tbl": tbl},
                    )
                    schema_json = {
                        "columns": [
                            {"name": c.get("column_name"), "type": c.get("data_type")}
                            for c in cols
                        ]
                    }
                    specs.append(
                        CatalogSpec(
                            entity_name=entity_name,
                            entity_type="TABLE",
                            schema_json=schema_json,
                        )
                    )
                except Exception as exc:
                    logger.warning("采集源 %s 表 %s 字段失败: %s", source_id, entity_name, exc)
                    failed_specs.append(FailedSpec(entity_name=entity_name, error=str(exc)))

        return CollectResult(specs=specs, failed_specs=failed_specs, source_id=source_id)

    async def probe(self) -> ProbeResult:
        """轻量探活：SELECT 1。"""
        start = time.monotonic()
        try:
            await self._connector.query("SELECT 1")
            return ProbeResult(ok=True, latency_ms=int((time.monotonic() - start) * 1000))
        except Exception as exc:
            return ProbeResult(
                ok=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                error=str(exc),
            )

    async def dispose(self) -> None:
        await self._connector.dispose()


def _build_postgres_url(cfg: dict[str, Any]) -> URL:
    """使用 SQLAlchemy URL.create() 构建 PostgreSQL 连接 URL。"""
    return URL.create(
        drivername="postgresql+asyncpg",
        username=cfg.get("user", ""),
        password=cfg.get("password", ""),
        host=cfg.get("host", "127.0.0.1"),
        port=cfg.get("port", 5432),
        database=cfg.get("database", ""),
    )


@registry.register("postgres")
def create_postgres_collector(cfg: dict[str, Any]) -> PostgresCollector:
    """PostgreSQL 采集器工厂函数。"""
    from app.services.collector.connectors.mysql import SqlalchemyConnector

    db_url = cfg.get("db_url") or _build_postgres_url(cfg)
    connector = SqlalchemyConnector(db_url, connect_timeout=10, query_timeout=60)
    # P1-2: schema 为空（且未配 database）→ 全库枚举
    return PostgresCollector(connector, schema=cfg.get("schema") or cfg.get("database"))
