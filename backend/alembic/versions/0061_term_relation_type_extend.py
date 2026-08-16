"""term_relation.relation_type 枚举扩展（TD §12.14 / FR-08）。

背景：
- 术语关系原仅 4 种（SYNONYM_OF / BROADER_THAN / NARROWER_THAN / RELATED_TO），
  无法表达业务反义、依赖、派生、实例等语义，产品要求丰富关系类型。
- 扩展为 8 值：新增 ANTONYM_OF / DEPENDS_ON / DERIVED_FROM / INSTANCE_OF。

可逆：downgrade 从枚举中移除 4 个新值（存量新类型数据在回退前需清理——
本迁移仅改枚举定义，不触碰数据行）。
"""

from __future__ import annotations

from alembic import op

revision = "0061_term_relation_type_extend"
down_revision = "0060_notification_actor"
branch_labels = None
depends_on = None

# 完整的 8 值枚举（保持与 app/models/glossary.py 的 TermRelationType 严格一致）
_FULL_ENUM = (
    "('SYNONYM_OF','BROADER_THAN','NARROWER_THAN','RELATED_TO',"
    "'ANTONYM_OF','DEPENDS_ON','DERIVED_FROM','INSTANCE_OF')"
)
# 回退为 4 值枚举
_BASE_ENUM = "('SYNONYM_OF','BROADER_THAN','NARROWER_THAN','RELATED_TO')"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.execute(
        f"ALTER TABLE term_relation MODIFY COLUMN relation_type "
        f"ENUM{_FULL_ENUM} NOT NULL COMMENT '关系类型'"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.execute(
        f"ALTER TABLE term_relation MODIFY COLUMN relation_type "
        f"ENUM{_BASE_ENUM} NOT NULL COMMENT '关系类型'"
    )
