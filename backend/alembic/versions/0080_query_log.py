"""consume 提数查询日志表（响应时效 KPI 数据源）。

背景：分析文档 P1「响应时效 KPI」——平台需统计提数需求响应时长（avg/p95/p99），
此前无提数需求/查询日志实体，无数据可统计。新增 query_log 表：每次真实执行查询
（POST /consume/query 与 POST /consume/metrics/{code}/query）best-effort 落一条，
记录指标、请求方、耗时、结果状态。

revision 挂 0079_measure_catalog_category（当前线性链后继）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0080_query_log"
down_revision = "0079_measure_catalog_category"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "query_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "metric_code",
            sa.String(length=64),
            nullable=False,
            comment="被查询指标编码",
        ),
        sa.Column(
            "requester_type",
            sa.Enum(
                "api_client",
                "internal",
                name="query_log_requester_type",
            ),
            nullable=False,
            comment="请求方类型：api_client 接入方 / internal 内部用户",
        ),
        sa.Column(
            "requester_id",
            sa.String(length=64),
            nullable=False,
            comment="请求方 ID（client_id / 用户 ID）",
        ),
        sa.Column(
            "requester_name",
            sa.String(length=64),
            nullable=True,
            comment="请求方名称快照（client_id / 用户名）",
        ),
        sa.Column(
            "duration_ms",
            sa.Integer(),
            nullable=False,
            comment="查询耗时（毫秒）",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            comment="结果状态：ok/error",
        ),
        sa.Column(
            "error_code",
            sa.String(length=64),
            nullable=True,
            comment="失败错误码（status=error 时）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="更新时间（UTC）",
        ),
        sa.Index("ix_query_log_metric_code", "metric_code"),
        sa.Index("ix_query_log_requester_id", "requester_id"),
        sa.Index("ix_query_log_created", "created_at"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        comment="提数查询日志（响应时效 KPI 数据源）",
    )


def downgrade() -> None:
    op.drop_table("query_log")
