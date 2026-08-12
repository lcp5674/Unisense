"""采集器 SPI（对齐 TD §12.1「SPI（多数据源适配器）」）。

设计要点：
- ``BaseCollector`` 抽象采集行为；``collect()`` 返回 ``CollectResult``
  （含成功 specs + 失败 failed_specs + source_id），实现单表跳过容错。
- ``build_collector`` 委托 ``CollectorRegistry`` 构建，支持插件式注册。
- 外部依赖（源库）失败统一转化为 ``ExternalDependencyError``（503 可重试），
  **不**静默吞没为 200。
- SQL 一律参数化（``Connector.query(sql, params)``），避免注入。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.services.collector.classifier import SensitivityClassifier


@dataclass
class CatalogSpec:
    """采集到的实体元数据规格。"""

    entity_name: str
    entity_type: str
    schema_json: dict[str, Any]
    etl_sql: str | None = None


@dataclass
class FailedSpec:
    """采集失败的实体记录（单表跳过容错）。"""

    entity_name: str
    error: str


@dataclass
class CollectResult:
    """采集结果（含成功与失败记录）。"""

    specs: list[CatalogSpec] = field(default_factory=list)
    failed_specs: list[FailedSpec] = field(default_factory=list)
    source_id: str = ""


class Connector(Protocol):
    """源库查询协议（便于测试注入假连接器）。"""

    async def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """执行参数化查询，返回行字典列表。"""
        ...

    async def dispose(self) -> None:
        """释放连接（源库引擎等）。"""
        ...


class BaseCollector(ABC):
    """采集器基类。"""

    def __init__(self, classifier: SensitivityClassifier | None = None) -> None:
        self._classifier = classifier or SensitivityClassifier()

    @abstractmethod
    async def collect(self, source: Any) -> CollectResult:
        """采集数据源，返回采集结果（含成功 specs 与失败 failed_specs）。"""
        ...

    async def dispose(self) -> None:
        """释放采集器持有的外部连接（如源库引擎）。默认无操作，子类按需实现。"""
        return None


def build_collector(collector_type: str, encrypted_config: str) -> BaseCollector:
    """按类型构建采集器（委托 CollectorRegistry）。

    Args:
        collector_type: 采集器类型（如 "mysql", "postgres" 等）。
        encrypted_config: DataSource.connection_config 密文。

    Returns:
        采集器实例。

    Raises:
        BusinessError: 类型未注册。
    """
    # 惰性导入以确保连接器模块已注册
    from app.services.collector.connectors import registry  # noqa: F401

    return registry.build(collector_type, encrypted_config)
