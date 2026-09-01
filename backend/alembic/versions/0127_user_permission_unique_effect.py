"""user_permission 唯一约束升级为 (user_id, action, effect)。

背景：0126 引入 ``effect`` 列后，同一 (user_id, action) 需要 allow/deny 两行共存
（deny 优先于 grant，fail-closed）。但唯一约束 ``uk_user_permission_user_action``
仍是旧的 ``(user_id, action)``——且 repository 整表替换采用「软删 + 重建」，
软删行（``deleted_at`` 非空）仍占据唯一键，导致同一 action 二次保存时
``Duplicate entry ... uk_user_permission_user_action``（500）。

本迁移：
1. 物理清理软删残留行（直挂配置是「当前状态」而非历史事实，软删行无保留价值；
   也避免重建唯一约束时被残留行撞键）。
2. 唯一约束升级为 ``(user_id, action, effect)``——allow/deny 两行可共存，
   且「软删重建」类操作不再受旧键约束干扰（repository 将改为物理删除）。

幂等：drop/create 由 alembic 版本表保证只执行一次；DELETE 软删行为纯清理。
downgrade 对称还原旧唯一约束（effect 列保留，由 0126 downgrade 处理）。
"""

from __future__ import annotations

from alembic import op

revision = "0127_user_permission_unique_effect"
down_revision = "0126_user_permission_effect"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 清理软删残留（避免 create 新唯一约束时被残留行撞键）
    op.execute("DELETE FROM user_permission WHERE deleted_at IS NOT NULL")
    # 2. 先建新约束（(user_id, action, effect) 更细粒度，旧数据天然满足），再 drop 旧约束
    op.create_unique_constraint(
        "uk_user_permission_user_action_effect",
        "user_permission",
        ["user_id", "action", "effect"],
    )
    op.drop_constraint(
        "uk_user_permission_user_action", "user_permission", type_="unique"
    )


def downgrade() -> None:
    op.create_unique_constraint(
        "uk_user_permission_user_action", "user_permission", ["user_id", "action"]
    )
    op.drop_constraint(
        "uk_user_permission_user_action_effect",
        "user_permission",
        type_="unique",
    )
