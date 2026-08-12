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
from app.services.collector.spi import BaseCollector


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

    def list_types(self) -> list[str]:
        """返回所有已注册的采集器类型。"""
        return sorted(self._registry.keys())


# 模块级全局注册中心实例
registry = CollectorRegistry()
