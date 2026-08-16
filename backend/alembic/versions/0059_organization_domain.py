"""organization 表新增 domain 列（团队可绑定业务域，方案 B）。

背景：
- 方案 B：组织（租户）改称「团队」，用户侧「所属域 + 所属组织」合并为「所属团队」；
  团队可绑定一个业务域（可选），其成员用户的 ``user.domain`` 自动继承团队域。
- ``organization.domain`` 为 String(64) 可空，仅存主题域 code（与 ``user.domain`` 同口径，
  不设外键——主题域可能被删除/停用，域隔离以 PDP 运行时校验为准）。

可逆：downgrade 删除 domain 列（存量行域信息随列删除，符合回退语义）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0059_organization_domain"
down_revision = "0058_lineage_edge_type_extend"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organization",
        sa.Column("domain", sa.String(length=64), nullable=True, comment="所属业务域（可空=不限域，成员自动继承）"),
    )


def downgrade() -> None:
    op.drop_column("organization", "domain")
