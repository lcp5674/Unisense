"""目录搜索性能索引（P1-8）。

背景：``list_catalogs`` 关键字搜索与库名（entity_name 前缀）浏览缺乏索引，
表量大时每次 ``cast(schema_json, String).ilike`` 全表扫描。本迁移为
``db_catalog.entity_name`` 加前缀索引（utf8mb4 下 191 字符前缀即 764 字节，
落在 InnoDB 索引上限内），加速：
- 按 source_id + entity_name 前缀（``库.表``）浏览目录；
- 关键字搜索中 ``entity_name LIKE 'kw%'`` 前缀匹配分支（优化器可用索引）。

注意：``%kw%`` 中间匹配与 schema_json 字段名搜索仍为全表扫描——那是低频
治理操作，由 SQL 端聚合的 description-coverage 分担主要负载；全文检索
（ES/倒排）留待后续专门迭代。

revision 挂 0070_sensitive_rule_conf_seed（当前线性链后继）。
"""

from __future__ import annotations

from alembic import op

revision = "0071_catalog_entity_name_idx"
down_revision = "0070_sensitive_rule_conf_seed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 前缀索引：utf8mb4 下 191 字符 = 764 字节（InnoDB 单列索引上限 3072 字节内）
    op.create_index(
        "idx_db_catalog_entity_name",
        "db_catalog",
        ["source_id", "entity_name(191)"],
    )


def downgrade() -> None:
    op.drop_index("idx_db_catalog_entity_name", table_name="db_catalog")
