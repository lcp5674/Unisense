"""采集器 SPI（对齐 TD §12.1「SPI（多数据源适配器）」）。

设计要点：
- ``BaseCollector`` 抽象采集行为；``InformationSchemaCollector`` 为默认实现，
  通过注入 ``Connector`` 读取 information_schema 生成 ``CatalogSpec``。
- 外部依赖（源库）失败统一转化为 ``ExternalDependencyError``（503 可重试），
  **不**静默吞没为 200。
- SQL 一律参数化（``Connector.query(sql, params)``），避免注入。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.exceptions import BusinessError, ExternalDependencyError
from app.core.secrets import SecretManager
from app.services.collector.classifier import SensitivityClassifier


@dataclass
class CatalogSpec:
    """采集到的实体元数据规格。"""

    entity_name: str
    entity_type: str
    schema_json: dict[str, Any]
    etl_sql: str | None = None


class Connector(Protocol):
    """源库查询协议（便于测试注入假连接器）。"""

    async def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """执行参数化查询，返回行字典列表。"""
        ...

    async def dispose(self) -> None:
        """释放连接（源库引擎等）。"""
        ...


class BaseCollector(ABC):
    """采集器基类。"""

    def __init__(self, classifier: SensitivityClassifier | None = None) -> None:
        self._classifier = classifier or SensitivityClassifier()

    @abstractmethod
    async def collect(self, source: Any) -> list[CatalogSpec]:
        """采集数据源，返回实体规格列表。"""
        ...

    async def dispose(self) -> None:
        """释放采集器持有的外部连接（如源库引擎）。默认无操作，子类按需实现。"""
        return None


class InformationSchemaCollector(BaseCollector):
    """基于 information_schema 的默认采集器（参数化查询）。"""

    def __init__(
        self, connector: Connector, classifier: SensitivityClassifier | None = None
    ) -> None:
        super().__init__(classifier)
        self._connector = connector

    async def collect(self, source: Any) -> list[CatalogSpec]:
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
            except Exception as exc:
                raise ExternalDependencyError(
                    f"采集源 {source_id} 表 {tbl} 字段失败: {exc}"
                ) from exc
            schema_json = {"columns": [c.get("column_name") for c in cols]}
            specs.append(CatalogSpec(entity_name=tbl, entity_type="TABLE", schema_json=schema_json))
        return specs

    async def dispose(self) -> None:
        """采集完成后释放源库异步引擎，避免连接池泄漏。"""
        await self._connector.dispose()


class SqlalchemyConnector:
    """基于 SQLAlchemy 异步引擎的真实连接器。"""

    def __init__(self, db_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(db_url, pool_pre_ping=True)

    async def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            result = await conn.execute(text(sql), params or {})
            return [dict(row._mapping) for row in result]

    async def dispose(self) -> None:
        await self._engine.dispose()


def build_collector(collector_type: str, encrypted_config: str) -> BaseCollector:
    """按类型构建采集器（从加密连接配置还原源库 URL）。

    Args:
        collector_type: 采集器类型（当前仅 ``information_schema``）。
        encrypted_config: DataSource.connection_config 密文。

    Returns:
        采集器实例。

    Raises:
        BusinessError: 类型不支持。
    """
    if collector_type != "information_schema":
        raise BusinessError(
            f"不支持的采集器类型: {collector_type}",
            error_code="UNSUPPORTED_COLLECTOR",
        )
    cfg = SecretManager.decrypt(encrypted_config)
    db_url = cfg.get("db_url") or _build_url(cfg)
    return InformationSchemaCollector(SqlalchemyConnector(db_url))


def _build_url(cfg: dict[str, Any]) -> str:
    """由 host/port/user/password/db 组装 mysql+aiomysql URL。"""
    driver = cfg.get("driver", "mysql+aiomysql")
    user = cfg.get("user", "")
    password = cfg.get("password", "")
    host = cfg.get("host", "127.0.0.1")
    port = cfg.get("port", 3306)
    database = cfg.get("database", "")
    return f"{driver}://{user}:{password}@{host}:{port}/{database}"
