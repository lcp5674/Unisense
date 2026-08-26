"""增量采集混合策略（对齐 TD §12.1 / spec FR-012/FR-014/Decision 2）。

设计要点：
- 优先使用源库 last_altered/UPDATE_TIME 时间戳（MySQL/StarRocks/Doris 支持）
- ClickHouse 使用 system.tables.metadata_modification_time
- PostgreSQL/Hive/Kafka 无增量支持，降级为全量采集
- 降级路径：增量模式请求但源库不支持 → 自动降级为 FULL，记录日志
- 采集水位缺失时也降级为全量（首次采集必须全量）
- P0-3: MySQL InnoDB UPDATE_TIME 通常为 NULL，<10%% 有效表时降级全量
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger("unisense.collector.incremental")

# 支持增量采集的数据源类型（基于源库时间戳）
_INCREMENTAL_SUPPORTED_TYPES = {"mysql", "clickhouse"}


def supports_incremental(source_type: str) -> bool:
    """判断数据源类型是否支持增量采集。

    Args:
        source_type: 数据源类型标识。

    Returns:
        True 如果支持增量采集。
    """
    return source_type in _INCREMENTAL_SUPPORTED_TYPES


def build_incremental_query(
    source_type: str,
    schema: str,
    watermark_ts: datetime | None = None,
) -> str | None:
    """根据源库类型生成增量查询 SQL。

    仅返回查询「有变更的表」的 SQL，后续的列查询仍使用全量逻辑。
    返回 None 表示不支持增量，需降级为全量。

    Args:
        source_type: 数据源类型标识。
        schema: 数据库/Schema 名称。
        watermark_ts: 上次采集水位时间戳。

    Returns:
        增量表查询 SQL 或 None（不支持增量）。
    """
    if watermark_ts is None:
        logger.info(
            "incremental_no_watermark: source_type=%s schema=%s, 降级为全量",
            source_type,
            schema,
        )
        return None

    if source_type == "mysql":
        # MySQL information_schema.tables.UPDATE_TIME
        return (
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = :schema AND table_type = :ttype "
            "AND (UPDATE_TIME IS NOT NULL AND UPDATE_TIME > :watermark)"
        )
    elif source_type in ("doris", "starrocks"):
        # Doris/StarRocks MySQL 协议兼容，但 information_schema 无 UPDATE_TIME
        # 降级为全量（Decision 2: Doris information_schema.tables 无 UPDATE_TIME）
        logger.info(
            "incremental_not_supported: source_type=%s 降级为全量",
            source_type,
        )
        return None
    elif source_type == "clickhouse":
        # ClickHouse system.tables.metadata_modification_time
        return (
            f"SELECT name FROM system.tables "
            f"WHERE database = '{schema}' "
            f"AND metadata_modification_time > '{watermark_ts.strftime('%Y-%m-%d %H:%M:%S')}' "
            f"FORMAT TabSeparated"
        )
    else:
        # PostgreSQL/Hive/Kafka 不支持增量
        logger.info(
            "incremental_not_supported: source_type=%s 降级为全量",
            source_type,
        )
        return None


def should_degrade_to_full(
    source_type: str,
    watermark_ts: datetime | None = None,
) -> bool:
    """判断是否应降级为全量采集。

    Args:
        source_type: 数据源类型标识。
        watermark_ts: 上次采集水位时间戳。

    Returns:
        True 如果应降级为全量。
    """
    if not supports_incremental(source_type):
        return True
    return watermark_ts is None


async def should_mix_in(
    source_type: str, connector: Any | None, ratio_threshold: float = 0.1
) -> bool:
    """MySQL InnoDB UPDATE_TIME 通常为 NULL，低于占比阈值时降级全量。

    检测逻辑：查询 information_schema.tables 中 UPDATE_TIME IS NOT NULL 的表占比，
    若低于阈值说明增量采集会漏采大量表，此时应降级为全量并记录 event。

    阈值可通过环境变量 UNISENSE_COLLECTOR_MYSQL_INCREMENTAL_RATIO_THRESHOLD 配置（默认 0.1）。

    Args:
        source_type: 数据源类型。
        connector: 数据库连接器（有 async query 方法）。
        ratio_threshold: 降级阈值（0.0-1.0），低于此占比时降级全量。

    Returns:
        True 表示应降级为全量（mix-in 全量）。
    """
    if source_type != "mysql" or connector is None:
        return False
    try:
        # X-5：此前走同步 _get_mysql_update_time_ratio —— 在已有 running loop 上
        # run_until_complete 必然抛 RuntimeError 被 except 吞掉恒返 False，导致
        # InnoDB UPDATE_TIME 为 NULL 的库静默漏采且无降级兜底。现改为原生 async。
        ratio = await _get_mysql_update_time_ratio_async(connector)
        if ratio < ratio_threshold:
            logger.warning(
                "mysql_update_time_sparse: ratio=%.2f%% < %.0f%%, 降级为全量",
                ratio * 100,
                ratio_threshold * 100,
            )
            return True
    except Exception as exc:
        logger.warning("mysql_update_time_ratio_check_failed: %s", exc)
    return False


async def _get_mysql_update_time_ratio_async(connector: Any) -> float:
    """异步获取 MySQL UPDATE_TIME IS NOT NULL 表占比。"""
    try:
        rows = await connector.query(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN UPDATE_TIME IS NOT NULL THEN 1 ELSE 0 END) as with_time "
            "FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'",
            {},
        )
        if not rows:
            return 0.0
        row = rows[0]
        total = int(row.get("total", 0) or 0)
        with_time = int(row.get("with_time", 0) or 0)
        if total == 0:
            return 0.0
        return with_time / total
    except Exception:
        return 0.0


class IncrementalCollectorMixin:
    """增量采集混入类，提供增量查询 SQL 生成与降级判定。

    使用方式：各连接器可选择混入此类以获得增量查询能力。
    """

    def get_incremental_tables_query(
        self,
        source_type: str,
        schema: str,
        watermark_ts: datetime | None = None,
    ) -> str | None:
        """获取增量表查询 SQL（委托模块级函数）。"""
        return build_incremental_query(source_type, schema, watermark_ts)

    def is_incremental_degrade(
        self, source_type: str, watermark_ts: datetime | None = None
    ) -> bool:
        """判断是否应降级为全量采集（委托模块级函数）。"""
        return should_degrade_to_full(source_type, watermark_ts)
