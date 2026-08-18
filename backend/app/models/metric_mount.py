"""指标挂载实体（OneData 挂载层，TD §4.2 dataset_metric）。

对齐 `docs/指标设计以及界限说明.md` 三条硬边界：
- 粒度由挂载表决定，不进指标定义（§2.3 第 3 条 / §6）——本实体承载 granularity
- 原子指标不挂物理表；挂载只出现在派生指标上（派生 = 原子 + 时间 + 业务限定 + 挂载）
- 一个派生指标一个挂载点（首期唯一约束），后续可扩展为多挂载

字段：源表/源列（映射原子逻辑度量）/粒度/默认统计周期/业务域。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class MetricMount(Base, BaseModel):
    __tablename__ = "metric_mount"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    metric_id: Mapped[int] = mapped_column(
        ForeignKey("metric.id", name="fk_mount_metric"),
        nullable=False,
        comment="所属指标 ID（派生指标；原子/复合不挂载）",
    )
    source_table: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="源表（可带库前缀，如 dwd.sales_detail）"
    )
    source_column: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="度量列（映射原子逻辑度量）"
    )
    #: 粒度从 metric.granularity 下沉到此（界限文档 §2.3 第 3 条）
    granularity: Mapped[str] = mapped_column(String(64), nullable=False, comment="粒度（一行代表什么）")
    default_period: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="默认统计周期（day/month/quarter…）"
    )
    domain: Mapped[str] = mapped_column(String(64), nullable=False, comment="业务域")

    __table_args__ = (
        UniqueConstraint("metric_id", name="uk_mount_metric"),
    )
