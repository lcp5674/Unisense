"""Hive 连接器（对齐 TD §12.1 / spec FR-001）。

通过 asyncio.create_subprocess_exec 调用 beeline CLI 执行查询，
解析表格输出为结构化数据。

- 无增量支持，始终全量
- 单表 try/catch 跳过容错
- 生产语义（FR-030）：按 connection_config.database 过滤；为空时枚举全部库
- @registry.register("hive") 注册

beeline 命令示例::

    beeline -u jdbc:hive2://host:10000 -e "SHOW TABLES IN schema"
    beeline -u jdbc:hive2://host:10000 -e "DESCRIBE schema.table"
"""

from __future__ import annotations

import asyncio
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
    """Hive 采集器：通过 beeline CLI 异步调用 HiveServer2。"""

    def __init__(
        self,
        host: str,
        port: int = 10000,
        user: str = "",
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
        self._jdbc_url = f"jdbc:hive2://{host}:{port}/{database}"

    async def _execute(self, sql: str) -> list[list[str]]:
        """通过 beeline CLI 执行 SQL，解析输出为表格数据。

        P1-6: 密码经临时文件（``--password-file``，0600 权限）传递，
        避免经命令行 ``-p`` 暴露在 ``ps`` 进程列表中。

        Args:
            sql: 要执行的 SQL 语句。

        Returns:
            解析后的行列表（每行为字段列表）。

        Raises:
            ExternalDependencyError: beeline 执行失败。
        """
        args = ["beeline", "-u", self._jdbc_url]
        password_file: str | None = None
        if self._user:
            args.extend(["-n", self._user])
        if self._password:
            import os
            import tempfile

            fd, password_file = tempfile.mkstemp(prefix="beeline_pwd_")
            os.write(fd, self._password.encode("utf-8"))
            os.close(fd)
            os.chmod(password_file, 0o600)
            args.extend(["--password-file", password_file])
        args.extend(["-e", sql, "--outputformat=table2"])

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="replace").strip()
                raise ExternalDependencyError(f"beeline 执行失败 (rc={proc.returncode}): {err_msg}")
        except TimeoutError as exc:
            raise ExternalDependencyError(f"beeline 执行超时 (120s): {sql}") from exc
        except FileNotFoundError as exc:
            raise ExternalDependencyError("beeline 命令不可用，请确认已安装 Hive 客户端") from exc
        finally:
            if password_file is not None:
                import contextlib
                import os

                with contextlib.suppress(OSError):
                    os.unlink(password_file)

        # 解析 table2 格式输出（以 | 分隔的表格）
        lines = stdout.decode("utf-8", errors="replace").strip().splitlines()
        rows: list[list[str]] = []
        for line in lines:
            # 跳过分隔行与空行
            stripped = line.strip()
            if not stripped or set(stripped) <= {"+", "-", "|"}:
                continue
            fields = [f.strip() for f in stripped.split("|") if f.strip()]
            if fields:
                rows.append(fields)
        # 第一行为表头，跳过
        return rows[1:] if len(rows) > 1 else []

    async def collect(self, source: Any) -> CollectResult:
        source_id = getattr(source, "source_id", "?")

        # 生产语义：database 为空时枚举全部库
        if self._database:
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
            safe_schema = self._safe_ident(schema)
            # 获取表列表
            try:
                table_rows = await self._execute(f"SHOW TABLES IN {safe_schema}")
            except Exception as exc:
                logger.warning("采集源 %s 库 %s 表列表失败: %s", source_id, schema, exc)
                continue

            for row in table_rows:
                tbl = row[0] if row else None
                if not tbl:
                    continue
                tbl = tbl.strip()
                entity_name = f"{schema}.{tbl}" if not self._database else tbl
                try:
                    desc_rows = await self._execute(
                        f"DESCRIBE {safe_schema}.{self._safe_ident(tbl)}"
                    )
                    columns = []
                    for desc_row in desc_rows:
                        # DESCRIBE schema.table 输出格式（TabSeparated）：
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
        """轻量探活：SELECT 1（经 beeline）。"""
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
    """Hive 采集器工厂函数。"""
    return HiveCollector(
        host=cfg.get("host", "127.0.0.1"),
        port=cfg.get("port", 10000),
        user=cfg.get("user", ""),
        password=cfg.get("password", ""),
        database=cfg.get("database", "default"),
    )
