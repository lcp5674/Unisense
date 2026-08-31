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
    description: str | None = None


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
    # 表级过滤统计（治理白/黑名单跳过；方案 B：采集结果/记录展示被过滤的表）
    filtered_count: int = 0
    filtered_names: list[str] = field(default_factory=list)


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
        self._databases: list[str] | None = None
        self._sampling_max_rows = 0

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

    def set_databases(self, databases: list[str] | None = None) -> None:
        """注入目标数据库列表（多库采集：逐库扫描指定库，None=采集全部非系统库）。

        由 service 层在 collect 前从 ``DataSource.databases`` 读取并注入；
        连接器在 ``collect`` 时优先采用该列表，否则枚举全部非系统库。
        连接库 ``connection_config.database`` 为纯连接凭据，不参与采集范围。
        """
        self._databases = databases

    def set_sampling(self, max_rows: int = 0) -> None:
        """注入样本采样配置（PII 精度增强：name+sample 双验证）。

        ``max_rows`` 为每列采样行数上限（0/负值=不采样）。由 service 层在
        collect 前从 ``DataSource.quota.sample_rows`` 读取并注入；连接器在
        采集到字段后按能力执行采样，样本打码写入 ``schema_json.columns[].sample``。
        """
        self._sampling_max_rows = max(0, int(max_rows or 0))

    async def sample_columns(
        self, entity_name: str, schema_json: dict[str, Any]
    ) -> dict[str, Any]:
        """对实体字段执行样本采样（可选能力，PII 识别精度增强）。

        默认不采样、原样返回 schema；支持采样的连接器覆盖本方法——对每列执行
        ``SELECT col ... LIMIT n`` 取代表值，经 ``_mask_sample`` 打码后写入
        ``columns[].sample``。连接器内部应在 ``collect`` 组装 schema 时调用，
        以复用已建立的源库连接（避免每表额外握手）。

        Args:
            entity_name: 实体（库.表）名。
            schema_json: 含 ``columns`` 列表的 schema 字典。

        Returns:
            写入 ``sample`` 后的 schema 字典（不支持采样时原样返回）。
        """
        return schema_json

    def _mask_sample(self, sample: str) -> str:
        """对样本值打码（委托 classifier：手机/身份证/邮箱/银行卡掩码）。"""
        return self._classifier.mask_sample(sample)

    def _sample_rule_id(self, sample: str) -> str | None:
        """判定样本明文命中的敏感类别（rule_id），供采样时随打码值落库。

        掩码会丢失格式特征（``138****1234`` 无法反推是手机还是身份证），
        故类别必须在打码前对明文判定并单独存储为 ``columns[].sample_rule``。
        """
        return self._classifier.classify_sample(sample)

    def _apply_samples(self, col: dict[str, Any], values: list[str]) -> None:
        """把采样值写入字段定义（打码 + 类别），各连接器共用。

        保留最多 ``_sampling_max_rows`` 条（按打码值去重）写入 ``columns[].sample``
        为列表；类别（``sample_rule``）记录首个明文命中的敏感类别——掩码不可逆，
        事后无法补判类别，故类别必须在打码前对明文判定。
        """
        seen: set[str] = set()
        masked: list[str] = []
        rule_id: str | None = None
        for v in values:
            s = str(v).strip()
            if not s or s == "NULL":
                continue
            m = self._mask_sample(s)
            if m in seen:
                continue
            seen.add(m)
            masked.append(m)
            if rule_id is None:
                rule_id = self._sample_rule_id(s)
            if len(masked) >= self._sampling_max_rows:
                break
        if masked:
            col["sample"] = masked
            if rule_id:
                col["sample_rule"] = rule_id

    async def list_databases(self) -> list[str]:
        """枚举该实例下可采集的非系统数据库（创建数据源时选择目标库）。

        连接器不支持枚举（如 Kafka）时返回空列表，前端可回退为手填。
        """
        return []

    async def list_tables(self, databases: list[str] | None = None) -> dict[str, list[str]]:
        """枚举指定库下的表（按库分组，创建数据源时级联选表）。

        连接器不支持枚举表（如 Kafka）时返回空字典，前端隐藏表级选择区。
        """
        return {}

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

    已落库数据源的采集/探活路径：放行私有网段（生产库就在内网），
    但仍拒绝回环/链路本地/保留地址（SSRF 纵深防御）。

    Args:
        collector_type: 采集器类型（如 "mysql", "postgres" 等）。
        encrypted_config: DataSource.connection_config 密文。

    Returns:
        采集器实例。

    Raises:
        BusinessError: 类型未注册，或连接目标命中 SSRF 禁区。
    """
    # 惰性导入以确保连接器模块已注册
    from app.services.collector.connectors import registry  # noqa: F401

    return registry.build(collector_type, encrypted_config, allow_private=True)
