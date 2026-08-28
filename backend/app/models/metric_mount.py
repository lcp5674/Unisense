"""指标挂载实体（OneData 挂载层，TD §4.2 dataset_metric）。

对齐 `docs/指标设计以及界限说明.md` 三条硬边界：
- 粒度由挂载表决定，不进指标定义（§2.3 第 3 条 / §6）——本实体承载 granularity
- 原子指标不挂物理表；挂载只出现在派生指标上（派生 = 原子 + 时间 + 业务限定 + 挂载）
- 一个派生指标可挂多个挂载点（多变体：粒度/业务限定/周期组合，2026-08-27 放开
  uk_mount_metric 唯一约束改普通索引；存量 1:1 数据天然兼容，N=1 特例）

字段：源表/源列（映射原子逻辑度量）/粒度/默认统计周期/业务域/业务限定（变体级）。
"""

from __future__ import annotations

from sqlalchemy import JSON, BigInteger, ForeignKey, Index, String
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
    granularity: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="粒度（一行代表什么）"
    )
    #: 粒度维度（组合粒度，2026-08-28 方案 B）：参与唯一性的业务实体维度列表——
    #: 「按月+医院统计订单金额」= 主粒度 month + 粒度维度 ["hospital"]。
    #: 与主粒度（时间频率）语义区分：主粒度表达「什么时候的」，粒度维度表达「谁的」。
    #: 空 = 纯时间粒度；多值 = 组合粒度（粒度维度是唯一性构成者，消费 SQL 固定进 GROUP BY）。
    granularity_dims: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True,
        comment="粒度维度（组合粒度唯一性实体列表，如 [\"hospital\"]）",
    )
    default_period: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="默认统计周期（day/month/quarter…）"
    )
    domain: Mapped[str] = mapped_column(String(64), nullable=False, comment="业务域")
    #: 业务限定（变体级，OneData 派生 = 基础原子 + 业务限定 + 周期）；缺省继承
    #: 指标级 definition_json.business_filter（default_business_filter 兜底）。
    business_filter: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="业务限定（变体级，如 病种=门特；缺省继承指标级）"
    )
    # ---- 变体级口径三方责任（与 metric 表同构，均可空）----
    # 多挂载（多变体）下不同变体可能归属不同需求方/开发角色（如「医院粒度费用」归
    # 张三、「药品粒度费用」归李四）。缺省继承指标级责任方——空 = 继承；详情页按
    # 行展示归属。仅治理属性，不进口径/破坏性判定（审核仍指标级整体走）。
    product_owner_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="变体级产品需求方用户 ID（缺省继承指标级）"
    )
    tech_owner_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="变体级技术方用户 ID（缺省继承指标级）"
    )
    dw_developer_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="变体级数仓开发用户 ID（缺省继承指标级）"
    )
    # 外部人员名称兜底（对齐 metric 表 product_owner_name 等）：责任方非平台用户时
    # 直接落名称。展示优先级：id 可解析 → 平台用户；id 空但 name 非空 → 外部人员。
    product_owner_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="变体级产品需求方名称（非平台用户直接填写）"
    )
    tech_owner_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="变体级技术方名称（非平台用户直接填写）"
    )
    dw_developer_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="变体级数仓开发名称（非平台用户直接填写）"
    )

    __table_args__ = (
        # 一指标多挂载（多变体）：放开 uk_mount_metric 唯一约束改普通索引
        Index("idx_mount_metric", "metric_id"),
    )
