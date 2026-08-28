"""db_catalog 表级搜索 FULLTEXT 索引（ngram 中文分词）

Revision ID: 0113_db_catalog_fulltext
Revises: 0112_audit_archive_hash_chain
Create Date: 2026-08-28

背景（审查 T19）：
- 资产地图/全局搜索对 db_catalog（平台最大表）用 ``LIKE '%kw%'`` 前导通配符，
  B-tree 索引失效 → 全表扫；2 万表规模可接受，规模化前置须 FULLTEXT。
- ngram 解析器支持中文（2-gram 分词），BOOLEAN MODE 短语查询 ``"kw"`` 语义
  接近子串匹配；查询代码对 <2 字符回退 LIKE（ngram 最小 token=2）。

仅 MySQL（InnoDB FULLTEXT + ngram）；SQLite（单测）跳过。可逆。
"""

from __future__ import annotations

from alembic import op

revision = "0113_db_catalog_fulltext"
down_revision = "0112_audit_archive_hash_chain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(
            "ALTER TABLE db_catalog ADD FULLTEXT INDEX "
            "ft_db_catalog_name (entity_name, source_id) WITH PARSER ngram"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("ALTER TABLE db_catalog DROP INDEX ft_db_catalog_name")
