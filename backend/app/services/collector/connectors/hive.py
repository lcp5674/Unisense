"""Hive 连接器（对齐 TD §12.1 / spec FR-001）。

经 pyhive（纯 Python Thrift 客户端）直连 HiveServer2，**不再依赖 beeline CLI**——
后端零外部命令依赖，密码经连接参数传递（不经命令行/临时文件，无 ``ps`` 暴露面）。

- 无增量支持，始终全量
- 单表 try/catch 跳过容错
- 生产语义（FR-030）：采集范围 = 目标库列表（DataSource.databases）→ 显式连接库 →
  未配置任何库时枚举全部库；连接库 ``connection_config.database`` 仅作连接凭据
  （pyhive 会话库），不参与采集范围（对齐前端「目标库留空=全部库」）
- 认证：有密码默认 LDAP（HiveServer2 标准密码认证，pyhive 要求 password 仅在
  LDAP/CUSTOM 模式设置），无密码走 NONE；可经 ``auth`` 配置显式覆盖
- @registry.register("hive") 注册

pyhive 调用示例::

    from pyhive import hive
    conn = hive.connect(host="hive-host", port=10000, username="u",
                        password="p", auth="LDAP")
    conn.cursor().execute("SHOW TABLES IN schema")
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import Any

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

logger = logging.getLogger("unisense.collector.connectors.hive")


class HiveCollector(BaseCollector):
    """Hive 采集器：经 pyhive 直连 HiveServer2（纯 Python，无 CLI 依赖）。"""

    def __init__(
        self,
        host: str,
        port: int = 10000,
        user: str = "",
        password: str = "",
        database: str | None = None,
        auth: str | None = None,
        connect_timeout: int = 10,
        query_timeout: int = 120,
        classifier: SensitivityClassifier | None = None,
    ) -> None:
        super().__init__(classifier)
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        # 连接库 database 仅作连接凭据，**不参与采集范围**——未显式配置时保持
        # None（collect 走枚举全部库），与前端「连接库仅作连接凭据；目标库留空=全部库」
        # 语义及 hive_metastore 对齐。
        self._database = database or None
        # 认证方式：显式配置优先；否则按是否有密码推断——有密码走 LDAP
        # （HiveServer2 标准密码认证），无密码走 NONE。
        self._auth = auth or ("LDAP" if self._password else "NONE")
        self._connect_timeout = connect_timeout
        self._query_timeout = query_timeout

    def _sync_query(self, sql: str) -> list[list[str]]:
        """同步执行 SQL（pyhive 阻塞 API），供 ``asyncio.to_thread`` 包装。

        Args:
            sql: 要执行的 SQL。

        Returns:
            数据行列表（每行为字段字符串列表，不含表头）。

        Raises:
            ExternalDependencyError: 连接或查询失败（503 可重试）。
        """
        from pyhive import hive as pyhive_hive

        conn = None
        try:
            conn = pyhive_hive.connect(
                host=self._host,
                port=self._port,
                username=self._user or None,
                database=self._database or "default",
                auth=self._auth,
                password=self._password or None,
                timeout=self._connect_timeout,
            )
        except Exception as exc:  # noqa: BLE001 - 连接失败（网络/认证）统一转 503
            raise ExternalDependencyError(
                f"Hive 连接失败 ({self._host}:{self._port}): {exc}"
            ) from exc
        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()
            return [[self._to_str(v) for v in row] for row in rows]
        except Exception as exc:  # noqa: BLE001 - 查询失败统一转 503
            raise ExternalDependencyError(f"Hive 查询失败: {exc}") from exc
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()

    @staticmethod
    def _to_str(value: Any) -> str:
        """DB-API 返回值转字符串（None → 空串，与 beeline 空输出一致）。"""
        return "" if value is None else str(value)

    async def _execute(self, sql: str) -> list[list[str]]:
        """经线程池执行 SQL（pyhive 阻塞 API），带查询超时。

        Args:
            sql: 要执行的 SQL。

        Returns:
            数据行列表。

        Raises:
            ExternalDependencyError: 查询超时或执行失败。
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._sync_query, sql), timeout=self._query_timeout
            )
        except TimeoutError as exc:
            raise ExternalDependencyError(
                f"Hive 查询超时 ({self._query_timeout}s): {sql}"
            ) from exc

    async def collect(self, source: Any) -> CollectResult:
        source_id = getattr(source, "source_id", "?")

        # 采集范围优先级：DataSource.databases（多目标库）→ 显式连接库（单库，
        # 向后兼容裸表名实体）→ 枚举全部库（未配置任何库时，对齐前端「留空=全部库」）。
        # 连接库 database 不再是隐式采集范围：工厂未填时传 None，绝不默认扫 default 空库。
        if getattr(self, "_databases", None):
            schemas = list(self._databases)
        elif self._database:
            schemas = [self._database]
        else:
            try:
                db_rows = await self._execute("SHOW DATABASES")
                schemas = [row[0].strip() for row in db_rows if row and row[0].strip()]
            except Exception as exc:
                raise ExternalDependencyError(f"采集源 {source_id} 枚举数据库失败: {exc}") from exc

        specs: list[CatalogSpec] = []
        failed_specs: list[FailedSpec] = []

        for schema in schemas:
            # 库名校验包进 try：枚举出的非法库名（如含点号）跳过，不拖垮整批
            try:
                safe_schema = self._safe_ident(schema)
            except Exception as exc:  # noqa: BLE001 - 非法库名跳过并记录
                logger.warning("采集源 %s 库名非法跳过: %s (%s)", source_id, schema, exc)
                failed_specs.append(FailedSpec(entity_name=schema, error=str(exc)))
                continue
            # 获取表列表——失败记入 failed_specs（避免「全部失败却静默 0 表」难排查）
            try:
                table_rows = await self._execute(f"SHOW TABLES IN {safe_schema}")
            except Exception as exc:
                logger.warning("采集源 %s 库 %s 表列表失败: %s", source_id, schema, exc)
                failed_specs.append(FailedSpec(entity_name=schema, error=str(exc)))
                continue

            for row in table_rows:
                tbl = row[0] if row else None
                if not tbl:
                    continue
                tbl = tbl.strip()
                single_db = bool(self._database) and not getattr(self, "_databases", None)
                entity_name = f"{schema}.{tbl}" if not single_db else tbl
                try:
                    desc_rows = await self._execute(
                        f"DESCRIBE {safe_schema}.{self._safe_ident(tbl)}"
                    )
                    columns = []
                    for desc_row in desc_rows:
                        # DESCRIBE schema.table 输出格式（pyhive 行）：
                        # 列名 \t 类型 \t [注释]
                        # 注释列可能为空（仅两列）。
                        if len(desc_row) >= 2:
                            col_name = desc_row[0].strip()
                            col_type = desc_row[1].strip()
                            col_comment = desc_row[2].strip() if len(desc_row) >= 3 else ""
                            # 跳过分区信息和表级信息（空行或非列条目）
                            if col_name and not col_name.startswith("#"):
                                columns.append(
                                    {
                                        "name": col_name,
                                        "type": col_type,
                                        "comment": col_comment,
                                    }
                                )
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
        """轻量探活：SELECT 1（经 pyhive 直连）。"""
        start = time.monotonic()
        try:
            await self._execute("SELECT 1")
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

        P2-6: 允许 ``-``（Hive 常见表名含连字符），不允许 ``.``（分隔符）。
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ExternalDependencyError(f"非法标识符: {name!r}")
        return name


@registry.register("hive")
def create_hive_collector(cfg: dict[str, Any]) -> HiveCollector:
    """Hive 采集器工厂函数。

    连接库 ``database`` 仅作连接凭据：未填时为 None（pyhive 会话库用 default 兜底），
    采集范围由目标库列表/全库枚举决定——避免「默认只扫 default 空库 → 注册 0」。
    ``auth`` 可选覆盖认证方式（缺省按有无密码推断 LDAP/NONE）。
    """
    return HiveCollector(
        host=cfg.get("host", "127.0.0.1"),
        port=cfg.get("port", 10000),
        user=cfg.get("user", ""),
        password=cfg.get("password", ""),
        database=cfg.get("database") or None,
        auth=cfg.get("auth") or None,
    )
