"""采集领域扩展模型（SchemaDriftLog + CollectionWatermark）。

对齐 TD §12.1 / spec FR-010/FR-011/FR-014。
新增表用于 Schema Drift 检测历史记录与采集水位追踪。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Enum, ForeignKey, Index, String
from sqlalchemy.dialects.mysql import DATETIME, INTEGER, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class SchemaDriftLog(Base, BaseModel):
    """Schema 变更日志。

    记录每次采集后检测到的 Schema 变更（新增列/删除列/类型变更等），
    满足 GB/T 36073 §6.4 审计要求。

    Attributes:
        source_id: 数据源标识。
        entity_name: 实体名。
        change_type: 变更类型（ADD_COLUMN/DROP_COLUMN/TYPE_CHANGE/SCHEMA_CHANGED）。
        before_signature: 变更前内容指纹。
        after_signature: 变更后内容指纹。
        before_schema: 变更前 schema。
        after_schema: 变更后 schema。
        diff_json: 差异详情（{added:[], removed:[], changed:[]}）。
        detected_at: 检测时间。
    """

    __tablename__ = "schema_drift_log"

    source_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("data_source.source_id", name="fk_drift_log_source"),
        nullable=False,
        comment="数据源标识",
    )
    entity_name: Mapped[str] = mapped_column(
        String(256), nullable=False, comment="实体名"
    )
    change_type: Mapped[str] = mapped_column(
        Enum(
            "ADD_COLUMN",
            "DROP_COLUMN",
            "TYPE_CHANGE",
            "SCHEMA_CHANGED",
            name="drift_change_type_enum",
        ),
        nullable=False,
        comment="变更类型",
    )
    before_signature: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="变更前内容指纹"
    )
    after_signature: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="变更后内容指纹"
    )
    before_schema: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="变更前 schema"
    )
    after_schema: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="变更后 schema"
    )
    diff_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="差异详情"
    )
    detected_at: Mapped[datetime] = mapped_column(
        DATETIME, nullable=False, comment="检测时间"
    )

    __table_args__ = (
        Index("idx_drift_source_entity", "source_id", "entity_name"),
        Index("idx_drift_detected_at", "detected_at"),
    )


class CollectionWatermark(Base, BaseModel):
    """采集水位记录。

    每个数据源一条记录，追踪最后采集时间、模式与指纹映射，
    用于增量采集与 Schema Drift 检测。

    Attributes:
        source_id: 数据源标识（唯一）。
        last_collected_at: 最后采集时间。
        mode: 采集模式（FULL/INCREMENTAL）。
        scanned_count: 采集表数。
        failed_count: 失败表数。
        content_fingerprints: 实体级指纹映射 {entity_name: signature}。
    """

    __tablename__ = "collection_watermark"

    source_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("data_source.source_id", name="fk_watermark_source"),
        nullable=False,
        unique=True,
        comment="数据源标识（唯一）",
    )
    last_collected_at: Mapped[datetime] = mapped_column(
        DATETIME, nullable=False, comment="最后采集时间"
    )
    mode: Mapped[str] = mapped_column(
        Enum("FULL", "INCREMENTAL", name="watermark_mode_enum"),
        nullable=False,
        default="FULL",
        comment="采集模式",
    )
    scanned_count: Mapped[int] = mapped_column(
        INTEGER, nullable=False, default=0, comment="采集表数"
    )
    failed_count: Mapped[int] = mapped_column(
        INTEGER, nullable=False, default=0, comment="失败表数"
    )
    content_fingerprints: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default={}, comment="实体级指纹映射"
    )

    __table_args__ = (Index("idx_watermark_source", "source_id"),)
