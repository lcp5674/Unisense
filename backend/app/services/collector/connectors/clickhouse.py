"""ClickHouse 连接器（对齐 TD §12.1 / spec FR-001）。

通过 ClickHouse HTTP API (8123 端口) 查询 system.tables + system.columns，
无需安装 clickhouse-driver。

- HTTP API: GET http://{host}:8123/?query=SQL
- 单表 try/catch 跳过容错
- 生产语义（FR-030）：按 connection_config.database 过滤；为空时枚举全部非系统库
- @registry.register("clickhouse") 注册
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

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

logger = logging.getLogger("unisense.collector.connectors.clickhouse")

_CLICKHOUSE_SYSTEM_DBS = ("system", "information_schema", "INFORMATION_SCHEMA", "default")


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

        P1-5：凭据经 HTTP Basic Auth 头传递（而非 URL query 参数），
        避免密码进入 ClickHouse / 代理访问日志。

        Args:
            sql: SQL 查询语句。

        Returns:
            ClickHouse 响应文本。

        Raises:
            ExternalDependencyError: 查询失败。
        """
        params: dict[str, str] = {
            "query": sql,
            "database": self._database,
        }
        # Basic Auth：user + password 走 Authorization 头（httpx auth 元组）
        auth = (self._user, self._password)

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.get(self._base_url, params=params, auth=auth)
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

        # 生产语义：database 为空时枚举全部非系统库
        if self._database:
            databases = [self._database]
        else:
            try:
                dbs_text = await self._query(
                    "SELECT name FROM system.databases FORMAT TabSeparated"
                )
                databases = [
                    d.strip()
                    for d in dbs_text.strip().splitlines()
                    if d.strip() and d.strip() not in _CLICKHOUSE_SYSTEM_DBS
                ]
            except Exception as exc:
                raise ExternalDependencyError(
                    f"采集源 {source_id} 枚举数据库失败: {exc}"
                ) from exc

        specs: list[CatalogSpec] = []
        failed_specs: list[FailedSpec] = []

        for database in databases:
            safe_db = self._safe_ident(database)
            # 获取表列表（P0-6：增量模式仅取水位后 metadata_modification_time 变更的表）
            try:
                incremental = (
                    getattr(self, "_incremental_mode", "FULL") == "INCREMENTAL"
                    and getattr(self, "_incremental_watermark", None) is not None
                )
                if incremental:
                    watermark_ts = self._incremental_watermark
                    wm = watermark_ts.strftime("%Y-%m-%d %H:%M:%S") if watermark_ts else ""
                    tables_text = await self._query(
                        f"SELECT name FROM system.tables WHERE database = '{safe_db}' "
                        f"AND metadata_modification_time > '{wm}' "
                        f"FORMAT TabSeparated"
                    )
                else:
                    tables_text = await self._query(
                        f"SELECT name FROM system.tables WHERE database = '{safe_db}' "
                        f"FORMAT TabSeparated"
                    )
            except Exception as exc:
                logger.warning("采集源 %s 库 %s 表列表失败: %s", source_id, database, exc)
                continue

            table_names = [
                line.strip() for line in tables_text.strip().splitlines() if line.strip()
            ]

            for tbl in table_names:
                if not tbl:
                    continue
                entity_name = f"{database}.{tbl}" if not self._database else tbl
                try:
                    columns_text = await self._query(
                        f"SELECT name, type FROM system.columns "
                        f"WHERE database = '{safe_db}' AND table = '{self._safe_ident(tbl)}' "
                        f"FORMAT TabSeparated"
                    )
                    columns = []
                    for line in columns_text.strip().splitlines():
                        parts = line.strip().split("\t")
                        if len(parts) >= 2:
                            columns.append({"name": parts[0], "type": parts[1]})
                    schema_json = {"columns": columns}
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
        """轻量探活：SELECT 1（ClickHouse HTTP 接口）。"""
        start = time.monotonic()
        try:
            await self._query("SELECT 1")
            return ProbeResult(ok=True, latency_ms=int((time.monotonic() - start) * 1000))
        except Exception as exc:
            return ProbeResult(
                ok=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                error=str(exc),
            )

    @staticmethod
    def _safe_ident(name: str) -> str:
        """校验库/表名为合法标识符，防止拼入 SQL 造成注入。

        P2-6: 允许 ``-``（ClickHouse 常见表名含连字符），不允许 ``.``（分隔符）。
        """
        import re

        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ExternalDependencyError(f"非法标识符: {name!r}")
        return name


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
