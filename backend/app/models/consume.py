"""consume 领域模型（TD §12.6 / FR-12,13 消费层）。

包含三类实体：
- ApiClient：消费方接入方（X-Api-Key 换取短效 JWT，承载 scope/配额）。
- MetricValueSnapshot：指标结果快照（WORM，只写不删，对齐 TD §4.1）。
- UserPreference：用户偏好/收藏（pinned_metrics 等前端映射，对齐 TD §5.4）。

对齐 DEV_GUIDE §8a.4（表名单数、主键 BIGINT）与 TD §4.1。
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Enum, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel, TimestampMixin


class ApiClientStatus(enum.StrEnum):
    """接入方状态（对齐 TD §4.1 api_client.status）。"""

    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class SnapshotGeneratedBy(enum.StrEnum):
    """快照生成来源（对齐 TD §4.1 metric_value_snapshot.generated_by）。"""

    QUERY = "QUERY"
    MATERIALIZE = "MATERIALIZE"


class ApiClient(Base, BaseModel):
    """消费方接入方（TD §4.1 api_client）。

    X-Api-Key 对应 client_id/secret；secret 以 bcrypt 哈希存储于 client_secret_ref。
    scope_domain 限定可访问域；metric_whitelist 为空表示不限（域内全量）。
    """

    __tablename__ = "api_client"

    client_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="接入方 ID（X-Api-Key 用户名）"
    )
    client_secret_ref: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="接入方密钥 bcrypt 哈希"
    )
    scope_domain: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="授权域（NULL=不限域，仅受白名单约束）"
    )
    metric_whitelist: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="指标白名单（NULL=域内全量）"
    )
    qps: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=20, comment="单接入方 QPS 配额"
    )
    daily_quota: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=100_000, comment="单接入方日查询配额"
    )
    scan_row_limit: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="单次查询行扫描上限（NULL=不限制）"
    )
    status: Mapped[ApiClientStatus] = mapped_column(
        Enum(ApiClientStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ApiClientStatus.ACTIVE,
        index=True,
        comment="接入方状态",
    )
    created_by: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="创建人（平台管理员）ID"
    )


class MetricValueSnapshot(Base, TimestampMixin):
    """指标结果快照（TD §4.1 metric_value_snapshot，WORM）。

    只写不删（继承 TimestampMixin 而非 BaseModel，不含 deleted_at）。
    热存 180d 后冷归档；唯一约束防止同口径重复快照。
    """

    __tablename__ = "metric_value_snapshot"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="快照 ID"
    )
    metric_code: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, comment="指标码"
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="生效版本")
    dims: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="维度组合（如 {province: 广东}）"
    )
    date_range: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="日期区间（如 2026-01~2026-03）"
    )
    value_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="结果值（含 rows/summary）"
    )
    quality_flag: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="质量标记（对齐 quality 等级或 NULL）"
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="数据生成时间"
    )
    generated_by: Mapped[SnapshotGeneratedBy] = mapped_column(
        Enum(SnapshotGeneratedBy, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=SnapshotGeneratedBy.QUERY,
        comment="生成来源",
    )


class UserPreference(Base, BaseModel):
    """用户偏好/收藏（TD §5.4 user_preference）。

    preference_key 区分 default_domain / pinned_metrics / search_scope / theme。
    """

    __tablename__ = "user_preference"

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="用户 ID")
    preference_key: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="偏好键（pinned_metrics 等）"
    )
    preference_value: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="偏好值（JSON）"
    )

    __table_args__ = (UniqueConstraint("user_id", "preference_key", name="uk_pref"),)
