"""Spark 连接器（对齐 TD §12.1 / spec FR-001）。

Spark Thrift Server 暴露 HiveServer2 兼容协议（JDBC ``jdbc:hive2://host:port/db``），
故采集逻辑与 Hive 完全一致：经 beeline CLI 执行 SHOW DATABASES / SHOW TABLES / DESCRIBE。
本实现继承 :class:`HiveCollector` 复用其 beeline 调用、table2 输出解析与 SQL 标识符校验。

- 无增量支持，始终全量
- 单表 try/catch 跳过容错
- 生产语义（FR-030）：按 connection_config.database 过滤；为空时枚举全部库
- @registry.register("spark") 注册

beeline 命令示例::

    beeline -u jdbc:hive2://host:10000 -e "SHOW TABLES IN schema"
    beeline -u jdbc:hive2://host:10000 -e "DESCRIBE schema.table"
"""

from __future__ import annotations

from typing import Any

from app.services.collector.connectors.collector_registry import registry
from app.services.collector.connectors.hive import HiveCollector


class SparkCollector(HiveCollector):
    """Spark 采集器：经 beeline 连接 Spark Thrift Server（HiveServer2 协议兼容）。

    与 HiveCollector 的差异仅在于类型标识与默认端口（Spark 官方默认 10000）；
    元数据采集 SQL（SHOW DATABASES / SHOW TABLES / DESCRIBE）完全一致，故直接复用。
    """


@registry.register("spark")
def create_spark_collector(cfg: dict[str, Any]) -> SparkCollector:
    """Spark 采集器工厂函数。"""
    return SparkCollector(
        host=cfg.get("host", "127.0.0.1"),
        port=cfg.get("port", 10000),
        user=cfg.get("user", ""),
        password=cfg.get("password", ""),
        database=cfg.get("database", "default"),
    )
