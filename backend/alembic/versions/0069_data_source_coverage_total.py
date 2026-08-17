"""数据源覆盖度基线字段（TD §2051：coverage = 已采集实体 / 源端实体总数）。

背景：`recompute_coverage` 此前以 ``quota.max_scan_rows``（行数配额）作分母——
未配置 quota 时恒为 0.0，且语义与「覆盖度 = 已采集实体 / 源端实体总数」不符。
本迁移新增 ``source_total_entities``（源端实体总数）：每次采集完成后用本次
扫描到的源端实体数刷新基线，coverage 即「已注册活跃实体 / 最近一次扫描实体总数」，
无基线（从未采集）时保持 0.0（覆盖率未知）。

revision 挂 0068_user_permission（当前线性链后继）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0069_data_source_coverage_total"
down_revision = "0068_user_permission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "data_source",
        sa.Column(
            "source_total_entities",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="源端实体总数（最近一次采集扫描数，coverage 分母基线）",
        ),
    )


def downgrade() -> None:
    op.drop_column("data_source", "source_total_entities")
