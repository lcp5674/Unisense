"""采集领域扩展模型（SchemaDriftLog + CollectionWatermark + CollectionRun）。

对齐 TD §12.1 / spec FR-010/FR-011/FR-014。
新增表用于 Schema Drift 检测历史记录、采集水位追踪与采集运行历史。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Enum, ForeignKey, Index, String
from sqlalchemy.dialects.mysql import DATETIME, INTEGER, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class SchemaDriftLog(Base, BaseModel):
    """Schema 变更日志。

    记录每次采集后检测到的 Schema 变更（新增列/删除列/类型变更等），
    满足 GB/T 36073 §6.4 审计要求。

    Attributes:
        source_id: 数据源标识。
        entity_name: 实体名。
        change_type: 变更类型（ADD_COLUMN/DROP_COLUMN/TYPE_CHANGE/SCHEMA_CHANGED）。
        before_signature: 变更前内容指纹。
        after_signature: 变更后内容指纹。
        before_schema: 变更前 schema。
        after_schema: 变更后 schema。
        diff_json: 差异详情（{added:[], removed:[], changed:[]}）。
        detected_at: 检测时间。
    """

    __tablename__ = "schema_drift_log"

    source_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("data_source.source_id", name="fk_drift_log_source"),
        nullable=False,
        comment="数据源标识",
    )
    entity_name: Mapped[str] = mapped_column(String(256), nullable=False, comment="实体名")
    change_type: Mapped[str] = mapped_column(
        Enum(
            "ADD_COLUMN",
            "DROP_COLUMN",
            "TYPE_CHANGE",
            "SCHEMA_CHANGED",
            name="drift_change_type_enum",
        ),
        nullable=False,
        comment="变更类型",
    )
    before_signature: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="变更前内容指纹"
    )
    after_signature: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="变更后内容指纹"
    )
    before_schema: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="变更前 schema"
    )
    after_schema: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="变更后 schema"
    )
    diff_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="差异详情"
    )
    detected_at: Mapped[datetime] = mapped_column(DATETIME, nullable=False, comment="检测时间")

    __table_args__ = (
        Index("idx_drift_source_entity", "source_id", "entity_name"),
        Index("idx_drift_detected_at", "detected_at"),
    )


class CollectionWatermark(Base, BaseModel):
    """采集水位记录。

    每个数据源一条记录，追踪最后采集时间、模式与指纹映射，
    用于增量采集与 Schema Drift 检测。

    Attributes:
        source_id: 数据源标识（唯一）。
        last_collected_at: 最后采集时间。
        mode: 采集模式（FULL/INCREMENTAL）。
        scanned_count: 采集表数。
        failed_count: 失败表数。
        content_fingerprints: 实体级指纹映射 {entity_name: signature}。
    """

    __tablename__ = "collection_watermark"

    source_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("data_source.source_id", name="fk_watermark_source"),
        nullable=False,
        unique=True,
        comment="数据源标识（唯一）",
    )
    last_collected_at: Mapped[datetime] = mapped_column(
        DATETIME, nullable=False, comment="最后采集时间"
    )
    mode: Mapped[str] = mapped_column(
        Enum("FULL", "INCREMENTAL", name="watermark_mode_enum"),
        nullable=False,
        default="FULL",
        comment="采集模式",
    )
    scanned_count: Mapped[int] = mapped_column(
        INTEGER, nullable=False, default=0, comment="采集表数"
    )
    failed_count: Mapped[int] = mapped_column(
        INTEGER, nullable=False, default=0, comment="失败表数"
    )
    content_fingerprints: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default={}, comment="实体级指纹映射"
    )

    __table_args__ = (Index("idx_watermark_source", "source_id"),)


class CollectionRun(Base, BaseModel):
    """一次采集运行的持久化记录（采集运行历史）。

    采集链路闭环的关键一环：job（JobStore）是 ephemeral 运行时数据（终态 7 天 TTL），
    而采集运行历史需长期可追溯（审计/运维/排障）。本表每次采集（手动/定时、同步/异步）
    落一行，状态 RUNNING → COMPLETED/FAILED，含全部关键指标与失败明细。

    Attributes:
        source_id: 数据源标识。
        job_id: 关联异步任务 ID（同步采集为 NULL）。
        trigger: 触发方式（manual 手动 / scheduled 定时）。
        mode: 请求采集模式（FULL/INCREMENTAL）。
        effective_mode: 实际执行模式（增量降级为全量后回填）。
        status: 运行状态（RUNNING/COMPLETED/FAILED）。
        actor_id: 触发人 ID（定时调度为 NULL）。
        started_at: 开始时间。
        finished_at: 结束时间。
        scanned: 扫描实体数。
        registered: 注册/更新实体数。
        pii_registered: PII 实体数。
        failed_count: 失败实体数。
        drift_count: Schema 漂移数。
        deprecated_count: 对账废弃数。
        coverage: 采集后资产覆盖率（0-1）。
        error: 失败原因（截断 512）。
        detail_json: 明细（failed_specs / drift_events / degrade_reason 等）。
    """

    __tablename__ = "collection_run"

    source_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("data_source.source_id", name="fk_collection_run_source"),
        nullable=False,
        comment="数据源标识",
    )
    job_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="关联异步任务 ID（同步采集为 NULL）"
    )
    trigger: Mapped[str] = mapped_column(
        Enum("manual", "scheduled", name="collection_run_trigger_enum"),
        nullable=False,
        default="manual",
        comment="触发方式",
    )
    mode: Mapped[str] = mapped_column(
        Enum("FULL", "INCREMENTAL", name="collection_run_mode_enum"),
        nullable=False,
        default="FULL",
        comment="请求采集模式",
    )
    effective_mode: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="实际执行模式（增量降级后回填）"
    )
    status: Mapped[str] = mapped_column(
        Enum("RUNNING", "COMPLETED", "FAILED", "CANCELLED", name="collection_run_status_enum"),
        nullable=False,
        default="RUNNING",
        comment="运行状态（CANCELLED：用户主动取消，2026-08-28 起与 JobStore 终态对齐）",
    )
    actor_id: Mapped[int | None] = mapped_column(
        nullable=True, comment="触发人 ID（定时调度为 NULL）"
    )
    started_at: Mapped[datetime] = mapped_column(DATETIME, nullable=False, comment="开始时间")
    finished_at: Mapped[datetime | None] = mapped_column(
        DATETIME, nullable=True, comment="结束时间"
    )
    scanned: Mapped[int] = mapped_column(INTEGER, nullable=False, default=0, comment="扫描实体数")
    registered: Mapped[int] = mapped_column(
        INTEGER, nullable=False, default=0, comment="注册/更新实体数"
    )
    pii_registered: Mapped[int] = mapped_column(
        INTEGER, nullable=False, default=0, comment="PII 实体数"
    )
    failed_count: Mapped[int] = mapped_column(
        INTEGER, nullable=False, default=0, comment="失败实体数"
    )
    drift_count: Mapped[int] = mapped_column(
        INTEGER, nullable=False, default=0, comment="Schema 漂移数"
    )
    deprecated_count: Mapped[int] = mapped_column(
        INTEGER, nullable=False, default=0, comment="对账废弃数"
    )
    coverage: Mapped[float | None] = mapped_column(
        nullable=True, comment="采集后资产覆盖率（0-1）"
    )
    error: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="失败原因（截断）"
    )
    detail_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="明细（failed_specs/drift_events/degrade_reason）"
    )

    __table_args__ = (
        Index("idx_collection_run_source", "source_id", "started_at"),
        Index("idx_collection_run_status", "status", "started_at"),
    )


class CollectionRunLog(Base, BaseModel):
    """采集运行日志明细（采集记录详情页「实时日志」）。

    采集执行期间的进度/阶段/错误逐条落此表，与 ``collection_run`` 主记录
    一一对应（run_id 外键）。数据流：采集期间先写 Redis List 实时缓冲
    （``collect:run_log:{run_id}``，前端 RUNNING 时轮询可见），任务终态时
    一次性 bulk 回写本表——实时性与长期可追溯兼得；purge 采集运行历史时
    级联清理本表（保留策略一致，默认 90 天）。

    Attributes:
        run_id: 关联采集运行记录 ID。
        ts: 日志时间（对齐进度事件发生时刻）。
        level: 日志级别（INFO/WARN/ERROR）。
        phase: 采集阶段（start/scanning/registering/complete/fail）。
        entity_name: 关联实体名（逐表注册日志，可空）。
        message: 日志内容（截断 512）。
    """

    __tablename__ = "collection_run_log"

    run_id: Mapped[int] = mapped_column(
        ForeignKey("collection_run.id", name="fk_run_log_run"),
        nullable=False,
        index=True,
        comment="关联采集运行记录 ID",
    )
    ts: Mapped[datetime] = mapped_column(DATETIME, nullable=False, comment="日志时间")
    level: Mapped[str] = mapped_column(
        String(8), nullable=False, default="INFO", comment="日志级别 INFO/WARN/ERROR"
    )
    phase: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="采集阶段 start/scanning/registering/complete/fail"
    )
    entity_name: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="关联实体名"
    )
    message: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="日志内容（截断）"
    )

    __table_args__ = (Index("idx_run_log_run_ts", "run_id", "ts"),)


class BatchInferHistory(Base, BaseModel):
    """跨表批量 LLM 推断历史（服务端持久化，跨设备/团队可见）。

    描述缺失治理「批量推断所选表」每次会话落一行（成功/失败/取消/新增字段/耗时），
    供历史视图查看与一键重新勾选重跑。与前端 localStorage 缓存不同，服务端记录
    不随设备丢失，且 actor_name 标明操作人，团队治理动作可追溯。

    Attributes:
        actor_id: 触发人 ID。
        actor_name: 触发人姓名快照（改名后历史仍可读）。
        tables_json: 本次会话涉及的表 [{catalog_id, entity_name}]。
        done: 成功表数。
        failed: 失败表数。
        cancelled: 取消表数。
        added: 新增字段描述数。
        elapsed: 总耗时（秒）。
        failed_tables_json: 失败表 [{catalog_id, entity_name}]（一键重跑用）。
    """

    __tablename__ = "batch_infer_history"

    actor_id: Mapped[int | None] = mapped_column(
        nullable=True, comment="触发人 ID"
    )
    actor_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="触发人姓名快照"
    )
    tables_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, comment="涉及的表 [{catalog_id, entity_name}]"
    )
    done: Mapped[int] = mapped_column(INTEGER, nullable=False, default=0, comment="成功表数")
    failed: Mapped[int] = mapped_column(INTEGER, nullable=False, default=0, comment="失败表数")
    cancelled: Mapped[int] = mapped_column(
        INTEGER, nullable=False, default=0, comment="取消表数"
    )
    added: Mapped[int] = mapped_column(
        INTEGER, nullable=False, default=0, comment="新增字段描述数"
    )
    elapsed: Mapped[int] = mapped_column(INTEGER, nullable=False, default=0, comment="总耗时（秒）")
    failed_tables_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, comment="失败表 [{catalog_id, entity_name}]"
    )

    __table_args__ = (Index("idx_batch_history_created", "created_at"),)
