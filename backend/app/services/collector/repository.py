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

from sqlalchemy import String, case, cast, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.core.logging import get_logger
from app.models.collector_models import CollectionRun, CollectionWatermark, SchemaDriftLog
from app.models.data_source import ColumnDescription, DataSource, DBCatalog
from app.models.governance import Classification
from app.models.user import User
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
    """字段是否有治理描述：仅 ``column_descriptions`` 的 manual/llm 记录。

    口径对齐汇总 ``fields_with_desc``（只计 manual/llm）——schema comment 是采集
    原始值、非治理产出，若计入会使 per_table 明细与汇总口径矛盾
    （摘要 fields_missing_desc ≠ 各表 missing_fields 之和，治理数据误导）。
    """
    name = str(col.get("name") or col.get("column"))
    return (catalog_id, name) in desc_keys


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
            # 采集运行历史一并改名保留（软删后仍可追溯历史记录）
            await self._db.execute(
                update(CollectionRun)
                .where(CollectionRun.source_id == source_id)
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

    async def set_source_governance(
        self,
        source_id: str,
        *,
        owner_id: int | None = None,
        description: str | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> DataSource | None:
        """更新数据源治理字段（owner_id/description/include/exclude patterns）。

        PATCH 语义：仅更新传入（非 None）的字段，未传字段保持原值。
        不存在返回 None。

        Args:
            owner_id: 数据源负责人用户 ID。
            description: 用途描述。
            include_patterns: 表级包含白名单（JSON list[str]）。
            exclude_patterns: 表级排除黑名单（JSON list[str]）。
        """
        src = await self.get_source(source_id)
        if src is None:
            return None
        if owner_id is not None:
            src.owner_id = owner_id
        if description is not None:
            src.description = description
        if include_patterns is not None:
            src.include_patterns = include_patterns
        if exclude_patterns is not None:
            src.exclude_patterns = exclude_patterns
        await self._db.flush()
        return src

    async def list_sources(
        self,
        *,
        domain: str | None,
        source_type: str | None,
        keyword: str | None,
        health_status: str | None = None,
        owner_id: int | None = None,
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
            # 通配符转义（对齐 FR-035）：keyword 含 %/_ 时须转义，否则模糊放大匹配
            # Med 4: 同时匹配名称与 source_id——前端占位「搜索数据源名称 / ID」，
            # 仅匹配 name 会让按 ID 搜索永远无结果（误导「数据源不存在」）。
            escaped = keyword.replace("/", "//").replace("%", "/%").replace("_", "/_")
            base = base.where(
                or_(
                    DataSource.name.ilike(f"%{escaped}%", escape="/"),
                    DataSource.source_id.ilike(f"%{escaped}%", escape="/"),
                )
            )
        if health_status:
            # 总览仪表「数据源」资产卡片按健康状态下钻（healthy/unhealthy/unknown）
            base = base.where(DataSource.health_status == health_status)
        if owner_id is not None:
            base = base.where(DataSource.owner_id == owner_id)
        count = await self._db.scalar(select(func.count()).select_from(base.subquery()))
        total = int(count) if count is not None else 0
        stmt = base.order_by(DataSource.id).offset((page - 1) * page_size).limit(page_size)
        res = await self._db.execute(stmt)
        return res.scalars().all(), total

    async def list_scheduled_sources(self) -> list[DataSource]:
        """列出配置了定时调度（schedule_cron 非空 + schedule_enabled）且启用中的活跃数据源。

        数据源停用（enabled=False）或调度停用（schedule_enabled=False）时不参与
        定时调度：前者避免维护窗口期被自动触发，后者是独立的「调度启停」开关
        （源仍可手动采集，仅暂停自动定时）。
        """
        res = await self._db.execute(
            select(DataSource).where(
                DataSource.deleted_at.is_(None),
                DataSource.enabled.is_(True),
                DataSource.schedule_enabled.is_(True),
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

    async def get_catalog_any_status(
        self, source_id: str, entity_name: str
    ) -> DBCatalog | None:
        """按幂等键取目录实体（**含软删行**）。

        唯一约束 ``uk_db_catalog_entity`` 仅含 (source_id, entity_name)、不含
        deleted_at——软删实体仍占着幂等键。源端表重现时若只查活跃行会误判为
        新建 → INSERT 撞软删行的唯一键。本方法供 upsert 复用软删行（清除
        deleted_at 重新激活），替代失败重试。
        """
        res = await self._db.execute(
            select(DBCatalog).where(
                DBCatalog.source_id == source_id,
                DBCatalog.entity_name == entity_name,
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
        description: str | None = None,
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
            # 源端表重现场景：活跃行不存在但可能有软删行占着幂等键。
            # 直接复用软删行（清 deleted_at 重新激活），避免 INSERT 撞
            # uk_db_catalog_entity（唯一键不含 deleted_at）。
            soft_deleted = await self.get_catalog_any_status(source_id, entity_name)
            if soft_deleted is not None and soft_deleted.deleted_at is not None:
                existing = soft_deleted
                existing.deleted_at = None

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
                description=description or None,
                description_source=("schema" if description else None),
                upstream_signature=_signature(source_id, entity_name),
                content_signature=new_signature,
                schema_incomplete=schema_incomplete,
            )
            if schema_incomplete:
                logger.warning("catalog_schema_incomplete: %s/%s", source_id, entity_name)
            try:
                # 竞态防护：并发采集同源/种子脚本直插时，两事务可能都读到 existing=None
                # 双双 INSERT 撞唯一键 uk_db_catalog_entity。用 SAVEPOINT 包裹 flush，
                # 撞键时仅回滚该 SAVEPOINT（新增对象被 expunge，不会污染外层事务），
                # 再重查走更新语义——避免 PendingRollback 拖垮整批采集。
                async with self._db.begin_nested():
                    self._db.add(cat)
                    await self._db.flush()
                return cat, True, None
            except IntegrityError:
                existing = await self.get_catalog_any_status(source_id, entity_name)
                if existing is None:
                    raise
                if existing.deleted_at is not None:
                    # 软删实体在源端重现：复用恢复（清 deleted_at），继续走更新语义
                    existing.deleted_at = None

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

        # 元数据增量短路：内容签名未变、无漂移、无归属变更且表描述未变 → 不写库。
        # 这是 PostgreSQL 等无源端修改时间戳类型的关键增量机制——
        # information_schema 目录扫描本身廉价，真正代价是逐实体 UPDATE；
        # 短路后全量扫描退化为「仅变更落库」，大幅降低写放大。
        # 表描述变化（采集值非空且不同于现值、且非人工编辑）纳入变更判定，
        # 否则 HMS 直连的 TBL_COMMENT 更新会因签名未变被静默丢弃。
        desc_changed = (
            description is not None
            and existing.description_source != "manual"
            and (existing.description or "") != description
        )
        if (
            drift_result is None
            and old_signature == new_signature
            and owner_id is None
            and not desc_changed
        ):
            return existing, False, None

        # 更新 catalog
        existing.schema_json = schema_json
        existing.etl_sql = etl_sql
        existing.sensitivity_level = sensitivity_level
        existing.content_signature = new_signature
        existing.schema_incomplete = not schema_json.get("columns")
        if owner_id is not None:
            existing.owner_id = owner_id
        if desc_changed:
            # 采集的表级描述（description_source=schema）写入；人工/LLM 描述不被覆盖
            existing.description = description
            existing.description_source = "schema"
        await self._db.flush()
        return existing, False, drift_info

    async def upsert_classification(
        self,
        catalog_id: int,
        sensitivity_level: str,
        pii_columns: list[dict[str, Any]],
        classified_by: str = "rule_engine",
        model_version: str = "rules-v2",
    ) -> None:
        """写/更新字段级 PII 命中明细到 classification 表（采集时随分级落库）。

        Args:
            catalog_id: db_catalog.id。
            sensitivity_level: 敏感级别（PII/CONFIDENTIAL/...）。
            pii_columns: ``detect_pii_fields`` 的命中明细（列名/类别/规则/置信度）。
            classified_by: 分级来源（默认规则引擎）。
            model_version: 规则版本（默认 rules-v2）。
        """
        existing = (
            await self._db.execute(
                select(Classification).where(
                    Classification.catalog_id == catalog_id,
                    Classification.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        # classification.sensitivity_level 枚举（SensitivityLevel）不含 NEEDS_REVIEW——
        # 待复核统一落 UNKNOWN（与 db_catalog 的 NEEDS_REVIEW 列语义等价）。
        level = "UNKNOWN" if sensitivity_level == "NEEDS_REVIEW" else sensitivity_level
        if existing is not None:
            existing.sensitivity_level = level
            existing.pii_columns = pii_columns
            existing.classified_by = classified_by
            existing.model_version = model_version
        else:
            self._db.add(
                Classification(
                    catalog_id=catalog_id,
                    sensitivity_level=level,
                    pii_columns=pii_columns,
                    classified_by=classified_by,
                    model_version=model_version,
                )
            )
        await self._db.flush()

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
        owner_id = getattr(params, "owner_id", None)
        if owner_id is not None:
            base = base.where(DBCatalog.owner_id == owner_id)
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
            esc_db = db_name.replace("/", "//").replace("%", "/%").replace("_", "/_")
            base = base.where(DBCatalog.entity_name.ilike(f"{esc_db}.%", escape="/"))
        if params.keyword:
            # 表+字段级搜索：entity_name 模糊 OR schema_json 字段名/注释模糊（CAST 跨方言）
            # LIKE 通配符转义（对齐 FR-035：% / _ 须转义，防模糊放大）。
            # 修复前：转义为 \\% 但 ilike() 无 escape 参数不生成 ESCAPE 子句，
            # MySQL 默认把 \\ 当普通字符、%/_ 仍当通配符 → 转义实际失效。
            # 改用 / 作转义符 + 显式 escape="/"（SQLAlchemy 生成 ESCAPE '/'）。
            escaped = params.keyword.replace("/", "//").replace("%", "/%").replace("_", "/_")
            base = base.where(
                or_(
                    DBCatalog.entity_name.ilike(f"%{escaped}%", escape="/"),
                    cast(DBCatalog.schema_json, String).ilike(f"%{escaped}%", escape="/"),
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
            select(DBCatalog, DataSource.deleted_at, DataSource.name, DataSource.domain)
            .outerjoin(DataSource, DataSource.source_id == DBCatalog.source_id)
            .where(DBCatalog.deleted_at.is_(None))
        )
        if params.source_id:
            base = base.where(DBCatalog.source_id == params.source_id)
        if params.entity_type:
            base = base.where(DBCatalog.entity_type == params.entity_type)
        if params.sensitivity_level:
            base = base.where(DBCatalog.sensitivity_level == params.sensitivity_level)
        owner_id = getattr(params, "owner_id", None)
        if owner_id is not None:
            base = base.where(DBCatalog.owner_id == owner_id)
        domain = getattr(params, "domain", None)
        if domain:
            # 已 outerjoin DataSource，直接按源域过滤（已删除源也能按原域匹配）
            base = base.where(DataSource.domain == domain)
        db_name = getattr(params, "database", None)
        if db_name:
            esc_db = db_name.replace("/", "//").replace("%", "/%").replace("_", "/_")
            base = base.where(DBCatalog.entity_name.ilike(f"{esc_db}.%", escape="/"))
        if params.keyword:
            # 同款修复：/ 作转义符 + 显式 escape="/"（对齐 FR-035，防模糊放大）
            escaped = params.keyword.replace("/", "//").replace("%", "/%").replace("_", "/_")
            base = base.where(
                or_(
                    DBCatalog.entity_name.ilike(f"%{escaped}%", escape="/"),
                    cast(DBCatalog.schema_json, String).ilike(f"%{escaped}%", escape="/"),
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
            # 瞬态属性（不入库），供 service 组装 source_deleted / source_name / domain
            cat._src_deleted = row[1] is not None
            cat._src_name = row[2]
            cat._src_domain = row[3]
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

    async def get_sources_domain(self, source_ids: Sequence[str]) -> dict[str, str]:
        """批量取数据源所属业务域（db_catalog 无 domain 列，经数据源继承）。"""
        ids = list(source_ids)
        if not ids:
            return {}
        res = await self._db.execute(
            select(DataSource.source_id, DataSource.domain).where(
                DataSource.source_id.in_(ids)
            )
        )
        return {row[0]: row[1] for row in res.all()}

    async def get_owner_names(self, user_ids: Sequence[int]) -> dict[int, str]:
        """批量取用户展示名（display_name 优先，回退 username），供目录责任人列展示。"""
        ids = list(user_ids)
        if not ids:
            return {}
        res = await self._db.execute(
            select(User.id, User.display_name, User.username).where(User.id.in_(ids))
        )
        return {row[0]: (row[1] or row[2]) for row in res.all()}

    async def list_catalog_databases(
        self, source_id: str | None = None, source_status: str | None = None
    ) -> list[str]:
        """目录去重库名列表（entity_name 前缀，供前端库名筛选下拉）。

        - 仅统计未删除（deleted_at IS NULL）的实体；
        - 指定 source_id 时仅统计该源；
        - source_status=active/deleted 时按源删除状态过滤（与列表默认「活跃源」
          对齐，避免已删源的库名出现在活跃下拉中）；
        - 无前缀（无 "." 的实体，如 Kafka topic）不计入库名。
        """
        base = select(DBCatalog.entity_name).where(DBCatalog.deleted_at.is_(None))
        if source_id:
            base = base.where(DBCatalog.source_id == source_id)
        if source_status in ("active", "deleted"):
            # outerjoin DataSource 按源软删状态过滤：active=仅活跃源 /
            # deleted=仅已删源；无对应 data_source 记录的行归入 active（同
            # _list_catalogs_with_source_status 语义，保持两处一致）。
            base = base.outerjoin(DataSource, DataSource.source_id == DBCatalog.source_id)
            if source_status == "active":
                base = base.where(DataSource.deleted_at.is_(None))
            else:
                base = base.where(DataSource.deleted_at.isnot(None))
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

    async def recompute_coverage(
        self, source_id: str, total_entities: int | None = None
    ) -> float:
        """重算资产覆盖率（TD §2051：已采集实体 / 源端实体总数）。

        - ``total_entities`` 提供时（采集完成后）：先刷新源端实体总数基线再计算；
        - 未提供时（单实体注册等增量路径）：沿用已存基线；
        - 无基线（从未采集/源端扫描数为 0）时 coverage=0.0（覆盖率未知，
          非误导性的 1.0）。
        """
        total = await self._db.scalar(
            select(func.count())
            .select_from(DBCatalog)
            .where(DBCatalog.source_id == source_id, DBCatalog.deleted_at.is_(None))
        )
        registered = int(total) if total is not None else 0

        src = await self.get_source(source_id)
        if src is None:
            return 0.0
        if total_entities is not None:
            src.source_total_entities = max(0, int(total_entities))
        expected = int(getattr(src, "source_total_entities", 0) or 0)
        coverage = 0.0 if expected <= 0 else min(1.0, registered / expected)
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
        """查询 Schema 变更日志。

        P2-17: 列表只加载列表所需列（``load_only`` 排除 before/after_schema
        大 JSON——列表仅展示 diff_json 摘要），避免全列传输/解析大字段。
        """
        base = (
            select(SchemaDriftLog)
            .options(
                load_only(
                    SchemaDriftLog.id,
                    SchemaDriftLog.source_id,
                    SchemaDriftLog.entity_name,
                    SchemaDriftLog.change_type,
                    SchemaDriftLog.before_signature,
                    SchemaDriftLog.after_signature,
                    SchemaDriftLog.diff_json,
                    SchemaDriftLog.detected_at,
                )
            )
            .where(SchemaDriftLog.source_id == source_id)
        )
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

    # ---- 采集运行历史相关方法 ----

    async def create_collection_run(
        self,
        *,
        source_id: str,
        trigger: str,
        mode: str,
        job_id: str | None = None,
        actor_id: int | None = None,
    ) -> CollectionRun:
        """创建一次采集运行记录（初始状态 RUNNING）。"""
        run = CollectionRun(
            source_id=source_id,
            job_id=job_id,
            trigger=trigger,
            mode=mode,
            status="RUNNING",
            actor_id=actor_id,
            started_at=datetime.now(UTC),
        )
        self._db.add(run)
        await self._db.flush()
        return run

    async def get_collection_run(self, run_id: int) -> CollectionRun | None:
        """按主键取采集运行记录（详情接口）。"""
        return (
            await self._db.execute(
                select(CollectionRun).where(
                    CollectionRun.id == run_id, CollectionRun.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()

    async def complete_collection_run(
        self, run_id: int, result: dict[str, Any]
    ) -> CollectionRun | None:
        """采集成功收尾：回填指标与明细，状态 → COMPLETED。"""
        run = await self.get_collection_run(run_id)
        if run is None:
            return None
        run.status = "COMPLETED"
        run.finished_at = datetime.now(UTC)
        run.effective_mode = result.get("mode")
        run.scanned = int(result.get("scanned") or 0)
        run.registered = int(result.get("registered") or 0)
        run.pii_registered = int(result.get("pii_registered") or 0)
        run.failed_count = int(result.get("failed_count") or 0)
        run.drift_count = int(result.get("drift_count") or 0)
        run.deprecated_count = int(result.get("deprecated_count") or 0)
        coverage = result.get("coverage")
        run.coverage = float(coverage) if coverage is not None else None
        # 明细：失败实体 / 漂移事件（明细为采集排障核心信息；entities 全量过大不落库）
        run.detail_json = {
            "failed_specs": result.get("failed_specs", []),
            "drift_events": result.get("drift_events", []),
            "degrade_reason": result.get("degrade_reason"),
            "dsd_count": int(result.get("dsd_count") or 0),
        }
        await self._db.flush()
        return run

    async def fail_collection_run(self, run_id: int, error: str) -> CollectionRun | None:
        """采集失败收尾：记录错误信息，状态 → FAILED。"""
        run = await self.get_collection_run(run_id)
        if run is None:
            return None
        run.status = "FAILED"
        run.finished_at = datetime.now(UTC)
        run.error = str(error)[:512]
        await self._db.flush()
        return run

    async def find_collection_run_by_job_id(self, job_id: str) -> CollectionRun | None:
        """按 job_id 定位仍在 RUNNING 的采集运行记录（H1 stale 清扫收尾用）。"""
        return (
            await self._db.execute(
                select(CollectionRun).where(
                    CollectionRun.job_id == job_id,
                    CollectionRun.deleted_at.is_(None),
                    CollectionRun.status == "RUNNING",
                )
            )
        ).scalar_one_or_none()

    async def list_collection_runs(
        self,
        *,
        source_id: str | None,
        status: str | None,
        trigger: str | None,
        page: int,
        page_size: int,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
    ) -> tuple[Sequence[CollectionRun], int]:
        """查询采集运行历史（按开始时间倒序，分页；可按 source/status/trigger/时间区间过滤）。"""
        base = select(CollectionRun).where(CollectionRun.deleted_at.is_(None))
        if source_id:
            base = base.where(CollectionRun.source_id == source_id)
        if status:
            base = base.where(CollectionRun.status == status)
        if trigger:
            base = base.where(CollectionRun.trigger == trigger)
        if started_after is not None:
            base = base.where(CollectionRun.started_at >= started_after)
        if started_before is not None:
            base = base.where(CollectionRun.started_at <= started_before)
        count = await self._db.scalar(select(func.count()).select_from(base.subquery()))
        total = int(count) if count is not None else 0
        stmt = (
            base.order_by(CollectionRun.started_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        res = await self._db.execute(stmt)
        return res.scalars().all(), total

    async def summarize_collection_runs(
        self,
        *,
        source_id: str | None,
        status: str | None,
        trigger: str | None,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
    ) -> dict[str, int]:
        """采集运行历史聚合统计（服务端 SQL 聚合，替代前端分页拉全量）。

        按与 ``list_collection_runs`` 相同的过滤条件一次性算出
        total/completed/failed/scanned/registered。前端此前用 ``page_size=200``
        拉全量在客户端聚合，总数 > 200 时成功/失败数只取前 200 条，与 ``total``
        口径矛盾（TD §12.1 统计一致性）。服务端一次 ``GROUP BY`` 等价聚合消除该矛盾。
        """
        base = select(CollectionRun).where(CollectionRun.deleted_at.is_(None))
        if source_id:
            base = base.where(CollectionRun.source_id == source_id)
        if status:
            base = base.where(CollectionRun.status == status)
        if trigger:
            base = base.where(CollectionRun.trigger == trigger)
        if started_after is not None:
            base = base.where(CollectionRun.started_at >= started_after)
        if started_before is not None:
            base = base.where(CollectionRun.started_at <= started_before)
        stmt = select(
            func.count().label("total"),
            func.sum(case((CollectionRun.status == "COMPLETED", 1), else_=0)).label(
                "completed"
            ),
            func.sum(case((CollectionRun.status == "FAILED", 1), else_=0)).label("failed"),
            func.sum(func.coalesce(CollectionRun.scanned, 0)).label("scanned"),
            func.sum(func.coalesce(CollectionRun.registered, 0)).label("registered"),
        ).select_from(base.subquery())
        row = (await self._db.execute(stmt)).one()
        return {
            "total": int(row.total or 0),
            "completed": int(row.completed or 0),
            "failed": int(row.failed or 0),
            "scanned": int(row.scanned or 0),
            "registered": int(row.registered or 0),
        }

    async def purge_collection_runs(self, before: datetime) -> int:
        """P2-13: 物理清理指定时间前的终态采集运行历史（保留策略）。

        仅清理 COMPLETED/FAILED 终态记录——RUNNING（采集中/崩溃未收尾）永不清理，
        保留现场供排查。``before`` 为保留期边界（如 now - 90 天）。

        Args:
            before: 早于该时间且为终态的记录将被删除。

        Returns:
            删除的记录数。
        """
        from sqlalchemy import delete

        result = await self._db.execute(
            delete(CollectionRun).where(
                CollectionRun.status.in_(("COMPLETED", "FAILED")),
                CollectionRun.started_at < before,
            )
        )
        # mypy: Result.rowcount 未在泛型中声明，运行时有效（与 notify 归档同模式）
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

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
        self,
        source_id: str,
        status: str,
        error: str | None = None,
        *,
        health_metrics: dict[str, Any] | None = None,
        degraded_since: datetime | None = None,
    ) -> None:
        """更新数据源健康状态（P1-3：可附带最近错误信息，供健康端点返回）。

        Args:
            health_metrics: 采集后健康指标（success_rate/attempted/failed/p95_ms）。
            degraded_since: 显式降级起始时间；恢复 healthy 时传 None 自动清空。
        """
        src = await self.get_source(source_id)
        if src is not None:
            src.health_status = status
            src.last_health_check = datetime.now(UTC)
            if health_metrics is not None:
                src.health_metrics = health_metrics
            if degraded_since is not None:
                src.degraded_since = degraded_since
            elif status == "healthy":
                # 恢复健康时清空降级起始时间与历史错误
                src.degraded_since = None
            if error is not None:
                src.last_error = error[:512]
            elif status == "healthy":
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

    async def get_description(
        self, catalog_id: int, column_name: str
    ) -> ColumnDescription | None:
        """获取单条字段描述（推断幂等短路用，避免重复调 LLM）。"""
        return (
            await self._db.execute(
                select(ColumnDescription).where(
                    ColumnDescription.catalog_id == catalog_id,
                    ColumnDescription.column_name == column_name,
                    ColumnDescription.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    async def get_descriptions_for_catalogs(
        self, catalog_ids: Sequence[int]
    ) -> dict[int, list[ColumnDescription]]:
        """批量按 catalog_id 查询字段描述（列表接口合并用，避免 N+1）。

        Returns:
            ``{catalog_id: [ColumnDescription, ...]}``；无记录时返回空 dict。
        """
        ids = list(catalog_ids)
        if not ids:
            return {}
        res = await self._db.execute(
            select(ColumnDescription).where(
                ColumnDescription.catalog_id.in_(ids),
                ColumnDescription.deleted_at.is_(None),
            )
        )
        out: dict[int, list[ColumnDescription]] = {}
        for d in res.scalars().all():
            out.setdefault(d.catalog_id, []).append(d)
        return out

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

    async def get_description_coverage(
        self,
        page: int = 1,
        page_size: int | None = None,
        source_id: str | None = None,
        keyword: str | None = None,
        database: str | None = None,
    ) -> dict[str, Any]:
        """描述缺失统计：汇总指标 SQL 端聚合 + per_table 服务端分页。

        优化（P1-8）：此前一次性拉全部 db_catalog（含 schema_json 大字段）+
        全部字段描述内存聚合，表量大时每次进页面全表装载。现改为：
        - 汇总指标（表数/有描述表数/字段数/有描述字段数）全部 SQL 端聚合
          （COUNT / SUM(json_length)），不装载 ORM 大字段；
        - per_table 明细按 page/page_size 服务端分页（page_size=None 返回
          全量，向后兼容旧前端契约）。

        治理筛选（source_id/keyword/database）：汇总与明细统一按筛选口径收窄——
        fields_with_desc 也 join db_catalog 限定范围，保证「按数据源/库/表治理」
        时统计卡与表格口径一致（筛选后统计卡反映该子集的覆盖率）。

        Args:
            page: 页码（≥1）。
            page_size: 每页条数；None 表示全量（向后兼容）。
            source_id: 数据源过滤（精确匹配）。
            keyword: 表名模糊过滤（LIKE 通配符转义，对齐 list_catalogs）。
            database: 库名过滤（entity_name 前缀精确匹配「库.表」，对齐 list_catalogs）。

        Returns:
            汇总 + per_table（分页后）+ per_table_total/page/page_size。
        """
        # 软删源过滤：join data_source.deleted_at IS NULL——软删源目录不再计入治理统计；
        # source_id/keyword 为治理筛选（采集目录治理面板按数据源/表筛选）
        filters = [DBCatalog.deleted_at.is_(None), DataSource.deleted_at.is_(None)]
        if source_id:
            filters.append(DBCatalog.source_id == source_id)
        if keyword:
            # LIKE 通配符转义（对齐 FR-035 / list_catalogs：% / _ 须转义防模糊放大）
            escaped = keyword.replace("/", "//").replace("%", "/%").replace("_", "/_")
            filters.append(DBCatalog.entity_name.ilike(f"%{escaped}%", escape="/"))
        if database:
            # 库名 = entity_name 前缀（库.表）；LIKE 通配符转义防模糊放大（对齐 list_catalogs）
            esc_db = database.replace("/", "//").replace("%", "/%").replace("_", "/_")
            filters.append(DBCatalog.entity_name.ilike(f"{esc_db}.%", escape="/"))

        # —— 汇总指标：SQL 端聚合（不装载 db_catalog 大字段）——
        total_tables = int(
            await self._db.scalar(
                select(func.count(DBCatalog.id))
                .join(DataSource, DataSource.source_id == DBCatalog.source_id)
                .where(*filters)
            )
            or 0
        )
        tables_with_desc = int(
            await self._db.scalar(
                select(func.count(DBCatalog.id))
                .join(DataSource, DataSource.source_id == DBCatalog.source_id)
                .where(
                    *filters,
                    DBCatalog.description.is_not(None),
                    DBCatalog.description != "",
                )
            )
            or 0
        )
        total_fields_row = await self._db.execute(
            select(
                func.coalesce(
                    func.sum(func.json_length(DBCatalog.schema_json["columns"])), 0
                )
            )
            .join(DataSource, DataSource.source_id == DBCatalog.source_id)
            .where(*filters)
        )
        total_fields = int(total_fields_row.scalar() or 0)
        fields_with_desc = int(
            await self._db.scalar(
                select(func.count(ColumnDescription.id))
                .join(DBCatalog, DBCatalog.id == ColumnDescription.catalog_id)
                .join(DataSource, DataSource.source_id == DBCatalog.source_id)
                .where(
                    ColumnDescription.deleted_at.is_(None),
                    ColumnDescription.source.in_(("manual", "llm")),
                    *filters,
                )
            )
            or 0
        )

        # —— per_table 明细：服务端分页 ——
        base = (
            select(DBCatalog)
            .join(DataSource, DataSource.source_id == DBCatalog.source_id)
            .where(*filters)
            .order_by(DBCatalog.id)
        )
        per_table_total = int(
            await self._db.scalar(select(func.count()).select_from(base.subquery()))
            or 0
        )
        if page_size is not None and page_size > 0:
            stmt = base.offset((page - 1) * page_size).limit(page_size)
        else:
            stmt = base
        cats = (await self._db.execute(stmt)).scalars().all()
        cat_ids = [c.id for c in cats]
        # 仅装载当前页表的字段描述（缩小装载量）
        descs: Sequence[ColumnDescription] = []
        if cat_ids:
            descs = (
                await self._db.execute(
                    select(ColumnDescription).where(
                        ColumnDescription.deleted_at.is_(None),
                        ColumnDescription.catalog_id.in_(cat_ids),
                    )
                )
            ).scalars().all()
        srcs = (
            await self._db.execute(
                select(DataSource.source_id, DataSource.domain, DataSource.name).where(
                    DataSource.deleted_at.is_(None)
                )
            )
        ).all()
        users = (
            await self._db.execute(
                select(User.id, User.display_name, User.username)
            )
        ).all()

        domain_map = {row.source_id: row.domain for row in srcs}
        src_name_map = {row.source_id: row.name for row in srcs}
        # 责任人展示名：display_name 优先，缺省回退 username
        owner_map = {row.id: (row.display_name or row.username) for row in users}
        # 仅 manual/llm 记录计入已描述（schema 来源与 comment 等价，避免重复计）
        desc_keys: set[tuple[int, str]] = {
            (d.catalog_id, d.column_name)
            for d in descs
            if d.source in ("manual", "llm")
        }

        per_table: list[dict[str, Any]] = []
        for cat in cats:
            columns = _catalog_columns(cat.schema_json)
            total = len(columns)
            covered = sum(1 for c in columns if _column_has_desc(c, cat.id, desc_keys))
            per_table.append(
                {
                    "catalog_id": cat.id,
                    "entity_name": cat.entity_name,
                    "source_id": cat.source_id,
                    "source_name": src_name_map.get(cat.source_id),
                    "entity_type": cat.entity_type,
                    "domain": domain_map.get(cat.source_id),
                    "sensitivity_level": cat.sensitivity_level,
                    "table_desc": bool(cat.description and cat.description.strip()),
                    "description": cat.description,
                    "description_source": cat.description_source,
                    "owner_name": owner_map.get(cat.owner_id) if cat.owner_id else None,
                    "total_fields": total,
                    "covered_fields": covered,
                    "missing_fields": total - covered,
                    "missing_field_names": [
                        c["name"]
                        for c in columns
                        if not _column_has_desc(c, cat.id, desc_keys)
                    ],
                    "updated_at": (
                        cat.updated_at.isoformat() if cat.updated_at else None
                    ),
                }
            )

        return {
            "total_tables": total_tables,
            "tables_with_desc": tables_with_desc,
            "tables_missing_desc": total_tables - tables_with_desc,
            "total_fields": total_fields,
            "fields_with_desc": fields_with_desc,
            "fields_missing_desc": total_fields - fields_with_desc,
            "per_table": per_table,
            "per_table_total": per_table_total,
            "page": page,
            "page_size": page_size,
        }


    async def get_source_overview(self, source_id: str) -> dict[str, Any]:
        """资产规模概览聚合（详情页头部）。

        一次查询汇总：实体类型分布、PII 敏感级分布、字段总数、漂移数、
        覆盖率、最近采集水位。数据源不存在返回 None 由 service 判定 404。
        """
        src = await self.get_source(source_id)
        if src is None:
            return {}
        base = DBCatalog.deleted_at.is_(None)
        type_rows = await self._db.execute(
            select(DBCatalog.entity_type, func.count(DBCatalog.id))
            .where(DBCatalog.source_id == source_id, base)
            .group_by(DBCatalog.entity_type)
        )
        type_dist: dict[str, int] = {k: int(v) for k, v in type_rows.all()}
        pii_rows = await self._db.execute(
            select(DBCatalog.sensitivity_level, func.count(DBCatalog.id))
            .where(DBCatalog.source_id == source_id, base)
            .group_by(DBCatalog.sensitivity_level)
        )
        pii_dist: dict[str, int] = {k: int(v) for k, v in pii_rows.all()}
        field_row = await self._db.execute(
            select(
                func.coalesce(
                    func.sum(func.json_length(DBCatalog.schema_json["columns"])), 0
                )
            ).where(DBCatalog.source_id == source_id, base)
        )
        drift_row = await self._db.execute(
            select(func.count(SchemaDriftLog.id)).where(
                SchemaDriftLog.source_id == source_id
            )
        )
        watermark = await self.get_watermark(source_id)
        return {
            "source_id": source_id,
            "entity_types": {k: int(v) for k, v in type_dist.items()},
            "by_sensitivity": {k: int(v) for k, v in pii_dist.items()},
            "total_fields": int(field_row.scalar() or 0),
            "drift_count": int(drift_row.scalar() or 0),
            "coverage": float(src.coverage or 0.0),
            "last_collected_at": (
                watermark.last_collected_at.isoformat()
                if watermark and watermark.last_collected_at
                else None
            ),
            "scanned_count": watermark.scanned_count if watermark else 0,
            "failed_count": watermark.failed_count if watermark else 0,
        }

    async def list_sources_signals(
        self, source_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """批量回填列表信号：表/视图数、PII 数、最近采集、漂移数、累计扫描/失败。

        一次 IN 查询聚合避免 N+1；无目录/水位记录的源保持 0/None 默认。
        """
        if not source_ids:
            return {}
        default = {
            "table_count": 0,
            "pii_count": 0,
            "last_collected_at": None,
            "drift_count": 0,
            "scanned_count": 0,
            "failed_count": 0,
        }
        signals = {sid: dict(default) for sid in source_ids}
        rows = await self._db.execute(
            select(
                DBCatalog.source_id,
                func.count(DBCatalog.id),
                func.sum(
                    case((DBCatalog.sensitivity_level == "PII", 1), else_=0)
                ),
            )
            .where(DBCatalog.source_id.in_(source_ids), DBCatalog.deleted_at.is_(None))
            .group_by(DBCatalog.source_id)
        )
        for source_id, table_count, pii_count in rows.all():
            signals[source_id]["table_count"] = int(table_count or 0)
            signals[source_id]["pii_count"] = int(pii_count or 0)
        wm_rows = await self._db.execute(
            select(
                CollectionWatermark.source_id,
                CollectionWatermark.last_collected_at,
                CollectionWatermark.scanned_count,
                CollectionWatermark.failed_count,
            ).where(CollectionWatermark.source_id.in_(source_ids))
        )
        for source_id, last, scanned, failed in wm_rows.all():
            signals[source_id]["last_collected_at"] = last
            signals[source_id]["scanned_count"] = int(scanned or 0)
            signals[source_id]["failed_count"] = int(failed or 0)
        drift_rows = await self._db.execute(
            select(SchemaDriftLog.source_id, func.count(SchemaDriftLog.id))
            .where(SchemaDriftLog.source_id.in_(source_ids))
            .group_by(SchemaDriftLog.source_id)
        )
        for source_id, count in drift_rows.all():
            signals[source_id]["drift_count"] = int(count)
        return signals
