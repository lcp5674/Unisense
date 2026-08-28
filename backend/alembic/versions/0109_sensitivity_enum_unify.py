"""敏感级别枚举统一：db_catalog / classification 两表对齐为 6 值并集

Revision ID: 0109_sensitivity_enum_unify
Revises: 0108_metric_aggregation_nullable
Create Date: 2026-08-28

背景（模型/DB 交叉错位，写入可 Data truncated 1265）：
- ``db_catalog.sensitivity_level`` 模型用 ``SensitivityLevelEnum``（含 NEEDS_REVIEW），
  但 DB 实际枚举为 PUBLIC/INTERNAL/CONFIDENTIAL/PII/UNKNOWN（无 NEEDS_REVIEW）；
- ``classification.sensitivity_level`` 模型用 ``SensitivityLevel``（含 UNKNOWN），
  但 DB 实际枚举为 PUBLIC/INTERNAL/CONFIDENTIAL/PII/NEEDS_REVIEW（无 UNKNOWN）。

任意一方写入对方枚举缺失值（如治理服务把 NEEDS_REVIEW 写入 db_catalog、
collector/分级引擎把 UNKNOWN 写入 classification）都会触发 MySQL 1265。

本迁移将两表枚举统一为并集 6 值：
``PUBLIC/INTERNAL/CONFIDENTIAL/PII/NEEDS_REVIEW/UNKNOWN``，与
``app.models.governance.SensitivityLevel``（权威枚举）完全一致。

可逆：downgrade 收回各自多余枚举值（需确保无存量数据使用这些值时方可执行）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0109_sensitivity_enum_unify"
down_revision = "0108_metric_aggregation_nullable"
branch_labels = None
depends_on = None

_UNIFIED = (
    "PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", "NEEDS_REVIEW", "UNKNOWN",
)
_OLD_DB_CATALOG = ("PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", "UNKNOWN")
_OLD_CLASSIFICATION = ("PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", "NEEDS_REVIEW")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.alter_column(
        "db_catalog",
        "sensitivity_level",
        existing_type=sa.Enum(*_OLD_DB_CATALOG, name="sensitivity_enum"),
        type_=sa.Enum(*_UNIFIED, name="sensitivity_enum"),
        existing_nullable=False,
    )
    op.alter_column(
        "classification",
        "sensitivity_level",
        existing_type=sa.Enum(*_OLD_CLASSIFICATION, name="sensitivity_enum"),
        type_=sa.Enum(*_UNIFIED, name="sensitivity_enum"),
        existing_nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.alter_column(
        "db_catalog",
        "sensitivity_level",
        existing_type=sa.Enum(*_UNIFIED, name="sensitivity_enum"),
        type_=sa.Enum(*_OLD_DB_CATALOG, name="sensitivity_enum"),
        existing_nullable=False,
    )
    op.alter_column(
        "classification",
        "sensitivity_level",
        existing_type=sa.Enum(*_UNIFIED, name="sensitivity_enum"),
        type_=sa.Enum(*_OLD_CLASSIFICATION, name="sensitivity_enum"),
        existing_nullable=False,
    )
