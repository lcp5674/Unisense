"""ClickHouse 连接器（对齐 TD §12.1 / spec FR-001）。

通过 ClickHouse HTTP API (8123 端口) 查询 system.tables + system.columns，
无需安装 clickhouse-driver。

- HTTP API: GET http://{host}:8123/?query=SQL
- 单表 try/catch 跳过容错
- @registry.register("clickhouse") 注册
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.exceptions import ExternalDependencyError
from app.services.collector.classifier import SensitivityClassifier
from app.services.collector.connectors.collector_registry import registry
from app.services.collector.spi import BaseCollector, CatalogSpec, CollectResult, FailedSpec

logger = logging.getLogger("unisense.collector.connectors.clickhouse")


class ClickHouseCollector(BaseCollector):
    """ClickHouse 采集器：通过 HTTP API (8123 端口) 查询。"""

    def __init__(
        self,
        host: str,
        port: int = 8123,
        user: str = "default",
        password: str = "",
        database: str = "default",
        classifier: SensitivityClassifier | None = None,
    ) -> None:
        super().__init__(classifier)
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._database = database
        self._base_url = f"http://{host}:{port}"

    async def _query(self, sql: str) -> str:
        """执行 ClickHouse HTTP 查询，返回原始文本响应。

        Args:
            sql: SQL 查询语句。

        Returns:
            ClickHouse 响应文本。

        Raises:
            ExternalDependencyError: 查询失败。
        """
        params: dict[str, str] = {
            "query": sql,
            "user": self._user,
            "database": self._database,
        }
        if self._password:
            params["password"] = self._password

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.get(self._base_url, params=params)
                response.raise_for_status()
                return response.text
        except httpx.TimeoutException as exc:
            raise ExternalDependencyError(f"ClickHouse 查询超时: {sql[:100]}") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            detail = exc.response.text[:200]
            raise ExternalDependencyError(
                f"ClickHouse 查询失败 (status={status}): {detail}"
            ) from exc
        except httpx.ConnectError as exc:
            raise ExternalDependencyError(f"ClickHouse 连接失败: {self._base_url}") from exc

    async def collect(self, source: Any) -> CollectResult:
        source_id = getattr(source, "source_id", "?")
        database = getattr(source, "domain", self._database)

        # 获取表列表
        try:
            tables_text = await self._query(
                f"SELECT name FROM system.tables WHERE database = '{database}' FORMAT TabSeparated"
            )
        except Exception as exc:
            raise ExternalDependencyError(f"采集源 {source_id} 获取表列表失败: {exc}") from exc

        table_names = [line.strip() for line in tables_text.strip().splitlines() if line.strip()]

        specs: list[CatalogSpec] = []
        failed_specs: list[FailedSpec] = []

        for tbl in table_names:
            if not tbl:
                continue
            try:
                columns_text = await self._query(
                    f"SELECT name, type FROM system.columns "
                    f"WHERE database = '{database}' AND table = '{tbl}' "
                    f"FORMAT TabSeparated"
                )
                columns = []
                for line in columns_text.strip().splitlines():
                    parts = line.strip().split("\t")
                    if len(parts) >= 2:
                        columns.append({"name": parts[0], "type": parts[1]})
                schema_json = {"columns": columns}
                specs.append(
                    CatalogSpec(entity_name=tbl, entity_type="TABLE", schema_json=schema_json)
                )
            except Exception as exc:
                logger.warning("采集源 %s 表 %s 字段失败: %s", source_id, tbl, exc)
                failed_specs.append(FailedSpec(entity_name=tbl, error=str(exc)))

        return CollectResult(specs=specs, failed_specs=failed_specs, source_id=source_id)


@registry.register("clickhouse")
def create_clickhouse_collector(cfg: dict[str, Any]) -> ClickHouseCollector:
    """ClickHouse 采集器工厂函数。"""
    return ClickHouseCollector(
        host=cfg.get("host", "127.0.0.1"),
        port=cfg.get("port", 8123),
        user=cfg.get("user", "default"),
        password=cfg.get("password", ""),
        database=cfg.get("database", "default"),
    )
