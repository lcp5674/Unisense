"""dp 源表列清单提供者（方案 3 schema 感知 star 展开的数据源）。

``SELECT *`` 投影的字段级血缘需要源表的**真实列清单**才能把星号展开为逐列
字段边。dp 场景下源表（如 ``wedw_dw.*`` / ``datamarket_*``）与 dp 调度元数据
（``dp_stable.dispatch_task``）可能不同库同实例，也可能在平台已登记的
Hive/Hive Metastore 数据源上——提供者按两级通道尽力获取，均失败返回
``None``（调用方回退降级标记，行为与方案 3 之前一致）。

通道设计：
- **A：dp 源 collector 直查** ``information_schema.columns``（MySQL 同实例
  形态：仓库表与 dp 元数据同库——本地测试/直连 MySQL 仓库场景）；
- **B：平台 Hive 系数据源 DESCRIBE**（生产主形态：仓库表在 Hive/Metastore，
  经 ``build_collector(source_type).list_columns``，与采集目录同通道）。

每次扫描轮内结果缓存（``columns`` 以表名为键），避免同一源表重复 DESCRIBE；
通道 B 的 collector 用完即 dispose，不长期占用连接。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from sqlalchemy import select

from app.models.data_source import DataSource
from app.models.enums import SourceTypeEnum

logger = logging.getLogger(__name__)

#: 通道 B 最多尝试的 Hive 系数据源个数（防止数据源很多时逐个卡顿）。
_MAX_HIVE_SOURCES = 3
#: 单次 DESCRIBE/信息 schema 查询超时（秒）——schema 获取是血缘解析的旁路，
#: 失败即降级，不能拖慢扫描主链路。
_COLUMN_QUERY_TIMEOUT = 20.0
#: as_map 有界并发度（阶段 1）：多源表逐表串行 = 每表至少 1 次网络往返（通道 A）
#: 或逐个 hive DESCRIBE——长 SQL 涉及几十张源表时拖慢二次解析。改为有界并发同时
#: 拉多张表；dp collector 为 SQLAlchemy 引擎池（query 每次引擎 connect），并发安全。
_SCHEMA_FETCH_CONCURRENCY = 6


def _split_table(table: str) -> tuple[str, str] | None:
    """拆 ``db.table``（或裸 ``table``）为 (db, table)；无法拆分返回 None。"""
    parts = [p.strip("`\" ") for p in table.split(".") if p.strip()]
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    return None


class DpSchemaProvider:
    """dp 扫描轮内的源表列清单提供者（实例级缓存，每轮新建 service 时创建）。"""

    def __init__(
        self,
        db: Any,
        dp_collector: Any,
        fetch_collector: Any,
    ) -> None:
        """
        Args:
            db: unisense 业务库会话（查已登记的 Hive 系数据源）。
            dp_collector: dp 元数据源 collector（通道 A 的 ``information_schema``
                查询走它；与扫描主链路共用连接，不 dispose）。
            fetch_collector: ``async (source_id) -> collector``（通道 B 构建
                Hive 系数据源连接；用完即 dispose）。
        """
        self._db = db
        self._dp_collector = dp_collector
        self._fetch_collector = fetch_collector
        self._cache: dict[str, list[str] | None] = {}

    async def columns(self, table: str) -> list[str] | None:
        """返回表的全部列名（有序）；不可知返回 ``None``（调用方降级）。"""
        if not table:
            return None
        if table in self._cache:
            return self._cache[table]
        cols: list[str] | None = None
        try:
            cols = await self._via_dp_information_schema(table)
            if cols is None:
                cols = await self._via_hive_source(table)
        except Exception as exc:  # noqa: BLE001 —— schema 获取失败即降级，不阻断扫描
            logger.warning(
                "dp_schema_lookup_failed table=%s error=%s", table, exc
            )
            cols = None
        self._cache[table] = cols
        return cols

    async def as_map(self, tables: list[str]) -> dict[str, list[str]]:
        """批量获取多张表的列清单（跳过不可知项），供 ``extract_field_lineage``。

        有界并发（阶段 1）：逐表串行 = 每表至少 1 次网络往返（通道 A）或逐个 hive
        DESCRIBE，长 SQL 涉及几十张源表时二次解析被拖慢。改为 ``asyncio.gather`` +
        Semaphore 并发拉取（缓存写入幂等——不同表键互不冲突，Python 单线程 asyncio
        无真竞态）。
        """
        out: dict[str, list[str]] = {}
        sem = asyncio.Semaphore(_SCHEMA_FETCH_CONCURRENCY)

        async def _one(t: str) -> tuple[str, list[str] | None]:
            async with sem:
                return t, await self.columns(t)

        results = await asyncio.gather(
            *(_one(t) for t in dict.fromkeys(tables))
        )
        for t, cols in results:
            if cols:
                out[t] = cols
        return out

    async def _via_dp_information_schema(self, table: str) -> list[str] | None:
        """通道 A：dp 源 collector 的 ``information_schema.columns``（同实例 MySQL）。"""
        if self._dp_collector is None:
            return None
        split = _split_table(table)
        if split is None:
            return None
        db_name, tbl = split
        rows = await self._dp_collector.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=:d AND table_name=:t ORDER BY ordinal_position",
            {"d": db_name, "t": tbl},
        )
        names = [
            str(r[0]).strip()
            for r in (rows or [])
            if r and r[0] and str(r[0]).strip()
        ]
        return names or None

    async def _via_hive_source(self, table: str) -> list[str] | None:
        """通道 B：平台已登记的 Hive/Hive Metastore/Spark 数据源 DESCRIBE。"""
        hive_types = (
            SourceTypeEnum.HIVE.value,
            SourceTypeEnum.HIVE_METASTORE.value,
            SourceTypeEnum.SPARK.value,
        )
        rows = (
            await self._db.execute(
                select(DataSource.source_id)
                .where(DataSource.source_type.in_(hive_types))
                .order_by(DataSource.id)
                .limit(_MAX_HIVE_SOURCES)
            )
        ).all()
        # 惰性 import：collector 模块注册 + Fernet 解密依赖较重，仅在需要时加载
        from app.services.collector.spi import build_collector

        for (source_id,) in rows:
            collector = None
            try:
                src = (
                    await self._db.execute(
                        select(DataSource).where(DataSource.source_id == source_id)
                    )
                ).scalar_one_or_none()
                if src is None:
                    continue
                collector = build_collector(src.source_type, src.connection_config)
                cols = await _describe_with_timeout(collector, table)
                if cols:
                    logger.info(
                        "dp_schema_via_hive table=%s source=%s columns=%d",
                        table,
                        source_id,
                        len(cols),
                    )
                    return cols
            except Exception as exc:  # noqa: BLE001 —— 单数据源失败继续尝试下一个
                logger.info(
                    "dp_schema_hive_source_miss source=%s table=%s error=%s",
                    source_id,
                    table,
                    exc,
                )
            finally:
                if collector is not None:
                    with contextlib.suppress(Exception):
                        await collector.dispose()
        return None


async def _describe_with_timeout(collector: Any, table: str) -> list[str] | None:
    """按 collector 能力分派的列清单查询，返回纯列名列表。

    - ``list_columns(table)``：HiveCollector（pyhive 直连 HiveServer2 DESCRIBE）；
    - ``_query_columns_one(schema, tbl)``：HiveMetastoreCollector（查 HMS 元库
      ``COLUMNS_V2``）——Metastore 只存表结构，列清单恰是它的主场；
    两者都不可用返回 ``None``（调用方尝试下一个数据源 / 降级）。
    """
    split = _split_table(table)
    if hasattr(collector, "list_columns"):
        cols = await asyncio.wait_for(
            collector.list_columns(table), timeout=_COLUMN_QUERY_TIMEOUT
        )
        return [str(c["name"]).strip() for c in cols if c.get("name")]
    if hasattr(collector, "_query_columns_one") and split is not None:
        schema, tbl = split
        rows = await asyncio.wait_for(
            collector._query_columns_one(schema, tbl), timeout=_COLUMN_QUERY_TIMEOUT
        )
        names: list[str] = []
        for r in rows or []:
            name = str(
                r.get("COLUMN_NAME") or r.get("column_name") or ""
            ).strip()
            if name:
                names.append(name)
        return names or None
    return None
