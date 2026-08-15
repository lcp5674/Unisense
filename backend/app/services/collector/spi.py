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


@dataclass
class ProbeResult:
    """连接探活结果（测试连接 / 实时健康检查）。"""

    ok: bool
    latency_ms: int
    error: str | None = None
    detail: dict[str, Any] | None = None


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
        self._include_patterns: list[str] | None = None
        self._exclude_patterns: list[str] | None = None

    @abstractmethod
    async def collect(self, source: Any) -> CollectResult:
        """采集数据源，返回采集结果（含成功 specs 与失败 failed_specs）。"""
        ...

    async def collect_entity(self, source: Any, entity_name: str) -> CatalogSpec | None:
        """采集单个实体（单表元数据刷新，生产运维场景）。

        仅刷新目标表/实体，不触发全源扫描。返回该实体的最新 CatalogSpec；
        连接器不支持单实体采集（如 Hive 启动开销大）时返回 None，
        调用方应回退到全量采集后仅取目标实体。

        Args:
            source: 数据源 ORM 对象。
            entity_name: 目录实体名（形如 ``schema.table`` 或 ``table``）。

        Returns:
            最新 CatalogSpec，不支持时返回 None。
        """
        return None

    def set_incremental_context(self, mode: str, watermark_ts: Any | None = None) -> None:
        """注入增量采集上下文（P0-6：由 service 层在 collect 前调用）。

        ``mode`` 为 "INCREMENTAL" 且 ``watermark_ts`` 非空时，支持增量的连接器
        只采集水位之后发生变更的实体；默认实现保持全量（不支持增量降级为全量）。

        Args:
            mode: 采集模式（FULL/INCREMENTAL）。
            watermark_ts: 上次采集水位时间戳。
        """
        self._incremental_mode = mode
        self._incremental_watermark = watermark_ts

    def set_table_filter(
        self,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> None:
        """注入表级采集过滤白黑名单（治理：include/exclude patterns）。

        由 service 层在 collect 前从 ``DataSource.include_patterns`` /
        ``DataSource.exclude_patterns`` 读取并注入；连接器按 fnmatch 风格过滤
        扫描到的实体名。默认实现仅保存（供子类 collect 时读取）。

        Args:
            include_patterns: 包含白名单（任一匹配即保留），空/None 表示不过滤。
            exclude_patterns: 排除黑名单（任一匹配即丢弃）。
        """
        self._include_patterns = include_patterns
        self._exclude_patterns = exclude_patterns

    async def list_databases(self) -> list[str]:
        """枚举该实例下可采集的非系统数据库（创建数据源时选择目标库）。

        连接器不支持枚举（如 Kafka）时返回空列表，前端可回退为手填。
        """
        return []

    async def probe(self) -> ProbeResult:
        """轻量连接探活（SELECT 1 或等价最小查询），供「测试连接 / 健康检查」使用。

        默认未实现；各连接器按自身协议覆盖。失败时返回 ``ok=False`` 而非抛出异常。
        """
        raise NotImplementedError(f"{type(self).__name__} 未实现 probe")

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
