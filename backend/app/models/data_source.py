"""数据源与元数据目录模型。

对齐 TD §4.1 data_source / db_catalog 表。
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import BOOLEAN, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel
from app.models.enums import EntityTypeEnum, SensitivityLevelEnum, SourceTypeEnum


class DescriptionSourceEnum(enum.StrEnum):
    """字段描述来源枚举。"""

    MANUAL = "manual"
    LLM = "llm"
    SCHEMA = "schema"


class ColumnDescription(Base, BaseModel):
    """独立字段描述记录（防止采集覆盖人工/LLM 编辑）。

    对齐 TD §4.1 column_descriptions 表。
    优先级链：manual > llm > schema_json 原始 comment。

    Attributes:
        catalog_id: 关联目录实体（FK→db_catalog.id）。
        column_name: 字段名。
        description: 描述文本。
        source: 描述来源（manual/llm/schema）。
        updated_by: 编辑者用户 ID（LLM 推断时为 NULL）。
    """

    __tablename__ = "column_descriptions"

    catalog_id: Mapped[int] = mapped_column(
        ForeignKey("db_catalog.id", name="fk_column_desc_catalog"),
        nullable=False,
        comment="关联目录实体",
    )
    column_name: Mapped[str] = mapped_column(String(256), nullable=False, comment="字段名")
    description: Mapped[str] = mapped_column(Text, nullable=False, comment="描述文本")
    source: Mapped[str] = mapped_column(
        Enum(
            *[e.value for e in DescriptionSourceEnum],
            name="description_source_enum",
        ),
        nullable=False,
        default="schema",
        comment="描述来源",
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", name="fk_column_desc_user"),
        nullable=True,
        comment="编辑者用户 ID（LLM 推断时为 NULL）",
    )

    __table_args__ = (
        UniqueConstraint("catalog_id", "column_name", name="uk_column_desc_catalog_col"),
        Index("idx_column_desc_source", "source"),
    )


class DataSource(Base, BaseModel):
    """数据源实体。

    Attributes:
        source_id: 数据源标识（唯一）。
        name: 数据源名称。
        source_type: 数据源类型。
        connection_config: 连接配置（JSON，加密存储）。
        domain: 所属域。
        coverage: 资产覆盖率。
        quota: 配额（max_concurrency/max_scan_rows）。
        health_status: 健康状态。
        cluster_id: 物理集群标识。
        last_health_check: 最后健康检查时间。
        created_by: 创建人 ID。
        schedule_cron: 定时调度 cron 表达式。
        collection_mode: 采集模式（FULL/INCREMENTAL）。
        owner_id: 数据源负责人（用户 ID）。
        description: 用途描述。
        include_patterns: 表级包含白名单（fnmatch 风格，None=全部）。
        exclude_patterns: 表级排除黑名单（fnmatch 风格）。
        health_metrics: 健康指标（p95_ms/success_rate/error_count/sample_count/period_hours）。
        degraded_since: 进入降级态起始时间（UTC）。
    """

    __tablename__ = "data_source"

    source_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="数据源标识（唯一）"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="数据源名称")
    source_type: Mapped[str] = mapped_column(
        Enum(
            *[e.value for e in SourceTypeEnum],
            name="source_type_enum",
        ),
        nullable=False,
        comment="数据源类型",
    )
    connection_config: Mapped[str] = mapped_column(
        Text, nullable=False, comment="连接配置（Fernet 加密存储的令牌）"
    )
    domain: Mapped[str] = mapped_column(String(64), nullable=False, comment="所属域")
    coverage: Mapped[float] = mapped_column(nullable=False, default=0.0, comment="资产覆盖率")
    quota: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="配额（max_concurrency/max_scan_rows）"
    )
    health_status: Mapped[str] = mapped_column(
        Enum("healthy", "unhealthy", "unknown", name="health_status_enum"),
        nullable=False,
        default="unknown",
        comment="健康状态",
    )
    cluster_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", comment="物理集群标识"
    )
    last_health_check: Mapped[datetime | None] = mapped_column(
        nullable=True, comment="最后健康检查时间"
    )
    last_error: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="最近一次采集/探活错误信息"
    )
    created_by: Mapped[int] = mapped_column(
        ForeignKey("user.id", name="fk_data_source_user"), nullable=False
    )
    schedule_cron: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="定时调度 cron 表达式"
    )
    collection_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="FULL", comment="采集模式（FULL/INCREMENTAL）"
    )
    enabled: Mapped[bool] = mapped_column(
        BOOLEAN,
        nullable=False,
        default=True,
        server_default="1",
        comment="是否启用（停用后不参与定时调度与手动采集）",
    )
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", name="fk_data_source_owner"),
        nullable=True,
        comment="数据源负责人（用户 ID）",
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="用途描述"
    )
    include_patterns: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="表级包含白名单（fnmatch 风格，None=全部）"
    )
    exclude_patterns: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="表级排除黑名单（fnmatch 风格）"
    )
    health_metrics: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="健康指标（p95_ms/success_rate/error_count/sample_count/period_hours）",
    )
    degraded_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="进入降级态起始时间（UTC）",
    )

    __table_args__ = (Index("idx_data_source_domain", "domain"),)


class DBCatalog(Base, BaseModel):
    """元数据库表/字段目录。

    采集落库的元数据。

    Attributes:
        source_id: 数据源标识。
        entity_name: 实体名（库.表）。
        entity_type: 实体类型（TABLE/VIEW/FIELD）。
        schema_json: 字段/类型/注释/索引（JSON）。
        etl_sql: 源端 ETL SQL（可空）。
        sensitivity_level: 敏感级别（含 NEEDS_REVIEW）。
        owner_id: Owner ID（可空，孤儿资产=NULL）。
        upstream_signature: 幂等键（source_id+entity_name）。
        content_signature: 内容指纹 SHA-256(canonical_schema_json)。
        schema_incomplete: 空 schema 标记。
    """

    __tablename__ = "db_catalog"

    source_id: Mapped[str] = mapped_column(
        ForeignKey("data_source.source_id", name="fk_db_catalog_source"),
        nullable=False,
        comment="数据源标识",
    )
    entity_name: Mapped[str] = mapped_column(String(256), nullable=False, comment="实体名（库.表）")
    entity_type: Mapped[str] = mapped_column(
        Enum(
            *[e.value for e in EntityTypeEnum],
            name="entity_type_enum",
        ),
        nullable=False,
        comment="实体类型",
    )
    schema_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="字段/类型/注释/索引"
    )
    etl_sql: Mapped[str | None] = mapped_column(Text, nullable=True, comment="源端 ETL SQL")
    sensitivity_level: Mapped[str] = mapped_column(
        Enum(
            *[e.value for e in SensitivityLevelEnum],
            name="sensitivity_enum",
        ),
        nullable=False,
        default="INTERNAL",
        comment="敏感级别",
    )
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", name="fk_db_catalog_owner"),
        nullable=True,
        comment="Owner ID（可空=孤儿资产待认领）",
    )
    upstream_signature: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="幂等键（source_id+entity_name）"
    )
    content_signature: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="内容指纹 SHA-256(canonical_schema_json)"
    )
    schema_incomplete: Mapped[bool] = mapped_column(
        BOOLEAN,
        nullable=False,
        default=False,
        comment="空 schema 标记",
    )
    # ---- 表级业务描述（治理补全，采集不覆盖，TD §12.1）----
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="表级业务描述（治理补全）"
    )
    description_source: Mapped[str | None] = mapped_column(
        Enum(
            *[e.value for e in DescriptionSourceEnum],
            name="description_source_enum",
        ),
        nullable=True,
        comment="表级描述来源（manual/llm）",
    )
    description_updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", name="fk_db_catalog_desc_user"),
        nullable=True,
        comment="表级描述编辑者（LLM 推断为 NULL）",
    )
    description_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="表级描述更新时间（UTC）",
    )

    __table_args__ = (
        UniqueConstraint("source_id", "entity_name", name="uk_db_catalog_entity"),
        Index("idx_db_catalog_owner", "owner_id"),
        Index("idx_db_catalog_sens", "sensitivity_level"),
    )
