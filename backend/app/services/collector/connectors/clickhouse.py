"""ClickHouse 连接器（对齐 TD §12.1 / spec FR-001）。

通过 ClickHouse HTTP API (8123 端口) 查询 system.tables + system.columns，
无需安装 clickhouse-driver。

- HTTP API: GET http://{host}:8123/?query=SQL
- 单表 try/catch 跳过容错
- 生产语义（FR-030）：按 connection_config.database 过滤；为空时枚举全部非系统库
- @registry.register("clickhouse") 注册
- 支持全字段样本采样（PII 精度增强：name+sample 双验证）
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable
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

#: 每批采样列数上限（控制单条采样 SQL 长度；全字段采样按批拆分查询）
_SAMPLE_BATCH = 20

#: ClickHouse 标识符合法字符（反引号包裹安全；与 _safe_ident 一致，允许连字符）
_COL_IDENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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
        # P1-3: httpx.AsyncClient 作为实例属性复用（单例），避免每次查询新建连接
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        """懒加载并复用单个 httpx.AsyncClient 实例（P1-3 单例复用）。"""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    async def _query(self, sql: str) -> str:
        """执行 ClickHouse HTTP 查询，返回原始文本响应。

        P1-5：凭据经 HTTP Basic Auth 头传递（而非 URL query 参数），
        避免密码进入 ClickHouse / 代理访问日志。

        P1-3：复用实例级 httpx.AsyncClient，避免每次查询重建连接。

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

        client = await self._ensure_client()
        try:
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

    async def __aenter__(self) -> ClickHouseCollector:
        """支持 `async with` 上下文管理，进入时确保 client 已建立。"""
        await self._ensure_client()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """退出上下文时释放 client 连接。"""
        await self.close()

    async def close(self) -> None:
        """关闭并释放底层 httpx.AsyncClient（P1-3 防连接泄漏）。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _unescape_tsv(value: str) -> str:
        """反转义 ClickHouse TabSeparated 字段（``\\t``/``\\n``/``\\r``/``\\\\``）。

        ClickHouse TabSeparated 默认对字段内的制表符/换行/回车/反斜杠做转义
        （``\\t``/``\\n``/``\\r``/``\\\\``），直接 ``split("\\t")`` 切分不会错位
        （字段分隔符是真实 tab，字段内 tab 是两字符转义序列），但**不反转义**
        会让含这些字符的样本值（如带换行的地址、带反斜杠的编码串）损坏。
        ``\\N``（NULL 字面量）不在此映射内，保留原样供调用方归约为 None。
        """
        if "\\" not in value:
            return value
        out: list[str] = []
        i = 0
        n = len(value)
        while i < n:
            ch = value[i]
            if ch == "\\" and i + 1 < n:
                nxt = value[i + 1]
                mapped = {"t": "\t", "n": "\n", "r": "\r", "\\": "\\"}.get(nxt)
                if mapped is not None:
                    out.append(mapped)
                    i += 2
                    continue
            out.append(ch)
            i += 1
        return "".join(out)

    @staticmethod
    def _parse_tsv(text: str, width: int) -> list[list[str]]:
        """解析 ClickHouse TabSeparated 响应为二维字符串数组。

        每行按制表符切分并对齐到 ``width`` 列（缺列补空串，多列截断）——
        采样只需按列序号取值，不做类型转换。字段内转义（``\\t``/``\\n`` 等）
        经 ``_unescape_tsv`` 还原，避免含特殊字符的注释/样本值损坏。

        Args:
            text: ClickHouse 原始响应文本。
            width: 期望列数（采样列数）。

        Returns:
            行列表（每行 width 个字符串）。
        """
        rows: list[list[str]] = []
        for line in text.strip().splitlines():
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < width:
                parts = parts + [""] * (width - len(parts))
            rows.append([ClickHouseCollector._unescape_tsv(p) for p in parts[:width]])
        return rows

    @staticmethod
    def _parse_tsv_named(text: str) -> list[dict[str, Any]]:
        """解析 ``TabSeparatedWithNames`` 响应为行字典（首行为列名）。

        用带列名格式而非 ``TabSeparated``，是为了让解析**自包含**：调用方无需
        外部传入列名与宽度即可还原每列归属，全列查询与单列补采可共用同一入口。
        ClickHouse 的 NULL 字面量 ``\\N`` 统一归约为 ``None``；字段内转义
        （``\\t``/``\\n`` 等）经 ``_unescape_tsv`` 还原。
        """
        lines = [ln for ln in text.strip().splitlines() if ln]
        if len(lines) < 2:
            return []
        names = lines[0].split("\t")
        rows: list[dict[str, Any]] = []
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) < len(names):
                parts = parts + [""] * (len(names) - len(parts))
            rows.append(
                {
                    name: (
                        None
                        if parts[idx] in ("", "\\N")
                        else ClickHouseCollector._unescape_tsv(parts[idx])
                    )
                    for idx, name in enumerate(names)
                }
            )
        return rows

    async def _query_rows(self, sql: str) -> list[dict[str, Any]]:
        """执行 SQL 并返回行字典（ClickHouse 文本响应 → 结构化行）。"""
        return self._parse_tsv_named(await self._query(sql))

    async def _sample_columns(
        self, entity_name: str, columns: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """对字段执行**行对齐**采样（记录视图 + PII 精度增强）。

        与 ``InformationSchemaCollector._sample_columns`` 同构：一次取整表全部列，
        不加非空过滤，保证「一行 = 源库一条真实记录」（避免跨行拼凑出假记录）。

        方言差异（ClickHouse HTTP API）：结果为文本而非行字典数组，故经
        ``_query_rows`` 按 ``TabSeparatedWithNames`` 还原；NULL 以 ``\\N``
        表示，由解析器归约为 ``None``（行视图中留空占位，避免列错位）。

        Args:
            entity_name: 实体名，形如 ``database.table``；单库模式下可为裸表名
                （此时回退 ``self._database`` 定位库）。
            columns: 字段定义列表（name/type/comment 等）。

        Returns:
            打码后的行视图列表（未开启采样/非法标识符时为 ``[]``）。
        """
        if not self._sampling_max_rows or not columns:
            return []
        if "." in entity_name:
            database, tbl = entity_name.rsplit(".", 1)
        else:
            database, tbl = self._database, entity_name
        if not database or not tbl:
            return []
        try:
            safe_db = self._safe_ident(database)
            safe_tbl = self._safe_ident(tbl)
        except ExternalDependencyError:
            return []
        safe: list[tuple[dict[str, Any], str]] = []
        for col in columns:
            name = str(col.get("name", "")).strip()
            if name and _COL_IDENT_RE.match(name):
                safe.append((col, name))
        if not safe:
            return []

        def select_sql(names: list[str], n: int) -> str:
            cols = ",".join(f"`{x}`" for x in names)
            return (
                f"SELECT {cols} FROM `{safe_db}`.`{safe_tbl}` "
                f"LIMIT {n} FORMAT TabSeparatedWithNames"
            )

        def one_sql(name: str, n: int) -> str:
            return (
                f"SELECT `{name}` FROM `{safe_db}`.`{safe_tbl}` "
                f"WHERE `{name}` IS NOT NULL LIMIT {n} FORMAT TabSeparatedWithNames"
            )

        def run_query(sql: str, _names: list[str]) -> Awaitable[list[dict[str, Any]]]:
            # TabSeparatedWithNames 自带列名，无需按列序还原
            return self._query_rows(sql)

        return await self._sample_rows_aligned(
            entity_name, safe, select_sql, one_sql, run_query
        )

    async def sample_columns(
        self, entity_name: str, schema_json: dict[str, Any]
    ) -> dict[str, Any]:
        """采样入口（手动触发/单表立即采样路径）。

        ``collect`` 内部已在组装 schema 时调用 ``_sample_columns``；本方法供
        service 层「不重跑全量采集、只对单表采样」时调用（HTTP API + 行视图）。
        """
        columns = schema_json.get("columns")
        if not isinstance(columns, list) or not columns:
            return schema_json
        rows = await self._sample_columns(entity_name, columns)
        if rows:
            schema_json["sample_rows"] = rows
        return schema_json

    async def collect_entity(self, source: Any, entity_name: str) -> CatalogSpec | None:
        """单表元数据刷新：仅查询目标表列元数据。

        仅支持单库模式（``self._database`` 非空）——多库枚举模式下无法从
        ``entity_name`` 可靠定位数据库，且 ``_query`` 受实例级 database 约束，
        回退 None 由调用方走全量采集。
        """
        if not self._database:
            return None
        if "." in entity_name:
            database, tbl = entity_name.rsplit(".", 1)
            if database != self._database:
                return None
        else:
            tbl = entity_name
        if not tbl:
            return None
        try:
            columns_text = await self._query(
                f"SELECT name, type, default_kind, default_expression, comment "
                f"FROM system.columns "
                f"WHERE database = '{self._safe_ident(self._database)}' "
                f"AND table = '{self._safe_ident(tbl)}' FORMAT TabSeparated"
            )
        except Exception as exc:
            raise ExternalDependencyError(f"刷新实体 {entity_name} 失败: {exc}") from exc
        cols = []
        for line in columns_text.strip().splitlines():
            parts = line.strip().split("\t")
            col_name = parts[0] if len(parts) >= 1 else ""
            col_type = parts[1] if len(parts) >= 2 else "unknown"
            default_kind = parts[2] if len(parts) >= 3 else ""
            default_expr = parts[3] if len(parts) >= 4 else ""
            comment_text = parts[4] if len(parts) >= 5 else ""
            if not col_name:
                continue
            col_default = default_expr if default_kind == "DEFAULT" else None
            cols.append(
                {
                    "name": col_name,
                    "type": col_type,
                    "comment": comment_text,
                    "default": col_default,
                }
            )
        if not cols:
            return None
        # PII 精度增强 + 样本记录视图：全字段行对齐采样（样本打码）
        schema_json: dict[str, Any] = {"columns": cols}
        if self._sampling_max_rows:
            sample_rows = await self._sample_columns(f"{self._database}.{tbl}", cols)
            if sample_rows:
                schema_json["sample_rows"] = sample_rows
        return CatalogSpec(
            entity_name=entity_name,
            entity_type="TABLE",
            schema_json=schema_json,
        )

    async def collect(self, source: Any) -> CollectResult:
        source_id = getattr(source, "source_id", "?")

        # 目标库优先级：DataSource.databases（多库）→ connection_config.database（单库）
        # → 枚举全部非系统库
        if getattr(self, "_databases", None):
            databases = list(self._databases)
        elif self._database:
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
                raise ExternalDependencyError(f"采集源 {source_id} 枚举数据库失败: {exc}") from exc

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

            total_in_schema = len(table_names)
            for idx, tbl in enumerate(table_names, 1):
                if not tbl:
                    continue
                single_db = bool(self._database) and not getattr(self, "_databases", None)
                entity_name = f"{database}.{tbl}" if not single_db else tbl
                try:
                    # P0: 补列注释（comment）和默认值（default_kind/default_expression），
                    # 完整 schema_json 供 PII 分类与下游消费。
                    columns_text = await self._query(
                        f"SELECT name, type, default_kind, default_expression, comment "
                        f"FROM system.columns "
                        f"WHERE database = '{safe_db}' AND table = '{self._safe_ident(tbl)}' "
                        f"FORMAT TabSeparated"
                    )
                    columns = []
                    for line in columns_text.strip().splitlines():
                        parts = line.strip().split("\t")
                        col_name = parts[0] if len(parts) >= 1 else ""
                        col_type = parts[1] if len(parts) >= 2 else "unknown"
                        default_kind = parts[2] if len(parts) >= 3 else ""
                        default_expr = parts[3] if len(parts) >= 4 else ""
                        comment_text = parts[4] if len(parts) >= 5 else ""
                        if not col_name:
                            continue
                        # 仅认 DEFAULT 类型，MATERIALIZED/EPHEMERAL/ALIAS 不是可写默认值
                        col_default = default_expr if default_kind == "DEFAULT" else None
                        columns.append(
                            {
                                "name": col_name,
                                "type": col_type,
                                "comment": comment_text,
                                "default": col_default,
                            }
                        )
                    # PII 精度增强 + 样本记录视图：全字段行对齐采样（样本打码）
                    schema_json: dict[str, Any] = {"columns": columns}
                    if self._sampling_max_rows:
                        # 采样阶段进度：逐表发 sampling 事件，避免前端停在 0%
                        await self._notify_progress(
                            {
                                "phase": "sampling",
                                "index": idx,
                                "total": total_in_schema,
                                "entity_name": entity_name,
                                "message": f"采样 {idx}/{total_in_schema}：{entity_name}",
                            }
                        )
                        sample_rows = await self._sample_columns(
                            f"{database}.{tbl}", columns
                        )
                        if sample_rows:
                            schema_json["sample_rows"] = sample_rows
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
