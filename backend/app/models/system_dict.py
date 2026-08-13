"""系统字典模型（统一管理粒度/单位/聚合等枚举字段，对齐 TD §12 / spec FR-005~FR-007）。

每个字典项含编码+显示名+排序+状态，支持启用/停用。
被指标引用的字典项不可删除，只能停用。
"""

from __future__ import annotations

from sqlalchemy import Enum, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class SystemDict(Base, BaseModel):
    """系统字典实体。

    Attributes:
        dict_type: 字典类型
            （granularity/unit/aggregation/time_semantics/freshness/dw_layer/
            metric_type/additivity/serving_mode/metric_tier）。
        code: 字典项编码。
        label: 显示名。
        sort_order: 排序序号。
        status: 状态（active/inactive）。
        description: 描述。
    """

    __tablename__ = "system_dict"

    dict_type: Mapped[str] = mapped_column(String(64), nullable=False, comment="字典类型")
    code: Mapped[str] = mapped_column(String(64), nullable=False, comment="字典项编码")
    label: Mapped[str] = mapped_column(String(128), nullable=False, comment="显示名")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="排序序号")
    status: Mapped[str] = mapped_column(
        Enum("active", "inactive", name="dict_status"),
        nullable=False,
        default="active",
        comment="状态",
    )
    description: Mapped[str | None] = mapped_column(String(256), nullable=True, comment="描述")

    __table_args__ = (
        Index("uk_dict_type_code", "dict_type", "code", unique=True),
        Index("idx_dict_type", "dict_type"),
        Index("idx_dict_status", "status"),
    )
