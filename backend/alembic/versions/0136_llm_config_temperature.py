"""LLM 实例增加 temperature（采样温度）可视化配置列。

背景：各业务调用点采样温度硬编码 0（确定性优先），LLM 实例无法配置。
本迁移为 ``llm_config`` 增加可空 ``temperature``（0~2）——管理员可在
「系统配置 → LLM 实例」编辑弹窗可视化配置；None=不覆盖（沿用调用方温度 0），
配置后该实例所有请求使用此采样温度（生成类场景可调高以增加多样性）。
存量行保持 NULL（不改变既有确定性行为）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0136_llm_config_temperature"
down_revision = "0135_llm_config_max_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 幂等：已加过（并行会话/重复执行）则跳过
    conn = op.get_bind()
    cols = {
        r[0]
        for r in conn.execute(
            sa.text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'llm_config'"
            )
        )
    }
    if "temperature" not in cols:
        op.add_column(
            "llm_config",
            sa.Column(
                "temperature",
                sa.Float(),
                nullable=True,
                comment="实例采样温度（None=不覆盖，沿用调用方温度 0；配置后该实例请求使用此温度 0~2）",
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    cols = {
        r[0]
        for r in conn.execute(
            sa.text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'llm_config'"
            )
        )
    }
    if "temperature" in cols:
        op.drop_column("llm_config", "temperature")
