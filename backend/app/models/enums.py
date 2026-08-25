"""共享枚举定义（对齐 TD §12.1 / spec FR-003/FR-007/FR-008）。

统一 schemas.py 与 data_source.py 中的枚举，消除重复定义与不一致。
"""

from __future__ import annotations

import enum


class SourceTypeEnum(enum.StrEnum):
    """数据源类型枚举（9 种生产类型，含 Hive Metastore 直连）。"""

    MYSQL = "mysql"
    POSTGRES = "postgres"
    HIVE = "hive"
    HIVE_METASTORE = "hive_metastore"
    SPARK = "spark"
    DORIS = "doris"
    CLICKHOUSE = "clickhouse"
    KAFKA = "kafka"
    STARROCKS = "starrocks"


class EntityTypeEnum(enum.StrEnum):
    """实体类型枚举（TABLE/VIEW/FIELD）。"""

    TABLE = "TABLE"
    VIEW = "VIEW"
    FIELD = "FIELD"


class SensitivityLevelEnum(enum.StrEnum):
    """敏感级别枚举（含 NEEDS_REVIEW）。"""

    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    PII = "PII"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class MetricStateEnum(enum.StrEnum):
    """指标状态机枚举（6 态，对齐 TD §12.3 / FR-001）。"""

    DRAFT = "DRAFT"
    REVIEW = "REVIEW"
    PUBLISHED = "PUBLISHED"
    EXPERIMENTAL = "EXPERIMENTAL"
    DEPRECATED = "DEPRECATED"
    DATA_SOURCE_DROPPED = "DATA_SOURCE_DROPPED"


class VersionStatusEnum(enum.StrEnum):
    """指标版本状态枚举（对齐 TD §12.3 / FR-038）。"""

    DRAFT = "DRAFT"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    PUBLISHED = "PUBLISHED"
    EXPERIMENTAL = "EXPERIMENTAL"
    ARCHIVED = "ARCHIVED"
    CANCELLED = "CANCELLED"


class DomainStatusEnum(enum.StrEnum):
    """主题域状态枚举（对齐 spec FR-001）。"""

    ACTIVE = "active"
    INACTIVE = "inactive"


class DictStatusEnum(enum.StrEnum):
    """系统字典状态枚举（对齐 spec FR-006）。"""

    ACTIVE = "active"
    INACTIVE = "inactive"


class DictTypeEnum(enum.StrEnum):
    """系统字典类型枚举（10 种字典类型，对齐 spec FR-005）。"""

    GRANULARITY = "granularity"
    UNIT = "unit"
    AGGREGATION = "aggregation"
    TIME_SEMANTICS = "time_semantics"
    FRESHNESS = "freshness"
    DW_LAYER = "dw_layer"
    METRIC_TYPE = "metric_type"
    ADDITIVITY = "additivity"
    SERVING_MODE = "serving_mode"
    METRIC_TIER = "metric_tier"
