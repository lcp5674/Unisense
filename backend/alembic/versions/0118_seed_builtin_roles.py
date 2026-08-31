"""种入内置七角色登记行（role 表 is_custom=False）。

背景（2026-08-31 用户反馈）：权限治理「新建授权」弹窗的角色选项框为空。
根因：``role`` 表自 0004 建表起从未种入数据——内置七角色（RoleName 枚举）只是
代码常量，从未登记进表；``GET /roles/options``（授权弹窗下拉数据源）只查
``role`` 表未删行，故恒为空。而「角色管理」页用 ``list_role_permissions``
（内置角色来自 policy 常量），有值——两处展示不一致。

本迁移把 RoleName 七枚举登记为 role 表行（``is_custom=False``），使授权弹窗
下拉与角色管理页一致（内置 + 自定义并集），且 ``grants.role_id`` 可指向内置
角色行 id。

幂等：对每个内置角色名，仅当 role 表不存在同名行时 INSERT（不覆盖用户数据）。
downgrade 仅删除本迁移种入的内置登记行（``is_custom=False`` 且名字在七枚举内），
不触碰自定义角色（``is_custom=True``）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0118_seed_builtin_roles"
down_revision = "0117_pii_rule_real_name_context"
branch_labels = None
depends_on = None

#: 内置七角色（对齐 models/governance.py RoleName）
BUILTIN_ROLES: list[tuple[str, str]] = [
    ("platform_admin", "平台管理员（跨域运维直通，受保护角色）"),
    ("domain_admin", "域管理员（本域业务全写 + 评审 + 授权）"),
    ("metric_owner", "指标负责人（指标生命周期主责）"),
    ("reviewer", "评审员（主数据 / 指标评审）"),
    ("compliance_officer", "合规官（PII 复核 / 审计治理）"),
    ("analyst", "分析师（只读消费 + 查询执行）"),
    ("viewer", "只读用户（浏览与检索）"),
]


def upgrade() -> None:
    bind = op.get_bind()
    for name, description in BUILTIN_ROLES:
        existing = bind.execute(
            sa.text("SELECT COUNT(*) FROM role WHERE name = :name"),
            {"name": name},
        ).scalar()
        if existing:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO role (name, description, is_custom, created_at, updated_at) "
                "VALUES (:name, :desc, 0, NOW(), NOW())"
            ),
            {"name": name, "desc": description},
        )


def downgrade() -> None:
    bind = op.get_bind()
    names = [name for name, _ in BUILTIN_ROLES]
    bind.execute(
        sa.text(
            "DELETE FROM role WHERE is_custom = 0 AND name IN :names"
        ),
        {"names": tuple(names)},
    )
