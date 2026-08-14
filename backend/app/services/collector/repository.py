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

from sqlalchemy import String, cast, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.collector_models import CollectionWatermark, SchemaDriftLog
from app.models.data_source import ColumnDescription, DataSource, DBCatalog
from app.services.collector.drift_detector import DriftDetector, compute_content_signature
from app.services.collector.schemas import BulkDeprecateItem

logger = get_logger("unisense.collector.repository")


def _signature(source_id: str, entity_name: str) -> str:
    return hashlib.sha256(f"{source_id}:{entity_name}".encode()).hexdigest()


def _catalog_columns(schema_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    """解析 schema_json 的字段清单（兼容 columns/fields 两种键）。"""
    schema = schema_json or {}
    columns = schema.get("columns") or schema.get("fields") or []
    return [c for c in columns if isinstance(c, dict) and (c.get("name") or c.get("column"))]


def _column_has_desc(
    col: dict[str, Any],
    catalog_id: int,
    desc_keys: set[tuple[int, str]],
) -> bool:
    """字段是否有描述：schema comment 非空 或 column_descriptions 有 manual/llm 记录。"""
    name = str(col.get("name") or col.get("column"))
    comment = (col.get("comment") or "").strip()
    return bool(comment) or (catalog_id, name) in desc_keys


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

        P0-1/P0-3/P2-9 修复：
        - 级联清理子表：先更新子表 source_id 引用，再 rename 父表 source_id，
          确保 FK 约束链完整（避免 MySQL FK 1451 错误）。
        - db_catalog / collection_watermark / schema_drift_log 的 source_id 均更新为新名。
        - 把软删记录的 ``source_id`` 改为 ``{source_id}__del_{ts}``，原 ID 即刻可复用，
          否则重建同名源会撞唯一约束抛 IntegrityError 500。

        Note:
            MySQL 外键默认 ``RESTRICT``——在父表改名之前，把子表 ``source_id`` 更新为
            尚不存在的新值会立即触发 FK 1452。因此整个级联改名在事务内临时关闭
            ``FOREIGN_KEY_CHECKS``（会话级开关，事务提交即失效，无并发风险）。
        """
        src = await self.get_source(source_id)
        if src is None:
            return False
        now = datetime.now(UTC)
        new_id = f"{source_id}__del_{int(now.timestamp())}"[:64]
        # P0-1 Fix: 事务内关闭 FK 检查，完成级联改名后再恢复（避免 FK 1452/1451）
        await self._db.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        try:
            await self._db.execute(
                update(CollectionWatermark)
                .where(CollectionWatermark.source_id == source_id)
                .values(source_id=new_id)
            )
            await self._db.execute(
                update(DBCatalog).where(DBCatalog.source_id == source_id).values(source_id=new_id)
            )
            # 变更审计日志一并改名保留（软删父源后仍可追溯）
            await self._db.execute(
                update(SchemaDriftLog)
                .where(SchemaDriftLog.source_id == source_id)
                .values(source_id=new_id)
            )
            # 释放唯一约束：改名保留软删记录
            src.source_id = new_id
            src.deleted_at = now
            await self._db.flush()
        finally:
            await self._db.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        return True

    async def set_source_enabled(self, source_id: str, enabled: bool) -> DataSource | None:
        """设置数据源启用状态（批量启停逐条复用；不存在返回 None）。"""
        src = await self.get_source(source_id)
        if src is None:
            return None
        src.enabled = enabled
        await self._db.flush()
        return src

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
        """列出配置了定时调度（schedule_cron 非空）且启用中的活跃数据源（P0-7 调度器扫描用）。

        停用（enabled=False）的数据源不参与定时调度，避免维护窗口期被自动触发。
        """
        res = await self._db.execute(
            select(DataSource).where(
                DataSource.deleted_at.is_(None),
                DataSource.enabled.is_(True),
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

    async def get_catalog_by_id(self, catalog_id: int) -> DBCatalog | None:
        """按主键取目录实体（血缘图谱表节点下钻详情用）。"""
        res = await self._db.execute(
            select(DBCatalog).where(
                DBCatalog.id == catalog_id,
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

        # 元数据增量短路：内容签名未变、无漂移且无归属变更 → 不写库。
        # 这是 PostgreSQL 等无源端修改时间戳类型的关键增量机制——
        # information_schema 目录扫描本身廉价，真正代价是逐实体 UPDATE；
        # 短路后全量扫描退化为「仅变更落库」，大幅降低写放大。
        if drift_result is None and old_signature == new_signature and owner_id is None:
            return existing, False, None

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
        # "all" 时退化为普通查询（不过滤删除状态，不过滤 source_id）
        source_status = getattr(params, "source_status", None)
        if source_status in ("active", "deleted"):
            return await self._list_catalogs_with_source_status(params)

        base = select(DBCatalog).where(DBCatalog.deleted_at.is_(None))
        if params.source_id:
            base = base.where(DBCatalog.source_id == params.source_id)
        if params.entity_type:
            base = base.where(DBCatalog.entity_type == params.entity_type)
        if params.sensitivity_level:
            base = base.where(DBCatalog.sensitivity_level == params.sensitivity_level)
        domain = getattr(params, "domain", None)
        if domain:
            # db_catalog 无 domain 列，经数据源继承过滤（仅活跃源归属明确）
            base = base.join(DataSource, DataSource.source_id == DBCatalog.source_id).where(
                DataSource.deleted_at.is_(None),
                DataSource.domain == domain,
            )
        db_name = getattr(params, "database", None)
        if db_name:
            # 库名 = entity_name 前缀（库.表）；LIKE 通配符转义防模糊放大
            esc_db = db_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            base = base.where(DBCatalog.entity_name.ilike(f"{esc_db}.%"))
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

    async def _list_catalogs_with_source_status(
        self, params: Any
    ) -> tuple[Sequence[DBCatalog], int]:
        """按源状态过滤目录（active=仅活跃源 / deleted=仅已删除源）。

        外连接 DataSource 取删除状态与名称，并以瞬态属性（``_src_deleted`` /
        ``_src_name``）挂到 ORM 对象上，供 service 层组装响应——不改变
        list_catalogs 的返回形态，避免破坏既有调用方。
        """
        base = (
            select(DBCatalog, DataSource.deleted_at, DataSource.name)
            .outerjoin(DataSource, DataSource.source_id == DBCatalog.source_id)
            .where(DBCatalog.deleted_at.is_(None))
        )
        if params.source_id:
            base = base.where(DBCatalog.source_id == params.source_id)
        if params.entity_type:
            base = base.where(DBCatalog.entity_type == params.entity_type)
        if params.sensitivity_level:
            base = base.where(DBCatalog.sensitivity_level == params.sensitivity_level)
        domain = getattr(params, "domain", None)
        if domain:
            # 已 outerjoin DataSource，直接按源域过滤（已删除源也能按原域匹配）
            base = base.where(DataSource.domain == domain)
        db_name = getattr(params, "database", None)
        if db_name:
            esc_db = db_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            base = base.where(DBCatalog.entity_name.ilike(f"{esc_db}.%"))
        if params.keyword:
            escaped = params.keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            base = base.where(
                or_(
                    DBCatalog.entity_name.ilike(f"%{escaped}%"),
                    cast(DBCatalog.schema_json, String).ilike(f"%{escaped}%"),
                )
            )
        if params.source_status == "active":
            base = base.where(DataSource.deleted_at.is_(None))
        else:
            base = base.where(DataSource.deleted_at.isnot(None))
        count = await self._db.scalar(select(func.count()).select_from(base.subquery()))
        total = int(count) if count is not None else 0
        stmt = (
            base.order_by(DBCatalog.id)
            .offset((params.page - 1) * params.page_size)
            .limit(params.page_size)
        )
        res = await self._db.execute(stmt)
        cats: list[DBCatalog] = []
        for row in res.all():
            cat = row[0]
            # 瞬态属性（不入库），供 service 组装 source_deleted / source_name
            cat._src_deleted = row[1] is not None
            cat._src_name = row[2]
            cats.append(cat)
        return cats, total

    async def get_sources_meta(self, source_ids: Sequence[str]) -> dict[str, tuple[str, bool]]:
        """批量取数据源名称与删除状态（供目录展示 source 维度信息）。

        Returns:
            {source_id: (名称, 是否已删除)}；源不存在时不在结果中。
        """
        ids = list(source_ids)
        if not ids:
            return {}
        res = await self._db.execute(
            select(DataSource.source_id, DataSource.name, DataSource.deleted_at).where(
                DataSource.source_id.in_(ids)
            )
        )
        return {row[0]: (row[1] or row[0], row[2] is not None) for row in res.all()}

    async def list_catalog_databases(self, source_id: str | None = None) -> list[str]:
        """目录去重库名列表（entity_name 前缀，供前端库名筛选下拉）。

        - 仅统计未删除（deleted_at IS NULL）的实体；
        - 指定 source_id 时仅统计该源；
        - 无前缀（无 "." 的实体，如 Kafka topic）不计入库名。
        """
        base = select(DBCatalog.entity_name).where(DBCatalog.deleted_at.is_(None))
        if source_id:
            base = base.where(DBCatalog.source_id == source_id)
        res = await self._db.execute(base)
        dbs: set[str] = set()
        for (name,) in res.all():
            if "." in name:
                dbs.add(name.split(".", 1)[0])
        return sorted(dbs)

    async def list_active_entity_names(self, source_id: str) -> list[str]:
        """返回数据源下所有未废弃（deleted_at IS NULL）的实体名，用于对账。"""
        res = await self._db.execute(
            select(DBCatalog.entity_name).where(
                DBCatalog.source_id == source_id,
                DBCatalog.deleted_at.is_(None),
            )
        )
        return [row[0] for row in res.all()]

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

    # ---- 字段描述 CRUD ----

    async def get_descriptions(self, catalog_id: int) -> Sequence[ColumnDescription]:
        """获取指定 catalog 下所有字段描述记录。"""
        res = await self._db.execute(
            select(ColumnDescription).where(
                ColumnDescription.catalog_id == catalog_id,
                ColumnDescription.deleted_at.is_(None),
            )
        )
        return res.scalars().all()

    async def upsert_description(
        self,
        catalog_id: int,
        column_name: str,
        description: str,
        source: str,
        updated_by: int | None = None,
    ) -> ColumnDescription:
        """Upsert 单条字段描述（按 catalog_id + column_name 唯一键）。"""
        existing = (
            await self._db.execute(
                select(ColumnDescription).where(
                    ColumnDescription.catalog_id == catalog_id,
                    ColumnDescription.column_name == column_name,
                    ColumnDescription.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            # 优先级链保护：manual 不被 llm 覆盖，llm 不被 schema 覆盖
            priority = {"manual": 3, "llm": 2, "schema": 1}
            if priority.get(source, 0) >= priority.get(existing.source, 0):
                existing.description = description
                existing.source = source
                existing.updated_by = updated_by
                await self._db.flush()
            return existing

        desc = ColumnDescription(
            catalog_id=catalog_id,
            column_name=column_name,
            description=description,
            source=source,
            updated_by=updated_by,
        )
        self._db.add(desc)
        await self._db.flush()
        return desc

    async def batch_upsert_descriptions(
        self,
        catalog_id: int,
        items: list[dict[str, Any]],
        source: str,
    ) -> list[ColumnDescription]:
        """批量 upsert 字段描述。

        Args:
            catalog_id: 目录 ID。
            items: [{column_name, description}, ...]
            source: 来源标记（llm/schema）。
        """
        results: list[ColumnDescription] = []
        for item in items:
            desc = await self.upsert_description(
                catalog_id=catalog_id,
                column_name=item["column_name"],
                description=item["description"],
                source=source,
            )
            results.append(desc)
        return results

    # ---- 表级业务描述（治理补全，TD §12.1）----

    async def update_table_description(
        self,
        catalog_id: int,
        description: str,
        source: str,
        updated_by: int | None = None,
    ) -> DBCatalog | None:
        """人工/LLM 更新表级业务描述（db_catalog.description）。

        采集 upsert 显式设置既有字段，不会覆盖这些治理列。

        Returns:
            更新后的 DBCatalog；目录不存在返回 None。
        """
        cat = (
            await self._db.execute(
                select(DBCatalog).where(
                    DBCatalog.id == catalog_id, DBCatalog.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if cat is None:
            return None
        cat.description = description
        cat.description_source = source
        cat.description_updated_by = updated_by
        cat.description_updated_at = datetime.now(UTC)
        await self._db.flush()
        return cat

    async def get_description_coverage(self) -> dict[str, Any]:
        """描述缺失统计：表/字段覆盖率 + 按表列缺失字段数（治理优先级）。

        Returns:
            {
                "total_tables": 表总数,
                "tables_with_desc": 有表级描述数,
                "tables_missing_desc": 缺表级描述数,
                "total_fields": 字段总数,
                "fields_with_desc": 有字段描述数,
                "fields_missing_desc": 缺字段描述数,
                "per_table": 按表列明细（可排序）,
            }
        """
        cats = (
            await self._db.execute(
                select(DBCatalog).where(DBCatalog.deleted_at.is_(None))
            )
        ).scalars().all()
        descs = (
            await self._db.execute(
                select(ColumnDescription).where(ColumnDescription.deleted_at.is_(None))
            )
        ).scalars().all()
        srcs = (
            await self._db.execute(
                select(DataSource.source_id, DataSource.domain)
            )
        ).all()

        domain_map = {row.source_id: row.domain for row in srcs}
        # 仅 manual/llm 记录计入已描述（schema 来源与 comment 等价，避免重复计）
        desc_keys: set[tuple[int, str]] = {
            (d.catalog_id, d.column_name)
            for d in descs
            if d.source in ("manual", "llm")
        }

        per_table: list[dict[str, Any]] = []
        total_fields = 0
        fields_with_desc = 0
        for cat in cats:
            columns = _catalog_columns(cat.schema_json)
            total = len(columns)
            covered = sum(1 for c in columns if _column_has_desc(c, cat.id, desc_keys))
            total_fields += total
            fields_with_desc += covered
            per_table.append(
                {
                    "catalog_id": cat.id,
                    "entity_name": cat.entity_name,
                    "source_id": cat.source_id,
                    "entity_type": cat.entity_type,
                    "domain": domain_map.get(cat.source_id),
                    "sensitivity_level": cat.sensitivity_level,
                    "table_desc": bool(cat.description and cat.description.strip()),
                    "total_fields": total,
                    "covered_fields": covered,
                    "missing_fields": total - covered,
                }
            )

        tables_with_desc = sum(1 for t in per_table if t["table_desc"])
        return {
            "total_tables": len(per_table),
            "tables_with_desc": tables_with_desc,
            "tables_missing_desc": len(per_table) - tables_with_desc,
            "total_fields": total_fields,
            "fields_with_desc": fields_with_desc,
            "fields_missing_desc": total_fields - fields_with_desc,
            "per_table": per_table,
        }
