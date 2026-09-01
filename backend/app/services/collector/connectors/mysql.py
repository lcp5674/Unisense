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
import fnmatch
import logging
import re
import time
from collections.abc import Awaitable
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

#: 每批采样列数上限（控制单条采样 SQL 长度；全字段采样按批拆分查询）
_SAMPLE_BATCH = 20

#: MySQL 标识符合法字符（反引号包裹安全，含 $ 允许；- 在 MySQL 表/列名中
#: 合法且经反引号包裹无歧义，与 ClickHouse/Hive 的采样标识符规则对齐）
_IDENT_RE = re.compile(r"^[A-Za-z0-9_$-]+$")


def _matches_any(name: str, patterns: list[str] | None) -> bool:
    """fnmatch 风格匹配：name 命中任一 pattern 即返回 True。"""
    if not patterns:
        return False
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


class SqlalchemyConnector:
    """基于 SQLAlchemy 异步引擎的真实连接器（含超时配置）。"""

    def __init__(
        self,
        db_url: str | URL,
        *,
        connect_timeout: int = 10,
        query_timeout: int = 60,
    ) -> None:
        # 按驱动分支注入 connect_args（HIGH-3）：asyncpg.connect 无 connect_timeout
        # 参数（用 timeout），aiomysql 接受 connect_timeout。若对 asyncpg 透传
        # connect_timeout，首次建连即 TypeError，Postgres 采集/探活整体不可用。
        drivername = ""
        if isinstance(db_url, URL):
            drivername = db_url.drivername or ""
        elif isinstance(db_url, str):
            drivername = db_url.split("://", 1)[0] if "://" in db_url else db_url
        connect_args = (
            {"timeout": connect_timeout}
            if "asyncpg" in drivername
            else {"connect_timeout": connect_timeout}
        )
        self._engine: AsyncEngine = create_async_engine(
            db_url,
            pool_pre_ping=True,
            connect_args=connect_args,
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
            # USE/SET 等无结果集语句：returns_rows=False → 返回空列表（供只读查询工作台
            # 执行 USE 等会话语句时优雅返回，而非抛「This result object does not return rows」）
            if not getattr(result, "returns_rows", True):
                return []
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
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> None:
        super().__init__(classifier)
        self._connector = connector
        self._database = database or None
        self._include_patterns = include_patterns
        self._exclude_patterns = exclude_patterns

    def _keep_table(self, entity_name: str) -> bool:
        """按 include/exclude 白黑名单过滤表（fnmatch 风格，include 优先）。

        - include_patterns 非空且命中：直接保留（即便同时命中黑名单——白名单优先）；
        - include_patterns 非空且未命中：拒绝；
        - include_patterns 为空/None：按 exclude_patterns 排除（命中即丢弃）；
        - 两者均空/None：保留全部。
        """
        if self._include_patterns and _matches_any(entity_name, self._include_patterns):
            return True
        if self._include_patterns:
            return False
        return not (
            self._exclude_patterns and _matches_any(entity_name, self._exclude_patterns)
        )

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

    async def list_tables(self, databases: list[str] | None = None) -> dict[str, list[str]]:
        """枚举指定库（或全部非系统库）下的 BASE TABLE，按库分组（供前端级联选表）。

        ``databases`` 为空时回退枚举全部非系统库；连接器不支持枚举表（如 Kafka）
        由基类返回空字典，前端隐藏表级选择区。单条 ``table_schema IN (...)``
        批量查询（复用连接池、无逐库往返），库多时从 N 次查询收敛为 1 次。
        """
        schemas = list(databases) if databases else await self._list_schemas()
        tables_by_db: dict[str, list[str]] = {}
        if not schemas:
            return tables_by_db
        rows = await self._connector.query(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema IN :schemas AND table_type = :ttype "
            "ORDER BY table_schema, table_name",
            {"schemas": tuple(schemas), "ttype": "BASE TABLE"},
        )
        for r in rows:
            schema = r.get("table_schema")
            tbl = r.get("table_name")
            if schema and tbl:
                tables_by_db.setdefault(str(schema), []).append(str(tbl))
        return tables_by_db

    async def query(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """执行任意只读查询（委托 SqlalchemyConnector，供维度枚举值拉取等复用）。

        SQL 须为只读 SELECT；连接复用 ``_connector`` 的超时与连接池配置。
        """
        return await self._connector.query(sql, params)

    async def _sample_columns(
        self, entity_name: str, columns: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """对字段执行**行对齐**采样（记录视图 + PII 精度增强）。

        一次 ``SELECT c1,...,ck FROM tbl LIMIT n`` 取整表全部列：刻意不加
        ``WHERE col IS NOT NULL`` 过滤——那会让各列各自取到不同行的非空值，
        拼出的「第 i 条样本」在源库并不存在（假记录）。行视图经 ``_mask_sample``
        打码返回；列式 ``sample`` 派生、稀疏列补采、整表失败降级逐列，均由
        基类 ``_sample_rows_aligned`` 统一处理。库/表/列名经标识符白名单校验
        （反引号包裹，防注入）。

        Args:
            entity_name: 实体（库.表）名。
            columns: 字段定义列表（name/type/comment 等）。

        Returns:
            打码后的行视图列表（未开启采样/非法标识符时为 ``[]``）。
        """
        if not self._sampling_max_rows or not columns or "." not in entity_name:
            return []
        schema, tbl = entity_name.rsplit(".", 1)
        if not _IDENT_RE.match(schema) or not _IDENT_RE.match(tbl):
            return []
        safe: list[tuple[dict[str, Any], str]] = []
        for col in columns:
            name = str(col.get("name", "")).strip()
            if name and _IDENT_RE.match(name):
                safe.append((col, name))
        if not safe:
            return []

        def select_sql(names: list[str], n: int) -> str:
            cols = ",".join(f"`{x}`" for x in names)
            return f"SELECT {cols} FROM `{schema}`.`{tbl}` LIMIT {n}"

        def one_sql(name: str, n: int) -> str:
            return (
                f"SELECT `{name}` FROM `{schema}`.`{tbl}` "
                f"WHERE `{name}` IS NOT NULL LIMIT {n}"
            )

        def run_query(sql: str, _names: list[str]) -> Awaitable[list[dict[str, Any]]]:
            # 驱动已返回行字典，无需按列序还原（``_names`` 仅为元组型驱动保留）
            return self._connector.query(sql)

        return await self._sample_rows_aligned(
            entity_name, safe, select_sql, one_sql, run_query
        )

    async def sample_columns(
        self, entity_name: str, schema_json: dict[str, Any]
    ) -> dict[str, Any]:
        """采样入口（手动触发/单表立即采样路径）。

        ``collect`` 内部已在组装 schema 时调用 ``_sample_columns``；本方法供
        service 层「不重跑全量采集、只对单表采样」时调用（复用连接池）。
        MySQL 协议兼容源（Doris/StarRocks 复用本类）均由此获得采样能力。
        """
        columns = schema_json.get("columns")
        if not isinstance(columns, list) or not columns:
            return schema_json
        rows = await self._sample_columns(entity_name, columns)
        if rows:
            schema_json["sample_rows"] = rows
        return schema_json

    async def collect_entity(self, source: Any, entity_name: str) -> CatalogSpec | None:
        """单表元数据刷新：仅查询目标表的列元数据，不触发全源扫描。

        ``entity_name`` 恒为 ``schema.table``（连接库为纯凭据，不再按连接库隐式解析
        schema）；裸表名无法定位 schema 时返回 None（由调用方回退全量采集）。
        """
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
        # PII 精度增强 + 样本记录视图：全字段行对齐采样（样本打码）
        schema_json: dict[str, Any] = {"columns": cols}
        if self._sampling_max_rows:
            sample_rows = await self._sample_columns(entity_name, cols)
            if sample_rows:
                schema_json["sample_rows"] = sample_rows
        return CatalogSpec(
            entity_name=entity_name,
            entity_type="TABLE",
            schema_json=schema_json,
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
            # 采集范围优先级：DataSource.databases（多目标库）→ 枚举全部非系统库。
            # 连接库 database 为纯连接凭据，不再隐式决定采集范围（方案 A）。
            target_dbs = getattr(self, "_databases", None)
            if target_dbs:
                schemas = list(target_dbs)
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

            # 采样阶段进度：整表查询可能很慢（数百张表 × 逐表采样），若只发
            # phase=start 前端会一直停在 0%。逐表发 sampling 进度（库内序号/总数
            # + 表名），让用户看到采集在推进。
            total_in_schema = len(tables)
            for idx, row in enumerate(tables, 1):
                tbl = row.get("table_name")
                if not tbl:
                    continue
                # 统一 库.表 命名避免跨库同名冲突（连接库为纯凭据，无单库裸表名模式）
                entity_name = f"{schema}.{tbl}"
                cols = columns_by_table.get(tbl, [])
                # PII 精度增强 + 样本记录视图：全字段行对齐采样（样本打码）
                schema_json: dict[str, Any] = {"columns": cols}
                if self._sampling_max_rows:
                    await self._notify_progress(
                        {
                            "phase": "sampling",
                            "index": idx,
                            "total": total_in_schema,
                            "entity_name": entity_name,
                            "message": f"采样 {idx}/{total_in_schema}：{entity_name}",
                        }
                    )
                    sample_rows = await self._sample_columns(entity_name, cols)
                    if sample_rows:
                        schema_json["sample_rows"] = sample_rows
                specs.append(
                    CatalogSpec(
                        entity_name=entity_name,
                        entity_type="TABLE",
                        schema_json=schema_json,
                    )
                )

        # 治理：按 include/exclude 白黑名单过滤扫描到的表（include 优先），
        # 并统计被过滤的表（方案 B：采集结果展示「过滤跳过 N 张表」）。
        kept_specs: list[CatalogSpec] = []
        filtered_names: list[str] = []
        for s in specs:
            if self._keep_table(s.entity_name):
                kept_specs.append(s)
            else:
                filtered_names.append(s.entity_name)
        specs = kept_specs

        return CollectResult(
            specs=specs,
            failed_specs=failed_specs,
            source_id=source_id,
            filtered_count=len(filtered_names),
            filtered_names=filtered_names,
        )

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
    """MySQL 采集器工厂函数。

    SSRF 防护：URL 一律由受控字段（host/port/user/password）构建，
    禁止任意 ``db_url`` 覆盖（防连接串内嵌任意主机）。
    """
    db_url = _build_mysql_url(cfg)
    connector = SqlalchemyConnector(db_url, connect_timeout=10, query_timeout=60)
    return InformationSchemaCollector(connector, database=cfg.get("database"))
