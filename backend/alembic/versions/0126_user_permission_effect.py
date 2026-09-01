"""user_permission 增加 effect 列（allow/deny 负向收窄）。

背景：用户授权弹窗仅支持正向直挂（allow），角色已含权限点为灰色只读、无法对
个别用户收窄。本迁移新增 ``effect`` 列（默认 ``allow``），使同一张表可表达
「直挂授权」与「用户级禁用（deny）」两类语义——``my_permissions`` 合并时
``(角色继承 ∪ 直挂 allow) − 直挂 deny``，deny 优先于 grant（fail-closed）。

幂等：add_column 由 alembic 版本表保证只执行一次；downgrade 删除该列，存量
allow 记录不受影响。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0126_user_permission_effect"
down_revision = "0124_tracking_event_target_type_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_permission",
        sa.Column(
            "effect",
            sa.String(length=8),
            nullable=False,
            server_default="allow",
            comment="授权效果: allow 正向授权 / deny 负向收窄",
        ),
    )


def downgrade() -> None:
    op.drop_column("user_permission", "effect")
