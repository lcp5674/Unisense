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
from app.services.collector.mojibake import contains_mojibake
from app.services.collector.placeholders import is_effective_comment
from app.services.collector.spi import (
    BaseCollector,
    CatalogSpec,
    CollectResult,
    FailedSpec,
    ProbeResult,
)

logger = logging.getLogger("unisense.collector.connectors.hive")

#: 每批采样列数上限（控制单条采样 SQL 长度；全字段采样按批拆分查询）
_SAMPLE_BATCH = 20


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

    def _connect(self) -> Any:
        """建立 pyhive 连接（同步阻塞，供 ``asyncio.to_thread`` 包装）。

        注意：pyhive 0.7.0 的 ``Connection.__init__`` **不接受 ``timeout`` 参数**
        （传了会 TypeError）——连接超时由 ``_execute`` 的
        ``asyncio.wait_for(connect_timeout)`` 承担，此处只传连接/认证参数。

        Returns:
            pyhive 连接对象。

        Raises:
            ExternalDependencyError: 连接失败（网络/认证）统一转 503（可重试）。
        """
        from pyhive import hive as pyhive_hive

        try:
            return pyhive_hive.connect(
                host=self._host,
                port=self._port,
                username=self._user or None,
                database=self._database or "default",
                auth=self._auth,
                password=self._password or None,
            )
        except Exception as exc:  # noqa: BLE001 - 连接失败（网络/认证）统一转 503
            raise ExternalDependencyError(
                f"Hive 连接失败 ({self._host}:{self._port}): {exc}"
            ) from exc

    def _query(self, conn: Any, sql: str) -> list[list[str]]:
        """在已建立连接上执行 SQL（同步阻塞，供 ``asyncio.to_thread`` 包装）。

        注意：本方法**不关闭连接**——连接生命周期由调用方管理（``_execute``
        单次路径自建自关；采集批量路径复用同一连接直到 schema 扫完），避免
        每张表新建 TCP+认证连接导致全量采集极慢。

        Args:
            conn: 由 ``_connect`` 建立的 pyhive 连接。
            sql: 要执行的 SQL。

        Returns:
            数据行列表（每行为字段字符串列表，不含表头）。

        Raises:
            ExternalDependencyError: 查询失败（语法/权限）统一转 503（可重试）。
        """
        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()
            return [[self._to_str(v) for v in row] for row in rows]
        except Exception as exc:  # noqa: BLE001 - 查询失败统一转 503
            raise ExternalDependencyError(f"Hive 查询失败: {exc}") from exc

    def _sync_query(self, sql: str) -> list[list[str]]:
        """同步执行 SQL（连接 + 查询一体），供测试与单次调用使用。

        自建连接并在查询后关闭（与 ``_execute`` 单次路径语义一致）；
        采集批量路径请用 ``_connect_managed`` + ``_execute(sql, conn=conn)``
        复用连接以避免每表一次握手。

        Args:
            sql: 要执行的 SQL。

        Returns:
            数据行列表（每行为字段字符串列表，不含表头）。

        Raises:
            ExternalDependencyError: 连接或查询失败（503 可重试）。
        """
        conn = self._connect()
        try:
            return self._query(conn, sql)
        finally:
            self._close(conn)

    @staticmethod
    def _to_str(value: Any) -> str:
        """DB-API 返回值转字符串（None → 空串，与 beeline 空输出一致）。"""
        return "" if value is None else str(value)

    async def _connect_managed(self) -> Any:
        """建立 pyhive 连接并施加连接超时（供 ``_execute`` 与采集批量复用）。

        Raises:
            ExternalDependencyError: 连接超时（503 可重试）。
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._connect), timeout=self._connect_timeout
            )
        except TimeoutError as exc:
            raise ExternalDependencyError(
                f"Hive 连接超时 ({self._connect_timeout}s): {self._host}:{self._port}"
            ) from exc

    @staticmethod
    def _close(conn: Any) -> None:
        """静默关闭连接（不抛异常，供线程池包装）。"""
        with contextlib.suppress(Exception):
            conn.close()

    async def _execute(self, sql: str, conn: Any | None = None) -> list[list[str]]:
        """经线程池执行 SQL（pyhive 阻塞 API），支持连接复用。

        - ``conn`` 为 None（单次调用：probe/枚举库）：自建连接并关闭，
          连接超时 ``connect_timeout`` 与查询超时 ``query_timeout`` 各自独立；
        - ``conn`` 非 None（采集批量）：复用调用方连接执行查询，仅施加
          查询超时，连接由调用方管理（本方法不关闭）。

        Args:
            sql: 要执行的 SQL。
            conn: 复用连接（None 时自建）。

        Returns:
            数据行列表。

        Raises:
            ExternalDependencyError: 连接超时/查询超时/执行失败（503 可重试）。
        """
        if conn is not None:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._query, conn, sql), timeout=self._query_timeout
                )
            except TimeoutError as exc:
                raise ExternalDependencyError(
                    f"Hive 查询超时 ({self._query_timeout}s): {sql}"
                ) from exc
        conn = await self._connect_managed()
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._query, conn, sql), timeout=self._query_timeout
            )
        except TimeoutError as exc:
            raise ExternalDependencyError(
                f"Hive 查询超时 ({self._query_timeout}s): {sql}"
            ) from exc
        finally:
            await asyncio.to_thread(self._close, conn)

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
            # 每个 schema 复用同一连接（消除每表一连接的极慢问题——wedata_tmp
            # 这类上千张临时表时，逐表 TCP+认证握手会让全量采集耗时数小时）。
            conn: Any | None = None
            try:
                conn = await self._connect_managed()
                table_rows = await self._execute(f"SHOW TABLES IN {safe_schema}", conn=conn)
            except Exception as exc:
                logger.warning("采集源 %s 库 %s 表列表失败: %s", source_id, schema, exc)
                failed_specs.append(FailedSpec(entity_name=schema, error=str(exc)))
                if conn is not None:
                    await asyncio.to_thread(self._close, conn)
                continue

            total_in_schema = len(table_rows)
            try:
                for idx, row in enumerate(table_rows, 1):
                    tbl = row[0] if row else None
                    if not tbl:
                        continue
                    tbl = tbl.strip()
                    single_db = bool(self._database) and not getattr(self, "_databases", None)
                    entity_name = f"{schema}.{tbl}" if not single_db else tbl
                    try:
                        desc_rows = await self._execute(
                            f"DESCRIBE {safe_schema}.{self._safe_ident(tbl)}", conn=conn
                        )
                        columns = []
                        for desc_row in desc_rows:
                            # DESCRIBE schema.table 输出格式（pyhive 行）：
                            # 列名 \t 类型 \t [注释]
                            # 注释列可能为空（仅两列）。
                            if len(desc_row) >= 2:
                                col_name = desc_row[0].strip()
                                col_type = desc_row[1].strip()
                                # 注释列可能为空（仅两列）；Spark Thrift 对无注释列
                                # 返回占位串 "from deserializer"，归一化为空串，
                                # 避免被当作真实 DDL 注释（批量推断全跳过）
                                col_comment = (
                                    desc_row[2].strip() if len(desc_row) >= 3 else ""
                                )
                                if not is_effective_comment(col_comment):
                                    col_comment = ""
                                # 源端注释编码乱码检测（GBK→UTF-8 替换残留）：
                                # 命中即登记，随 schema_json 标记并在采集日志告警，
                                # 避免乱码注释静默入库后无法溯源。
                                if col_comment and contains_mojibake(col_comment):
                                    self._note_mojibake_field(col_name, comment=True)
                                # 跳过分区信息和表级信息（空行或非列条目）
                                if col_name and not col_name.startswith("#"):
                                    columns.append(
                                        {
                                            "name": col_name,
                                            "type": col_type,
                                            "comment": col_comment,
                                        }
                                    )
                        # PII 精度增强：全字段采样（复用同一连接，批量列查询）。
                        # 行视图（返回值）写入 schema_json——Hive 大表采样在
                        # Tez/Spark 引擎下走 fetch-operator（非聚合 SELECT LIMIT
                        # 不触发全表 MapReduce），成本受控，无需 TABLESAMPLE
                        # （BUCKET 采样仅对分桶表有效、非分桶表会直接报错）。
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
                            sample_rows = await self._sample_columns(
                                entity_name, columns, conn
                            )
                            schema_json = {"columns": columns}
                            self._annotate_mojibake(schema_json, source_id, entity_name)
                            if sample_rows:
                                schema_json["sample_rows"] = sample_rows
                        else:
                            schema_json = {"columns": columns}
                            self._annotate_mojibake(schema_json, source_id, entity_name)
                        specs.append(
                            CatalogSpec(
                                entity_name=entity_name,
                                entity_type="TABLE",
                                schema_json=schema_json,
                            )
                        )
                    except Exception as exc:
                        logger.warning(
                            "采集源 %s 表 %s 字段失败: %s", source_id, entity_name, exc
                        )
                        failed_specs.append(
                            FailedSpec(entity_name=entity_name, error=str(exc))
                        )
            finally:
                if conn is not None:
                    await asyncio.to_thread(self._close, conn)

        return CollectResult(specs=specs, failed_specs=failed_specs, source_id=source_id)

    def _annotate_mojibake(
        self, schema_json: dict[str, Any], source_id: str, entity_name: str
    ) -> None:
        """把本表累积的源端编码乱码标记写入 schema_json 并告警。

        命中时：① ``schema_json["mojibake"]`` 记录 sample_fields/comment_fields
        （前端据此展示「源端存在编码乱码」提示）；② 采集日志 warning 告警，
        明确「源端 GBK→UTF-8 替换、信息已丢失、请在 Hive 侧修复注释/数据后重采」，
        避免乱码静默入库无法溯源。
        """
        mojibake = self._take_mojibake()
        if not mojibake:
            return
        schema_json["mojibake"] = mojibake
        logger.warning(
            "采集源 %s 表 %s 检测到源端编码乱码（GBK→UTF-8 替换，信息已在源头丢失，"
            "请在 Hive 侧修复注释/数据后重新采集）: %s",
            source_id,
            entity_name,
            mojibake,
        )

    async def list_databases(self) -> list[str]:
        """枚举实例下全部库（SHOW DATABASES，供创建数据源时选择目标库）。

        与 MySQL 连接器的「非系统库」语义对齐：Hive 无 information_schema 等
        系统库，``default`` 也可能承载业务表，故返回全部库由前端展示选择。

        Returns:
            库名列表。

        Raises:
            ExternalDependencyError: 连接/查询失败（503 可重试）。
        """
        rows = await self._execute("SHOW DATABASES")
        return [row[0].strip() for row in rows if row and row[0].strip()]

    async def list_tables(self, databases: list[str] | None = None) -> dict[str, list[str]]:
        """枚举指定库（或全部库）下的表，按库分组（供前端级联选表）。

        Args:
            databases: 要枚举表的库列表；空则由 ``list_databases`` 回退全部库。

        Returns:
            ``{库: [表名...]}``；非法库名跳过不拖垮整批。

        Raises:
            ExternalDependencyError: 连接/查询超时（503 可重试，前端降级为手动输入）。
        """
        schemas = list(databases) if databases else await self.list_databases()

        async def _enumerate() -> dict[str, list[str]]:
            # 复用单连接枚举全部库：避免每库一次 pyhive 握手——库多时串行握手
            # 是前端级联选表「长时间无选项」的主因（N 库 = N 次 TCP+SASL+session）。
            conn = await self._connect_managed()
            try:
                tables_by_db: dict[str, list[str]] = {}
                for schema in schemas:
                    try:
                        safe_schema = self._safe_ident(schema)
                    except Exception:  # noqa: BLE001 - 非法库名跳过
                        continue
                    rows = await self._execute(f"SHOW TABLES IN {safe_schema}", conn=conn)
                    tables_by_db[schema] = [
                        row[0].strip() for row in rows if row and row[0].strip()
                    ]
                return tables_by_db
            finally:
                await asyncio.to_thread(self._close, conn)

        # 整体兜底超时：连接复用后单库查询通常 <1s，但实例库多 / HS2 慢时仍需
        # 上限，避免前端长时间空等（超时由调用方降级为手动输入表名）。
        timeout = min(self._query_timeout, 30)
        try:
            return await asyncio.wait_for(_enumerate(), timeout=timeout)
        except TimeoutError as exc:
            raise ExternalDependencyError(
                f"Hive 枚举表超时 ({timeout}s, {len(schemas)} 个库)"
            ) from exc

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

    # ---- 样本采样（PII 精度增强：name+sample 双验证）----

    async def _sample_columns(
        self,
        entity_name: str,
        columns: list[dict[str, Any]],
        conn: Any,
    ) -> list[dict[str, str]]:
        """对字段执行**行对齐**采样（复用调用方连接，避免每表新握手）。

        一次 ``SELECT c1,...,ck FROM tbl LIMIT n`` 取整表全部列，不加非空过滤，
        保证「一行 = 源库一条真实记录」。列名经 ``_safe_ident`` 校验（防注入），
        非法列跳过。行视图经 ``_mask_sample`` 打码返回；列式 ``sample`` 派生、
        稀疏列补采、整表失败降级逐列，均由基类统一处理。

        Args:
            entity_name: 实体（库.表）名。
            columns: 字段定义列表（name/type/comment）。
            conn: 复用连接（collect 已建立，避免每表 TCP+认证握手）。

        Returns:
            打码后的行视图列表（未开启采样/非法标识符时为 ``[]``）。
        """
        if not self._sampling_max_rows or not columns or "." not in entity_name:
            return []
        schema, tbl = entity_name.rsplit(".", 1)
        if not schema or not tbl:
            return []
        try:
            ident = f"{self._safe_ident(schema)}.{self._safe_ident(tbl)}"
        except Exception:  # noqa: BLE001 - 非法标识符跳过采样（元数据采集不受影响）
            return []
        # 行视图的 key 必须是**原始列名**（``_safe_ident`` 会加反引号，作 key 会
        # 让前端无法与 columns[].name 对齐）；转义只发生在 SQL 构造处。
        safe: list[tuple[dict[str, Any], str]] = []
        for col in columns:
            name = str(col.get("name", "")).strip()
            if not name:
                continue
            try:
                self._safe_ident(name)
            except Exception:  # noqa: BLE001 - 非法列名跳过该列采样
                continue
            safe.append((col, name))
        if not safe:
            return []

        def select_sql(names: list[str], n: int) -> str:
            cols = ",".join(self._safe_ident(x) for x in names)
            return f"SELECT {cols} FROM {ident} LIMIT {n}"

        def one_sql(name: str, n: int) -> str:
            col = self._safe_ident(name)
            return f"SELECT {col} FROM {ident} WHERE {col} IS NOT NULL LIMIT {n}"

        async def run_query(sql: str, names: list[str]) -> list[dict[str, Any]]:
            """pyhive 返回**元组**行，须按本次查询列序还原为字段字典。"""
            tuples = await self._execute(sql, conn=conn)
            rows: list[dict[str, Any]] = []
            for tup in tuples:
                rows.append(
                    {
                        name: (tup[idx] if idx < len(tup) else None)
                        for idx, name in enumerate(names)
                    }
                )
            return rows

        return await self._sample_rows_aligned(
            entity_name, safe, select_sql, one_sql, run_query
        )

    async def sample_columns(
        self, entity_name: str, schema_json: dict[str, Any]
    ) -> dict[str, Any]:
        """采样入口（外部调用/手动触发路径）：自建连接采样后关闭。

        ``collect`` 内部走 ``_sample_columns(conn=...)`` 复用连接；本方法供
        service 层在需要单独采样时调用（自建连接）。
        """
        columns = schema_json.get("columns")
        if not self._sampling_max_rows or not isinstance(columns, list) or not columns:
            return schema_json
        conn = await self._connect_managed()
        try:
            rows = await self._sample_columns(entity_name, columns, conn)
        finally:
            await asyncio.to_thread(self._close, conn)
        if rows:
            schema_json["sample_rows"] = rows
        return schema_json


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
