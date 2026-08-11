"""指标服务层（业务逻辑）。

对齐 DEV_GUIDE §8b.2（Service 层：编排 repository + 调用其他 service）。
包含指标 CRUD、状态机流转、版本管理。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BusinessError,
    ConflictError,
    NotFoundError,
)
from app.db.redis import redis_client
from app.models.metric import Metric, MetricVersion
from app.services.semantic.cache import MetricCache
from app.services.semantic.repository import MetricRepository
from app.services.semantic.schemas import (
    MetricCreateRequest,
    MetricListParams,
    MetricPublishRequest,
    MetricResponse,
    MetricUpdateRequest,
)

logger = structlog.get_logger("unisense.semantic.service")

# 口径层破坏性变更字段：这些字段变更会破坏下游消费方
# 同时用于 _is_breaking_change 与 _compute_diff，保证判定一致（修复原实现中二者对
# dependencies 的判定互相矛盾的问题）。
BREAKING_DEF_FIELDS = ("expression", "aggregation", "granularity", "dependencies")


class MetricService:
    """指标服务。

    封装指标的业务逻辑：CRUD、状态流转、版本管理。
    """

    def __init__(self, db: AsyncSession, cache: MetricCache | None = None) -> None:
        """初始化服务。

        Args:
            db: 异步数据库会话。
            cache: 指标读缓存；缺省使用默认 Redis 客户端（不可用时自动降级 DB）。
        """
        self._db = db
        self._repo = MetricRepository(db)
        self._cache = cache if cache is not None else MetricCache.from_defaults(redis_client)

    async def create_metric(self, request: MetricCreateRequest, owner_id: int) -> Metric:
        """创建指标（初始状态 DRAFT）。

        Args:
            request: 创建请求。
            owner_id: 创建人（Owner）ID。

        Returns:
            创建的指标。

        Raises:
            ConflictError: 指标编码已存在。
        """
        # 检查编码唯一性
        existing = await self._repo.get_by_code(request.metric_code)
        if existing is not None:
            raise ConflictError(
                f"指标编码已存在: {request.metric_code}",
                ctx={"code": "CONFLICT", "metric_code": request.metric_code},
            )

        metric = Metric(
            metric_code=request.metric_code,
            name=request.name,
            domain=request.domain,
            type=request.type,
            granularity=request.granularity,
            unit=request.unit,
            currency=request.currency,
            aggregation=request.aggregation,
            time_semantics=request.time_semantics,
            freshness=request.freshness,
            sla=request.sla,
            dw_layer=request.dw_layer,
            metric_tier=request.metric_tier,
            serving_mode=request.serving_mode,
            additivity=request.additivity,
            non_additive_dimensions=request.non_additive_dimensions,
            definition_json=request.definition_json,
            version=1,
            row_version=1,
            status="DRAFT",
            owner_id=owner_id,
            pii_flag=request.pii_flag,
            compliance_reviewed=False,
        )

        metric = await self._repo.create(metric)

        # 创建初始版本（状态为 DRAFT，待发布时转正为 PUBLISHED）
        version = MetricVersion(
            metric_id=metric.id,
            version=1,
            change_type="CREATE",
            definition_json=request.definition_json,
            diff_json=None,
            status="DRAFT",
            change_reason="初始创建",
            created_by=owner_id,
            published_at=None,
        )
        await self._repo.create_version(version)

        logger.info(
            "metric_created",
            metric_code=metric.metric_code,
            domain=metric.domain,
            actor_id=owner_id,
        )
        return metric

    async def get_metric(self, metric_code: str) -> Metric:
        """获取指标详情。

        Args:
            metric_code: 指标编码。

        Returns:
            指标对象。

        Raises:
            NotFoundError: 指标不存在。
        """
        metric = await self._repo.get_by_code(metric_code)
        if metric is None:
            raise NotFoundError(f"指标不存在: {metric_code}")
        return metric

    async def get_metric_public(self, metric_code: str) -> MetricResponse:
        """经缓存获取指标详情（API 读路径，含 cache-aside + 熔断降级）。

        Redis 命中直接返回；未命中/降级时回源 MySQL 并回写缓存。
        该方法用于对外读接口，与内部 `get_metric`（始终走 DB，供状态流转使用）
        分离，避免缓存与状态机耦合。

        Args:
            metric_code: 指标编码。

        Returns:
            指标详情响应。

        Raises:
            NotFoundError: 指标不存在。
        """
        cached = await self._cache.get(metric_code)
        if cached is not None:
            return MetricResponse.model_validate(cached)
        metric = await self._repo.get_by_code(metric_code)
        if metric is None:
            raise NotFoundError(f"指标不存在: {metric_code}")
        await self._cache.set(metric)
        return MetricResponse.model_validate(metric)

    async def list_metrics(self, params: MetricListParams) -> tuple[list[Metric], int]:
        """分页查询指标列表。

        Args:
            params: 查询参数。

        Returns:
            (指标列表, 总数)。
        """
        offset = (params.page - 1) * params.page_size
        return await self._repo.list_metrics(
            domain=params.domain,
            status=params.status,
            metric_tier=params.metric_tier,
            keyword=params.keyword,
            offset=offset,
            limit=params.page_size,
        )

    async def update_metric(
        self, metric_code: str, request: MetricUpdateRequest, actor_id: int
    ) -> Metric:
        """更新指标（乐观锁）。

        仅允许在 DRAFT / REVIEW / PUBLISHED 状态下更新。
        更新会创建新版本（如口径变更）。

        Args:
            metric_code: 指标编码。
            request: 更新请求。
            actor_id: 操作人 ID。

        Returns:
            更新后的指标。

        Raises:
            NotFoundError: 指标不存在。
            BusinessError: 状态不允许更新。
            ConflictError: 乐观锁冲突。
        """
        metric = await self.get_metric(metric_code)

        if metric.status not in ("DRAFT", "REVIEW", "PUBLISHED"):
            raise BusinessError(
                f"指标状态 {metric.status} 不允许更新",
                error_code="VALIDATION_ERROR",
            )

        # 收集更新字段
        updates: dict[str, Any] = {}
        for field in ("name", "granularity", "unit", "sla", "consumption_guide", "backup_owner_id"):
            val = getattr(request, field, None)
            if val is not None:
                updates[field] = val

        # 口径变更 → 新版本
        if request.definition_json is not None:
            old_def = metric.definition_json
            new_def = request.definition_json
            is_breaking = self._is_breaking_change(old_def, new_def)

            new_version_num = metric.version + 1
            updates["definition_json"] = new_def
            updates["version"] = new_version_num

            # 创建版本记录
            version = MetricVersion(
                metric_id=metric.id,
                version=new_version_num,
                change_type="BREAKING" if is_breaking else "UPDATE",
                definition_json=new_def,
                diff_json=self._compute_diff(old_def, new_def),
                status="DRAFT",
                change_reason=request.change_reason,
                created_by=actor_id,
            )
            await self._repo.create_version(version)

        updates["change_reason"] = request.change_reason

        updated = await self._repo.update_with_optimistic_lock(
            metric.id, metric.row_version, **updates
        )

        await self._cache.invalidate(metric_code)

        logger.info(
            "metric_updated",
            metric_code=metric_code,
            actor_id=actor_id,
            fields=list(updates.keys()),
        )
        return updated

    async def publish_metric(
        self, metric_code: str, request: MetricPublishRequest, actor_id: int
    ) -> Metric:
        """发布指标（DRAFT/REVIEW → PUBLISHED）。

        Args:
            metric_code: 指标编码。
            request: 发布请求。
            actor_id: 操作人 ID。

        Returns:
            发布后的指标。

        Raises:
            NotFoundError: 指标不存在。
            BusinessError: 状态不允许发布。
        """
        metric = await self.get_metric(metric_code)

        if metric.status not in ("DRAFT", "REVIEW"):
            raise BusinessError(
                f"指标状态 {metric.status} 不允许发布，仅 DRAFT/REVIEW 可发布",
                error_code="VALIDATION_ERROR",
            )

        # PII 指标须先过合规审核
        if metric.pii_flag and not metric.compliance_reviewed:
            raise BusinessError(
                "PII 指标须先通过合规审核",
                error_code="COMPLIANCE_BLOCKED",
            )

        # 定位待发布版本（缺省为当前版本），校验版本存在
        target_version = request.version or metric.version
        version = await self._repo.get_version(metric.id, target_version)
        if version is None:
            raise NotFoundError(f"版本不存在: {target_version}")

        updated = await self._repo.update_with_optimistic_lock(
            metric.id,
            metric.row_version,
            status="PUBLISHED",
            approver_id=actor_id,
            effective_version=target_version,
        )

        # 版本转正：将指定版本标记为 PUBLISHED 并记录发布时间
        await self._repo.mark_version_published(metric.id, target_version, datetime.now(UTC))

        await self._cache.invalidate(metric_code)

        logger.info(
            "metric_published",
            metric_code=metric_code,
            version=target_version,
            actor_id=actor_id,
        )
        return updated

    async def deprecate_metric(
        self, metric_code: str, successor_code: str | None, actor_id: int
    ) -> Metric:
        """废弃指标（→ DEPRECATED）。

        Args:
            metric_code: 指标编码。
            successor_code: 替代指标编码。
            actor_id: 操作人 ID。

        Returns:
            废弃后的指标。
        """
        metric = await self.get_metric(metric_code)

        if metric.status == "DEPRECATED":
            raise BusinessError("指标已废弃", error_code="VALIDATION_ERROR")

        from datetime import timedelta

        sunset_days = 30  # 对齐 TD §13 metric_version.sunset_days
        now = datetime.now(UTC)

        updated = await self._repo.update_with_optimistic_lock(
            metric.id,
            metric.row_version,
            status="DEPRECATED",
            successor_code=successor_code,
            deprecated_at=now,
            sunset_until=(now + timedelta(days=sunset_days)).date(),
        )

        await self._cache.invalidate(metric_code)

        logger.info(
            "metric_deprecated",
            metric_code=metric_code,
            successor=successor_code,
            actor_id=actor_id,
        )
        return updated

    async def review_compliance(self, metric_code: str, actor_id: int) -> Metric:
        """PII 合规复核（置 compliance_reviewed=True，打通 PII 指标发布闸门）。

        Args:
            metric_code: 指标编码。
            actor_id: 操作人 ID。

        Returns:
            复核后的指标。

        Raises:
            NotFoundError: 指标不存在。
            ConflictError: 乐观锁冲突。
        """
        metric = await self.get_metric(metric_code)
        if metric.owner_id == actor_id:
            raise BusinessError(
                "合规复核禁止指标 Owner 自审",
                error_code="SELF_REVIEW_BLOCKED",
            )
        updated = await self._repo.update_with_optimistic_lock(
            metric.id, metric.row_version, compliance_reviewed=True
        )
        await self._cache.invalidate(metric_code)
        logger.info(
            "metric_compliance_reviewed",
            metric_code=metric_code,
            actor_id=actor_id,
        )
        return updated

    async def get_versions(self, metric_code: str) -> list[MetricVersion]:
        """获取指标的所有版本。

        Args:
            metric_code: 指标编码。

        Returns:
            版本列表。
        """
        metric = await self.get_metric(metric_code)
        return await self._repo.list_versions(metric.id)

    # ---- 内部方法 ----

    @staticmethod
    def _is_breaking_change(old_def: dict[str, Any], new_def: dict[str, Any]) -> bool:
        """判断口径变更是否为破坏性变更。

        Args:
            old_def: 旧口径。
            new_def: 新口径。

        Returns:
            是否为破坏性变更。
        """
        # 类型/聚合/粒度/依赖变更 = 破坏性
        return any(old_def.get(field) != new_def.get(field) for field in BREAKING_DEF_FIELDS)

    @staticmethod
    def _compute_diff(old_def: dict[str, Any], new_def: dict[str, Any]) -> dict[str, Any]:
        """计算口径变更的结构化 diff。

        Args:
            old_def: 旧口径。
            new_def: 新口径。

        Returns:
            结构化 diff: {field: {before, after, change_type}}。
        """
        diff: dict[str, Any] = {}
        all_keys = set(old_def.keys()) | set(new_def.keys())
        for key in all_keys:
            old_val = old_def.get(key)
            new_val = new_def.get(key)
            if old_val != new_val:
                diff[key] = {
                    "before": old_val,
                    "after": new_val,
                    "change_type": ("BREAKING" if key in BREAKING_DEF_FIELDS else "UPDATE"),
                }
        return diff
