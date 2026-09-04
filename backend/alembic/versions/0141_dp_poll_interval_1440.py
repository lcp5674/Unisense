"""dp 同步配置轮询间隔上限放宽：1~60 → 1~1440（最长 24 小时）。

背景：``poll_interval_minutes`` 原设计上限 60 分钟（spec D 系列决策），前端
``max={60}`` 卡死——低频同步（如每日一次 = 1440 分钟）只能靠「停用开关」表达，
而停用是完全不扫描，与「低频扫描」语义不同。本迁移把列注释对齐为 1~1440；
范围校验在 API 层（``PUT /config``，非法值返回 VALIDATION_ERROR），前端控件
改为档位选择（5/15/30/60/120/360/720/1440）+ 自定义输入。列类型（Integer）
与既有数据不变，无数据迁移。
"""

from alembic import op

revision = "0141"
down_revision = "0140"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE dp_sync_config MODIFY COLUMN poll_interval_minutes "
        "INT NOT NULL DEFAULT 5 "
        "COMMENT '轮询间隔（1~1440 分钟，最长 24 小时，前端可配置）'"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE dp_sync_config MODIFY COLUMN poll_interval_minutes "
        "INT NOT NULL DEFAULT 5 "
        "COMMENT '轮询间隔（1~60 分钟，前端可配置）'"
    )
