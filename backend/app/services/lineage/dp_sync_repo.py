"""dp 调度血缘同步仓储（新表 + 资产 Owner 回填）。

对齐 `spec/dp-lineage-ingest/plan.md` §3/§4.4：
- dp_sync_config / dp_sync_watermark / dp_sync_run_log / dp_resolution_ticket
  的读写（幂等）
- lineage_field_mapping 独立表写入（uq 幂等，供字段级反查）
- 资产 Owner 回填（仅孤儿回填 + 影子用户自动创建，D10）

本仓储只做 DB 读写；三态解析/LLM 编排在 ``dp_sync_service``。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, update

from app.db.mysql import AsyncSession
from app.models.data_source import DBCatalog
from app.models.dp_sync import (
    DpResolutionTicket,
    DpSyncConfig,
    DpSyncRunLog,
    DpSyncWatermark,
    LineageFieldMapping,
)
from app.models.lineage import LineageEdge
from app.models.user import Organization, User

#: 影子用户归属组织（不存在时自动创建）。
SHADOW_ORG_CODE = "external"
SHADOW_ORG_NAME = "外部协作"

#: 影子用户 email 后缀（User.email 唯一，须避免与真实用户冲突）。
SHADOW_EMAIL_SUFFIX = "@external.local"

#: 影子用户默认角色（无平台权限，仅作为资产 Owner 挂接展示）。
SHADOW_ROLE = "viewer"


def _column_eq(column, value):
    """列等值比较：value 为 None 时匹配 ``IS NULL``，否则 ``= value``。

    SQLAlchemy 的 ``.is_()`` 仅适用于 NULL/布尔比较，对字符串会编译成
    ``col IS 'x'``（MySQL 语法错误）——等值必须用 ``==``。
    """
    return column.is_(None) if value is None else column == value


class DpLineageRepository:
    """dp 血缘同步仓储。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ---- dp_sync_config ----
    async def get_config(self) -> DpSyncConfig | None:
        stmt = (
            select(DpSyncConfig)
            .where(DpSyncConfig.deleted_at.is_(None))
            .order_by(DpSyncConfig.id)
            .limit(1)
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def create_default_config(self, source_id: str) -> DpSyncConfig:
        """创建默认配置行（source_id 必填——dp 数据源标识）。"""
        cfg = DpSyncConfig(
            source_id=source_id,
            enabled=False,
            poll_interval_minutes=5,
            task_type_filter=[1],
            step_type_filter=[7],
            llm_enabled=True,
            resolve_memory_enabled=True,
            owner_backfill="orphan_only",
        )
        self._db.add(cfg)
        await self._db.flush()
        return cfg

    async def update_config(self, cfg_id: int, **fields: Any) -> None:
        allowed = {
            "enabled",
            "source_id",
            "schema_name",
            "task_table",
            "step_table",
            "poll_interval_minutes",
            "task_type_filter",
            "step_type_filter",
            "exclude_task_patterns",
            "exclude_table_patterns",
            "llm_enabled",
            "llm_complexity_rules",
            "llm_model",
            "resolve_memory_enabled",
            "owner_backfill",
            "updated_by",
        }
        # 可置 NULL 的字段（DB nullable=True）；显式传 null 表示「清空/回默认」。
        # 非空字段收到 null 忽略（防 DB not-null 报错）。
        nullable = {
            "task_type_filter",
            "step_type_filter",
            "exclude_task_patterns",
            "exclude_table_patterns",
            "llm_complexity_rules",
            "llm_model",
            "updated_by",
        }
        data: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if value is None and key not in nullable:
                continue
            data[key] = value
        if data:
            data["updated_at"] = datetime.now(UTC)
            await self._db.execute(
                update(DpSyncConfig).where(DpSyncConfig.id == cfg_id).values(**data)
            )

    # ---- dp_sync_watermark ----
    async def get_watermark(self, table_name: str) -> DpSyncWatermark | None:
        stmt = (
            select(DpSyncWatermark)
            .where(
                DpSyncWatermark.table_name == table_name,
                DpSyncWatermark.deleted_at.is_(None),
            )
            .order_by(DpSyncWatermark.id.desc())
            .limit(1)
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def update_watermark(
        self,
        table_name: str,
        *,
        last_max_update: datetime | None = None,
        last_scan_at: datetime | None = None,
        full_scan: bool = False,
    ) -> None:
        row = await self.get_watermark(table_name)
        now = datetime.now(UTC)
        if row is None:
            row = DpSyncWatermark(table_name=table_name)
            self._db.add(row)
        if last_max_update is not None:
            row.last_max_update = last_max_update
        if last_scan_at is not None:
            row.last_scan_at = last_scan_at
        if full_scan:
            row.last_full_scan_at = now
        row.updated_at = now

    # ---- dp_sync_run_log ----
    async def create_run_log(self, **fields: Any) -> DpSyncRunLog:
        log = DpSyncRunLog(run_at=fields.get("run_at") or datetime.now(UTC))
        for key, value in fields.items():
            if key != "run_at" and hasattr(log, key):
                setattr(log, key, value)
        self._db.add(log)
        await self._db.flush()
        return log

    async def update_run_log(self, log_id: int, **fields: Any) -> None:
        data = {k: v for k, v in fields.items() if hasattr(DpSyncRunLog, k)}
        if data:
            await self._db.execute(
                update(DpSyncRunLog).where(DpSyncRunLog.id == log_id).values(**data)
            )

    # ---- 统计概览 ----
    async def sync_stats(self) -> dict[str, Any]:
        """dp 血缘同步统计聚合（运维页「统计概览」卡；只读，不连 dp 源）。

        四块数据：
        - ``cumulative``：历史成功轮累计（run_log SUM）——解析成功 = parsed_ok +
          llm_confirmed，未解析成功 = diverged + llm_fallback + unparseable + errors
        - ``last_full_scan``：最近一次成功全量轮（最贴近「dp 数据源全量解析成果」
          ——增量轮只扫变更任务，计数远小于全量，不作概览口径）
        - ``pending_tickets``：待抉择存量（未裁决单按状态计数）
        - ``lineage``：dp 通道血缘沉淀——活跃表级边 / 涉及 distinct 表节点 /
          字段映射条数（lineage_field_mapping 仅 dp 同步写入，全表即 dp 字段数）
        """
        success = DpSyncRunLog.status == "success"
        not_deleted = DpSyncRunLog.deleted_at.is_(None)
        sum_cols = {
            "scanned_tasks": func.sum(DpSyncRunLog.scanned_tasks),
            "scanned_steps": func.sum(DpSyncRunLog.scanned_steps),
            "parsed_ok": func.sum(DpSyncRunLog.parsed_ok),
            "llm_confirmed": func.sum(DpSyncRunLog.llm_confirmed),
            "diverged": func.sum(DpSyncRunLog.diverged),
            "llm_fallback": func.sum(DpSyncRunLog.llm_fallback),
            "unparseable": func.sum(DpSyncRunLog.unparseable),
            "errors": func.sum(DpSyncRunLog.errors),
        }
        row = (
            await self._db.execute(
                select(func.count(DpSyncRunLog.id), *sum_cols.values()).where(
                    success, not_deleted
                )
            )
        ).one()
        cumulative = {
            "runs": int(row[0] or 0),
            **{k: int(v or 0) for k, v in zip(sum_cols.keys(), row[1:], strict=True)},
        }

        full_row = (
            await self._db.execute(
                select(DpSyncRunLog)
                .where(
                    success,
                    DpSyncRunLog.scan_mode == "full",
                    not_deleted,
                )
                .order_by(DpSyncRunLog.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        ticket_rows = (
            await self._db.execute(
                select(DpResolutionTicket.status, func.count(DpResolutionTicket.id))
                .where(
                    DpResolutionTicket.deleted_at.is_(None),
                    DpResolutionTicket.resolution.is_(None),
                )
                .group_by(DpResolutionTicket.status)
            )
        ).all()
        pending = {str(r[0]): int(r[1]) for r in ticket_rows}

        edge_cond = and_(
            LineageEdge.provenance == "dp_sql", LineageEdge.deleted_at.is_(None)
        )
        table_edges = (
            await self._db.execute(select(func.count(LineageEdge.id)).where(edge_cond))
        ).scalar() or 0
        nodes_sq = select(LineageEdge.source_node.label("n")).where(edge_cond).union_all(
            select(LineageEdge.target_node.label("n")).where(edge_cond)
        )
        table_nodes = (
            await self._db.execute(
                select(func.count(func.distinct(nodes_sq.subquery().c.n)))
            )
        ).scalar() or 0
        field_mappings = (
            await self._db.execute(
                select(func.count(LineageFieldMapping.id)).where(
                    LineageFieldMapping.deleted_at.is_(None)
                )
            )
        ).scalar() or 0
        return {
            "cumulative": cumulative,
            "last_full_scan": full_row.to_dict() if full_row else None,
            "pending_tickets": pending,
            "lineage": {
                "table_edges": int(table_edges),
                "table_nodes": int(table_nodes),
                "field_mappings": int(field_mappings),
            },
        }

    # ---- lineage_field_mapping（uq 幂等） ----
    async def upsert_field_mapping(
        self,
        *,
        edge_id: int,
        source_table: str,
        source_column: str | None,
        target_table: str,
        target_column: str,
        expression: str | None = None,
        degraded: bool = False,
        confidence: float = 1.0,
        provenance: str = "sqlglot",
        sql_hash: str,
        task_id: int | None = None,
        step_id: int | None = None,
    ) -> None:
        """按唯一索引幂等写入字段映射（已存在则忽略，不重复）。

        uq_lineage_field_mapping 不含 deleted_at：SQL 演进（soft_delete_field_mappings
        软删旧 hash 映射）后同 tuple 重插会撞 1062。故先查活跃行（存在即忽略），
        无活跃行时再查软删墓碑（存在则复活覆盖），最后才新建（H2）。
        """
        stmt = select(LineageFieldMapping).where(
            LineageFieldMapping.source_table == source_table,
            _column_eq(LineageFieldMapping.source_column, source_column),
            LineageFieldMapping.target_table == target_table,
            LineageFieldMapping.target_column == target_column,
            LineageFieldMapping.degraded.is_(degraded),
            LineageFieldMapping.deleted_at.is_(None),
        )
        existing = (await self._db.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return
        # 软删墓碑复活：同 tuple 曾被 soft_delete_field_mappings 软删（SQL 演进），
        # 复活覆盖并更新 provenance（合并保留历史来源）——避免撞 uq 1062 拖垮整轮。
        tombstone = (
            await self._db.execute(
                select(LineageFieldMapping)
                .where(
                    LineageFieldMapping.source_table == source_table,
                    _column_eq(LineageFieldMapping.source_column, source_column),
                    LineageFieldMapping.target_table == target_table,
                    LineageFieldMapping.target_column == target_column,
                    LineageFieldMapping.degraded.is_(degraded),
                    LineageFieldMapping.deleted_at.is_not(None),
                )
                .order_by(LineageFieldMapping.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if tombstone is not None:
            tombstone.deleted_at = None
            tombstone.edge_id = edge_id
            tombstone.expression = expression
            tombstone.confidence = confidence
            tombstone.sql_hash = sql_hash
            tombstone.task_id = task_id
            tombstone.step_id = step_id
            # provenance 多来源合并（与 lineage/repository.merge_provenances 同语义）：
            # 复活时保留墓碑既有通道 token，防其他通道失去对该映射的保护。
            tokens: list[str] = []
            for chunk in (tombstone.provenance or "").split("+"):
                chunk = chunk.strip()
                if chunk and chunk not in tokens:
                    tokens.append(chunk)
            if provenance and provenance not in tokens:
                tokens.append(provenance)
            tombstone.provenance = "+".join(tokens)
            return
        self._db.add(
            LineageFieldMapping(
                edge_id=edge_id,
                source_table=source_table,
                source_column=source_column,
                target_table=target_table,
                target_column=target_column,
                expression=expression,
                degraded=degraded,
                confidence=confidence,
                provenance=provenance,
                sql_hash=sql_hash,
                task_id=task_id,
                step_id=step_id,
            )
        )

    async def list_field_mappings(self, edge_id: int) -> list[LineageFieldMapping]:
        stmt = (
            select(LineageFieldMapping)
            .where(
                LineageFieldMapping.edge_id == edge_id,
                LineageFieldMapping.deleted_at.is_(None),
            )
            .order_by(LineageFieldMapping.id)
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def list_field_mappings_by_table(
        self, table: str
    ) -> list[LineageFieldMapping]:
        """按表反查参与该表的字段级血缘映射（字段钻取子图数据源）。

        ``table`` 为 ``db.tbl``（不带 ``table:`` 前缀）。返回该表作为
        源（下游流向）或目标（上游来源）的全部**有效列映射**
        （``source_column`` 非空 + 未软删），供血缘图谱字段级钻取。
        """
        stmt = (
            select(LineageFieldMapping)
            .where(
                LineageFieldMapping.deleted_at.is_(None),
                LineageFieldMapping.source_column.is_not(None),
                (
                    (LineageFieldMapping.source_table == table)
                    | (LineageFieldMapping.target_table == table)
                ),
            )
            .order_by(LineageFieldMapping.id)
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def soft_delete_field_mappings(
        self, *, step_id: int, keep_sql_hash: str | None = None
    ) -> int:
        """软删某节点（step）下不再对应当前 SQL 指纹的字段映射（P2-8 防膨胀）。

        SQL 演进（sql_hash 变化）后旧映射行永久残留会表膨胀且展示过时；
        同 step 仅保留 keep_sql_hash 的映射（None=清空该 step 全部）。
        """
        stmt = update(LineageFieldMapping).where(
            LineageFieldMapping.step_id == step_id,
            LineageFieldMapping.deleted_at.is_(None),
        )
        if keep_sql_hash is not None:
            stmt = stmt.where(LineageFieldMapping.sql_hash != keep_sql_hash)
        result = await self._db.execute(
            stmt.values(deleted_at=datetime.now(UTC))
        )
        return result.rowcount or 0

    # ---- dp_resolution_ticket ----
    async def find_ticket_by_step_hash(
        self, step_id: int, sql_hash: str
    ) -> DpResolutionTicket | None:
        stmt = (
            select(DpResolutionTicket)
            .where(
                DpResolutionTicket.step_id == step_id,
                DpResolutionTicket.sql_hash == sql_hash,
                DpResolutionTicket.deleted_at.is_(None),
            )
            .order_by(DpResolutionTicket.id.desc())
            .limit(1)
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def create_ticket(
        self,
        *,
        task_id: int,
        step_id: int,
        sql_text: str,
        sql_hash: str,
        status: str,
        task_name: str | None = None,
        out_table: str | None = None,
        task_refs: dict | None = None,
        sqlglot_result: dict | None = None,
        llm_opinion: dict | None = None,
        divergence_reason: str | None = None,
    ) -> DpResolutionTicket:
        existing = await self.find_ticket_by_step_hash(step_id, sql_hash)
        if existing is not None:
            # SQL 未变：已存在单（可能裁决中/已裁决记忆），不重复创建
            return existing
        ticket = DpResolutionTicket(
            task_id=task_id,
            step_id=step_id,
            task_name=task_name,
            out_table=out_table,
            sql_text=sql_text,
            sql_hash=sql_hash,
            status=status,
            task_refs_json=task_refs,
            sqlglot_result=sqlglot_result,
            llm_opinion=llm_opinion,
            divergence_reason=divergence_reason,
        )
        self._db.add(ticket)
        await self._db.flush()
        return ticket

    async def get_ticket(self, ticket_id: int) -> DpResolutionTicket | None:
        stmt = (
            select(DpResolutionTicket)
            .where(
                DpResolutionTicket.id == ticket_id,
                DpResolutionTicket.deleted_at.is_(None),
            )
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def list_retryable_llm_tickets(
        self, *, limit: int = 500, ticket_ids: list[int] | None = None
    ) -> list[DpResolutionTicket]:
        """筛选「可 LLM 重试」的未裁决单：LLM 当时失败/未跑/兜底低置信。

        范围（resolution is None）：
          - status == llm_fallback（LLM 兜底低置信参考，可重试刷新意见）
          - diverged/unparseable 且原因标记 LLM 关闭或输出异常（LLM 当时未给出
            真实意见，恢复后重跑 confirm/fallback 才有意义）。
        """
        stmt = select(DpResolutionTicket).where(
            DpResolutionTicket.deleted_at.is_(None),
            DpResolutionTicket.resolution.is_(None),
            or_(
                DpResolutionTicket.status == "llm_fallback",
                and_(
                    DpResolutionTicket.status.in_(("diverged", "unparseable")),
                    or_(
                        DpResolutionTicket.divergence_reason.like("LLM 已关闭%"),
                        DpResolutionTicket.divergence_reason.like("LLM 确认输出异常%"),
                        DpResolutionTicket.divergence_reason.like("LLM 兜底输出异常%"),
                    ),
                ),
            ),
        )
        if ticket_ids:
            stmt = stmt.where(DpResolutionTicket.id.in_(ticket_ids))
        stmt = stmt.order_by(DpResolutionTicket.id.asc()).limit(limit)
        return list((await self._db.execute(stmt)).scalars().all())

    async def update_ticket_llm(
        self,
        ticket_id: int,
        *,
        status: str | None = None,
        llm_opinion: dict | None = None,
        divergence_reason: str | None = None,
    ) -> DpResolutionTicket | None:
        """LLM 重试后刷新单的意见/原因/状态（不裁决、不动 resolution/resolved_*）。"""
        ticket = await self.get_ticket(ticket_id)
        if ticket is None:
            return None
        if status is not None:
            ticket.status = status
        if llm_opinion is not None:
            ticket.llm_opinion = llm_opinion
        if divergence_reason is not None:
            ticket.divergence_reason = divergence_reason
        return ticket

    async def list_tickets(
        self,
        *,
        status: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[DpResolutionTicket], int]:
        stmt = select(DpResolutionTicket).where(DpResolutionTicket.deleted_at.is_(None))
        if status:
            stmt = stmt.where(DpResolutionTicket.status == status)
        if keyword:
            like = f"%{keyword}%"
            stmt = stmt.where(
                DpResolutionTicket.task_name.like(like)
                | DpResolutionTicket.out_table.like(like)
                | DpResolutionTicket.sql_text.like(like)
            )
        count_stmt = select(DpResolutionTicket.id).where(
            *stmt._where_criteria
        )
        total = len((await self._db.execute(count_stmt)).scalars().all())
        stmt = (
            stmt.order_by(DpResolutionTicket.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = list((await self._db.execute(stmt)).scalars().all())
        return rows, total

    async def resolve_ticket(
        self,
        ticket_id: int,
        *,
        resolution: str,
        resolved_by: int,
        manual_edges: dict | None = None,
    ) -> DpResolutionTicket | None:
        ticket = await self.get_ticket(ticket_id)
        if ticket is None:
            return None
        ticket.resolution = resolution
        ticket.resolved_by = resolved_by
        ticket.resolved_at = datetime.now(UTC)
        if manual_edges is not None:
            ticket.manual_edges_json = manual_edges
        ticket.status = "ignored" if resolution == "ignore" else "resolved"
        return ticket

    # ---- 资产 Owner 回填（D10） ----
    async def find_orphan_catalogs(self, entity_name: str) -> list[DBCatalog]:
        """查 owner 为空的同名表资产实体（仅孤儿回填，绝不覆盖人工治理）。"""
        stmt = (
            select(DBCatalog)
            .where(
                DBCatalog.entity_name == entity_name,
                DBCatalog.entity_type == "TABLE",
                DBCatalog.owner_id.is_(None),
                DBCatalog.deleted_at.is_(None),
            )
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        return list(rows)

    async def find_user_by_username(self, username: str) -> User | None:
        stmt = select(User).where(User.username == username)
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def get_or_create_external_org(self) -> Organization:
        stmt = select(Organization).where(Organization.code == SHADOW_ORG_CODE)
        org = (await self._db.execute(stmt)).scalar_one_or_none()
        if org is not None:
            return org
        org = Organization(name=SHADOW_ORG_NAME, code=SHADOW_ORG_CODE, status="active")
        self._db.add(org)
        await self._db.flush()
        return org

    async def create_shadow_user(self, username: str, director_cn: str | None = None) -> User:
        """创建不可登录影子用户（status=disabled，归属外部协作组织）。

        用户名已存在（含真实用户）直接复用，不重复创建；email 冲突时追加序号。
        """
        existing = await self.find_user_by_username(username)
        if existing is not None:
            return existing
        org = await self.get_or_create_external_org()
        display = director_cn or username
        email = f"dp_{username}{SHADOW_EMAIL_SUFFIX}"
        # email 唯一冲突兜底：追加随机后缀（极少见，防并发/历史占用）
        if await self._find_email(email):
            email = f"dp_{username}_{datetime.now(UTC).microsecond}{SHADOW_EMAIL_SUFFIX}"
        user = User(
            org_id=org.id,
            username=username,
            email=email,
            password_hash="!",  # 不可登录：无有效 bcrypt 哈希
            display_name=display,
            role=SHADOW_ROLE,
            status="disabled",
            must_change_password=False,
        )
        self._db.add(user)
        await self._db.flush()
        return user

    async def _find_email(self, email: str) -> bool:
        stmt = select(User.id).where(User.email == email).limit(1)
        return (await self._db.execute(stmt)).scalar_one_or_none() is not None

    async def update_catalog_owner(self, catalog_id: int, owner_id: int) -> None:
        await self._db.execute(
            update(DBCatalog).where(DBCatalog.id == catalog_id).values(owner_id=owner_id)
        )

    # ---- dp_task_refs 工具 ----
    @staticmethod
    def merge_task_refs(existing: str | None, new_ref: dict[str, Any]) -> str:
        """把新的 task#step 引用合并进边的 dp_task_refs JSON（按 step_id 去重）。"""
        refs: list[dict[str, Any]] = []
        if existing:
            try:
                parsed = json.loads(existing)
                if isinstance(parsed, list):
                    refs = [r for r in parsed if isinstance(r, dict)]
            except (ValueError, TypeError):
                refs = []
        step_id = new_ref.get("step_id")
        refs = [r for r in refs if r.get("step_id") != step_id]
        refs.append(new_ref)
        return json.dumps(refs, ensure_ascii=False)
