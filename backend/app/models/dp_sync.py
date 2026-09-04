"""dp 调度血缘同步领域模型。

对齐 `spec/dp-lineage-ingest/plan.md`（dp 调度血缘接入）：
将本地 dp 数据源（数开/调度平台元库 ``dp_stable``）中所有开发任务的全部
SQL 节点解析为血缘写入血缘模块；支持字段级全链路、LLM 分级确认与人工抉择、
前端可视化配置与运维、生产准实时增量更新。

五张新表：
- ``dp_sync_config``：同步配置（单行语义 + key-value 可扩展）
- ``dp_sync_watermark``：增量水位（task/step 分表记录）
- ``lineage_field_mapping``：字段映射独立成表（一等查询对象）
- ``dp_resolution_ticket``：待抉择单（LLM 分歧/兜底/无法解析）
- ``dp_sync_run_log``：每轮扫描运行记录（运维区）

`LineageEdge.dp_task_refs`（Text JSON）承载任务/节点静态身份与准静态元数据
（D10：静态落边快照 + 增量顺刷；动态运行态不落边，展示层实时旁路）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    )
from sqlalchemy import (
    Enum as SQLEnum,
    )
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class DpSyncConfig(Base, BaseModel):
    """dp 血缘同步配置（单行语义 id=1；key-value 可扩展列承载规则）。"""

    __tablename__ = "dp_sync_config"

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="同步总开关（停用不再轮询/解析，血缘保留）",
    )
    source_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="dp 数据源 source_id（如 mysql_uncategorized）",
    )
    schema_name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="dp_stable",
        comment="dp 元库库名",
    )
    task_table: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="dispatch_task",
        comment="任务表名",
    )
    step_table: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="dispatch_task_step",
        comment="节点表名",
    )
    poll_interval_minutes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=5,
        comment="轮询间隔（1~60 分钟，前端可配置）",
    )
    task_type_filter: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
        default=list,
        comment="任务类型过滤（默认 [1]=SQL 任务）",
    )
    step_type_filter: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
        default=list,
        comment="节点类型过滤（默认 [7]=Hive/Spark SQL）",
    )
    exclude_task_patterns: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
        default=list,
        comment="排除任务名正则（tmp/temp/_bak/adhoc）",
    )
    exclude_table_patterns: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
        default=list,
        comment="排除目标表前缀/正则（明显临时表不入图）",
    )
    llm_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="LLM 开关（关 = 纯 sqlglot，复杂/失败全进待抉择）",
    )
    llm_complexity_rules: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        default=dict,
        comment="复杂度分级特征规则（子查询深度/CTE 数/窗口/多 join/方言/告警阈值）",
    )
    llm_model: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        default=None,
        comment="LLM 模型（空=平台默认）",
    )
    resolve_memory_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="待抉择记忆复用开关（同 sql_hash 自动复用裁决）",
    )
    owner_backfill: Mapped[str] = mapped_column(
        SQLEnum("orphan_only", "never", name="dp_owner_backfill"),
        nullable=False,
        default="orphan_only",
        comment="资产 owner 回填策略：orphan_only=仅孤儿回填（默认）/ never=不回填",
    )
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        default=None,
        comment="最近更新人 id",
    )

    __table_args__ = (Index("ux_dp_sync_config_id", "id"),)


class DpSyncWatermark(Base, BaseModel):
    """dp 血缘同步增量水位（task/step 分表记录）。"""

    __tablename__ = "dp_sync_watermark"

    table_name: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="表名（task/step）",
    )
    last_max_update: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        comment="上次扫描到的最大更新时间（UTC）",
    )
    last_scan_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        comment="上次扫描时间（UTC）",
    )
    last_full_scan_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        comment="上次全量扫描时间（UTC）",
    )

    __table_args__ = (Index("ux_dp_sync_watermark_table", "table_name", unique=True),)


class LineageFieldMapping(Base, BaseModel):
    """字段级血缘映射（独立成表，一等查询对象）。

    每行一个「源列 → 目标列」映射；聚合/计算列无直接字段来源时
    source_column=NULL 且 expression 非空（表达式列，落到表级）。
    """

    __tablename__ = "lineage_field_mapping"

    edge_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="所属表级边 id（lineage_edge.id）",
    )
    source_table: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="源表，如 db.tbl",
    )
    source_column: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        default=None,
        comment="源列（NULL=表级降级占位）",
    )
    target_table: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="目标表",
    )
    target_column: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="目标列",
    )
    expression: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
        comment="表达式（聚合/计算列时非空且 source_column=NULL）",
    )
    degraded: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="SELECT * 等无法枚举字段的降级标记",
    )
    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        comment="映射置信度（high=1.0 / low 参考=0.5）",
    )
    provenance: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="sqlglot",
        comment="来源：sqlglot / sqlglot+llm / llm / manual",
    )
    sql_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="来源节点 SQL 内容指纹（记忆复用 key）",
    )
    task_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        default=None,
        comment="来源任务 id",
    )
    step_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        default=None,
        comment="来源节点 id",
    )

    __table_args__ = (
        Index(
            "uq_lineage_field_mapping",
            "source_table",
            "source_column",
            "target_table",
            "target_column",
            "degraded",
            unique=True,
            mysql_length={
                "source_table": 255,
                "target_table": 255,
                "source_column": 128,
                "target_column": 128,
            },
        ),
        Index("ix_lfm_target_column", "target_table", "target_column"),
        Index("ix_lfm_edge", "edge_id"),
    )


class DpResolutionTicket(Base, BaseModel):
    """dp 血缘待抉择单（一节点一次解析一张单）。

    status：diverged（sqlglot/LLM 不一致）/ llm_fallback（兜底参考）/ unparseable
    （双方失败，展示原文供手动配置）；裁决后 pending → resolved / ignored。
    uq(step_id, sql_hash)：SQL 未变不重复进单（裁决记忆）。
    """

    __tablename__ = "dp_resolution_ticket"

    task_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="来源任务 id",
    )
    step_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="来源节点 id",
    )
    task_name: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        default=None,
        comment="任务名（列表展示冗余）",
    )
    out_table: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        default=None,
        comment="任务产出表（列表展示冗余）",
    )
    sql_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="原始 script_info（无法解析时展示供手动配置）",
    )
    sql_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SQL 内容指纹（裁决记忆 key）",
    )
    status: Mapped[str] = mapped_column(
        SQLEnum(
            "diverged",
            "llm_fallback",
            "unparseable",
            "pending",
            "resolved",
            "ignored",
            name="dp_ticket_status",
        ),
        nullable=False,
        comment="diverged/llm_fallback/unparseable=待抉择；pending/resolved/ignored=已处理",
    )
    sqlglot_result: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        default=None,
        comment="sqlglot 表级+字段级边候选",
    )
    llm_opinion: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        default=None,
        comment="LLM 意见（agree/纠正/兜底流转/无法提炼）",
    )
    divergence_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
        comment="不一致/无法解析原因",
    )
    resolution: Mapped[str | None] = mapped_column(
        SQLEnum("accept_sqlglot", "accept_llm", "manual", "ignore", name="dp_ticket_resolution"),
        nullable=True,
        default=None,
        comment="裁决：accept_sqlglot/accept_llm/manual/ignore",
    )
    manual_edges_json: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        default=None,
        comment="手动配置的边/字段映射（manual 时）",
    )
    task_refs_json: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        default=None,
        comment="建单时任务/节点静态身份快照（build_task_ref 产物，裁决入库时还原完整元数据）",
    )
    resolved_by: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        default=None,
        comment="裁决人 id",
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        comment="裁决时间（UTC）",
    )

    __table_args__ = (
        Index("ux_dp_ticket_step_hash", "step_id", "sql_hash", unique=True),
        Index("ix_dp_ticket_status", "status"),
        Index("ix_dp_ticket_task", "task_id"),
    )


class DpSyncRunLog(Base, BaseModel):
    """dp 血缘同步每轮扫描运行记录（运维区，与 LineageIngestRun 职责分离）。"""

    __tablename__ = "dp_sync_run_log"

    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="运行时间（UTC）",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="running",
        comment="running/success/failed/cancelled",
    )
    scan_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="incremental",
        comment="扫描模式：full=全量（首次/重置/周期自动全量/手动立即全量）；"
        "incremental=增量（周期水位）",
    )
    scanned_tasks: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="本轮扫描任务量",
    )
    scanned_steps: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="本轮扫描节点量",
    )
    parsed_ok: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="sqlglot 直入数",
    )
    llm_confirmed: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="LLM 确认一致数",
    )
    diverged: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="分歧待抉择数",
    )
    llm_fallback: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="LLM 兜底数",
    )
    unparseable: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="无法解析数",
    )
    tickets_created: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="抉择单新增数",
    )
    tickets_resolved: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="抉择单裁决（记忆复用）数",
    )
    errors: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="错误数",
    )
    llm_calls: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="LLM 调用量",
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="耗时（毫秒）",
    )
    detail_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
        comment="每 step 结果摘要快照（JSON，可下钻）",
    )
    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
        comment="失败原因（status=failed）",
    )

    __table_args__ = (Index("ix_dp_run_log_run_at", "run_at"),)
