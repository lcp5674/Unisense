"""指标健康度评分模型（对齐 TD §12.3 / spec FR-025~FR-028）。

五维加权评分：口径完整度 25% / 活跃度 20% / 质量 25% / Owner 响应 15% / 血缘覆盖 15%。
分级：≥85 EXCELLENT / 70-84 GOOD / 55-69 WARNING / <55 CRITICAL。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, Index, Integer
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class MetricHealthScore(Base, BaseModel):
    """指标健康度评分。

    Attributes:
        metric_id: 指标 ID（唯一）。
        score: 综合评分 0-100。
        level: 分级（EXCELLENT/GOOD/WARNING/CRITICAL）。
        completeness_score: 口径完整度 0-100。
        activity_score: 活跃度 0-100。
        quality_score: 质量 0-100。
        owner_response_score: Owner 响应 0-100。
        lineage_coverage_score: 血缘覆盖 0-100。
        missing_dimensions: 数据不足的维度列表。
        calculated_at: 评分计算时间。
    """

    __tablename__ = "metric_health_score"

    metric_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True, comment="指标 ID（唯一）"
    )
    score: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="综合评分 0-100"
    )
    level: Mapped[str] = mapped_column(
        Enum("EXCELLENT", "GOOD", "WARNING", "CRITICAL", name="health_level_enum"),
        nullable=False,
        default="CRITICAL",
        comment="分级",
    )
    completeness_score: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="口径完整度 0-100"
    )
    activity_score: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="活跃度 0-100"
    )
    quality_score: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="质量 0-100"
    )
    owner_response_score: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="Owner 响应 0-100"
    )
    lineage_coverage_score: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="血缘覆盖 0-100"
    )
    missing_dimensions: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="数据不足的维度列表"
    )
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="评分计算时间"
    )

    __table_args__ = (
        Index("idx_health_level", "level"),
        Index("idx_health_score", "score"),
    )

    @staticmethod
    def compute_level(score: int) -> str:
        """根据分数计算分级。"""
        if score >= 85:
            return "EXCELLENT"
        if score >= 70:
            return "GOOD"
        if score >= 55:
            return "WARNING"
        return "CRITICAL"
