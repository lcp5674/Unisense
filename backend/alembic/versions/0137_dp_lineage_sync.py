"""dp 调度血缘同步：5 张新表 + LineageEdge.dp_task_refs 列。

对齐 `spec/dp-lineage-ingest/plan.md` §3（数据模型）：
- dp_sync_config / dp_sync_watermark / lineage_field_mapping /
  dp_resolution_ticket / dp_sync_run_log
- lineage_edge 增加 dp_task_refs（Text JSON，D10 任务/节点静态身份快照）
- 全部幂等：表/列存在则跳过（并行会话/重复执行安全）
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0137_dp_lineage_sync"
down_revision = "0136_llm_config_temperature"
branch_labels = None
depends_on = None


def _table_exists(conn, name: str) -> bool:
    rows = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :name"
        ),
        {"name": name},
    )
    return rows.scalar() > 0


def _create_table_if_absent(name: str, columns: list[sa.Column]) -> None:
    conn = op.get_bind()
    if _table_exists(conn, name):
        return
    op.create_table(name, *columns)


def upgrade() -> None:
    # ---- lineage_edge 增加 dp_task_refs 列（幂等） ----
    conn = op.get_bind()
    cols = {
        r[0]
        for r in conn.execute(
            sa.text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'lineage_edge'"
            )
        )
    }
    if "dp_task_refs" not in cols:
        op.add_column(
            "lineage_edge",
            sa.Column(
                "dp_task_refs",
                sa.Text(),
                nullable=True,
                comment="dp 调度任务/节点静态身份 JSON（来源 provenance=dp_sql 时承载）",
            ),
        )

    # ---- dp_sync_config（单行配置） ----
    _create_table_if_absent(
        "dp_sync_config",
        [
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("0"), comment="同步总开关"),
            sa.Column("source_id", sa.BigInteger(), nullable=False, comment="dp 数据源 id"),
            sa.Column("schema_name", sa.String(64), nullable=False, server_default="dp_stable", comment="dp 元库库名"),
            sa.Column("task_table", sa.String(128), nullable=False, server_default="dispatch_task", comment="任务表名"),
            sa.Column("step_table", sa.String(128), nullable=False, server_default="dispatch_task_step", comment="节点表名"),
            sa.Column("poll_interval_minutes", sa.Integer(), nullable=False, server_default="5", comment="轮询间隔（1~60）"),
            sa.Column("task_type_filter", sa.JSON(), nullable=True, comment="任务类型过滤"),
            sa.Column("step_type_filter", sa.JSON(), nullable=True, comment="节点类型过滤"),
            sa.Column("exclude_task_patterns", sa.JSON(), nullable=True, comment="排除任务名正则"),
            sa.Column("exclude_table_patterns", sa.JSON(), nullable=True, comment="排除目标表正则"),
            sa.Column("llm_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1"), comment="LLM 开关"),
            sa.Column("llm_complexity_rules", sa.JSON(), nullable=True, comment="复杂度分级规则"),
            sa.Column("llm_model", sa.String(64), nullable=True, comment="LLM 模型"),
            sa.Column("resolve_memory_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1"), comment="记忆复用开关"),
            sa.Column(
                "owner_backfill",
                sa.Enum("orphan_only", "never", name="dp_owner_backfill"),
                nullable=False,
                server_default="orphan_only",
                comment="资产 owner 回填策略",
            ),
            sa.Column("updated_by", sa.BigInteger(), nullable=True, comment="最近更新人"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Index("ux_dp_sync_config_id", "id"),
        ],
    )

    # ---- dp_sync_watermark ----
    _create_table_if_absent(
        "dp_sync_watermark",
        [
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("table_name", sa.String(32), nullable=False, comment="表名（task/step）"),
            sa.Column("last_max_update", sa.DateTime(timezone=True), nullable=True, comment="上次最大更新时间"),
            sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True, comment="上次扫描时间"),
            sa.Column("last_full_scan_at", sa.DateTime(timezone=True), nullable=True, comment="上次全量时间"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Index("ux_dp_sync_watermark_table", "table_name", unique=True),
        ],
    )

    # ---- lineage_field_mapping ----
    _create_table_if_absent(
        "lineage_field_mapping",
        [
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("edge_id", sa.BigInteger(), nullable=False, comment="所属表级边 id"),
            sa.Column("source_table", sa.String(512), nullable=False, comment="源表"),
            sa.Column("source_column", sa.String(256), nullable=True, comment="源列（NULL=表级降级）"),
            sa.Column("target_table", sa.String(512), nullable=False, comment="目标表"),
            sa.Column("target_column", sa.String(256), nullable=False, comment="目标列"),
            sa.Column("expression", sa.Text(), nullable=True, comment="表达式"),
            sa.Column("degraded", sa.Boolean(), nullable=False, server_default=sa.text("0"), comment="降级标记"),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0", comment="置信度"),
            sa.Column("provenance", sa.String(32), nullable=False, server_default="sqlglot", comment="来源"),
            sa.Column("sql_hash", sa.String(64), nullable=False, comment="SQL 指纹"),
            sa.Column("task_id", sa.BigInteger(), nullable=True, comment="来源任务 id"),
            sa.Column("step_id", sa.BigInteger(), nullable=True, comment="来源节点 id"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Index(
                "uq_lineage_field_mapping",
                "source_table",
                "source_column",
                "target_table",
                "target_column",
                "degraded",
                unique=True,
                mysql_length={"source_table": 255, "target_table": 255, "source_column": 128, "target_column": 128},
            ),
            sa.Index("ix_lfm_target_column", "target_table", "target_column"),
            sa.Index("ix_lfm_edge", "edge_id"),
        ],
    )

    # ---- dp_resolution_ticket ----
    _create_table_if_absent(
        "dp_resolution_ticket",
        [
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("task_id", sa.BigInteger(), nullable=False, comment="来源任务 id"),
            sa.Column("step_id", sa.BigInteger(), nullable=False, comment="来源节点 id"),
            sa.Column("task_name", sa.String(256), nullable=True, comment="任务名"),
            sa.Column("out_table", sa.String(512), nullable=True, comment="任务产出表"),
            sa.Column("sql_text", sa.Text(), nullable=False, comment="原始 script_info"),
            sa.Column("sql_hash", sa.String(64), nullable=False, comment="SQL 指纹"),
            sa.Column(
                "status",
                sa.Enum(
                    "diverged",
                    "llm_fallback",
                    "unparseable",
                    "pending",
                    "resolved",
                    "ignored",
                    name="dp_ticket_status",
                ),
                nullable=False,
                comment="待抉择/已处理状态",
            ),
            sa.Column("sqlglot_result", sa.JSON(), nullable=True, comment="sqlglot 边候选"),
            sa.Column("llm_opinion", sa.JSON(), nullable=True, comment="LLM 意见"),
            sa.Column("divergence_reason", sa.Text(), nullable=True, comment="不一致原因"),
            sa.Column(
                "resolution",
                sa.Enum("accept_sqlglot", "accept_llm", "manual", "ignore", name="dp_ticket_resolution"),
                nullable=True,
                comment="裁决方式",
            ),
            sa.Column("manual_edges_json", sa.JSON(), nullable=True, comment="手动配置边"),
            sa.Column("resolved_by", sa.BigInteger(), nullable=True, comment="裁决人"),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True, comment="裁决时间"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Index("ux_dp_ticket_step_hash", "step_id", "sql_hash", unique=True),
            sa.Index("ix_dp_ticket_status", "status"),
            sa.Index("ix_dp_ticket_task", "task_id"),
        ],
    )

    # ---- dp_sync_run_log ----
    _create_table_if_absent(
        "dp_sync_run_log",
        [
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("run_at", sa.DateTime(timezone=True), nullable=False, comment="运行时间"),
            sa.Column("status", sa.String(16), nullable=False, server_default="running", comment="running/success/failed"),
            sa.Column("scanned_tasks", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("scanned_steps", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("parsed_ok", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("llm_confirmed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("diverged", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("llm_fallback", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("unparseable", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tickets_created", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tickets_resolved", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("errors", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("detail_json", sa.Text(), nullable=True, comment="每 step 结果摘要"),
            sa.Column("error", sa.Text(), nullable=True, comment="失败原因"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Index("ix_dp_run_log_run_at", "run_at"),
        ],
    )


def downgrade() -> None:
    conn = op.get_bind()
    for name in (
        "dp_sync_run_log",
        "dp_resolution_ticket",
        "lineage_field_mapping",
        "dp_sync_watermark",
        "dp_sync_config",
    ):
        if _table_exists(conn, name):
            op.drop_table(name)
    cols = {
        r[0]
        for r in conn.execute(
            sa.text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'lineage_edge'"
            )
        )
    }
    if "dp_task_refs" in cols:
        op.drop_column("lineage_edge", "dp_task_refs")
