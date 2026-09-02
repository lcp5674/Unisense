"""新建 seed_marker 表：参照数据「只初始化一次」的持久标记。

背景：部署自举（scripts/bootstrap.py）把首次初始化收敛为启动期自动执行，
但参照数据（维度/术语/业务主题域）seed 的「成员指纹不一致 → 删旧重灌」语义
会在迭代重建容器时覆盖业务运行期修改。标记存 DB（非 Redis），容器重建后仍
存在 → bootstrap 检测到标记即跳过，实现「首次零手工、迭代不重灌」。

幂等：create_table 由 alembic 版本表保证只执行一次；downgrade 对称删除。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0130_seed_marker"
down_revision = "0129_user_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "seed_marker",
        sa.Column("name", sa.String(64), primary_key=True, comment="标记名"),
        sa.Column("version", sa.String(32), nullable=False, comment="数据版本"),
        sa.Column(
            "seeded_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            comment="初始化完成时间",
        ),
        sa.Column("detail", sa.JSON(), nullable=True, comment="执行摘要"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )


def downgrade() -> None:
    op.drop_table("seed_marker")
