"""指标挂载实体（OneData 挂载层，TD §4.2 dataset_metric）。

对齐 `docs/指标设计以及界限说明.md` 三条硬边界：
- 粒度由挂载表决定，不进指标定义（§2.3 第 3 条 / §6）——本实体承载 granularity
- 原子指标不挂物理表；挂载只出现在派生指标上（派生 = 原子 + 时间 + 业务限定 + 挂载）
- 一个派生指标可挂多个挂载点（多变体：粒度/业务限定/周期组合，2026-08-27 放开
  uk_mount_metric 唯一约束改普通索引；存量 1:1 数据天然兼容，N=1 特例）

字段：源表/源列（映射原子逻辑度量）/粒度/默认统计周期/业务域/业务限定（变体级）。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Index, String
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
    #: 业务限定（变体级，OneData 派生 = 基础原子 + 业务限定 + 周期）；缺省继承
    #: 指标级 definition_json.business_filter（default_business_filter 兜底）。
    business_filter: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="业务限定（变体级，如 病种=门特；缺省继承指标级）"
    )

    __table_args__ = (
        # 一指标多挂载（多变体）：放开 uk_mount_metric 唯一约束改普通索引
        Index("idx_mount_metric", "metric_id"),
    )
