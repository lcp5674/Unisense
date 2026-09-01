"""metric 表搜索 FULLTEXT 索引（ngram 中文分词）

Revision ID: 0124_metric_fulltext
Revises: 0123_query_log_composite_index
Create Date: 2026-09-01

背景（审查 P4）：
- 指标目录关键词搜索对 metric 用 ``LIKE '%kw%'`` 前导通配符（name/description/
  metric_code），B-tree 索引失效 → 每次带关键词的列表请求 2 次全表扫描
  （count + 列表）。
- ngram 解析器支持中文（2-gram 分词），BOOLEAN MODE 短语查询 ``"kw"`` 语义
  接近子串匹配；查询代码对 <2 字符回退 LIKE（ngram 最小 token=2）。

仅 MySQL（InnoDB FULLTEXT + ngram）；SQLite（单测）跳过。可逆。
"""

from __future__ import annotations

from alembic import op

revision = "0124_metric_fulltext"
down_revision = "0123_query_log_composite_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(
            "ALTER TABLE metric ADD FULLTEXT INDEX "
            "ft_metric_search (name, description, metric_code) WITH PARSER ngram"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("ALTER TABLE metric DROP INDEX ft_metric_search")
