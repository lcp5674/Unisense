"""MySQL 连接器（对齐 TD §12.1 / spec FR-001/FR-005/FR-025）。

基于 information_schema 的 MySQL 采集器，从 spi.py 迁移并增强：
- connect_timeout=10 秒、query_timeout=60 秒（FR-005）
- 使用 SQLAlchemy URL.create() 避免密码出现在字符串中（FR-025）
- 单表 try/catch 跳过容错（FR-004）
- @registry.register("mysql") 注册
- 生产语义（FR-030）：按 connection_config.database 过滤；database 为空时
  遍历该实例下所有非系统库（entity_name 以 ``{database}.{table}`` 命名避免冲突）
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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

logger = logging.getLogger("unisense.collector.connectors.mysql")

# MySQL 系（含 Doris/StarRocks）的系统库，全库采集时排除
_EXCLUDE_SCHEMAS = frozenset(
    {
        "information_schema",
        "performance_schema",
        "mysql",
        "sys",
        "__internal_schema",
        "_statistics_",
        "_impala_builtins",
    }
)


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
        """执行查询；受 ``query_timeout`` 约束，超时抛外部依赖错误。

        FR-005 声明的 60s 查询超时此前为死配置（仅存储不使用），
        源库挂起时会永久占死 arq worker/事件循环——此处用
        ``asyncio.wait_for`` 真正落地超时。
        """
        async with self._engine.connect() as conn:
            try:
                result = await asyncio.wait_for(
                    conn.execute(text(sql), params or {}),
                    timeout=self._query_timeout,
                )
            except TimeoutError as exc:
                raise ExternalDependencyError(
                    f"查询超时（>{self._query_timeout}s）: {sql[:120]}"
                ) from exc
            # MySQL information_schema 列标签为大写（SCHEMA_NAME/TABLE_NAME），
            # 统一规范化为小写，保证下游 row.get("table_name") 键访问稳定。
            return [{k.lower(): v for k, v in row._mapping.items()} for row in result]

    async def dispose(self) -> None:
        await self._engine.dispose()


class InformationSchemaCollector(BaseCollector):
    """基于 information_schema 的默认采集器（参数化查询 + 单表容错）。

    数据库语义：``database`` 为空时采集该实例下全部非系统库。
    """

    def __init__(
        self,
        connector: SqlalchemyConnector,
        classifier: SensitivityClassifier | None = None,
        *,
        database: str | None = None,
    ) -> None:
        super().__init__(classifier)
        self._connector = connector
        self._database = database or None

    async def _list_schemas(self) -> list[str]:
        """当未指定 database 时枚举全部非系统库（SQL + Python 双重过滤）。"""
        rows = await self._connector.query(
            "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name"
        )
        names: list[str] = []
        for row in rows:
            name = row.get("schema_name")
            if name and name not in _EXCLUDE_SCHEMAS:
                names.append(str(name))
        return names

    async def list_databases(self) -> list[str]:
        """枚举实例下全部非系统数据库（复用 _list_schemas，供创建时选择目标库）。"""
        return await self._list_schemas()

    async def collect_entity(self, source: Any, entity_name: str) -> CatalogSpec | None:
        """单表元数据刷新：仅查询目标表的列元数据，不触发全源扫描。

        ``entity_name`` 在单库模式为 ``table``；多库模式为 ``schema.table``。
        表不存在或实体名无法定位 schema 时返回 None（由调用方回退全量采集）。
        """
        if self._database:
            schema, tbl = self._database, entity_name
        else:
            if "." not in entity_name:
                return None
            schema, tbl = entity_name.rsplit(".", 1)
        if not schema or not tbl:
            return None
        try:
            rows = await self._connector.query(
                "SELECT table_name, column_name, data_type, is_nullable, "
                "column_comment, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = :tbl "
                "ORDER BY ordinal_position",
                {"schema": schema, "tbl": tbl},
            )
        except Exception as exc:
            raise ExternalDependencyError(f"刷新实体 {entity_name} 失败: {exc}") from exc
        cols = [
            {
                "name": r.get("column_name"),
                "type": r.get("data_type") or "unknown",
                "nullable": str(r.get("is_nullable") or "YES").upper() == "YES",
                "comment": r.get("column_comment") or "",
                "default": r.get("column_default"),
            }
            for r in rows
            if r.get("column_name")
        ]
        if not cols:
            # 源端无此表 → 无法刷新，由调用方回退全量采集
            return None
        return CatalogSpec(
            entity_name=entity_name,
            entity_type="TABLE",
            schema_json={"columns": cols},
        )

    async def collect(self, source: Any) -> CollectResult:
        source_id = getattr(source, "source_id", "?")
        # P0-6: 读取增量上下文（service 层 collect 前注入）
        incremental = (
            getattr(self, "_incremental_mode", "FULL") == "INCREMENTAL"
            and getattr(self, "_incremental_watermark", None) is not None
        )
        watermark_ts = getattr(self, "_incremental_watermark", None)
        try:
            if self._database:
                schemas = [self._database]
            else:
                schemas = await self._list_schemas()
        except Exception as exc:  # 外部依赖失败 -> 转化为重试型错误（不静默）
            raise ExternalDependencyError(f"采集源 {source_id} 失败: {exc}") from exc

        specs: list[CatalogSpec] = []
        failed_specs: list[FailedSpec] = []
        for schema in schemas:
            try:
                if incremental:
                    # 增量：只取 UPDATE_TIME 晚于水位的表（P0-6 真实接入）
                    from app.services.collector.incremental import build_incremental_query

                    inc_sql = build_incremental_query("mysql", schema, watermark_ts)
                    tables = (
                        await self._connector.query(
                            inc_sql or "",
                            {
                                "schema": schema,
                                "ttype": "BASE TABLE",
                                "watermark": watermark_ts,
                            },
                        )
                        if inc_sql
                        else []
                    )
                else:
                    tables = await self._connector.query(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :schema AND table_type = :ttype",
                        {"schema": schema, "ttype": "BASE TABLE"},
                    )
            except Exception as exc:
                logger.warning("采集源 %s 库 %s 表列表失败: %s", source_id, schema, exc)
                continue

            # P1-1: 一次批量查询该库全部列并按表分组，消除「每表一次查询」的 N+1；
            # P0: 补列类型（data_type）、可空（is_nullable）、注释（column_comment）、
            #     默认值（column_default）——schema_json 完整存储供 PII 分类
            #     （依赖列注释匹配）和下游消费。
            try:
                col_rows = await self._connector.query(
                    "SELECT table_name, column_name, data_type, is_nullable, "
                    "column_comment, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_schema = :schema ORDER BY table_name, ordinal_position",
                    {"schema": schema},
                )
            except Exception as exc:
                logger.warning("采集源 %s 库 %s 列列表失败: %s", source_id, schema, exc)
                col_rows = []
            columns_by_table: dict[str, list[dict[str, Any]]] = {}
            for r in col_rows:
                tbl = r.get("table_name")
                col = r.get("column_name")
                if tbl and col:
                    columns_by_table.setdefault(tbl, []).append(
                        {
                            "name": col,
                            "type": r.get("data_type") or "unknown",
                            "nullable": str(r.get("is_nullable") or "YES").upper() == "YES",
                            "comment": r.get("column_comment") or "",
                            "default": r.get("column_default"),
                        }
                    )

            for row in tables:
                tbl = row.get("table_name")
                if not tbl:
                    continue
                # 多库采集时以 库.表 命名，避免跨库同名表冲突
                entity_name = f"{schema}.{tbl}" if not self._database else tbl
                cols = columns_by_table.get(tbl, [])
                schema_json = {"columns": cols}
                specs.append(
                    CatalogSpec(
                        entity_name=entity_name,
                        entity_type="TABLE",
                        schema_json=schema_json,
                    )
                )

        return CollectResult(specs=specs, failed_specs=failed_specs, source_id=source_id)

    async def probe(self) -> ProbeResult:
        """轻量探活：SELECT 1（FR-030 连接测试）。"""
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
    return InformationSchemaCollector(connector, database=cfg.get("database"))
