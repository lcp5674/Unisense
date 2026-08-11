"""采集领域仓储（对齐 DEV_GUIDE §8b.1 服务层 / TD §12.1）。

全部查询走 SQLAlchemy ORM 参数化（注入根因防护）；批量废弃逐条 try，
部分失败不影响已成功项（返回 207 语义由 service 层组装）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_source import DataSource, DBCatalog
from app.services.collector.schemas import BulkDeprecateItem


def _signature(source_id: str, entity_name: str) -> str:
    return hashlib.sha256(f"{source_id}:{entity_name}".encode()).hexdigest()


class CollectorRepository:
    """采集领域数据访问。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_source(self, source_id: str) -> DataSource | None:
        res = await self._db.execute(
            select(DataSource).where(
                DataSource.source_id == source_id, DataSource.deleted_at.is_(None)
            )
        )
        return res.scalar_one_or_none()

    async def create_source(self, source: DataSource) -> DataSource:
        self._db.add(source)
        await self._db.flush()
        return source

    async def soft_delete_source(self, source_id: str) -> bool:
        src = await self.get_source(source_id)
        if src is None:
            return False
        src.deleted_at = datetime.now(UTC)
        await self._db.flush()
        return True

    async def list_sources(
        self,
        *,
        domain: str | None,
        source_type: str | None,
        keyword: str | None,
        page: int,
        page_size: int,
    ) -> tuple[Sequence[DataSource], int]:
        base = select(DataSource).where(DataSource.deleted_at.is_(None))
        if domain:
            base = base.where(DataSource.domain == domain)
        if source_type:
            base = base.where(DataSource.source_type == source_type)
        if keyword:
            # 参数化 LIKE（值经绑定，无字符串拼接 SQL）
            base = base.where(DataSource.name.ilike(f"%{keyword}%"))
        count = await self._db.scalar(select(func.count()).select_from(base.subquery()))
        total = int(count) if count is not None else 0
        stmt = base.order_by(DataSource.id).offset((page - 1) * page_size).limit(page_size)
        res = await self._db.execute(stmt)
        return res.scalars().all(), total

    async def get_catalog(self, source_id: str, entity_name: str) -> DBCatalog | None:
        res = await self._db.execute(
            select(DBCatalog).where(
                DBCatalog.source_id == source_id,
                DBCatalog.entity_name == entity_name,
                DBCatalog.deleted_at.is_(None),
            )
        )
        return res.scalar_one_or_none()

    async def upsert_catalog(
        self,
        *,
        source_id: str,
        entity_name: str,
        entity_type: str,
        schema_json: dict[str, Any],
        etl_sql: str | None,
        sensitivity_level: str,
        owner_id: int | None,
    ) -> tuple[DBCatalog, bool]:
        """幂等 upsert（按 source_id+entity_name）。返回 (实体, 是否新建)。"""
        existing = await self.get_catalog(source_id, entity_name)
        if existing is None:
            cat = DBCatalog(
                source_id=source_id,
                entity_name=entity_name,
                entity_type=entity_type,
                schema_json=schema_json,
                etl_sql=etl_sql,
                sensitivity_level=sensitivity_level,
                owner_id=owner_id,
                upstream_signature=_signature(source_id, entity_name),
            )
            self._db.add(cat)
            await self._db.flush()
            return cat, True
        existing.schema_json = schema_json
        existing.etl_sql = etl_sql
        existing.sensitivity_level = sensitivity_level
        if owner_id is not None:
            existing.owner_id = owner_id
        await self._db.flush()
        return existing, False

    async def list_catalogs(self, params: Any) -> tuple[Sequence[DBCatalog], int]:
        base = select(DBCatalog).where(DBCatalog.deleted_at.is_(None))
        if params.source_id:
            base = base.where(DBCatalog.source_id == params.source_id)
        if params.entity_type:
            base = base.where(DBCatalog.entity_type == params.entity_type)
        if params.sensitivity_level:
            base = base.where(DBCatalog.sensitivity_level == params.sensitivity_level)
        if params.keyword:
            base = base.where(DBCatalog.entity_name.ilike(f"%{params.keyword}%"))
        count = await self._db.scalar(select(func.count()).select_from(base.subquery()))
        total = int(count) if count is not None else 0
        stmt = (
            base.order_by(DBCatalog.id)
            .offset((params.page - 1) * params.page_size)
            .limit(params.page_size)
        )
        res = await self._db.execute(stmt)
        return res.scalars().all(), total

    async def deprecate_catalog(self, source_id: str, entity_name: str) -> bool:
        cat = await self.get_catalog(source_id, entity_name)
        if cat is None:
            return False
        cat.deleted_at = datetime.now(UTC)
        await self._db.flush()
        return True

    async def bulk_deprecate(
        self, items: list[BulkDeprecateItem]
    ) -> tuple[list[BulkDeprecateItem], list[dict[str, Any]]]:
        succeeded: list[BulkDeprecateItem] = []
        failed: list[dict[str, Any]] = []
        for it in items:
            try:
                if await self.deprecate_catalog(it.source_id, it.entity_name):
                    succeeded.append(it)
                else:
                    failed.append({"item": it.model_dump(), "reason": "NOT_FOUND"})
            except Exception as exc:  # 单条失败不影响其余（批量 207 语义）
                failed.append({"item": it.model_dump(), "reason": str(exc)})
        return succeeded, failed

    async def recompute_coverage(self, source_id: str) -> float:
        scanned = await self._db.scalar(
            select(func.count())
            .select_from(DBCatalog)
            .where(DBCatalog.source_id == source_id, DBCatalog.deleted_at.is_(None))
        )
        total = int(scanned) if scanned is not None else 0
        src = await self.get_source(source_id)
        quota = getattr(src, "quota", None)
        expected = 0
        if isinstance(quota, dict):
            expected = int(quota.get("max_scan_rows", 0) or 0)
        elif isinstance(quota, int):
            expected = quota
        coverage = 1.0 if total else 0.0 if expected <= 0 else min(1.0, total / expected)
        if src is not None:
            src.coverage = coverage
            await self._db.flush()
        return float(coverage)
