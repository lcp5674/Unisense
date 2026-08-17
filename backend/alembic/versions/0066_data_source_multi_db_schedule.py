"""data_source 表新增多目标库与调度启停能力。

需求（采集增强）：
1. 多目标库：``databases`` JSON 列存储目标库列表（None=采集全部库/单库配置）；
2. 调度启停：``schedule_enabled`` BOOLEAN 独立于 ``enabled``（数据源启停）——
   停用调度仅保留 cron 配置不触发定时采集，源仍可手动采集。
"""

from __future__ import annotations

from alembic import op

revision = "0066_data_source_multi_db_schedule"
down_revision = "0065_health_status_degraded"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # databases 为 MySQL 保留字，必须反引号转义（否则 ALTER 触发 1064 语法错误）
    op.execute(
        "ALTER TABLE data_source "
        "ADD COLUMN `databases` JSON NULL COMMENT '目标数据库列表（None=采集全部库/单库配置）' "
        "AFTER connection_config"
    )
    op.execute(
        "ALTER TABLE data_source "
        "ADD COLUMN schedule_enabled TINYINT(1) NOT NULL DEFAULT 1 "
        "COMMENT '是否启用定时调度（停用后仅保留 cron 配置不触发）' AFTER schedule_cron"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE data_source DROP COLUMN schedule_enabled")
    op.execute("ALTER TABLE data_source DROP COLUMN `databases`")
