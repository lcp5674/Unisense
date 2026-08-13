"""采集器注册中心（对齐 TD §12.1 / spec FR-002）。

插件式注册：新增数据源类型无需修改 spi.py 源码，只需在 connectors/ 目录
新建文件并使用 ``@registry.register("type")`` 装饰器即可。

设计要点：
- 模块级全局实例 ``registry`` 供所有连接器注册。
- ``register()`` 既可作装饰器也可直接调用。
- ``build()`` 从加密配置还原源库连接信息，委托已注册的工厂函数构建采集器。
- ``list_types()`` 返回所有已注册类型（供 API 层校验 source_type）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.core.exceptions import BusinessError
from app.core.secrets import SecretManager
from app.services.collector.schemas import DataSourceTypeInfo
from app.services.collector.spi import BaseCollector

# 各类型元信息（供前端动态渲染 + 连接测试默认端口）。
# source_type 键与 @registry.register("type") 一一对应。
TYPE_INFO: dict[str, DataSourceTypeInfo] = {
    "mysql": DataSourceTypeInfo(
        source_type="mysql",
        label="MySQL",
        default_port=3306,
        supports_database=True,
        supports_schema=False,
        description="关系型数据库，采集 information_schema 元数据",
    ),
    "postgres": DataSourceTypeInfo(
        source_type="postgres",
        label="PostgreSQL",
        default_port=5432,
        supports_database=True,
        supports_schema=True,
        description="关系型数据库，按库内 schema 采集元数据",
    ),
    "hive": DataSourceTypeInfo(
        source_type="hive",
        label="Hive",
        default_port=10000,
        supports_database=True,
        supports_schema=False,
        description="数据仓库，经 beeline 连接 HiveServer2",
    ),
    "doris": DataSourceTypeInfo(
        source_type="doris",
        label="Doris",
        default_port=9030,
        supports_database=True,
        supports_schema=False,
        description="MPP 分析库（MySQL 协议兼容）",
    ),
    "clickhouse": DataSourceTypeInfo(
        source_type="clickhouse",
        label="ClickHouse",
        default_port=8123,
        supports_database=True,
        supports_schema=False,
        description="列式分析库，HTTP 接口采集",
    ),
    "kafka": DataSourceTypeInfo(
        source_type="kafka",
        label="Kafka",
        default_port=9092,
        supports_database=False,
        supports_schema=False,
        description="消息队列，采集 Topic 与 Schema Registry",
    ),
    "starrocks": DataSourceTypeInfo(
        source_type="starrocks",
        label="StarRocks",
        default_port=9030,
        supports_database=True,
        supports_schema=False,
        description="MPP 分析库（MySQL 协议兼容）",
    ),
}


class CollectorRegistry:
    """采集器注册表：维护 collector_type → factory 函数映射。"""

    def __init__(self) -> None:
        self._registry: dict[str, Callable[[dict[str, Any]], BaseCollector]] = {}

    def register(
        self, collector_type: str, factory: Callable[[dict[str, Any]], BaseCollector] | None = None
    ) -> Callable[..., Any]:
        """注册采集器工厂函数（支持装饰器与直接调用两种方式）。

        用法一（装饰器）::

            @registry.register("mysql")
            def create_mysql_collector(cfg: dict) -> BaseCollector:
                ...

        用法二（直接调用）::

            registry.register("mysql", create_mysql_collector)

        Args:
            collector_type: 采集器类型标识（如 "mysql", "postgres" 等）。
            factory: 工厂函数，接收解密后的 connection_config 字典，返回 BaseCollector。

        Returns:
            装饰器函数（当 factory 为 None 时）或原工厂函数。
        """
        if factory is not None:
            self._registry[collector_type] = factory
            return factory

        def decorator(
            fn: Callable[[dict[str, Any]], BaseCollector],
        ) -> Callable[[dict[str, Any]], BaseCollector]:
            self._registry[collector_type] = fn
            return fn

        return decorator

    def build(self, collector_type: str, encrypted_config: str) -> BaseCollector:
        """按类型构建采集器（从加密连接配置还原源库连接信息）。

        Args:
            collector_type: 采集器类型标识。
            encrypted_config: DataSource.connection_config 密文。

        Returns:
            采集器实例。

        Raises:
            BusinessError: 类型未注册。
        """
        factory = self._registry.get(collector_type)
        if factory is None:
            available = ", ".join(sorted(self._registry.keys())) or "(空)"
            raise BusinessError(
                f"不支持的采集器类型: {collector_type}，已注册类型: [{available}]",
                error_code="UNSUPPORTED_COLLECTOR",
            )
        cfg = SecretManager.decrypt(encrypted_config)
        return factory(cfg)

    def build_from_cfg(self, collector_type: str, cfg: dict[str, Any]) -> BaseCollector:
        """按类型构建采集器（明文连接配置，用于连接预检，不落库）。

        Args:
            collector_type: 采集器类型。
            cfg: 解密后的 connection_config 明文。

        Returns:
            采集器实例。

        Raises:
            BusinessError: 类型未注册。
        """
        factory = self._registry.get(collector_type)
        if factory is None:
            available = ", ".join(sorted(self._registry.keys())) or "(空)"
            raise BusinessError(
                f"不支持的采集器类型: {collector_type}，已注册类型: [{available}]",
                error_code="UNSUPPORTED_COLLECTOR",
            )
        return factory(cfg)

    def list_types(self) -> list[str]:
        """返回所有已注册的采集器类型。"""
        return sorted(self._registry.keys())

    def list_type_info(self) -> list[DataSourceTypeInfo]:
        """返回全部已注册类型的元信息（缺失元信息时以默认值兜底）。"""
        info: list[DataSourceTypeInfo] = []
        for t in self.list_types():
            base = TYPE_INFO.get(t)
            if base is not None:
                info.append(base)
                continue
            # 兜底：插件新增类型但未补充元信息
            info.append(
                DataSourceTypeInfo(
                    source_type=t,
                    label=t,
                    default_port=0,
                    supports_database=True,
                    supports_schema=False,
                    description="插件扩展类型",
                )
            )
        return info


# 模块级全局注册中心实例
registry = CollectorRegistry()
