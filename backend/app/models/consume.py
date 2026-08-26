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

from sqlalchemy import JSON, BigInteger, DateTime, Enum, Index, Integer, String, UniqueConstraint
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
    热存 180d 后冷归档；``uk_snapshot_metric_version_range_dims`` 唯一约束
    防同口径重复快照（JSON dims 无法直接建索引，用 ``dims_signature``
    确定性签名承载唯一键）。
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
    #: 维度组合确定性签名（sorted JSON 摘要前 32 位）——承载唯一约束（JSON 列不能直接建索引）
    dims_signature: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="维度组合确定性签名（唯一键承载）"
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

    __table_args__ = (
        UniqueConstraint(
            "metric_code",
            "version",
            "date_range",
            "dims_signature",
            name="uk_snapshot_metric_version_range_dims",
        ),
    )


class FavoriteAssetType(enum.StrEnum):
    """可收藏资产类型（C 层多资产收藏，对齐总览全资产方向）。

    asset_id 统一使用各资产的**业务编码**（非数据库 id，删除/重建不受影响）：
    - METRIC → Metric.metric_code
    - TABLE → DBCatalog.entity_name（库.表）
    - TERM → Term.term_code
    - DIMENSION → Dimension.dim_code
    - TEMPLATE → MetricTemplate.code
    """

    METRIC = "METRIC"
    TABLE = "TABLE"
    TERM = "TERM"
    DIMENSION = "DIMENSION"
    TEMPLATE = "TEMPLATE"


class Favorite(Base, BaseModel):
    """用户收藏（TD §5.4 favorite，通用多资产收藏模型）。

    取代 UserPreference.pinned_metrics 的 JSON 数组存储，演进为独立行模型：
    每行 = 用户 × 资产类型 × 资产业务编码，天然支持收藏时间（created_at）、
    软删除与唯一约束。asset_type/asset_id 组合唯一。
    """

    __tablename__ = "favorite"

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="用户 ID")
    asset_type: Mapped[FavoriteAssetType] = mapped_column(
        Enum(FavoriteAssetType, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        index=True,
        comment="资产类型",
    )
    asset_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment=(
            "资产业务编码（metric_code / entity_name / "
            "term_code / dim_code / code）"
        ),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "asset_type", "asset_id", name="uk_fav_user_asset"),
        # 同资产类型下按收藏时间排序的常用查询路径
        Index("ix_fav_user_type_time", "user_id", "asset_type", "created_at"),
    )


class UserPreference(Base, BaseModel):
    """用户偏好（TD §5.4 user_preference）。

    preference_key 区分 default_domain / search_scope / theme 等；收藏已迁移至 favorite 表。
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


class QueryRequesterType(enum.StrEnum):
    """提数请求方类型（响应时效 KPI 数据源 query_log）。"""

    API_CLIENT = "api_client"
    INTERNAL = "internal"


class QueryLog(Base, TimestampMixin):
    """提数查询日志（TD §12.6 增强：响应时效 KPI 数据源）。

    每次真实执行查询（POST /consume/query 与 POST /consume/metrics/{code}/query）后
    best-effort 落一条：记录指标、请求方、耗时（毫秒）、结果状态，供平台响应时效统计
    （日均请求量 / avg / p95 / p99）使用。查询耗时在 API 层计时，落库独立 try/except，
    失败绝不阻断查询响应。
    """

    __tablename__ = "query_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    metric_code: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, comment="被查询指标编码"
    )
    requester_type: Mapped[QueryRequesterType] = mapped_column(
        Enum(QueryRequesterType, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        comment="请求方类型：api_client 接入方 / internal 内部用户",
    )
    requester_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, comment="请求方 ID（client_id / 用户 ID）"
    )
    requester_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="请求方名称快照（client_id / 用户名）"
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="查询耗时（毫秒）"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="结果状态：ok/error"
    )
    error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="失败错误码（status=error 时）"
    )

    __table_args__ = (Index("ix_query_log_created", "created_at"),)
