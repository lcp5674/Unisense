"""metric_template 补齐 OneData 字段（方案A：模板对齐当前指标注册信息结构）。

背景：OneData 重构后指标注册升级为「逻辑度量 + 挂载实体 + 三方责任」新结构，
模板仍是旧字段集——原子模板实例化必 422（缺 measure_id）、派生模板实例化出
"无家"指标（缺 mount）、三方责任只能创建后再补。本迁移补齐模板的 OneData 字段：

- ``measure_id``：逻辑度量预设（原子指标 OneData 原子层，实例化时继承度量格式/单位）；
- ``mount``（JSON）：挂载实体预设（派生指标：源表/列/粒度/周期/域）；
- 口径三方责任 6 字段（product_owner_id/tech_owner_id/dw_developer_id + 外部人员
  名称兜底 _name），与 metric 表字段命名一致。

另清洗存量非法 ``serving_mode`` 枚举值：模板编辑页曾提供 "REALTIME" 选项，但
MetricCreateRequest 合法枚举为 BATCH_ONLY/REALTIME_ONLY/BATCH_REALTIME_DUAL——
存量含 "REALTIME" 的模板在实例化/编辑时会撞 Literal 校验 422，统一规整为
"REALTIME_ONLY"（语义一致：实时服务）。其他预设字段前端仅提供合法枚举值，无迁移。

revision 挂 0084_master_data_review（当前线性链后继）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0085_metric_template_onedata"
down_revision = "0084_master_data_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("metric_template", sa.Column("measure_id", sa.BigInteger(), nullable=True,
                                               comment="逻辑度量预设（原子指标 OneData 原子层）"))
    op.create_foreign_key("fk_template_measure", "metric_template", "measure_catalog",
                          ["measure_id"], ["id"])
    op.add_column("metric_template", sa.Column("mount", sa.JSON(), nullable=True,
                                               comment="挂载实体预设（派生指标：源表/列/粒度/周期/域）"))
    op.add_column("metric_template", sa.Column("product_owner_id", sa.BigInteger(), nullable=True,
                                               comment="产品需求方用户 ID 预设"))
    op.add_column("metric_template", sa.Column("tech_owner_id", sa.BigInteger(), nullable=True,
                                               comment="技术方用户 ID 预设"))
    op.add_column("metric_template", sa.Column("dw_developer_id", sa.BigInteger(), nullable=True,
                                               comment="数仓开发用户 ID 预设"))
    op.add_column("metric_template", sa.Column("product_owner_name", sa.String(length=128),
                                               nullable=True, comment="产品需求方名称预设"))
    op.add_column("metric_template", sa.Column("tech_owner_name", sa.String(length=128),
                                               nullable=True, comment="技术方名称预设"))
    op.add_column("metric_template", sa.Column("dw_developer_name", sa.String(length=128),
                                               nullable=True, comment="数仓开发名称预设"))
    # 存量非法枚举清洗：编辑页曾提供 serving_mode="REALTIME"，非 MetricCreateRequest 合法值
    # （BATCH_ONLY/REALTIME_ONLY/BATCH_REALTIME_DUAL）——实例化/编辑会 422，统一规整为 REALTIME_ONLY。
    op.execute(
        "UPDATE metric_template SET serving_mode = 'REALTIME_ONLY' "
        "WHERE serving_mode = 'REALTIME'"
    )


def downgrade() -> None:
    op.drop_constraint("fk_template_measure", "metric_template", type_="foreignkey")
    for col in (
        "measure_id", "mount", "product_owner_id", "tech_owner_id", "dw_developer_id",
        "product_owner_name", "tech_owner_name", "dw_developer_name",
    ):
        op.drop_column("metric_template", col)
