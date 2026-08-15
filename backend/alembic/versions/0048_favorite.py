"""新建 favorite 通用收藏表，并迁移 UserPreference.pinned_metrics 存量数据。

背景（C 层多资产收藏）：
- 原收藏以 UserPreference.preference_key='pinned_metrics' 的 JSON 数组存储（仅指标），
  无法承载收藏时间与多资产类型（数据表/术语/维度/模板）。
- 演进为独立 favorite 表：user_id × asset_type × asset_id（资产业务编码），
  天然支持收藏时间（created_at）、唯一约束与软删除。

迁移内容：
1. 建 favorite 表（InnoDB + utf8mb4，对齐 TD §4.1 与 DEV_GUIDE §8a.4）。
2. 将存量 pinned_metrics 逐条写入 favorite（asset_type=METRIC, asset_id=metric_code），
   保留 UserPreference 原行（兼容既有读取方，后续可清理）。

可逆：downgrade 删除 favorite 表（存量 UserPreference 数据已在表内，无需恢复）。
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0048_favorite"
down_revision = "0047_metric_arbitration_mark"
branch_labels = None
depends_on = None


def _table_opts() -> dict[str, str]:
    return {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "favorite",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="用户 ID"),
        sa.Column(
            "asset_type",
            sa.Enum("METRIC", "TABLE", "TERM", "DIMENSION", "TEMPLATE", name="favorite_asset_type"),
            nullable=False,
            comment="资产类型",
        ),
        sa.Column("asset_id", sa.String(64), nullable=False, comment="资产业务编码"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "asset_type", "asset_id", name="uk_fav_user_asset"),
        sa.Index("ix_fav_user_type_time", "user_id", "asset_type", "created_at"),
        **_table_opts(),
    )
    op.create_index("ix_favorite_user_id", "favorite", ["user_id"], unique=False)
    op.create_index("ix_favorite_asset_type", "favorite", ["asset_type"], unique=False)

    # ---- 迁移存量 pinned_metrics ----
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT user_id, preference_value FROM user_preference "
            "WHERE preference_key = 'pinned_metrics' AND deleted_at IS NULL"
        )
    ).fetchall()
    for user_id, value_json in rows:
        try:
            value = json.loads(value_json) if isinstance(value_json, str) else value_json
        except (TypeError, ValueError):
            continue
        metrics = value.get("metrics") if isinstance(value, dict) else None
        if not metrics:
            continue
        for code in metrics:
            if not isinstance(code, str) or not code:
                continue
            bind.execute(
                sa.text(
                    "INSERT IGNORE INTO favorite "
                    "(user_id, asset_type, asset_id, created_at, updated_at) "
                    "VALUES (:uid, 'METRIC', :code, NOW(6), NOW(6))"
                ),
                {"uid": user_id, "code": code},
            )


def downgrade() -> None:
    op.drop_index("ix_favorite_asset_type", table_name="favorite")
    op.drop_index("ix_favorite_user_id", table_name="favorite")
    op.drop_table("favorite")
