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

from sqlalchemy import select, update

from app.db.mysql import AsyncSession
from app.models.data_source import DBCatalog
from app.models.dp_sync import (
    DpResolutionTicket,
    DpSyncConfig,
    DpSyncRunLog,
    DpSyncWatermark,
    LineageFieldMapping,
)
from app.models.user import Organization, User

#: 影子用户归属组织（不存在时自动创建）。
SHADOW_ORG_CODE = "external"
SHADOW_ORG_NAME = "外部协作"

#: 影子用户 email 后缀（User.email 唯一，须避免与真实用户冲突）。
SHADOW_EMAIL_SUFFIX = "@external.local"

#: 影子用户默认角色（无平台权限，仅作为资产 Owner 挂接展示）。
SHADOW_ROLE = "viewer"


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
        """按唯一索引幂等写入字段映射（已存在则忽略，不重复）。"""
        stmt = select(LineageFieldMapping).where(
            LineageFieldMapping.source_table == source_table,
            LineageFieldMapping.source_column.is_(source_column),
            LineageFieldMapping.target_table == target_table,
            LineageFieldMapping.target_column == target_column,
            LineageFieldMapping.degraded.is_(degraded),
            LineageFieldMapping.deleted_at.is_(None),
        )
        existing = (await self._db.execute(stmt)).scalar_one_or_none()
        if existing is not None:
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
