"""user 表新增 domains（权限域列表，团队继承 ∪ 显式指定并集）。

背景：方案 B 增强——用户权限域 = 团队继承域 ∪ 单独指定域（并集），
而非二选一。``user.domain`` 保留主域（兼容展示/Owner 责任链），
新增 ``domains`` JSON 存并集后的全部权限域；权限判定统一用
``User.domains_all()``（动态并入所属团队域，团队改域成员自动继承）。

幂等：add_column 由 alembic 版本表保证只执行一次；存量用户 domains 为空
（None），domains_all() 回退为主域 + 团队域，行为与现状一致。
downgrade 对称删除该列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0129_user_domains"
down_revision = "0128_user_totp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "domains",
            sa.JSON(),
            nullable=True,
            comment="权限域列表（团队继承∪显式指定，并集去重）",
        ),
    )


def downgrade() -> None:
    op.drop_column("user", "domains")
