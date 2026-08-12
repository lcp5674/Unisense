"""指标服务层（业务逻辑）。

对齐 DEV_GUIDE §8b.2（Service 层：编排 repository + 调用其他 service）。
包含指标 CRUD、状态机流转、版本管理。

P3: 继承 BaseService Protocol，统一 db+eventbus+settings 注入模式。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.exceptions import (
    AuthError,
    BusinessError,
    ConflictError,
    NotFoundError,
)
from app.db.redis import get_redis
from app.models.metric import Metric, MetricVersion
from app.services.semantic.cache import MetricCache
from app.services.semantic.repository import MetricRepository
from app.services.semantic.schemas import (
    MetricCreateRequest,
    MetricListParams,
    MetricPublishRequest,
    MetricResponse,
    MetricSubmitRequest,
    MetricUpdateRequest,
)
from app.services.semantic.state_machine import MetricStateMachine

logger = structlog.get_logger("unisense.semantic.service")


def _redis_available() -> bool:
    """检查 Redis 连接池是否已初始化。"""
    try:
        get_redis()
        return True
    except RuntimeError:
        return False

# 口径层破坏性变更字段：这些字段变更会破坏下游消费方
# 同时用于 _is_breaking_change 与 _compute_diff，保证判定一致（修复原实现中二者对
# dependencies 的判定互相矛盾的问题）。
BREAKING_DEF_FIELDS = ("expression", "aggregation", "granularity", "dependencies")


def redact_definition(defn: dict[str, Any]) -> dict[str, Any]:
    """递归脱敏口径定义：保留键结构，所有叶子值替换为 ``"***"``。

    用于 PII 指标读路径分级（非敏感角色只能看到口径骨架，看不到具体取值）。
    """
    def _redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: _redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_redact(item) for item in value]
        return "***"

    return cast(dict[str, Any], _redact(defn))


def _normalize_pii(definition: dict[str, Any], pii_flag: bool) -> tuple[dict[str, Any], bool]:
    """归一化 PII 双源：``metric.pii_flag`` 与 ``definition_json.pii`` 保持一致。

    ``pii_flag`` 为权威源：definition 显式声明 pii 时以它为准回写 pii_flag；
    反之若 pii_flag=True 而 definition 未声明，则回填 ``pii`` 键（消费侧
    ``MetricService.is_pii`` 读取 definition_json.pii，需保证双源一致）。
    """
    def_pii = definition.get("pii")
    if def_pii is not None:
        pii_flag = bool(def_pii)
    definition = dict(definition)
    if pii_flag:
        definition["pii"] = True
    else:
        definition.pop("pii", None)
    return definition, pii_flag


class MetricService(BaseService):
    """指标服务。

    封装指标的业务逻辑：CRUD、状态流转、版本管理。
    继承 BaseService 获得统一的 _write_audit / _publish_event 辅助方法。
    """

    def __init__(self, db: AsyncSession, cache: MetricCache | None = None) -> None:
        """初始化服务。

        Args:
            db: 异步数据库会话。
            cache: 指标读缓存；缺省使用默认 Redis 客户端（不可用时自动降级 DB）。
        """
        super().__init__(db)
        self._repo = MetricRepository(db)
        self._cache = cache if cache is not None else MetricCache.from_defaults(
            get_redis() if _redis_available() else None
        )

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

        # PII 双源归一化：definition_json.pii 与 pii_flag 保持一致（pii_flag 为权威源）
        definition, pii_flag = _normalize_pii(request.definition_json, request.pii_flag)

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
            definition_json=definition,
            version=1,
            row_version=1,
            status="DRAFT",
            owner_id=owner_id,
            pii_flag=pii_flag,
            compliance_reviewed=False,
        )

        metric = await self._repo.create(metric)

        # 创建初始版本（状态为 DRAFT，待发布时转正为 PUBLISHED）
        version = MetricVersion(
            metric_id=metric.id,
            version=1,
            change_type="CREATE",
            definition_json=definition,
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

        # 发布 metric.created 事件（对齐 FR-016）
        await self._publish_event(
            "metric.created",
            {
                "metric_code": metric.metric_code,
                "domain": metric.domain,
                "type": metric.type,
                "owner_id": owner_id,
                "version": 1,
            },
            actor_id=str(owner_id),
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
        self,
        metric_code: str,
        request: MetricUpdateRequest,
        actor_id: int,
        role: str,
    ) -> Metric:
        """更新指标（乐观锁）。

        仅允许在 DRAFT / REVIEW / PUBLISHED 状态下更新。
        更新会创建新版本（如口径变更）。

        Args:
            metric_code: 指标编码。
            request: 更新请求。
            actor_id: 操作人 ID。
            role: 操作人角色。

        Returns:
            更新后的指标。

        Raises:
            NotFoundError: 指标不存在。
            AuthError: metric_owner 操作他人指标（越权）。
            BusinessError: 状态不允许更新。
            ConflictError: 乐观锁冲突。
        """
        metric = await self.get_metric(metric_code)
        self._assert_owner_or_admin(metric, actor_id, role)

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
            # PII 双源归一化：definition.pii 与 pii_flag 保持一致（pii_flag 为权威源）
            new_def, synced_pii = _normalize_pii(request.definition_json, metric.pii_flag)
            is_breaking = self._is_breaking_change(old_def, new_def)

            new_version_num = metric.version + 1
            updates["definition_json"] = new_def
            updates["version"] = new_version_num
            if synced_pii != metric.pii_flag:
                updates["pii_flag"] = synced_pii

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

        # 注意：change_reason 仅写入 MetricVersion 快照（上方），metric 主表无该列，
        # 不能写入 updates，否则 update(Metric).values(change_reason=...) 抛 CompileError。

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
        self,
        metric_code: str,
        request: MetricPublishRequest,
        actor_id: int,
        role: str,
    ) -> Metric:
        """发布指标（内部兼容，已废弃）。

        .. deprecated::
            使用 ``submit_metric`` + ``approve_metric`` 替代。
            本方法保留为内部兼容，标记 deprecated。
            DRAFT→REVIEW→PUBLISHED 不再跳步，须先 submit 再 approve。

        原有行为：DRAFT/REVIEW → PUBLISHED。
        新行为：路由到 approve_metric(mode="standard")。

        Args:
            metric_code: 指标编码。
            request: 发布请求。
            actor_id: 操作人 ID。
            role: 操作人角色。

        Returns:
            发布后的指标。
        """
        # 路由到 approve_metric（内部兼容）
        approve_req = MetricApproveRequest(
            mode="standard",
            target_version=request.version,
        )
        return await self.approve_metric(metric_code, approve_req, actor_id)

    async def submit_review(
        self, metric_code: str, actor_id: int, role: str
    ) -> Metric:
        """提交评审（DRAFT → REVIEW）。

        Args:
            metric_code: 指标编码。
            actor_id: 操作人 ID。
            role: 操作人角色。

        Returns:
            评审中的指标。

        Raises:
            NotFoundError: 指标不存在。
            AuthError: metric_owner 操作他人指标（越权）。
            ConflictError: 非法状态跃迁。
        """
        metric = await self.get_metric(metric_code)
        self._assert_owner_or_admin(metric, actor_id, role)

        invalid = MetricStateMachine.validate_transition(metric.status, "REVIEW")
        if invalid is not None:
            raise ConflictError(invalid, error_code="INVALID_TRANSITION")

        updated = await self._repo.update_with_optimistic_lock(
            metric.id, metric.row_version, status="REVIEW"
        )
        await self._cache.invalidate(metric_code)

        logger.info("metric_submit_review", metric_code=metric_code, actor_id=actor_id)
        return updated

    async def submit_metric(
        self, metric_code: str, request: MetricSubmitRequest, actor_id: int
    ) -> Metric:
        """提交指标审核（DRAFT → REVIEW，对齐 FR-003）。

        Args:
            metric_code: 指标编码。
            request: 提交请求。
            actor_id: 操作人 ID。

        Returns:
            提交后的指标。

        Raises:
            NotFoundError: 指标不存在。
            ConflictError: 非法状态跃迁。
        """
        metric = await self.get_metric(metric_code)

        # 状态机校验：DRAFT→REVIEW
        invalid = MetricStateMachine.validate_transition(metric.status, "REVIEW")
        if invalid is not None:
            raise ConflictError(invalid, error_code="INVALID_TRANSITION")

        updated = await self._repo.update_with_optimistic_lock(
            metric.id, metric.row_version, status="REVIEW"
        )
        await self._cache.invalidate(metric_code)

        # 发布 metric.submitted 事件（对齐 FR-003：通知 domain_admin 待审）
        await self._publish_event(
            "metric.submitted",
            {
                "metric_code": metric_code,
                "domain": metric.domain,
                "submitter_id": actor_id,
                "change_reason": request.change_reason,
            },
            actor_id=str(actor_id),
        )

        logger.info(
            "metric_submitted",
            metric_code=metric_code,
            actor_id=actor_id,
        )
        return updated

    async def review_metric(
        self,
        metric_code: str,
        *,
        approved: bool,
        actor_id: int,
        role: str,
        change_reason: str,
    ) -> Metric:
        """评审指标（approve → PUBLISHED / reject → DRAFT）。

        Args:
            metric_code: 指标编码。
            approved: 是否通过评审。
            actor_id: 操作人 ID。
            role: 操作人角色。
            change_reason: 评审意见。

        Returns:
            评审后的指标。

        Raises:
            NotFoundError: 指标不存在。
            AuthError: metric_owner 操作他人指标（越权）。
            BusinessError: Owner 自审 / 指标不在评审中。
            ConflictError: 非法状态跃迁。
        """
        metric = await self.get_metric(metric_code)
        self._assert_owner_or_admin(metric, actor_id, role)
        # 评审者不能是 Owner（对齐 review_compliance 的 SELF_REVIEW_BLOCKED 逻辑）
        if metric.owner_id == actor_id:
            raise BusinessError(
                "评审禁止指标 Owner 自审",
                error_code="SELF_REVIEW_BLOCKED",
            )
        if metric.status != "REVIEW":
            raise BusinessError(
                f"指标状态 {metric.status} 不在评审中",
                error_code="VALIDATION_ERROR",
            )

        if approved:
            # 通过评审 = 发布（复用发布逻辑，含 PII 合规闸门）
            updated = await self._publish(metric, metric.version, actor_id)
            logger.info(
                "metric_review_approved",
                metric_code=metric_code,
                actor_id=actor_id,
                change_reason=change_reason,
            )
            return updated

        invalid = MetricStateMachine.validate_transition("REVIEW", "DRAFT")
        if invalid is not None:
            raise ConflictError(invalid, error_code="INVALID_TRANSITION")

        updated = await self._repo.update_with_optimistic_lock(
            metric.id, metric.row_version, status="DRAFT"
        )
        await self._cache.invalidate(metric_code)

        logger.info(
            "metric_review_rejected",
            metric_code=metric_code,
            actor_id=actor_id,
            change_reason=change_reason,
        )
        return updated

    async def approve_metric(
        self, metric_code: str, request: MetricApproveRequest, actor_id: int
    ) -> Metric:
        """审核通过指标（REVIEW → PUBLISHED/EXPERIMENTAL，对齐 FR-004）。

        含 PII 门禁 + 依赖校验 + 状态机校验。
        metric.status 更新与 version.status 转正在同一事务中原子执行（对齐 FR-042）。

        Args:
            metric_code: 指标编码。
            request: 审核请求（含 mode/gray_tenant_ids/target_version）。
            actor_id: 操作人 ID。

        Returns:
            审核通过后的指标。

        Raises:
            NotFoundError: 指标不存在。
            ConflictError: 非法状态跃迁。
            BusinessError: PII 未过合规审核 / 依赖校验失败 / 环检测失败。
        """
        metric = await self.get_metric(metric_code)

        # 确定目标状态
        target_status = "PUBLISHED"
        version_status = "PUBLISHED"
        extra_updates: dict[str, Any] = {}

        if request.mode == "experimental":
            target_status = "EXPERIMENTAL"
            version_status = "EXPERIMENTAL"
            if request.gray_tenant_ids:
                extra_updates["gray_tenant_ids"] = request.gray_tenant_ids

        # 状态机校验
        invalid = MetricStateMachine.validate_transition(metric.status, target_status)
        if invalid is not None:
            raise ConflictError(invalid, error_code="INVALID_TRANSITION")

        # PII 指标须先过合规审核（不可跳过）
        if metric.pii_flag and not metric.compliance_reviewed:
            raise BusinessError(
                "PII 指标须先通过合规审核",
                error_code="COMPLIANCE_BLOCKED",
            )

        # 依赖校验（对齐 FR-010/FR-011，派生/复合指标须校验依赖）
        if metric.type in ("derived", "composite"):
            from app.services.semantic.dependency_checker import DependencyChecker

            checker = DependencyChecker(self._db)
            unpublished = await checker.check_dependencies_published(metric.definition_json)
            if unpublished:
                raise BusinessError(
                    f"依赖指标未发布或已废弃: {', '.join(unpublished)}",
                    error_code="DEPENDENCY_NOT_PUBLISHED",
                    ctx={"unpublished_dependencies": unpublished},
                )
            cycle = await checker.detect_cycle(metric_code, metric.definition_json)
            if cycle:
                raise BusinessError(
                    f"检测到循环依赖: {'→'.join(cycle)}",
                    error_code="CYCLIC_DEPENDENCY",
                    ctx={"cycle_path": cycle},
                )

        # 定位待发布版本
        target_version = request.target_version or metric.version
        version_obj = await self._repo.get_version(metric.id, target_version)
        if version_obj is None:
            raise NotFoundError(f"版本不存在: {target_version}")

        # 同一事务中原子更新 metric.status + version.status（对齐 FR-042）
        now = datetime.now(UTC)
        updated = await self._repo.update_with_optimistic_lock(
            metric.id,
            metric.row_version,
            status=target_status,
            approver_id=actor_id,
            effective_version=target_version,
            **extra_updates,
        )

        # 版本转正：将指定版本标记为对应状态
        await self._repo.mark_version_published(metric.id, target_version, now)

        await self._cache.invalidate(metric_code)

        # 发布 metric.approved 事件（对齐 FR-014：lineage(Neo4j)/search(ES)/notify）
        event_payload: dict[str, Any] = {
            "metric_code": metric_code,
            "version": target_version,
            "type": metric.type,
            "domain": metric.domain,
            "definition_json": metric.definition_json,
            "mode": request.mode,
        }
        if metric.type in ("derived", "composite"):
            event_payload["dependencies"] = metric.definition_json.get("dependencies", [])

        await self._publish_event(
            "metric.approved",
            event_payload,
            actor_id=str(actor_id),
        )

        logger.info(
            "metric_approved",
            metric_code=metric_code,
            target_status=target_status,
            version=target_version,
            actor_id=actor_id,
        )
        return updated

    async def reject_metric(
        self, metric_code: str, request: MetricRejectRequest, actor_id: int
    ) -> Metric:
        """审核驳回指标（REVIEW → DRAFT，对齐 FR-005）。

        Args:
            metric_code: 指标编码。
            request: 驳回请求（含 reason）。
            actor_id: 操作人 ID。

        Returns:
            驳回后的指标。

        Raises:
            NotFoundError: 指标不存在。
            ConflictError: 非法状态跃迁。
        """
        metric = await self.get_metric(metric_code)

        # 状态机校验：REVIEW→DRAFT
        invalid = MetricStateMachine.validate_transition(metric.status, "DRAFT")
        if invalid is not None:
            raise ConflictError(invalid, error_code="INVALID_TRANSITION")

        updated = await self._repo.update_with_optimistic_lock(
            metric.id, metric.row_version, status="DRAFT"
        )
        await self._cache.invalidate(metric_code)

        # 发布 metric.rejected 事件（对齐 FR-005：通知 Owner 驳回原因）
        await self._publish_event(
            "metric.rejected",
            {
                "metric_code": metric_code,
                "domain": metric.domain,
                "owner_id": metric.owner_id,
                "reason": request.reason,
                "reviewer_id": actor_id,
            },
            actor_id=str(actor_id),
        )

        logger.info(
            "metric_rejected",
            metric_code=metric_code,
            reason=request.reason,
            actor_id=actor_id,
        )
        return updated

    async def deprecate_metric(
        self,
        metric_code: str,
        successor_code: str | None,
        actor_id: int,
        role: str,
    ) -> Metric:
        """废弃指标（PUBLISHED → DEPRECATED，对齐 FR-002/FR-039）。

        仅 PUBLISHED 状态可废弃；successor_code 存在且 PUBLISHED 时允许替代。
        发布 metric.deprecated 事件（对齐 FR-015：lineage + notify 下游消费方）。

        Args:
            metric_code: 指标编码。
            successor_code: 替代指标编码（须为已 PUBLISHED 指标）。
            actor_id: 操作人 ID。
            role: 操作人角色。

        Returns:
            废弃后的指标。

        Raises:
            NotFoundError: 指标或替代指标不存在。
            AuthError: metric_owner 操作他人指标（越权）。
            ConflictError: 非法状态跃迁。
            BusinessError: 指标已废弃 / 替代指标未发布。
        """
        metric = await self.get_metric(metric_code)
        self._assert_owner_or_admin(metric, actor_id, role)

        # 状态机校验：仅 PUBLISHED 可废弃（对齐 FR-002）
        invalid = MetricStateMachine.validate_transition(metric.status, "DEPRECATED")
        if invalid is not None:
            raise ConflictError(invalid, error_code="INVALID_TRANSITION")

        # 替代指标校验：存在且已发布（对齐 FR-039）
        if successor_code is not None:
            successor = await self._repo.get_by_code(successor_code)
            if successor is None:
                raise NotFoundError(f"替代指标不存在: {successor_code}")
            if successor.status != "PUBLISHED":
                raise BusinessError(
                    f"替代指标 {successor_code} 未发布，无法作为替代",
                    error_code="VALIDATION_ERROR",
                )

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

        # 发布 metric.deprecated 事件（对齐 FR-015）
        await self._publish_event(
            "metric.deprecated",
            {
                "metric_code": metric_code,
                "domain": metric.domain,
                "successor_code": successor_code,
                "deprecated_at": now.isoformat(),
            },
            actor_id=str(actor_id),
        )

        logger.info(
            "metric_deprecated",
            metric_code=metric_code,
            successor=successor_code,
            actor_id=actor_id,
        )
        return updated

    async def delete_metric(self, metric_code: str, actor_id: int) -> Metric:
        """软删除指标（仅 DRAFT 状态）。

        Args:
            metric_code: 指标编码。
            actor_id: 操作人 ID。

        Returns:
            被删除的指标。

        Raises:
            NotFoundError: 指标不存在。
            BusinessError: 指标非 DRAFT 状态不可删除。
        """
        metric = await self.get_metric(metric_code)
        if metric.status != "DRAFT":
            raise BusinessError(
                f"仅 DRAFT 状态的指标可删除，当前状态 {metric.status}",
                error_code="VALIDATION_ERROR",
            )

        await self._repo.soft_delete(metric.id)
        await self._cache.invalidate(metric_code)

        logger.info("metric_deleted", metric_code=metric_code, actor_id=actor_id)
        return metric

    async def review_compliance(
        self, metric_code: str, actor_id: int, role: str
    ) -> Metric:
        """PII 合规复核（置 compliance_reviewed=True，打通 PII 指标发布闸门）。

        Args:
            metric_code: 指标编码。
            actor_id: 操作人 ID。
            role: 操作人角色。

        Returns:
            复核后的指标。

        Raises:
            NotFoundError: 指标不存在。
            AuthError: metric_owner 操作他人指标（越权）。
            BusinessError: 指标 Owner 自审。
            ConflictError: 乐观锁冲突。
        """
        metric = await self.get_metric(metric_code)
        self._assert_owner_or_admin(metric, actor_id, role)
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

    def _assert_owner_or_admin(
        self, metric: Metric, actor_id: int, role: str
    ) -> None:
        """越权守卫：metric_owner 仅可操作本人（或副 Owner）的指标。

        platform_admin / domain_admin 放行；metric_owner 校验 owner_id /
        backup_owner_id；其余角色一律拒绝。

        Args:
            metric: 指标对象。
            actor_id: 操作人 ID。
            role: 操作人角色。

        Raises:
            AuthError: 无权操作该指标。
        """
        if role in ("platform_admin", "domain_admin"):
            return
        if role == "metric_owner":
            if metric.owner_id == actor_id or metric.backup_owner_id == actor_id:
                return
            raise AuthError(
                "无权操作他人指标",
                error_code="FORBIDDEN",
                ctx={
                    "metric_code": metric.metric_code,
                    "actor_id": actor_id,
                    "owner_id": metric.owner_id,
                },
            )
        raise AuthError(
            "无权操作该指标",
            error_code="FORBIDDEN",
            ctx={"metric_code": metric.metric_code, "role": role},
        )

    async def _publish(self, metric: Metric, target_version: int, actor_id: int) -> Metric:
        """执行发布落库：PII 合规闸门 + 状态/生效版本转正 + 版本标记 + 缓存失效。

        供 publish_metric 与 review_metric(approved=True) 复用，保证评审通过
        与直接发布走同一套发布语义（含 PII 合规闸门）。

        Args:
            metric: 已加载的指标对象。
            target_version: 待发布版本（须等于当前版本，由调用方保证）。
            actor_id: 操作人 ID。

        Returns:
            发布后的指标。

        Raises:
            BusinessError: PII 指标未过合规审核。
            NotFoundError: 版本不存在。
            ConflictError: 乐观锁冲突。
        """
        # PII 指标须先过合规审核
        if metric.pii_flag and not metric.compliance_reviewed:
            raise BusinessError(
                "PII 指标须先通过合规审核",
                error_code="COMPLIANCE_BLOCKED",
            )

        # 校验版本存在
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

        await self._cache.invalidate(metric.metric_code)
        return updated

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
