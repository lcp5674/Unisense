"""采集领域仓储（对齐 DEV_GUIDE §8b.1 服务层 / TD §12.1）。

全部查询走 SQLAlchemy ORM 参数化（注入根因防护）；批量废弃逐条 try，
部分失败不影响已成功项（返回 207 语义由 service 层组装）。

增强：内容指纹(SHA-256) + Schema Drift 检测 + 变更历史记录。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import String, cast, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.collector_models import CollectionWatermark, SchemaDriftLog
from app.models.data_source import DataSource, DBCatalog
from app.services.collector.drift_detector import DriftDetector, compute_content_signature
from app.services.collector.schemas import BulkDeprecateItem

logger = get_logger("unisense.collector.repository")


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
        """软删除数据源，并释放 ``source_id`` 唯一约束以允许重建同名源。

        P0-3/P2-9 修复：
        - 级联清理子表：db_catalog 软删、collection_watermark / schema_drift_log 硬删
          （既避免孤儿数据，又解除外键引用，使父表改名可执行）。
        - 把软删记录的 ``source_id`` 改为 ``{source_id}__del_{ts}``，原 ID 即刻可复用，
          否则重建同名源会撞唯一约束抛 IntegrityError 500。
        """
        src = await self.get_source(source_id)
        if src is None:
            return False
        now = datetime.now(UTC)
        # 1) 清理子表（外键引用 → 先于父表改名）
        await self._db.execute(
            update(DBCatalog)
            .where(DBCatalog.source_id == source_id, DBCatalog.deleted_at.is_(None))
            .values(deleted_at=now)
        )
        await self._db.execute(
            delete(CollectionWatermark).where(CollectionWatermark.source_id == source_id)
        )
        await self._db.execute(
            delete(SchemaDriftLog).where(SchemaDriftLog.source_id == source_id)
        )
        # 2) 释放唯一约束：改名保留软删记录
        new_id = f"{source_id}__del_{int(now.timestamp())}"[:64]
        src.source_id = new_id
        src.deleted_at = now
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

    async def list_scheduled_sources(self) -> list[DataSource]:
        """列出配置了定时调度（schedule_cron 非空）的活跃数据源（P0-7 调度器扫描用）。"""
        res = await self._db.execute(
            select(DataSource).where(
                DataSource.deleted_at.is_(None),
                DataSource.schedule_cron.isnot(None),
                DataSource.schedule_cron != "",
            )
        )
        return list(res.scalars().all())

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
    ) -> tuple[DBCatalog, bool, dict[str, Any] | None]:
        """幂等 upsert（按 source_id+entity_name）。

        Returns:
            (实体, 是否新建, drift_info)。
            drift_info 为 None 表示无 Drift 或首次采集；
            非 None 时含 change_type/diff_json/before_schema/after_schema。
        """
        # 计算内容指纹
        new_signature = compute_content_signature(schema_json)

        existing = await self.get_catalog(source_id, entity_name)
        drift_info: dict[str, Any] | None = None

        if existing is None:
            # 首次采集，检查空 schema
            schema_incomplete = not schema_json.get("columns")
            cat = DBCatalog(
                source_id=source_id,
                entity_name=entity_name,
                entity_type=entity_type,
                schema_json=schema_json,
                etl_sql=etl_sql,
                sensitivity_level=sensitivity_level,
                owner_id=owner_id,
                upstream_signature=_signature(source_id, entity_name),
                content_signature=new_signature,
                schema_incomplete=schema_incomplete,
            )
            if schema_incomplete:
                logger.warning("catalog_schema_incomplete: %s/%s", source_id, entity_name)
            self._db.add(cat)
            await self._db.flush()
            return cat, True, None

        # 比对内容指纹检测 Drift
        old_signature = existing.content_signature
        old_schema = existing.schema_json

        drift_result = DriftDetector.detect(
            source_id=source_id,
            entity_name=entity_name,
            old_signature=old_signature,
            new_signature=new_signature,
            old_schema=old_schema,
            new_schema=schema_json,
        )

        if drift_result is not None:
            drift_info = {
                "change_type": drift_result.change_type,
                "diff_json": drift_result.diff_json,
                "before_schema": drift_result.before_schema,
                "after_schema": drift_result.after_schema,
                "before_signature": old_signature,
                "after_signature": new_signature,
            }
            logger.info(
                "catalog_schema_drifted: %s/%s change_type=%s",
                source_id,
                entity_name,
                drift_result.change_type,
            )

        # 更新 catalog
        existing.schema_json = schema_json
        existing.etl_sql = etl_sql
        existing.sensitivity_level = sensitivity_level
        existing.content_signature = new_signature
        existing.schema_incomplete = not schema_json.get("columns")
        if owner_id is not None:
            existing.owner_id = owner_id
        await self._db.flush()
        return existing, False, drift_info

    async def list_catalogs(self, params: Any) -> tuple[Sequence[DBCatalog], int]:
        base = select(DBCatalog).where(DBCatalog.deleted_at.is_(None))
        if params.source_id:
            base = base.where(DBCatalog.source_id == params.source_id)
        if params.entity_type:
            base = base.where(DBCatalog.entity_type == params.entity_type)
        if params.sensitivity_level:
            base = base.where(DBCatalog.sensitivity_level == params.sensitivity_level)
        if params.keyword:
            # 表+字段级搜索：entity_name 模糊 OR schema_json 字段名/注释模糊（CAST 跨方言）
            # LIKE 通配符转义（对齐 FR-035：% / _ 须转义，防模糊放大）
            escaped = params.keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            base = base.where(
                or_(
                    DBCatalog.entity_name.ilike(f"%{escaped}%"),
                    cast(DBCatalog.schema_json, String).ilike(f"%{escaped}%"),
                )
            )
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
        # P2-3: 无配额基线时 coverage=0.0（表示"覆盖率未知"），
        # 而非旧的 1.0（"无配额=全覆盖"是误导性数据）。
        coverage = 0.0 if expected <= 0 else min(1.0, total / expected)
        if src is not None:
            src.coverage = coverage
            await self._db.flush()
        return float(coverage)

    # ---- Schema Drift 相关方法 ----

    async def save_drift_log(self, drift_log: SchemaDriftLog) -> SchemaDriftLog:
        """保存 Schema 变更日志。"""
        self._db.add(drift_log)
        await self._db.flush()
        return drift_log

    async def list_drift_logs(
        self,
        source_id: str,
        entity_name: str | None = None,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[Sequence[SchemaDriftLog], int]:
        """查询 Schema 变更日志。"""
        base = select(SchemaDriftLog).where(SchemaDriftLog.source_id == source_id)
        if entity_name:
            base = base.where(SchemaDriftLog.entity_name == entity_name)
        count = await self._db.scalar(select(func.count()).select_from(base.subquery()))
        total = int(count) if count is not None else 0
        stmt = (
            base.order_by(SchemaDriftLog.detected_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        res = await self._db.execute(stmt)
        return res.scalars().all(), total

    # ---- 采集水位相关方法 ----

    async def get_watermark(self, source_id: str) -> CollectionWatermark | None:
        """获取数据源的采集水位。"""
        res = await self._db.execute(
            select(CollectionWatermark).where(CollectionWatermark.source_id == source_id)
        )
        return res.scalar_one_or_none()

    async def save_watermark(self, watermark: CollectionWatermark) -> CollectionWatermark:
        """保存/更新采集水位。"""
        self._db.add(watermark)
        await self._db.flush()
        return watermark

    async def update_watermark_after_collection(
        self,
        source_id: str,
        mode: str,
        scanned_count: int,
        failed_count: int,
        content_fingerprints: dict[str, str] | None = None,
    ) -> CollectionWatermark:
        """采集完成后更新采集水位。

        首次采集时创建新记录，后续更新已有记录。

        Args:
            source_id: 数据源标识。
            mode: 采集模式（FULL/INCREMENTAL）。
            scanned_count: 采集表数。
            failed_count: 失败表数。
            content_fingerprints: 实体级内容指纹映射 {entity_name: signature}
                （P2-4：此前该列声明但从不写入，现由 service 层采集后回填）。
        """
        existing = await self.get_watermark(source_id)
        now = datetime.now(UTC)
        if existing is not None:
            existing.last_collected_at = now
            existing.mode = mode
            existing.scanned_count = scanned_count
            existing.failed_count = failed_count
            if content_fingerprints:
                existing.content_fingerprints = content_fingerprints
            await self._db.flush()
            return existing

        watermark = CollectionWatermark(
            source_id=source_id,
            last_collected_at=now,
            mode=mode,
            scanned_count=scanned_count,
            failed_count=failed_count,
            content_fingerprints=content_fingerprints or {},
        )
        self._db.add(watermark)
        await self._db.flush()
        return watermark

    async def update_health_status(
        self, source_id: str, status: str, error: str | None = None
    ) -> None:
        """更新数据源健康状态（P1-3：可附带最近错误信息，供健康端点返回）。"""
        src = await self.get_source(source_id)
        if src is not None:
            src.health_status = status
            src.last_health_check = datetime.now(UTC)
            if error is not None:
                src.last_error = error[:512]
            elif status == "healthy":
                # 恢复健康时清空历史错误
                src.last_error = None
            await self._db.flush()
