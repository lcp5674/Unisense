"""conflict 表加活跃对唯一索引（防并发双落 OPEN，P11 C-3）

Revision ID: 0100
Revises: 0099
Create Date: 2026-08-27

背景：冲突 pair 走「查库再插」（count_open_for_pair 无锁 SELECT 后插入），
无 DB 级兜底——两并发请求（如两批 batch_register 互相命中）可对同一对指标
落两条 OPEN，仲裁台重复处置。

方案：新增生成列 ``active_pair``（仅活跃未决冲突有值，关闭/软删/双侧无 id
时置 NULL），对生成列建唯一索引。MySQL 唯一索引中 NULL 不参与约束，因此
历史 RESOLVED/软删记录与幽灵记录不冲突，只有「同一对指标同时 OPEN」才被
DB 拦截——恰是并发竞态要防的场景。

注意：生成列仅建在 DB 层（ORM 不映射，避免全列查询/写入问题），应用层在
``ConflictService`` 落库处捕获 IntegrityError 兜底（转为"已存在跳过"）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None

#: 生成列表达式：活跃未决（非软删 + OPEN/NEGOTIATING/ESCALATED + 至少一侧有 id）才生成键。
_GENERATED_COL = (
    "CASE "
    "WHEN deleted_at IS NOT NULL THEN NULL "
    "WHEN status NOT IN ('OPEN','NEGOTIATING','ESCALATED') THEN NULL "
    "WHEN metric_a IS NULL AND metric_b IS NULL THEN NULL "
    "ELSE CONCAT_WS(':', IFNULL(metric_a,''), IFNULL(metric_b,'')) "
    "END"
)


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE conflict "
        f"ADD COLUMN active_pair VARCHAR(96) "
        f"GENERATED ALWAYS AS ({_GENERATED_COL}) STORED "
        f"COMMENT '活跃未决冲突对（唯一键承载，防并发双落）'"
    )
    op.create_index(
        "uk_conflict_active_pair", "conflict", ["active_pair"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uk_conflict_active_pair", table_name="conflict")
    op.drop_column("conflict", "active_pair")
