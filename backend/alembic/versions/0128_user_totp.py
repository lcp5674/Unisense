"""user 表新增 TOTP 双因子认证列。

背景：P2 加固——登录环节补双因子认证（RFC 6238 TOTP）。``totp_secret``
存 Fernet 加密后的密钥（setup 时写入、confirm 校验动态码后启用），
``totp_enabled`` 标记是否启用（启用后登录需密码 + 动态码两步）。

幂等：add_column 由 alembic 版本表保证只执行一次。
downgrade 对称删除两列（无存量数据迁移语义——未启用用户两列均空/False）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0128_user_totp"
down_revision = "0127_user_permission_unique_effect"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "totp_secret",
            sa.String(512),
            nullable=True,
            comment="TOTP 双因子密钥（Fernet 加密存储，setup 时写入、confirm 时启用）",
        ),
    )
    op.add_column(
        "user",
        sa.Column(
            "totp_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="是否已启用 TOTP 双因子认证",
        ),
    )


def downgrade() -> None:
    op.drop_column("user", "totp_enabled")
    op.drop_column("user", "totp_secret")
