"""指标模板模型（P2: US13 指标模板化注册）。

提供预设的指标模板，用户可从模板快速创建指标，减少重复填写。
模板包含：预填字段默认值、必填字段列表、适用域、模板版本。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class MetricTemplate(Base, BaseModel):
    """指标模板实体。

    Attributes:
        code: 模板编码（唯一），格式: tpl_{domain}_{name}。
        name: 模板名称。
        domain: 适用域。
        description: 模板说明。
        defaults_json: 预填字段默认值（JSON）。
        required_fields: 必填字段列表（JSON array of string）。
        type: 指标类型预设。
        granularity: 粒度预设。
        unit: 单位预设。
        aggregation: 聚合方式预设。
        time_semantics: 时间语义预设。
        freshness: 数据新鲜度预设。
        dw_layer: 数仓分层预设。
        serving_mode: 服务模式预设。
        additivity: 可加性预设。
        metric_tier: 指标分级预设。
        measure_id: 逻辑度量预设（原子指标 OneData 原子层）。
        mount: 挂载实体预设（派生指标）。
        product_owner_id/tech_owner_id/dw_developer_id(+_name): 口径三方责任预设。
        version: 模板版本号。
        is_active: 是否启用。
        owner_id: 责任人（Owner）ID（可空，模板负责人）。
        created_by: 创建人 ID。
        published_at: 发布时间（可空）。
    """

    __tablename__ = "metric_template"

    # ---- OneData 预设（方案A：模板对齐当前指标注册信息结构）----
    #: 关联逻辑度量 ID（原子指标预设；实例化 atomic 时透传给 MetricCreateRequest.measure_id）
    measure_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("measure_catalog.id", name="fk_template_measure"),
        nullable=True,
        comment="逻辑度量预设（原子指标 OneData 原子层，实例化时继承度量格式/单位）",
    )
    #: 挂载实体预设（派生指标专用：源表/列/粒度/周期/域，实例化 derived 时透传落 metric_mount）
    mount: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="挂载实体预设（派生指标：源表/列/粒度/周期/域）"
    )
    #: 口径三方责任预设（实例化时作为指标默认责任方，均可空）
    product_owner_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="产品需求方用户 ID 预设"
    )
    tech_owner_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="技术方用户 ID 预设"
    )
    dw_developer_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="数仓开发用户 ID 预设"
    )
    product_owner_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="产品需求方名称预设（非平台用户直接填写）"
    )
    tech_owner_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="技术方名称预设（非平台用户直接填写）"
    )
    dw_developer_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="数仓开发名称预设（非平台用户直接填写）"
    )

    code: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="模板编码（唯一）"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="模板名称")
    domain: Mapped[str] = mapped_column(String(64), nullable=False, comment="适用域")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="模板说明")

    # ---- 预设字段 ----
    defaults_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, comment="预填字段默认值"
    )
    required_fields: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="必填字段列表"
    )

    # ---- 指标属性预设 ----
    type: Mapped[str | None] = mapped_column(
        Enum("atomic", "derived", "composite", name="template_metric_type"),
        nullable=True,
        comment="指标类型预设",
    )
    granularity: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="粒度预设")
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="单位预设")
    aggregation: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="聚合方式预设"
    )
    time_semantics: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="时间语义预设"
    )
    freshness: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="数据新鲜度预设"
    )
    dw_layer: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="数仓分层预设")
    serving_mode: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="服务模式预设"
    )
    additivity: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="可加性预设")
    metric_tier: Mapped[str | None] = mapped_column(
        String(8), nullable=True, comment="指标分级预设"
    )

    # ---- 版本与状态 ----
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="模板版本号")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="是否启用"
    )
    owner_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("user.id", name="fk_template_owner"),
        nullable=True,
        comment="责任人（Owner）ID",
    )
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="创建人 ID")
    published_at: Mapped[datetime | None] = mapped_column(nullable=True, comment="发布时间")

    __table_args__ = (
        Index("idx_template_domain", "domain"),
        Index("idx_template_active", "is_active"),
        Index("idx_template_owner", "owner_id"),
    )

    def to_dict(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        data = super().to_dict(include_sensitive=include_sensitive)
        data["defaults_json"] = self.defaults_json
        data["required_fields"] = self.required_fields
        return data
