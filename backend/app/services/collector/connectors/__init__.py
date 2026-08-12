"""采集连接器包（对齐 TD §12.1「SPI（多数据源适配器）」）。

7 种数据源连接器 + CollectorRegistry 注册中心。
新增数据源类型只需在本目录新建文件 + @registry.register("type") 装饰器。

导入此包即触发所有连接器的 @registry.register() 装饰器注册。
"""

# 首先导入 registry（其他模块注册依赖它）
from app.services.collector.connectors.clickhouse import ClickHouseCollector  # noqa: F401
from app.services.collector.connectors.collector_registry import registry
from app.services.collector.connectors.doris import create_doris_collector  # noqa: F401
from app.services.collector.connectors.hive import HiveCollector  # noqa: F401
from app.services.collector.connectors.kafka import KafkaCollector  # noqa: F401

# 导入各连接器模块（触发 @registry.register 装饰器）
from app.services.collector.connectors.mysql import (  # noqa: F401
    InformationSchemaCollector,
    SqlalchemyConnector,
)
from app.services.collector.connectors.postgres import PostgresCollector  # noqa: F401
from app.services.collector.connectors.starrocks import create_starrocks_collector  # noqa: F401

__all__ = [
    "registry",
    "InformationSchemaCollector",
    "SqlalchemyConnector",
    "PostgresCollector",
    "HiveCollector",
    "ClickHouseCollector",
    "KafkaCollector",
]
