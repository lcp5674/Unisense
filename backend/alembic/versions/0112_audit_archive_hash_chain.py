"""审计归档日志增加 sha256 哈希链（content_sha256 + prev_sha256）

Revision ID: 0112_audit_archive_hash_chain
Revises: 0111_measure_catalog_row_version
Create Date: 2026-08-28

背景（S18 审查修复）：
- 审计归档上传 MinIO 后物理删除热表行，MinIO 未配 object-lock，JSONL 无完整性链——
  持有 MinIO 凭据者可静默篡改冷存审计。
- 为每个归档文件记录 sha256（content_sha256）+ 上一文件哈希（prev_sha256）形成链；
  篡改任一文件会破坏链，独立介质核对可发现。

可逆：downgrade 删除两列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0112_audit_archive_hash_chain"
down_revision = "0111_measure_catalog_row_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audit_archive_log",
        sa.Column("content_sha256", sa.String(64), nullable=True, comment="本归档文件 sha256"),
    )
    op.add_column(
        "audit_archive_log",
        sa.Column("prev_sha256", sa.String(64), nullable=True, comment="上一归档文件 sha256（哈希链）"),
    )


def downgrade() -> None:
    op.drop_column("audit_archive_log", "prev_sha256")
    op.drop_column("audit_archive_log", "content_sha256")
