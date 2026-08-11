"""数据源与元数据目录模型。

对齐 TD §4.1 data_source / db_catalog 表。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Enum, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


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
    """

    __tablename__ = "data_source"

    source_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="数据源标识（唯一）"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="数据源名称")
    source_type: Mapped[str] = mapped_column(
        Enum(
            "mysql",
            "postgres",
            "hive",
            "doris",
            "starrocks",
            "clickhouse",
            "maxcompute",
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
    created_by: Mapped[int] = mapped_column(
        ForeignKey("user.id", name="fk_data_source_user"), nullable=False
    )

    __table_args__ = (Index("idx_data_source_domain", "domain"),)


class DBCatalog(Base, BaseModel):
    """元数据库表/字段目录。

    采集落库的元数据。

    Attributes:
        source_id: 数据源标识。
        entity_name: 实体名（库.表）。
        entity_type: 实体类型（table/field）。
        schema_json: 字段/类型/注释/索引（JSON）。
        etl_sql: 源端 ETL SQL（可空）。
        sensitivity_level: 敏感级别。
        owner_id: Owner ID（可空，孤儿资产=NULL）。
        upstream_signature: 幂等键（source_id+entity_name）。
    """

    __tablename__ = "db_catalog"

    source_id: Mapped[str] = mapped_column(
        ForeignKey("data_source.source_id", name="fk_db_catalog_source"),
        nullable=False,
        comment="数据源标识",
    )
    entity_name: Mapped[str] = mapped_column(String(256), nullable=False, comment="实体名（库.表）")
    entity_type: Mapped[str] = mapped_column(
        Enum("table", "field", name="entity_type_enum"),
        nullable=False,
        comment="实体类型",
    )
    schema_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="字段/类型/注释/索引"
    )
    etl_sql: Mapped[str | None] = mapped_column(Text, nullable=True, comment="源端 ETL SQL")
    sensitivity_level: Mapped[str] = mapped_column(
        Enum("PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", name="sensitivity_enum"),
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

    __table_args__ = (
        UniqueConstraint("source_id", "entity_name", name="uk_db_catalog_entity"),
        Index("idx_db_catalog_owner", "owner_id"),
        Index("idx_db_catalog_sens", "sensitivity_level"),
    )
