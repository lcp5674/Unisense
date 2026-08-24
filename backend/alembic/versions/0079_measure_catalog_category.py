"""measure_catalog 增加度量分类与统计口径字段。

背景：度量目录从"电商演示"扩展到"医疗实际场景"（HIS 门诊数据），需要按业务视角
组织度量（流量/费用/药品/医保/效率/质量），并在目录中登记统计口径（业务侧如何计算）。
新增两列：
- category：度量分类（FLOW/FEE/DRUG/MEDICAL_INSURANCE/EFFICIENCY/QUALITY/OTHER），
  存量行默认 OTHER，不破坏既有数据。
- stat_caliber：统计口径（自由文本，业务说明）。

revision 挂 0078_metric_responsible_owner_names（当前线性链后继）。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0079_measure_catalog_category"
down_revision = "0078_metric_responsible_owner_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "measure_catalog",
        sa.Column(
            "category",
            sa.String(length=32),
            nullable=False,
            server_default="OTHER",
            comment="度量分类（FLOW/FEE/DRUG/MEDICAL_INSURANCE/EFFICIENCY/QUALITY/OTHER）",
        ),
    )
    op.add_column(
        "measure_catalog",
        sa.Column(
            "stat_caliber",
            sa.Text(),
            nullable=True,
            comment="统计口径（业务侧如何计算该度量）",
        ),
    )


def downgrade() -> None:
    op.drop_column("measure_catalog", "stat_caliber")
    op.drop_column("measure_catalog", "category")
