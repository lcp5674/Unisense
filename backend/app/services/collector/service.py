"""采集领域服务（对齐 TD §12.1 / DEV_GUIDE §8b.1）。

职责：数据源注册/查询/删除、元数据注册（含敏感分级与幂等）、批量废弃（207）、
自动采集编排。所有写操作经 SecretManager 加密凭据、经审计落库、经事件发布（熔断）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.secrets import SecretManager
from app.db.redis import get_redis
from app.models.data_source import DataSource
from app.services.collector.classifier import SensitivityClassifier
from app.services.collector.events import CatalogEventPublisher
from app.services.collector.queue import CollectionQueue, create_collection_queue
from app.services.collector.repository import CollectorRepository
from app.services.collector.schemas import (
    BulkDeprecateRequest,
    BulkDeprecateResult,
    DataSourceCreateRequest,
    DataSourceResponse,
    DBCatalogCreateRequest,
    DBCatalogListParams,
    DBCatalogListResponse,
    DBCatalogResponse,
)
from app.services.collector.spi import BaseCollector

logger = get_logger("unisense.collector.service")


def _redis_available() -> bool:
    """检查 Redis 连接池是否已初始化。"""
    try:
        get_redis()
        return True
    except RuntimeError:
        return False


class CollectorService(BaseService):
    """采集领域服务。"""

    def __init__(
        self,
        db: AsyncSession,
        *,
        secrets: type[SecretManager] | SecretManager | None = None,
        events: CatalogEventPublisher | None = None,
        classifier: SensitivityClassifier | None = None,
    ) -> None:
        super().__init__(db)
        self._db = db
        self._repo = CollectorRepository(db)
        # secrets 作为工具类使用（静态方法），缺省用 SecretManager
        self._secrets = secrets or SecretManager
        self._events = events or CatalogEventPublisher(
            get_redis() if _redis_available() else None
        )
        self._classifier = classifier or SensitivityClassifier()

    @staticmethod
    def _to_source_response(src: DataSource) -> DataSourceResponse:
        return DataSourceResponse(
            source_id=src.source_id,
            name=src.name,
            source_type=src.source_type,
            domain=src.domain,
            cluster_id=src.cluster_id,
            coverage=float(src.coverage or 0.0),
            health_status=src.health_status or "UNKNOWN",
            connection_config_present=bool(src.connection_config),
            created_by=src.created_by,
            created_at=src.created_at,
            updated_at=src.updated_at,
        )

    async def create_source(
        self, req: DataSourceCreateRequest, actor_id: int
    ) -> DataSourceResponse:
        if await self._repo.get_source(req.source_id) is not None:
            raise ConflictError(f"数据源已存在: {req.source_id}")
        encrypted = self._secrets.encrypt(req.connection_config)
        src = DataSource(
            source_id=req.source_id,
            name=req.name,
            source_type=req.source_type,
            connection_config=encrypted,
            domain=req.domain,
            cluster_id=req.cluster_id,
            coverage=0.0,
            health_status="UNKNOWN",
            quota={},
            created_by=actor_id,
        )
        await self._repo.create_source(src)
        return self._to_source_response(src)

    async def list_sources(
        self,
        *,
        domain: str | None = None,
        source_type: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[DataSourceResponse], int]:
        sources, total = await self._repo.list_sources(
            domain=domain,
            source_type=source_type,
            keyword=keyword,
            page=page,
            page_size=page_size,
        )
        return [self._to_source_response(s) for s in sources], total

    async def get_source(self, source_id: str) -> DataSourceResponse:
        src = await self._repo.get_source(source_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        return self._to_source_response(src)

    async def delete_source(self, source_id: str) -> None:
        if not await self._repo.soft_delete_source(source_id):
            raise NotFoundError(f"数据源不存在: {source_id}")

    async def get_source_orm(self, source_id: str) -> DataSource:
        """取原始 DataSource（供采集编排还原连接配置，不对外暴露明文）。"""
        src = await self._repo.get_source(source_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        return src

    async def register_catalog(
        self, req: DBCatalogCreateRequest, actor_id: int
    ) -> DBCatalogResponse:
        sensitivity = self._classifier.classify(req.entity_name, req.schema_def)

        # P2 增强：使用结构化 LLM 输出进行置信度分流
        # 当 LLM 可用且 confidence < 0.7 时，标记为 "needs_review"
        llm_sensitivity = await self._llm_classify_sensitivity(req.entity_name, req.schema_def)
        if llm_sensitivity is not None and llm_sensitivity.get("confidence", 1.0) < 0.7:
                sensitivity = "needs_review"
                logger.info(
                    "catalog_llm_low_confidence",
                    entity_name=req.entity_name,
                    confidence=llm_sensitivity.get("confidence"),
                    original_sensitivity=self._classifier.classify(req.entity_name, req.schema_def),
                )

        cat, _created = await self._repo.upsert_catalog(
            source_id=req.source_id,
            entity_name=req.entity_name,
            entity_type=req.entity_type,
            schema_json=req.schema_def,
            etl_sql=req.etl_sql,
            sensitivity_level=sensitivity,
            owner_id=req.owner_id,
        )
        await self._repo.recompute_coverage(req.source_id)
        await self._events.publish(
            "catalog_registered",
            {
                "source_id": req.source_id,
                "entity_name": req.entity_name,
                "sensitivity": sensitivity,
            },
        )
        return DBCatalogResponse.model_validate(cat)

    async def _llm_classify_sensitivity(
        self, entity_name: str, schema_def: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """使用 LLM 辅助分类敏感级别，返回结构化结果。

        LLM 不可用时返回 None（不阻断主流程）。
        """
        try:
            from app.services.llm.client import build_llm_client

            client = build_llm_client()
            if not client.enabled:
                return None

            # 构建分类 prompt
            schema_str = str(schema_def)[:2000] if schema_def else "无 schema 信息"
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是数据敏感分类专家。根据表名和 schema 判断敏感级别。\n"
                        "返回 JSON 格式：{\n"
                        '  "content": "PII|CONFIDENTIAL|INTERNAL|PUBLIC",\n'
                        '  "confidence": 0.0-1.0,\n'
                        '  "reasoning": "判断依据",\n'
                        '  "candidates": [{"level": "...", "score": 0.0}]\n'
                        "}\n"
                        "confidence < 0.7 表示不确定，需要人工审核。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"表名: {entity_name}\nSchema: {schema_str}",
                },
            ]

            result = await client.chat(
                messages,
                temperature=0.0,
                max_tokens=500,
                response_format={"type": "json_object"},
            )
            await client.close()
            return result
        except Exception as exc:  # noqa: BLE001 - LLM 失败不阻断主流程
            logger.warning("llm_classify_failed: %s", exc)
            return None

    async def list_catalogs(self, params: DBCatalogListParams) -> DBCatalogListResponse:
        cats, total = await self._repo.list_catalogs(params)
        return DBCatalogListResponse(
            items=[DBCatalogResponse.model_validate(c) for c in cats],
            total=total,
            page=params.page,
            page_size=params.page_size,
        )

    async def bulk_deprecate(self, req: BulkDeprecateRequest, actor_id: int) -> BulkDeprecateResult:
        succeeded, failed = await self._repo.bulk_deprecate(req.items)
        for it in succeeded:
            await self._events.publish(
                "catalog_deprecated",
                {"source_id": it.source_id, "entity_name": it.entity_name},
            )
        return BulkDeprecateResult(succeeded=succeeded, failed=failed)

    async def collect_and_register(
        self, source_id: str, collector: BaseCollector, actor_id: int
    ) -> dict[str, Any]:
        src = await self._repo.get_source(source_id)
        if src is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        specs = await collector.collect(src)
        registered = 0
        pii_registered = 0
        for spec in specs:
            sensitivity = self._classifier.classify(spec.entity_name, spec.schema_json)
            if sensitivity == "PII":
                pii_registered += 1
            await self._repo.upsert_catalog(
                source_id=source_id,
                entity_name=spec.entity_name,
                entity_type=spec.entity_type,
                schema_json=spec.schema_json,
                etl_sql=spec.etl_sql,
                sensitivity_level=sensitivity,
                owner_id=None,
            )
            registered += 1
            await self._events.publish(
                "catalog_registered",
                {
                    "source_id": source_id,
                    "entity_name": spec.entity_name,
                    "sensitivity": sensitivity,
                },
            )
        coverage = await self._repo.recompute_coverage(source_id)
        return {
            "source_id": source_id,
            "scanned": len(specs),
            "registered": registered,
            "pii_registered": pii_registered,
            "coverage": coverage,
        }

    async def schedule_collection(
        self, source_id: str, actor_id: int, queue: CollectionQueue | None = None
    ) -> str:
        """将全量采集任务投递到异步队列，立即返回 job_id（请求内不再同步执行）。

        当 ``queue`` 未提供时，根据配置自动选择：
        - ``settings.redis_url`` 非空 → ArqCollectionQueue（Redis 持久化）
        - ``settings.redis_url`` 为空 → InMemoryCollectionQueue（降级）

        Raises:
            NotFoundError: 数据源不存在。
        """
        if await self._repo.get_source(source_id) is None:
            raise NotFoundError(f"数据源不存在: {source_id}")
        from app.core.config import settings as _settings

        q = queue or create_collection_queue(redis_url=_settings.redis_url)
        return await q.enqueue(source_id, actor_id)

    async def get_job_status(
        self, job_id: str, queue: CollectionQueue | None = None
    ) -> dict[str, Any] | None:
        """查询采集任务状态（队列自带状态存储时直接读取）。"""
        from app.core.config import settings as _settings

        q = queue or create_collection_queue(redis_url=_settings.redis_url)
        getter = getattr(q, "get", None)
        if getter is None:
            return None
        result: dict[str, Any] | None = await getter(job_id)
        return result
