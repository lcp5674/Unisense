"""Hive Metastore 直连连接器（生产形态：HMS backend 为 MySQL）。

真实生产环境中 Hive 元数据存储在 MySQL（Hive Metastore 的 backend 库，
含 DBS/TBLS/SDS/COLUMNS_V2/PARTITIONS 等表）。本连接器以只读账号直连该
MySQL，从 metastore 表一次 JOIN 拿全量库/表/表描述/owner/存储/字段/字段描述，
规避 beeline/HiveServer2 依赖与逐表 ``DESCRIBE`` 的 N+1 查询。

关联语义（metastore 外键即关联）：
- ``DBS.NAME``（库）→ ``TBLS``（表）+ ``TBL_COMMENT``（表描述）+ ``OWNER`` + ``TBL_TYPE``
- ``TBLS.SD_ID`` → ``SDS``（存储 LOCATION）
- ``SDS.CD_ID`` → ``COLUMNS_V2``（字段 COLUMN_NAME/TYPE_NAME/COMMENT）

健壮性：
- 目标库范围：``DataSource.databases``（多库）→ 枚举全部非系统 DBS；连接库
  ``connection_config.database`` 为 HMS 库名（纯连接凭据），不参与采集范围。
- Hive 2.3+ 才有的 ``TBL_COMMENT`` 列 / Hive 2.x 才有的 ``COLUMNS_V2.INTEGER_IDX``
  列在旧版本缺失时降级查询（探测 Unknown column 后去掉该列重查）。
- SQL 全参数化（IN 用 ``:d0,:d1`` 占位符拼接），无标识符拼接注入面。

@registry.register("hive_metastore") 注册。
"""

from __future__ import annotations

import fnmatch
import logging
import time
from typing import Any

from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError

from app.core.exceptions import ExternalDependencyError
from app.services.collector.classifier import SensitivityClassifier
from app.services.collector.connectors.collector_registry import registry
from app.services.collector.connectors.mysql import SqlalchemyConnector
from app.services.collector.spi import (
    BaseCollector,
    CatalogSpec,
    CollectResult,
    FailedSpec,
    ProbeResult,
)

logger = logging.getLogger("unisense.collector.connectors.hive_metastore")

# HMS 库里可能出现的系统库（全库枚举时排除）
_EXCLUDE_DBS = frozenset({"information_schema", "sys", "mysql", "performance_schema"})

# Hive 表类型 → 平台实体类型（视图单独建模为 VIEW）
_VIEW_TYPES = frozenset({"VIRTUAL_VIEW", "MATERIALIZED_VIEW"})


def _matches_any(name: str, patterns: list[str] | None) -> bool:
    """fnmatch 风格匹配：name 命中任一 pattern 即返回 True。"""
    if not patterns:
        return False
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _in_placeholders(values: list[str]) -> tuple[str, dict[str, str]]:
    """为 IN 子句生成 ``:d0,:d1,...`` 占位符与参数映射（参数化安全）。"""
    marks: list[str] = []
    params: dict[str, str] = {}
    for i, v in enumerate(values):
        key = f"d{i}"
        params[key] = v
        marks.append(f":{key}")
    return ", ".join(marks), params


class HiveMetastoreCollector(BaseCollector):
    """HMS 直连采集器：从 metastore MySQL 库一次 JOIN 全量元数据。"""

    def __init__(
        self,
        connector: SqlalchemyConnector,
        classifier: SensitivityClassifier | None = None,
        *,
        database: str | None = None,
    ) -> None:
        super().__init__(classifier)
        self._connector = connector
        # database 为 HMS 库名（连接凭据），采集范围由 _databases/枚举决定
        self._database = database or None

    # ---- 库/表枚举（供前端级联选择 + collect 范围）----

    async def _list_schemas(self) -> list[str]:
        """枚举 HMS 中全部非系统库（DBS.NAME）。"""
        rows = await self._connector.query("SELECT NAME FROM DBS ORDER BY NAME")
        return [
            str(r["name"])
            for r in rows
            if r.get("name") and str(r["name"]) not in _EXCLUDE_DBS
        ]

    async def list_databases(self) -> list[str]:
        """枚举业务库（DBS.NAME，排除系统库），供前端「目标数据库」多选。"""
        return await self._list_schemas()

    async def list_tables(self, databases: list[str] | None = None) -> dict[str, list[str]]:
        """枚举指定业务库（或全部）下的表，按库分组（前端级联选表）。"""
        dbs = list(databases) if databases else await self._list_schemas()
        marks, params = _in_placeholders(dbs)
        rows = await self._connector.query(
            "SELECT d.NAME AS db_name, t.TBL_NAME FROM TBLS t "
            "JOIN DBS d ON d.DB_ID = t.DB_ID "
            f"WHERE d.NAME IN ({marks}) ORDER BY d.NAME, t.TBL_NAME",
            params,
        )
        result: dict[str, list[str]] = {}
        for r in rows:
            db = str(r["db_name"])
            tbl = str(r["tbl_name"])
            result.setdefault(db, []).append(tbl)
        return result

    # ---- 主查询（一次 JOIN）----

    async def _query_tables(
        self, dbs: list[str], *, include_comment: bool = True
    ) -> list[dict[str, Any]]:
        """查询目标库全部表 + 表描述 + owner + 存储位置。

        旧版 Hive（<2.3）无 ``TBLS.TBL_COMMENT`` 列时降级为不含该列重查。
        """
        marks, params = _in_placeholders(dbs)
        comment_col = "t.TBL_COMMENT, " if include_comment else ""
        sql = (
            "SELECT d.NAME AS db_name, t.TBL_NAME, t.TBL_TYPE, t.OWNER, "
            f"{comment_col}s.LOCATION "
            "FROM TBLS t "
            "JOIN DBS d ON d.DB_ID = t.DB_ID "
            "LEFT JOIN SDS s ON s.SD_ID = t.SD_ID "
            f"WHERE d.NAME IN ({marks}) ORDER BY d.NAME, t.TBL_NAME"
        )
        try:
            return await self._connector.query(sql, params)
        except OperationalError as exc:
            if include_comment and "TBL_COMMENT" in str(exc):
                return await self._query_tables(dbs, include_comment=False)
            raise

    async def _query_columns(
        self, dbs: list[str], *, order_by_idx: bool = True
    ) -> list[dict[str, Any]]:
        """查询目标库全部表的字段 + 类型 + 字段描述（COLUMNS_V2）。

        旧版 Hive（<2.0）无 ``COLUMNS_V2.INTEGER_IDX`` 列时降级按列名排序重查。
        """
        marks, params = _in_placeholders(dbs)
        order = "c.INTEGER_IDX" if order_by_idx else "c.COLUMN_NAME"
        sql = (
            "SELECT d.NAME AS db_name, t.TBL_NAME, c.COLUMN_NAME, c.TYPE_NAME, c.COMMENT "
            "FROM COLUMNS_V2 c "
            "JOIN SDS s ON s.CD_ID = c.CD_ID "
            "JOIN TBLS t ON t.SD_ID = s.SD_ID "
            "JOIN DBS d ON d.DB_ID = t.DB_ID "
            f"WHERE d.NAME IN ({marks}) ORDER BY d.NAME, t.TBL_NAME, {order}"
        )
        try:
            return await self._connector.query(sql, params)
        except OperationalError as exc:
            if order_by_idx and "INTEGER_IDX" in str(exc):
                return await self._query_columns(dbs, order_by_idx=False)
            raise

    # ---- 单表刷新 ----

    async def collect_entity(self, source: Any, entity_name: str) -> CatalogSpec | None:
        """单表元数据刷新：仅查目标表的字段与描述，不触发全源扫描。"""
        if "." not in entity_name:
            return None
        schema, tbl = entity_name.rsplit(".", 1)
        if not schema or not tbl:
            return None
        tbl_rows = await self._query_tables_one(schema, tbl)
        if not tbl_rows:
            return None
        info = tbl_rows[0]
        cols = await self._query_columns_one(schema, tbl)
        return self._build_spec(schema, tbl, info, cols)

    async def _query_tables_one(self, schema: str, tbl: str) -> list[dict[str, Any]]:
        sql = (
            "SELECT d.NAME AS db_name, t.TBL_NAME, t.TBL_TYPE, t.OWNER, "
            "t.TBL_COMMENT, s.LOCATION "
            "FROM TBLS t "
            "JOIN DBS d ON d.DB_ID = t.DB_ID "
            "LEFT JOIN SDS s ON s.SD_ID = t.SD_ID "
            "WHERE d.NAME = :db AND t.TBL_NAME = :tbl"
        )
        try:
            return await self._connector.query(sql, {"db": schema, "tbl": tbl})
        except OperationalError as exc:
            if "TBL_COMMENT" in str(exc):
                return await self._connector.query(
                    "SELECT d.NAME AS db_name, t.TBL_NAME, t.TBL_TYPE, t.OWNER, s.LOCATION "
                    "FROM TBLS t "
                    "JOIN DBS d ON d.DB_ID = t.DB_ID "
                    "LEFT JOIN SDS s ON s.SD_ID = t.SD_ID "
                    "WHERE d.NAME = :db AND t.TBL_NAME = :tbl",
                    {"db": schema, "tbl": tbl},
                )
            raise

    async def _query_columns_one(self, schema: str, tbl: str) -> list[dict[str, Any]]:
        sql = (
            "SELECT c.COLUMN_NAME, c.TYPE_NAME, c.COMMENT "
            "FROM COLUMNS_V2 c "
            "JOIN SDS s ON s.CD_ID = c.CD_ID "
            "JOIN TBLS t ON t.SD_ID = s.SD_ID "
            "JOIN DBS d ON d.DB_ID = t.DB_ID "
            "WHERE d.NAME = :db AND t.TBL_NAME = :tbl "
            "ORDER BY c.INTEGER_IDX"
        )
        try:
            return await self._connector.query(sql, {"db": schema, "tbl": tbl})
        except OperationalError as exc:
            if "INTEGER_IDX" in str(exc):
                return await self._connector.query(
                    "SELECT c.COLUMN_NAME, c.TYPE_NAME, c.COMMENT "
                    "FROM COLUMNS_V2 c "
                    "JOIN SDS s ON s.CD_ID = c.CD_ID "
                    "JOIN TBLS t ON t.SD_ID = s.SD_ID "
                    "JOIN DBS d ON d.DB_ID = t.DB_ID "
                    "WHERE d.NAME = :db AND t.TBL_NAME = :tbl "
                    "ORDER BY c.COLUMN_NAME",
                    {"db": schema, "tbl": tbl},
                )
            raise

    # ---- 组装 ----

    def _build_spec(
        self,
        schema: str,
        tbl: str,
        info: dict[str, Any],
        col_rows: list[dict[str, Any]],
    ) -> CatalogSpec:
        """组装 CatalogSpec：库.表 实体 + 字段（含描述）+ 表描述 + _meta。"""
        cols = [
            {
                "name": str(r.get("column_name") or ""),
                "type": str(r.get("type_name") or "unknown"),
                "comment": str(r.get("comment") or ""),
            }
            for r in col_rows
            if r.get("column_name")
        ]
        tbl_type = str(info.get("tbl_type") or "MANAGED_TABLE")
        entity_type = "VIEW" if tbl_type in _VIEW_TYPES else "TABLE"
        description = str(info.get("tbl_comment") or "").strip() or None
        meta: dict[str, Any] = {
            "owner": info.get("owner") or None,
            "table_type": tbl_type,
            "location": info.get("location") or None,
        }
        schema_json: dict[str, Any] = {"columns": cols}
        if any(meta.values()):
            schema_json["_meta"] = {k: v for k, v in meta.items() if v is not None}
        return CatalogSpec(
            entity_name=f"{schema}.{tbl}",
            entity_type=entity_type,
            schema_json=schema_json,
            description=description,
        )

    def _keep_table(self, entity_name: str) -> bool:
        """按 include/exclude 白黑名单过滤表（fnmatch，include 优先）。"""
        if self._include_patterns and _matches_any(entity_name, self._include_patterns):
            return True
        if self._include_patterns:
            return False
        return not (
            self._exclude_patterns and _matches_any(entity_name, self._exclude_patterns)
        )

    # ---- 主流程 ----

    async def collect(self, source: Any) -> CollectResult:
        source_id = getattr(source, "source_id", "?")
        # 采集范围：DataSource.databases（多业务库）→ 枚举全部非系统库
        try:
            target_dbs = getattr(self, "_databases", None)
            if target_dbs:
                dbs = list(target_dbs)
            else:
                dbs = await self._list_schemas()
            if not dbs:
                return CollectResult(specs=[], failed_specs=[], source_id=source_id)
        except Exception as exc:
            raise ExternalDependencyError(f"采集源 {source_id} 枚举库失败: {exc}") from exc

        try:
            table_rows = await self._query_tables(dbs)
            col_rows = await self._query_columns(dbs)
        except Exception as exc:
            raise ExternalDependencyError(f"采集源 {source_id} 读取 HMS 元数据失败: {exc}") from exc

        # 按 (db, tbl) 归组列（保持查询返回的表序）
        cols_by_table: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in col_rows:
            db, tbl = str(r.get("db_name") or ""), str(r.get("tbl_name") or "")
            if db and tbl:
                cols_by_table.setdefault((db, tbl), []).append(r)

        specs: list[CatalogSpec] = []
        failed_specs: list[FailedSpec] = []
        for info in table_rows:
            db, tbl = str(info.get("db_name") or ""), str(info.get("tbl_name") or "")
            if not db or not tbl:
                continue
            entity_name = f"{db}.{tbl}"
            try:
                spec = self._build_spec(db, tbl, info, cols_by_table.get((db, tbl), []))
                if self._keep_table(entity_name):
                    specs.append(spec)
            except Exception as exc:
                logger.warning(
                    "collect_hms_entity_failed: source=%s entity=%s error=%s",
                    source_id,
                    entity_name,
                    exc,
                )
                failed_specs.append(FailedSpec(entity_name=entity_name, error=str(exc)))

        return CollectResult(specs=specs, failed_specs=failed_specs, source_id=source_id)

    async def probe(self) -> ProbeResult:
        """轻量探活：SELECT 1（经 HMS 库连接）。"""
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
        """释放 HMS 库异步引擎，避免连接池泄漏。"""
        await self._connector.dispose()


def _build_metastore_url(cfg: dict[str, Any]) -> URL:
    """用 URL.create 构建 HMS MySQL 连接 URL（避免密码入字符串）。"""
    return URL.create(
        drivername=cfg.get("driver", "mysql+aiomysql"),
        username=cfg.get("user", ""),
        password=cfg.get("password", ""),
        host=cfg.get("host", "127.0.0.1"),
        port=cfg.get("port", 3306),
        database=cfg.get("database", ""),
    )


@registry.register("hive_metastore")
def create_hive_metastore_collector(cfg: dict[str, Any]) -> HiveMetastoreCollector:
    """Hive Metastore 采集器工厂：直连 HMS backend MySQL。"""
    url = _build_metastore_url(cfg)
    connector = SqlalchemyConnector(url, connect_timeout=10, query_timeout=60)
    return HiveMetastoreCollector(connector, database=cfg.get("database"))
