"""指标服务层（业务逻辑）。

对齐 DEV_GUIDE §8b.2（Service 层：编排 repository + 调用其他 service）。
包含指标 CRUD、状态机流转、版本管理。

P3: 继承 BaseService Protocol，统一 db+eventbus+settings 注入模式。
"""

from __future__ import annotations

import json
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.error_codes import ErrorCode
from app.core.exceptions import (
    AuthError,
    BusinessError,
    ConflictError,
    NotFoundError,
)
from app.db.redis import get_redis
from app.models.metric import Metric
from app.models.metric_version import MetricVersion
from app.services.governance.service import GovernanceService  # noqa: F401 供测试 patch 定位
from app.services.semantic.cache import MetricCache
from app.services.semantic.repository import MetricRepository
from app.services.semantic.schemas import (
    MetricApproveRequest,
    MetricCreateRequest,
    MetricDescriptionUpdateRequest,
    MetricEmergencyPublishRequest,
    MetricListParams,
    MetricPublishRequest,
    MetricRejectRequest,
    MetricResponse,
    MetricSubmitRequest,
    MetricUpdateRequest,
)
from app.services.semantic.state_machine import MetricStateMachine

logger = structlog.get_logger("unisense.semantic.service")

# 指标描述推断的 LLM 响应格式（对齐 collector：json_schema 强约束优先 + json_object 降级）
_METRIC_DESC_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "metric_description_result",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": ["description", "confidence"],
            "additionalProperties": False,
        },
    },
}
_METRIC_JSON_OBJECT_FORMAT: dict[str, Any] = {"type": "json_object"}
# 格式重试提示：解析为 None 时追加，迫使模型收敛到合规 JSON（最多重试 1 次）。
_METRIC_STRICT_JSON_HINT = "请严格只输出符合 JSON Schema 的 JSON，不要任何额外文字。"


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

# Top-level 破坏性变更字段：直接修改 metric 表上的这些字段等同于口径变更
# （对齐 TD §12 metric_version：granularity/unit 变更触发 PENDING_VERSION）
# aggregation（聚合方式）语义上就是"怎么算"，SUM→AVG 是完全不同的口径——
# 必须与 granularity/unit 同级触发 PENDING_VERSION（此前误归治理属性静默更新，
# 与 definition_json 路径的 BREAKING_DEF_FIELDS 判定矛盾，R40 修复）。
BREAKING_TOP_LEVEL_FIELDS = ("granularity", "unit", "aggregation")

# 指标间依赖边的 edge_type 映射（register_metric_dependency 由血缘团队负责，
# 此处按 metric.type 直接写 LineageEdge）。
# 注意：lineage_edge_type 枚举当前仅含 DERIVED_FROM/LINEAGE_UP/LINEAGE_DOWN/
# CONSUMED_BY/EXTERNAL_BREAK，不含 COMPOSED_OF（composite 专属）。待血缘模型扩展
# 枚举后，composite 可单独使用 COMPOSED_OF 表达「组合」语义；当前统一以 DERIVED_FROM
# 表达（保证 impact_preview 下游遍历一致），避免写入未枚举值导致 MySQL 拒绝落库。
_METRIC_DEP_EDGE_TYPE: dict[str, str] = {
    "derived": "DERIVED_FROM",
    "composite": "DERIVED_FROM",
}


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

    事件发布策略（对齐 FR-014~FR-018 / Decision 7）：
    - 所有事件使用 BaseService._publish_event（best-effort）
    - 发布失败仅告警，不阻断主流程
    - 失败事件入 Arq 重试队列（见 app.tasks.semantic_tasks.retry_event_publish）
    - 读侧对 Neo4j/ES 缺失标 stale
    """

    def __init__(
        self,
        db: AsyncSession,
        cache: MetricCache | None = None,
        governance_svc: GovernanceService | None = None,
    ) -> None:
        """初始化服务。

        Args:
            db: 异步数据库会话。
            cache: 指标读缓存；缺省使用默认 Redis 客户端（不可用时自动降级 DB）。
            governance_svc: 治理服务实例（PDP 鉴权用）；缺省按需创建。
        """
        super().__init__(db)
        self._repo = MetricRepository(db)
        self._cache = (
            cache
            if cache is not None
            else MetricCache.from_defaults(get_redis() if _redis_available() else None)
        )
        self._governance_svc = governance_svc

    def _gov_svc(self) -> GovernanceService:
        """获取治理服务实例（延迟创建，支持测试注入 mock）。"""
        if self._governance_svc is not None:
            return self._governance_svc
        from app.services.governance.service import GovernanceService

        return GovernanceService(self._db)

    async def invalidate_cache(self, metric_codes: list[str]) -> None:
        """批量失效指标读缓存（best-effort，失败仅告警）。

        供跨服务联动使用（如冲突仲裁软删/废弃/标记指标后主动失效缓存，
        保证详情/健康读数与 DB 即时一致，避免 cache-aside 旧数据残留）。
        """
        if not metric_codes:
            return
        try:
            await self._cache.invalidate_batch(metric_codes)
        except Exception:
            logger.warning("metric_cache_invalidate_batch_failed", codes=metric_codes)

    # ---- 字典校验辅助方法（对齐 spec FR-008/FR-009, plan.md D2）----

    async def _validate_domain_active(self, domain_code: str) -> None:
        """校验 domain 存在且 active（应用层可选校验，对齐 D1）。

        降级语义：subject_domain 未配置该域（表为空/未种子/查询异常）时放行，
        仅对"已配置但停用"的域拦截——避免迁移 0026 空表导致存量指标创建全阻断。
        """
        from app.core.exceptions import NotFoundError
        from app.services.subject_domain.service import SubjectDomainService

        try:
            svc = SubjectDomainService(self._db)
            await svc.validate_domain_active(domain_code)
        except NotFoundError:
            # 域未在 subject_domain 配置（兼容存量/未种子环境）→ 放行
            return
        except Exception:
            # 查询异常（表不存在/DB 抖动）→ best-effort 放行，不阻断创建
            return

    async def _get_domain_defaults(self, domain_code: str) -> dict[str, Any]:
        """获取域默认值预设。"""
        try:
            from app.services.subject_domain.service import SubjectDomainService

            svc = SubjectDomainService(self._db)
            return await svc.get_defaults(domain_code)
        except Exception:
            return {}

    async def _validate_dict_fields(self, request: MetricCreateRequest) -> None:
        """校验字典字段值存在于 SystemDict 且 active（应用层可选校验，对齐 D2）。

        降级语义：字典项未配置（空表/未种子/查询异常）时放行，仅对"已配置但停用"
        的值拦截——避免迁移 0025 空表导致存量指标创建全阻断。
        """
        from app.core.exceptions import BusinessError, NotFoundError
        from app.services.system_dict.service import SystemDictService

        try:
            svc = SystemDictService(self._db)
            # 需要校验的 dict_type → request 字段映射
            dict_validations: list[tuple[str, str]] = [
                ("granularity", request.granularity),
                ("unit", request.unit),
                ("aggregation", request.aggregation),
                ("time_semantics", request.time_semantics),
                ("freshness", request.freshness),
                ("dw_layer", request.dw_layer),
                ("metric_type", request.type),
                ("additivity", request.additivity),
                ("serving_mode", request.serving_mode),
                ("metric_tier", request.metric_tier),
            ]
            for dict_type, code in dict_validations:
                if code:
                    await svc.validate_dict_value(dict_type, code)
        except NotFoundError:
            # 字典项未配置（空表/未种子环境）→ 放行，不阻断创建
            return
        except BusinessError:
            raise  # 已配置但停用 → 拦截
        except Exception:
            # 查询异常（表不存在/DB 抖动）→ best-effort 放行，不阻断创建
            return

    async def _generate_metric_code(self, request: MetricCreateRequest) -> str:
        """自动生成唯一指标编码（4 段式：域_业务对象_度量_统计周期）。

        规则（对齐 FR-010 / batch_register 的既有逻辑）：
        1. 源表 + 度量列 + 周期齐全 → 用 auto_fill 引擎生成 ``{domain}_{biz}_{measure}_{period}``；
        2. 否则回退 ``{domain}_entity_{measure}_day``；
        3. 冲突时追加 ``_2/_3/...`` 后缀（上限 100 次）。
        """
        from app.core.codegen import generate_unique_code
        from app.services.semantic.auto_fill import (
            extract_biz_object,
            extract_measure,
        )

        domain = (request.domain or "domain").strip().lower() or "domain"
        measure = extract_measure(request.measure_column or "value") or "value"
        if request.source_table and request.period:
            biz_obj = extract_biz_object(request.source_table) or "entity"
            base = f"{domain}_{biz_obj}_{measure}_{request.period.strip().lower()}"
        else:
            base = f"{domain}_entity_{measure}_day"

        async def _exists(code: str) -> bool:
            return await self._repo.get_by_code(code) is not None

        try:
            return await generate_unique_code(base, _exists)
        except RuntimeError as exc:
            from app.core.exceptions import ConflictError

            raise ConflictError(
                f"无法为指标自动生成唯一编码（已尝试 100 次），请手动指定: {base}",
                ctx={"code": "CODE_EXHAUSTED", "metric_code": base},
            ) from exc

    async def create_metric(self, request: MetricCreateRequest, owner_id: int) -> Metric:
        """创建指标（初始状态 DRAFT）。

        对齐 FR-012/FR-013：metric_code 校验委托 ConflictPrechecker.validate_code_format，
        创建后异步调 ConflictPrechecker.precheck，命中相似口径→挂 pending_conflict 标记。

        Args:
            request: 创建请求。
            owner_id: 创建人（Owner）ID。

        Returns:
            创建的指标。

        Raises:
            ConflictError: 指标编码已存在。
        """
        # ---- 编码自动生成（FR-010：缺省时系统生成，非人为创造）----
        if not request.metric_code:
            request.metric_code = await self._generate_metric_code(request)

        # 检查编码唯一性
        existing = await self._repo.get_by_code(request.metric_code)
        if existing is not None:
            raise ConflictError(
                f"指标编码已存在: {request.metric_code}",
                error_code="METRIC_CODE_EXISTS",
                ctx={"code": "METRIC_CODE_EXISTS", "metric_code": request.metric_code},
            )

        # ---- 字典校验 + 自动推断（对齐 spec FR-008/FR-009/FR-011）----
        # 1. 校验 domain 存在且 active
        await self._validate_domain_active(request.domain)

        # 2. 自动推断：用 source_table/measure_column/period 补全缺失字段
        if request.source_table or request.measure_column or request.period:
            from app.services.semantic.auto_fill import auto_fill as _auto_fill

            domain_defaults = await self._get_domain_defaults(request.domain)
            suggested = _auto_fill(
                domain_code=request.domain,
                source_table=request.source_table,
                measure_column=request.measure_column,
                period=request.period,
                domain_defaults=domain_defaults,
            )
            # 用推断值补全缺失字段（仅当原值为默认值时覆盖）
            for field_name, suggested_val in suggested.get("defaults", {}).items():
                if suggested_val is not None:
                    current = getattr(request, field_name, None)
                    # 仅当当前值是默认值时才覆盖（保留用户显式设定的值）
                    field_info = request.model_fields.get(field_name)
                    if field_info and current == field_info.default and suggested_val != current:
                        setattr(request, field_name, suggested_val)

        # 3. 校验字典字段值存在于 SystemDict（对齐 FR-009）
        await self._validate_dict_fields(request)

        # 3b. 口径完整性：把 top-level 的 source_table/measure_column 合入 definition_json。
        # 血缘差异同步（register_metric_from_definition）读 definition.source_table /
        # measure_column 建「指标↔落地表」边——但批量注册/模板实例化等后端构造路径此前
        # 不写这两个键，导致请求传了 source_table 却无血缘边（与前端单条 buildDefinitionJson
        # 合入 ②源表/度量列的行为不一致）。此处后端统一兜底，覆盖全部创建路径。
        if request.source_table or request.measure_column:
            _defn = dict(request.definition_json or {})
            if request.source_table and not _defn.get("source_table"):
                _defn["source_table"] = request.source_table
            if request.measure_column and not _defn.get("measure_column"):
                _defn["measure_column"] = request.measure_column
            request.definition_json = _defn

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

        # 异步冲突预检（对齐 FR-012）：创建后调 ConflictPrechecker.precheck
        # 命中相似口径→更新 pending_conflict=True + pending_conflict_detail
        try:
            from app.services.semantic.conflict_precheck import ConflictPrechecker

            async def _load_existing_metrics() -> list[dict[str, Any]]:
                """加载已存在口径供预检比对（仅取预检所需字段，避免整模型暴露）。"""
                metrics, _ = await self._repo.list_metrics(limit=1000)
                rows: list[dict[str, Any]] = []
                for m in metrics:
                    defn = m.definition_json or {}
                    rows.append(
                        {
                            "metric_code": m.metric_code,
                            "domain": m.domain,
                            "definition": (defn.get("definition") or defn.get("expression") or ""),
                            "source_tables": defn.get("source_tables") or [],
                            "has_pii": bool(m.pii_flag),
                            "pii_authorized": bool(m.compliance_reviewed),
                            "status": m.status,
                            "metric_id": m.id,
                        }
                    )
                return rows

            prechecker = ConflictPrechecker(existing_loader=_load_existing_metrics)
            conflict_detail = await prechecker.precheck(metric.metric_code, definition)
            if conflict_detail is not None:
                metric = await self._repo.update_with_optimistic_lock(
                    metric.id,
                    metric.row_version,
                    pending_conflict=True,
                    pending_conflict_detail=conflict_detail,
                )
                logger.info(
                    "metric_conflict_detected",
                    metric_code=metric.metric_code,
                    conflict_detail=conflict_detail,
                )
        except Exception:
            # 冲突预检失败不阻塞创建（best-effort）
            logger.warning(
                "metric_conflict_precheck_failed",
                metric_code=metric.metric_code,
            )

        # PII 血缘传播（对齐 US13/TD §12.6）：创建时若声明的上游字段带 PII 标记，
        # 则联动治理服务标记指标 pii_flag + lineage_edge.pii_inherited。
        # best-effort：治理/血缘不可用不阻塞指标创建。
        try:
            from app.services.governance.service import GovernanceService

            upstream_columns = [
                {"column": f.get("name") or f.get("column"), "pii": bool(f.get("pii"))}
                for f in (definition.get("source_fields") or [])
                if isinstance(f, dict)
            ]
            if upstream_columns:
                await GovernanceService(self._db).propagate_pii_to_metric(
                    metric.metric_code,
                    upstream_source_columns=upstream_columns,
                )
                await self._db.flush()
        except Exception:  # noqa: BLE001 - best-effort 不阻断创建
            logger.warning(
                "metric_pii_propagation_failed",
                metric_code=metric.metric_code,
            )

        # 指标完整血缘注册（表血缘 + 指标间依赖边，best-effort 不阻断创建）
        await self._register_metric_lineage_full(metric)

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
            # 详情直访的友好作废引导：指标因口径仲裁被软删（deleted_at + successor）时，
            # 返回结构化 METRIC_ARCHIVED（携带胜方 successor），供前端渲染「作废 + 查看权威」页，
            # 而非对历史链接直接给出裸 404「指标不存在」。
            archived = await self._repo.get_archived_by_code(metric_code)
            if archived is not None and archived.successor_code:
                raise NotFoundError(
                    f"指标已因口径裁决作废: {metric_code}",
                    error_code=ErrorCode.METRIC_ARCHIVED,
                    ctx={
                        "metric_code": metric_code,
                        "successor_code": archived.successor_code,
                        "arbitration_mark": archived.arbitration_mark,
                    },
                )
            raise NotFoundError(f"指标不存在: {metric_code}")
        await self._cache.set(metric)
        return MetricResponse.model_validate(metric)

    async def get_archived_metric_public(self, metric_code: str) -> dict[str, Any]:
        """作废指标详情（含 successor 指针与历史口径），供作废引导页展示。

        对因口径仲裁被软删（deleted_at + successor）的指标，返回其完整历史
        口径定义与裁决指针——前端据此渲染「作废指标详情 + 跳转权威指标」，
        而非仅凭错误码展示一张错误卡片。

        Args:
            metric_code: 作废指标编码。

        Returns:
            {"metric": MetricResponse, "successor_code": str|None, "arbitration_mark": dict|None}。

        Raises:
            NotFoundError: 指标不存在或未作废。
        """
        archived = await self._repo.get_archived_by_code(metric_code)
        if archived is None:
            raise NotFoundError(f"指标不存在: {metric_code}")
        return {
            "metric": MetricResponse.model_validate(archived),
            "successor_code": archived.successor_code,
            "arbitration_mark": archived.arbitration_mark,
        }

    async def list_metrics(self, params: MetricListParams) -> tuple[list[Metric], int]:
        """分页查询指标列表。

        Args:
            params: 查询参数。

        Returns:
            (指标列表, 总数)。
        """
        offset = (params.page - 1) * params.page_size
        return await self._repo.list_metrics(
            deleted=params.deleted,
            domain=params.domain,
            status=params.status,
            metric_tier=params.metric_tier,
            keyword=params.keyword,
            owner_id=params.owner_id,
            approver_id=params.approver_id,
            reviewed_by=params.reviewed_by,
            pii_flag=params.pii_flag,
            created_after=params.created_after,
            created_before=params.created_before,
            updated_after=params.updated_after,
            updated_before=params.updated_before,
            sort_by=params.sort_by,
            sort_order=params.sort_order,
            offset=offset,
            limit=params.page_size,
        )

    async def update_metric(
        self,
        metric_code: str,
        request: MetricUpdateRequest,
        actor_id: int,
        role: str,
        user_domain: str | None = None,
    ) -> Metric:
        """更新指标（乐观锁）。

        仅允许在 DRAFT / REVIEW / PUBLISHED 状态下更新。
        更新会创建新版本（如口径变更）。

        Args:
            metric_code: 指标编码。
            request: 更新请求。
            actor_id: 操作人 ID。
            role: 操作人角色。
            user_domain: 操作人所属域（API 层传入，避免 service 内额外查 DB）。

        Returns:
            更新后的指标。

        Raises:
            NotFoundError: 指标不存在。
            AuthError: metric_owner 操作他人指标（越权）。
            BusinessError: 状态不允许更新 / PII 未合规复核。
            ConflictError: 乐观锁冲突。
        """
        metric = await self.get_metric(metric_code)
        self._assert_owner_or_admin(metric, actor_id, role)

        # 跨请求乐观锁（TD §4.1）：前端编辑弹窗回传 row_version，
        # 若与当前值不一致说明数据已被他人修改 → 409 拒绝（防静默覆盖）。
        expected = getattr(request, "row_version", None)
        if expected is not None and expected != metric.row_version:
            raise ConflictError(
                "指标已被他人修改，请刷新后重试",
                error_code="OPTIMISTIC_LOCK_CONFLICT",
                ctx={
                    "metric_code": metric_code,
                    "current_row_version": metric.row_version,
                    "expected_row_version": expected,
                },
            )

        # PII 合规闸门（COMPL-1）：未经合规复核的 PII 指标，禁止 update
        # 修复前：update 操作不校验 compliance_reviewed，pii_flag=1 且未复核的指标可被修改
        if metric.pii_flag and not metric.compliance_reviewed:
            raise BusinessError(
                "PII 指标未通过合规复核，禁止修改",
                error_code="FORBIDDEN_PII",
                ctx={"metric_code": metric_code},
            )

        # PDP 域权限闸门：update 须有 write 权限（同域或跨域 grant）
        decision = await self._gov_svc().check_metric_permission(
            metric_code=metric_code,
            action="write",
            user_id=actor_id,
            role=role,
            user_domain=user_domain,
        )
        if not decision.allow:
            raise BusinessError(
                decision.reason or "无权更新该指标",
                error_code=decision.error_code or "FORBIDDEN",
                ctx={"metric_code": metric_code, "actor_id": actor_id},
            )

        # TECH-07 (T050): metric_code 不可通过 definition_json.code 修改
        if (
            request.definition_json is not None
            and "code" in request.definition_json
            and request.definition_json["code"] != metric_code
        ):
            raise BusinessError(
                f"指标编码不可修改（当前: {metric_code}，请求: {request.definition_json['code']}）",
                error_code="FORBIDDEN",
            )

        if metric.status not in ("DRAFT", "REVIEW", "PUBLISHED"):
            raise BusinessError(
                f"指标状态 {metric.status} 不允许更新",
                error_code="VALIDATION_ERROR",
            )

        # 收集更新字段
        updates: dict[str, Any] = {}
        # 治理属性（currency/time_semantics/freshness/dw_layer/metric_tier/
        # serving_mode/additivity/non_additive_dimensions）：非破坏性变更，仅更新主表
        # 治理列、不触发版本递增/PENDING 期（不在 BREAKING_TOP_LEVEL_FIELDS）。
        # aggregation（聚合方式）本质是口径变更（SUM→AVG 口径不同），已并入
        # BREAKING_TOP_LEVEL_FIELDS 与 granularity/unit 同级触发 PENDING_VERSION，
        # 不再属于治理属性（R40 修复其与 definition_json 判定矛盾）。
        # 修复前：指标创建后治理字段不可改（分层纠正/时效调整/分级晋升/币种修正
        # 等生产高频场景只能重建指标），与注册页可选字段契约不一致。
        for field in (
            "name",
            "granularity",
            "unit",
            "currency",
            "aggregation",
            "time_semantics",
            "freshness",
            "dw_layer",
            "metric_tier",
            "serving_mode",
            "additivity",
            "non_additive_dimensions",
            "sla",
            "consumption_guide",
            "backup_owner_id",
        ):
            val = getattr(request, field, None)
            if val is not None:
                updates[field] = val

        # Top-level 破坏性字段变更检测（granularity/unit 直接修改等同口径变更）
        # 当 definition_json 未同时提交时，需独立判定是否触发 PENDING_VERSION
        top_level_breaking = False
        if metric.status == "PUBLISHED":
            for field in BREAKING_TOP_LEVEL_FIELDS:
                if field in updates:
                    old_val = getattr(metric, field, None)
                    new_val = updates[field]
                    if old_val != new_val:
                        top_level_breaking = True
                        logger.info(
                            "top_level_breaking_change_detected",
                            metric_code=metric_code,
                            field=field,
                            old=old_val,
                            new=new_val,
                        )
                        break

        # 口径变更 → 新版本
        if request.definition_json is not None:
            old_def = metric.definition_json
            # PII 双源归一化：definition.pii 与 pii_flag 保持一致（pii_flag 为权威源）
            new_def, synced_pii = _normalize_pii(request.definition_json, metric.pii_flag)
            is_breaking = self._is_breaking_change(old_def, new_def) or top_level_breaking

            # top-level 破坏性字段 diff（与 elif 分支同构，供 PENDING 转正回写主表）
            top_diff: dict[str, Any] = {}
            for field in BREAKING_TOP_LEVEL_FIELDS:
                old_val = getattr(metric, field, None)
                new_val = getattr(request, field, None)
                if new_val is not None and old_val != new_val:
                    top_diff[field] = {
                        "before": old_val,
                        "after": new_val,
                        "change_type": "BREAKING",
                    }

            new_version_num = metric.version + 1
            updates["definition_json"] = new_def
            updates["version"] = new_version_num
            if synced_pii != metric.pii_flag:
                updates["pii_flag"] = synced_pii

            # S-02 修复：PUBLISHED 状态口径变更走 PENDING_VERSION 期，不直接生效
            if metric.status == "PUBLISHED" and is_breaking:
                # 破坏性变更：创建 PENDING 版本，不更新 metric 主表口径；
                # 同时收敛 top-level 破坏性字段（granularity/unit），
                # 防止组合请求绕过确认期直写主表（与 elif 分支一致）。
                # 注意：主表 version 立即递增以「预留版本号」，避免 PENDING 期间
                # 再次提交同版本号触发 MetricVersion 唯一键冲突（与 elif 分支一致）。
                updates.pop("definition_json", None)
                for field in BREAKING_TOP_LEVEL_FIELDS:
                    updates.pop(field, None)
                version_status = "PENDING_CONFIRMATION"
            else:
                version_status = "DRAFT"

            # 合并定义 diff 与 top-level diff，供转正时回写主表
            merged_diff = self._compute_diff(old_def, new_def)
            if top_diff:
                merged_diff.update(top_diff)

            # 创建版本记录
            version = MetricVersion(
                metric_id=metric.id,
                version=new_version_num,
                change_type="BREAKING" if is_breaking else "UPDATE",
                definition_json=new_def,
                diff_json=merged_diff,
                status=version_status,
                change_reason=request.change_reason,
                created_by=actor_id,
            )
            await self._repo.create_version(version)

            # PUBLISHED + 破坏性变更 → 创建 PendingVersionConfirmation 记录
            if metric.status == "PUBLISHED" and is_breaking:
                from app.services.semantic.pending_version_manager import PendingVersionManager

                # 消费方 = 指标 Owner（+备份 Owner），负责在 14 天确认期内确认/拒绝；
                # 生产环境可扩展为经血缘反查下游消费方列表。
                consumer_ids = [metric.owner_id]
                if metric.backup_owner_id is not None:
                    consumer_ids.append(metric.backup_owner_id)

                pvm = PendingVersionManager(self._db)
                await pvm.create_pending(metric, version, consumer_ids)
                logger.info(
                    "pending_version_created",
                    metric_code=metric_code,
                    version=new_version_num,
                    consumers=consumer_ids,
                    reason="breaking_change_on_published",
                )

        # Top-level 破坏性变更但无 definition_json 提交时，仍需创建版本记录+PENDING
        elif top_level_breaking:
            new_version_num = metric.version + 1
            updates["version"] = new_version_num

            # PUBLISHED 状态 → PENDING_CONFIRMATION（不直接生效 top-level 破坏性字段）
            if metric.status == "PUBLISHED":
                # 移除破坏性 top-level 字段，不直接更新 metric 主表
                for field in BREAKING_TOP_LEVEL_FIELDS:
                    updates.pop(field, None)
                version_status = "PENDING_CONFIRMATION"
            else:
                # top_level_breaking 仅在 PUBLISHED 状态被检测（见上方判定），此分支不可达；
                # 保留以防御未来扩展。
                version_status = "DRAFT"  # pragma: no cover - 不可达防御分支

            # 构造 diff（top-level 字段变更）
            old_def = metric.definition_json or {}
            top_level_diff: dict[str, Any] = {}
            for field in BREAKING_TOP_LEVEL_FIELDS:
                old_val = getattr(metric, field, None)
                new_val = getattr(request, field, None)
                if new_val is not None and old_val != new_val:
                    top_level_diff[field] = {
                        "before": old_val,
                        "after": new_val,
                        "change_type": "BREAKING",
                    }

            version = MetricVersion(
                metric_id=metric.id,
                version=new_version_num,
                change_type="BREAKING",
                definition_json=old_def,  # 口径不变
                diff_json=top_level_diff,
                status=version_status,
                change_reason=request.change_reason,
                created_by=actor_id,
            )
            await self._repo.create_version(version)

            if metric.status == "PUBLISHED":
                from app.services.semantic.pending_version_manager import PendingVersionManager

                consumer_ids = [metric.owner_id]
                if metric.backup_owner_id is not None:
                    consumer_ids.append(metric.backup_owner_id)

                pvm = PendingVersionManager(self._db)
                await pvm.create_pending(metric, version, consumer_ids)
                logger.info(
                    "pending_version_created",
                    metric_code=metric_code,
                    version=new_version_num,
                    consumers=consumer_ids,
                    reason="top_level_breaking_change_on_published",
                )

        # 注意：change_reason 仅写入 MetricVersion 快照（上方），metric 主表无该列，
        # 不能写入 updates，否则 update(Metric).values(change_reason=...) 抛 CompileError。

        # 「保留差异+指定一方改名」（TD §12.4）：Owner 在详情页改名后，清除
        # arbitration_mark.rename_required 标记并记录 resolved_at，完成治理闭环。
        # 仅在仲裁要求改名且本次确实变更了 name 时触发（幂等：已清除不再写）。
        if updates.get("name") is not None and updates["name"] != metric.name:
            mark = metric.arbitration_mark or {}
            if mark.get("rename_required"):
                mark["rename_required"] = False
                mark["resolved_at"] = datetime.now(UTC).isoformat()
                updates["arbitration_mark"] = mark
                logger.info(
                    "metric_rename_resolved",
                    metric_code=metric_code,
                    old_name=metric.name,
                    new_name=updates["name"],
                )

        # REVIEW 状态编辑即撤回重提（FR-005 闭环）：评审中的指标被修改后重置为
        # DRAFT 并清空评审指派——否则评审人看到的是已提交旧版本，修改静默不生效
        # 且无重新提审。提交人编辑后需重新提交评审（触发新指派与通知）。
        if metric.status == "REVIEW":
            updates["status"] = "DRAFT"
            updates["reviewer_id"] = None
            updates["reviewer_type"] = None
            updates["reviewer_domain"] = None

        updated = await self._repo.update_with_optimistic_lock(
            metric.id, metric.row_version, **updates
        )

        await self._cache.invalidate(metric_code)

        # 口径变更时刷新指标间依赖血缘（表血缘通常不变，但 dependencies 解析需重跑）；
        # 仅当提交 definition_json 时触发，best-effort 不阻断更新。
        # PUBLISHED 破坏性变更（触发 PENDING_VERSION，新口径未生效）**不立即注册**——
        # 否则血缘图显示"未来口径"误导影响分析、且消费方拒绝后被拒口径边已注册残留。
        # 此类变更由转正路径（_promote_pending_version）在新口径生效后注册血缘。
        if request.definition_json is not None and not (
            metric.status == "PUBLISHED" and is_breaking
        ):
            await self._register_metric_lineage_full(metric)

        logger.info(
            "metric_updated",
            metric_code=metric_code,
            actor_id=actor_id,
            fields=list(updates.keys()),
        )
        return updated

    async def update_metric_description(
        self,
        metric_code: str,
        request: MetricDescriptionUpdateRequest,
        actor_id: int,
        role: str,
        user_domain: str | None = None,
    ) -> Metric:
        """更新指标业务描述（治理补充 TD §12.1，不触发版本/不参与口径变更）。

        与 ``update_metric`` 的版本状态机解耦：描述是运营层补充说明，
        不属口径定义，因此不创建 MetricVersion、不设 PII 复核闸门。
        权限沿用 owner/admin + PDP write。

        Args:
            metric_code: 指标编码。
            request: 描述更新请求（空串=清除描述）。
            actor_id: 操作人 ID。
            role: 操作人角色。
            user_domain: 操作人所属域（API 层传入）。

        Raises:
            NotFoundError: 指标不存在。
            AuthError: metric_owner 操作他人指标（越权）。
            BusinessError: PDP 无 write 权限。
        """
        metric = await self.get_metric(metric_code)
        self._assert_owner_or_admin(metric, actor_id, role)

        # 跨请求乐观锁（TD §4.1）：描述编辑回传 row_version，他人已改则 409（防静默覆盖）
        expected = getattr(request, "row_version", None)
        if expected is not None and expected != metric.row_version:
            raise ConflictError(
                "指标已被他人修改，请刷新后重试",
                error_code="OPTIMISTIC_LOCK_CONFLICT",
                ctx={
                    "metric_code": metric_code,
                    "current_row_version": metric.row_version,
                    "expected_row_version": expected,
                },
            )

        decision = await self._gov_svc().check_metric_permission(
            metric_code=metric_code,
            action="write",
            user_id=actor_id,
            role=role,
            user_domain=user_domain,
        )
        if not decision.allow:
            raise BusinessError(
                decision.reason or "无权更新该指标描述",
                error_code=decision.error_code or "FORBIDDEN",
                ctx={"metric_code": metric_code, "actor_id": actor_id},
            )

        stripped = request.description.strip()
        updates: dict[str, Any] = {
            "description": stripped or None,
            "description_source": "manual" if stripped else None,
            "description_updated_by": actor_id,
            "description_updated_at": datetime.now(UTC),
        }
        updated = await self._repo.update_with_optimistic_lock(
            metric.id, metric.row_version, **updates
        )
        await self._cache.invalidate(metric_code)

        logger.info(
            "metric_description_updated",
            metric_code=metric_code,
            actor_id=actor_id,
            cleared=not stripped,
        )
        return updated

    async def infer_metric_description(
        self,
        metric_code: str,
        actor_id: int,
        role: str,
        user_domain: str | None = None,
        force: bool = False,
    ) -> Metric:
        """用 LLM 推断指标业务描述并落库（TD §12.1，不触发版本/不参与口径变更）。

        与 ``update_metric_description`` 同语义：描述是运营层补充，不创建 MetricVersion、
        不设 PII 复核闸门；权限沿用 owner/admin + PDP write。

        Args:
            metric_code: 指标编码。
            actor_id: 操作人 ID。
            role: 操作人角色。
            user_domain: 操作人所属域（API 层传入）。
            force: 强制重新推断。默认 False 时若已存在 LLM 推断描述则短路返回
                （避免重复调用 LLM 造成耗时与成本浪费）；True 时忽略已有描述重新生成。

        Raises:
            NotFoundError: 指标不存在。
            AuthError: metric_owner 操作他人指标（越权）。
            BusinessError: PDP 无 write 权限，或 LLM 不可用/推断失败。
        """
        metric = await self.get_metric(metric_code)
        self._assert_owner_or_admin(metric, actor_id, role)

        decision = await self._gov_svc().check_metric_permission(
            metric_code=metric_code,
            action="write",
            user_id=actor_id,
            role=role,
            user_domain=user_domain,
        )
        if not decision.allow:
            raise BusinessError(
                decision.reason or "无权更新该指标描述",
                error_code=decision.error_code or "FORBIDDEN",
                ctx={"metric_code": metric_code, "actor_id": actor_id},
            )

        # 幂等短路：已存在 LLM 推断描述且未强制重新生成 → 直接返回，避免重复调 LLM
        if not force and metric.description_source == "llm" and metric.description:
            logger.info(
                "metric_description_infer_skipped_existing",
                metric_code=metric_code,
                actor_id=actor_id,
            )
            return metric

        inferred = await self._llm_infer_metric_description(metric)
        if inferred is None:
            raise BusinessError(
                "LLM 推断不可用：请检查 LLM 配置或稍后重试",
                error_code="LLM_INFER_UNAVAILABLE",
                ctx={"metric_code": metric_code},
            )

        updates: dict[str, Any] = {
            "description": inferred["description"],
            "description_source": "llm",
            "description_updated_by": actor_id,
            "description_updated_at": datetime.now(UTC),
        }
        updated = await self._repo.update_with_optimistic_lock(
            metric.id, metric.row_version, **updates
        )
        await self._cache.invalidate(metric_code)

        logger.info(
            "metric_description_inferred",
            metric_code=metric_code,
            actor_id=actor_id,
            confidence=inferred.get("confidence"),
        )
        return updated

    async def _build_llm_client(self) -> Any:
        """构建 LLM 客户端：优先 DB 配置（env 兜底参与路由），DB 读取失败回退 env 静态客户端。

        与 collector/ai 消费方一致，避免描述推断走已失效的 env 静态客户端
        （如 kilo.ai 模型下线 → 404 → LLM_INFER_UNAVAILABLE）。
        """
        try:
            from app.services.llm.config_service import LlmConfigService

            return await LlmConfigService(self._db).build_client()
        except Exception:  # noqa: BLE001 - DB 配置读取异常降级 env 静态客户端，不阻断推断
            logger.warning("llm_db_config_load_failed, fallback to env client", exc_info=True)
            from app.services.llm.client import build_llm_client

            return build_llm_client()

    async def _llm_infer_metric_description(self, metric: Metric) -> dict[str, Any] | None:
        """使用 LLM 推断指标业务描述，返回结构化结果（失败返回 None）。

        复用 ``LlmConfigService.build_client``（DB 配置优先 + 路由/熔断），
        解析走统一解析器 ``llm.parse.parse_description_result``；
        json_schema 强约束优先，失败降级 json_object，解析失败追加约束提示重试 1 次。
        """
        from app.services.llm.parse import parse_description_result

        client = None
        try:
            client = await self._build_llm_client()
            if not getattr(client, "enabled", False):
                return None

            definition = metric.definition_json or {}
            context_lines = [
                f"- 指标名称: {metric.name}",
                f"- 指标编码: {metric.metric_code}",
                f"- 所属域: {metric.domain}",
                f"- 指标类型: {metric.type}",
                f"- 粒度: {metric.granularity}",
                f"- 单位: {metric.unit}",
                f"- 聚合方式: {metric.aggregation}",
                f"- 时间语义: {metric.time_semantics}",
                f"- 数仓层: {metric.dw_layer}",
                f"- 口径定义: {json.dumps(definition, ensure_ascii=False)[:1500]}",
            ]
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是数据指标治理专家。根据指标元数据与口径定义，推断该指标的中文业务描述。\n"
                        "返回 JSON 格式：{\n"
                        '  "description": "指标的中文业务描述",\n'
                        '  "confidence": 0.0-1.0\n'
                        "}\n"
                        "要求：\n"
                        "1. 描述简洁准确，20-100字\n"
                        "2. 说明指标的业务含义、计算口径要点与使用场景\n"
                        "3. confidence < 0.5 表示不确定"
                    ),
                },
                {"role": "user", "content": "\n".join(context_lines)},
            ]

            for attempt in (0, 1):
                aug = messages
                if attempt:
                    aug = [*messages, {"role": "user", "content": _METRIC_STRICT_JSON_HINT}]
                for fmt in (_METRIC_DESC_RESPONSE_FORMAT, _METRIC_JSON_OBJECT_FORMAT):
                    try:
                        result = await client.chat(
                            aug, temperature=0.0, max_tokens=300, response_format=fmt
                        )
                    except Exception:  # noqa: BLE001 - LLM 网关错误按格式失败降级重试
                        continue
                    description, confidence = parse_description_result(result.get("content", ""))
                    if description is not None and confidence is not None:
                        return {"description": description, "confidence": confidence}
            logger.warning("llm_infer_metric_desc_all_formats_failed")
            return None
        except (TimeoutError, ConnectionError, OSError) as exc:
            logger.warning("llm_infer_metric_desc_timeout_error: %s", exc)
            return None
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("llm_infer_metric_desc_format_error: %s", exc)
            return None
        except RuntimeError as exc:
            logger.warning("llm_infer_metric_desc_runtime_error: %s", exc)
            return None
        except Exception:  # noqa: BLE001 - 兜底：推断是辅助能力，绝不阻断主流程
            logger.warning("llm_infer_metric_desc_unexpected_error", exc_info=True)
            return None
        finally:
            if client is not None:
                with suppress(Exception):  # 释放失败不阻断
                    await client.close()

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
        return await self.approve_metric(metric_code, approve_req, actor_id, role)

    async def submit_metric(
        self,
        metric_code: str,
        request: MetricSubmitRequest,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> Metric:
        """提交指标审核（DRAFT → REVIEW，对齐 FR-003）。

        Args:
            metric_code: 指标编码。
            request: 提交请求。
            actor_id: 操作人 ID。
            role: 操作人角色（PDP 域权限判定用）。
            user_domain: 操作人所属域（API 层传入，避免 service 内额外查 DB）。

        Returns:
            提交后的指标。

        Raises:
            NotFoundError: 指标不存在。
            AuthError: metric_owner 操作他人指标（越权）。
            BusinessError: PDP 拒绝（跨域/无权限）。
            ConflictError: 非法状态跃迁。
        """
        metric = await self.get_metric(metric_code)
        self._assert_owner_or_admin(metric, actor_id, role or "")

        # PDP 域权限闸门：提交审核须有 write 权限（同域或跨域 grant）。
        # skip_pii_gate=True：提交审核是 PII 合规流程入口——未复核的 PII 指标
        # 必须先进入 REVIEW 状态才能被合规复核，若在此处拦截 PII 将形成死锁。
        decision = await self._gov_svc().check_metric_permission(
            metric_code=metric_code,
            action="write",
            user_id=actor_id,
            role=role or "",
            user_domain=user_domain,
            skip_pii_gate=True,
        )
        if not decision.allow:
            raise BusinessError(
                decision.reason or "无权提交该指标审核",
                error_code=decision.error_code or "FORBIDDEN",
                ctx={"metric_code": metric_code, "actor_id": actor_id},
            )

        # 状态机校验：DRAFT→REVIEW；DEPRECATED→REVIEW（废弃指标重评审闭环，TD §13）。
        # 重评审时标记 is_resubmit，用于事件类型区分（metric.resubmitted）与废弃标记清除。
        is_resubmit = metric.status == "DEPRECATED"
        invalid = MetricStateMachine.validate_transition(metric.status, "REVIEW")
        if invalid is not None:
            raise ConflictError(invalid, error_code="INVALID_TRANSITION")

        # 口径完整性校验（FR-012 数据完整性）：提交评审的指标必须可被评审，
        # 空心指标（无表达式/无 SQL/无源表）进入 REVIEW 将导致评审人无法判断，
        # 且发布后血缘无 L3 边。DRAFT 允许骨架，REVIEW 前必须完整。
        _defn = metric.definition_json or {}
        _has_expr = bool(str(_defn.get("expression") or "").strip())
        _has_sql = bool(str(_defn.get("sql") or "").strip())
        _has_tables = bool(_defn.get("source_tables")) or bool(_defn.get("source_table"))
        if not (_has_expr or _has_sql or _has_tables):
            raise BusinessError(
                "指标口径尚未定义，请先在编辑中完善表达式/源表后提交评审",
                error_code="DEFINITION_INCOMPLETE",
            )

        # 评审指派解析与校验（TD §13）：user 类型须带 reviewer_id；domain 类型
        # 缺省用指标自身域；均不传则未指派（域管理员兜底评审）。
        reviewer_updates: dict[str, Any] = {
            "reviewer_id": None,
            "reviewer_type": None,
            "reviewer_domain": None,
        }
        rtype = request.reviewer_type
        if rtype == "user":
            if not request.reviewer_id:
                raise BusinessError(
                    "指定评审用户时须填写评审人",
                    error_code="REVIEWER_ASSIGN_INVALID",
                )
            reviewer_updates["reviewer_id"] = request.reviewer_id
            reviewer_updates["reviewer_type"] = "user"
        elif rtype == "domain":
            reviewer_updates["reviewer_type"] = "domain"
            reviewer_updates["reviewer_domain"] = request.reviewer_domain or metric.domain
        elif request.reviewer_id:
            # 兼容旧调用：仅传 reviewer_id（未显式声明类型）按 user 处理
            reviewer_updates["reviewer_id"] = request.reviewer_id
            reviewer_updates["reviewer_type"] = "user"

        updated = await self._repo.update_with_optimistic_lock(
            metric.id,
            metric.row_version,
            status="REVIEW",
            submitted_by=actor_id,
            # 重评审（DEPRECATED→REVIEW）：清除废弃标记，恢复为普通待审指标；
            # 审核通过后再走 REVIEW→PUBLISHED 恢复发布（状态闭环）。
            **(
                {"successor_code": None, "deprecated_at": None, "sunset_until": None}
                if is_resubmit
                else {}
            ),
            # 重新提审即清空历史驳回原因（生命周期闭环）：被驳回草稿经修改重提后
            # 不再残留"被驳回"标识；目录/详情据此区分"当前被驳回"与"曾驳回已重提"。
            reject_reason=None,
            reject_reviewer_id=None,
            rejected_at=None,
            **reviewer_updates,
        )
        await self._cache.invalidate(metric_code)

        # 发布提交事件：首次提交用 metric.submitted；废弃重评审用 metric.resubmitted（TD §13 闭环）
        submit_event = "metric.resubmitted" if is_resubmit else "metric.submitted"
        await self._publish_event(
            submit_event,
            {
                "metric_code": metric_code,
                "domain": metric.domain,
                "submitter_id": actor_id,
                "change_reason": request.change_reason,
                "reviewer_id": reviewer_updates["reviewer_id"],
                "reviewer_type": reviewer_updates["reviewer_type"],
                "reviewer_domain": reviewer_updates["reviewer_domain"],
            },
            actor_id=str(actor_id),
        )
        # 审批流定向闭环：定向通知「待审核」。优先通知被指定的评审人/域评审组；
        # 未指派时通知该域审核人（TD §13：发起评审联通通知中心）。
        assigned_reviewer = reviewer_updates["reviewer_id"]
        notify_targets = {
            "metric_code": metric_code,
            "domain": metric.domain,
            "submitter_id": actor_id,
            "to_reviewers": not assigned_reviewer,  # 已指定评审人时只通知本人，不广播全域
            "assigned_reviewer_id": assigned_reviewer,
            "payload": {
                "metric_code": metric_code,
                "domain": metric.domain,
                "change_reason": request.change_reason,
                "reviewer_id": reviewer_updates["reviewer_id"],
                "reviewer_type": reviewer_updates["reviewer_type"],
                "reviewer_domain": reviewer_updates["reviewer_domain"],
            },
        }
        await self._notify_metric_stakeholders(
            submit_event,
            "指标待评审" if assigned_reviewer else "指标待审核",
            **notify_targets,
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
        if invalid is not None:  # pragma: no cover - REVIEW→DRAFT 在状态机矩阵中恒合法，防御分支
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
        self,
        metric_code: str,
        request: MetricApproveRequest,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> Metric:
        """审核通过指标（REVIEW → PUBLISHED/EXPERIMENTAL，对齐 FR-004）。

        含 PII 门禁 + 依赖校验 + 状态机校验。
        metric.status 更新与 version.status 转正在同一事务中原子执行（对齐 FR-042）。

        Args:
            metric_code: 指标编码。
            request: 审核请求（含 mode/gray_tenant_ids/target_version）。
            actor_id: 操作人 ID。
            role: 操作人角色（platform_admin/domain_admin 豁免自审禁止）。
            user_domain: 操作人所属域（API 层传入，避免 service 内额外查 DB）。

        自审豁免：approve/reject 端点仅 platform_admin/domain_admin 可调用，管理员拥有
        最终审核权，允许审核自己提交的指标（小团队/单管理员场景的兜底）；
        普通角色传 role 后仍严格禁止自审。role 缺省（None）时按严格模式处理。

        Args:
            metric_code: 指标编码。
            request: 审核请求（含 mode/gray_tenant_ids/target_version）。
            actor_id: 操作人 ID。
            role: 操作人角色（platform_admin/domain_admin 豁免自审禁止）。

        Returns:
            审核通过后的指标。

        Raises:
            NotFoundError: 指标不存在。
            ConflictError: 非法状态跃迁。
            BusinessError: PII 未过合规审核 / 依赖校验失败 / 环检测失败 / 非管理员自审。
        """
        metric = await self.get_metric(metric_code)

        # 评审人身份校验（TD §13）：仅被指派评审人（或 platform_admin 兜底）可通过
        self._assert_reviewer_authorized(metric, actor_id, role or "", user_domain)

        # 自审禁止（对齐治理 COMPL-2）：提交人与审核人不得为同一人；管理员豁免
        if (
            role not in ("platform_admin", "domain_admin")
            and metric.submitted_by is not None
            and metric.submitted_by == actor_id
        ):
            raise BusinessError(
                "提交人与审核人不得为同一人（禁止自审）",
                error_code="SELF_REVIEW_BLOCKED",
                ctx={"metric_code": metric_code, "submitted_by": metric.submitted_by},
            )

        # PDP 域权限闸门：approve 须有 approve 权限（同域或跨域 grant）。
        # skip_pii_gate=True：PII 合规门禁由下方业务层统一处理（返回语义
        # 更清晰的 COMPLIANCE_BLOCKED），此处仅做域/角色校验。
        decision = await self._gov_svc().check_metric_permission(
            metric_code=metric_code,
            action="approve",
            user_id=actor_id,
            role=role or "metric_owner",
            user_domain=user_domain,
            skip_pii_gate=True,
        )
        if not decision.allow:
            raise BusinessError(
                decision.reason or "无权审核该指标",
                error_code=decision.error_code or "FORBIDDEN",
                ctx={"metric_code": metric_code, "actor_id": actor_id},
            )

        # 确定目标状态
        target_status = "PUBLISHED"
        extra_updates: dict[str, Any] = {}

        if request.mode == "experimental":
            target_status = "EXPERIMENTAL"
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
            # 审批通过（PUBLISHED/灰度 EXPERIMENTAL）即清空历史驳回原因：
            # 驳回已解决，不再残留"被驳回"状态（生命周期闭环）。
            reject_reason=None,
            reject_reviewer_id=None,
            rejected_at=None,
            **extra_updates,
        )

        # 版本转正：将指定版本标记为对应状态（对齐 FR-042：metric+version 原子）
        version_status = target_status  # PUBLISHED 或 EXPERIMENTAL
        await self._repo.mark_version_published(
            metric.id, target_version, now, status=version_status
        )

        await self._cache.invalidate(metric_code)

        # 发布审批通过事件（对齐 FR-014：lineage(Neo4j)/search(ES)/notify）。
        # 灰度发布（EXPERIMENTAL，仅指定租户试点）与标准发布（PUBLISHED）语义不同，
        # 事件/通知须区分，避免 stakeholders 收到「指标已通过」却实际仅灰度试点。
        is_gray = target_status == "EXPERIMENTAL"
        event_type = "metric.gray_published" if is_gray else "metric.approved"
        notify_title = "指标灰度发布" if is_gray else "指标已通过"
        event_payload: dict[str, Any] = {
            "metric_code": metric_code,
            "version": target_version,
            "type": metric.type,
            "domain": metric.domain,
            "definition_json": metric.definition_json,
            "mode": request.mode,
        }
        if is_gray:
            event_payload["gray_tenant_ids"] = request.gray_tenant_ids or []
        if metric.type in ("derived", "composite"):
            event_payload["dependencies"] = metric.definition_json.get("dependencies", [])

        await self._publish_event(
            event_type,
            event_payload,
            actor_id=str(actor_id),
        )
        # 审批流定向闭环：定向通知提交人「已通过/灰度发布」（独立 session，不依赖订阅）
        await self._notify_metric_stakeholders(
            event_type,
            notify_title,
            metric_code=metric_code,
            domain=metric.domain,
            submitter_id=metric.submitted_by,
            payload={
                "metric_code": metric_code,
                "version": target_version,
                "domain": metric.domain,
                "mode": request.mode,
            },
        )

        logger.info(
            "metric_approved",
            metric_code=metric_code,
            target_status=target_status,
            version=target_version,
            actor_id=actor_id,
            is_gray=is_gray,
        )
        return updated

    async def reject_metric(
        self,
        metric_code: str,
        request: MetricRejectRequest,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> Metric:
        """审核驳回指标（REVIEW → DRAFT，对齐 FR-005）。

        Args:
            metric_code: 指标编码。
            request: 驳回请求（含 reason）。
            actor_id: 操作人 ID。
            role: 操作人角色（platform_admin/domain_admin 豁免自审禁止，同 approve_metric）。
            user_domain: 操作人所属域（评审人身份校验用）。

        Returns:
            驳回后的指标。

        Raises:
            NotFoundError: 指标不存在。
            ConflictError: 非法状态跃迁。
            BusinessError: 非管理员自审。
        """
        metric = await self.get_metric(metric_code)

        # 评审人身份校验（TD §13）：仅被指派评审人（或 platform_admin 兜底）可打回
        self._assert_reviewer_authorized(metric, actor_id, role or "", user_domain)

        # 自审禁止（对齐治理 COMPL-2）：提交人与审核人不得为同一人；管理员豁免
        if (
            role not in ("platform_admin", "domain_admin")
            and metric.submitted_by is not None
            and metric.submitted_by == actor_id
        ):
            raise BusinessError(
                "提交人与审核人不得为同一人（禁止自审）",
                error_code="SELF_REVIEW_BLOCKED",
                ctx={"metric_code": metric_code, "submitted_by": metric.submitted_by},
            )

        # 状态机校验：REVIEW→DRAFT
        invalid = MetricStateMachine.validate_transition(metric.status, "DRAFT")
        if invalid is not None:
            raise ConflictError(invalid, error_code="INVALID_TRANSITION")

        updated = await self._repo.update_with_optimistic_lock(
            metric.id,
            metric.row_version,
            status="DRAFT",
            reject_reason=(request.reason or "").strip()[:500],
            reject_reviewer_id=actor_id,
            rejected_at=datetime.now(UTC).replace(tzinfo=None),
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
        # 审批流定向闭环：定向通知提交人「已驳回」（独立 session，不依赖订阅）
        await self._notify_metric_stakeholders(
            "metric.rejected",
            "指标已驳回",
            metric_code=metric_code,
            domain=metric.domain,
            submitter_id=metric.submitted_by,
            payload={
                "metric_code": metric_code,
                "domain": metric.domain,
                "reason": request.reason,
            },
        )

        logger.info(
            "metric_rejected",
            metric_code=metric_code,
            reason=request.reason,
            actor_id=actor_id,
        )
        return updated

    async def _notify_metric_stakeholders(
        self,
        event_type: str,
        title: str,
        *,
        metric_code: str,
        domain: str | None,
        submitter_id: int | None,
        to_reviewers: bool = False,
        assigned_reviewer_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """审批流定向通知（独立 session，不干扰业务事务；best-effort）。

        与 conflict.py ``notify_user`` 范式一致：IN_APP 定向送达，不依赖订阅偏好。
        - ``assigned_reviewer_id`` 非空：仅通知被指定的评审人（TD §13 指定评审闭环）；
        - ``to_reviewers=True``：通知该域下可审核角色（domain_admin/reviewer，active），
          供「指标待审核」场景（TD §5.5 审批流定向闭环）；
        - 否则通知指标提交人（submitted_by），供「已通过/已驳回」场景。
        失败仅告警，不阻断审批主流程。
        """
        from app.db.mysql import async_session_factory
        from app.services.notify.service import NotifyService

        targets: list[int] = []
        if assigned_reviewer_id is not None:
            # 指定评审人优先：仅通知该评审人（不含提交人本人）
            if submitter_id is None or assigned_reviewer_id != submitter_id:
                targets = [assigned_reviewer_id]
        elif to_reviewers:
            from sqlalchemy import select

            from app.models.user import User

            async with async_session_factory() as session:
                stmt = select(User.id).where(
                    User.status == "active",
                    User.role.in_(("domain_admin", "reviewer")),
                )
                if domain:
                    stmt = stmt.where(User.domain == domain)
                result = await session.execute(stmt)
                targets = [r[0] for r in result.all()]
            # 排除提交人本人（自审已被禁止，通知列表亦不应包含自己）
            if submitter_id is not None:
                targets = [uid for uid in targets if uid != submitter_id]
        elif submitter_id is not None:
            targets = [submitter_id]

        for uid in targets:
            async with async_session_factory() as session:
                try:
                    await NotifyService(session).notify_user(
                        user_id=uid,
                        event_type=event_type,
                        title=title,
                        payload=payload,
                    )
                except Exception as exc:
                    logger.warning(
                        "metric_stakeholder_notify_failed event_type=%s metric=%s user=%s err=%s",
                        event_type,
                        metric_code,
                        uid,
                        exc,
                    )

    async def _register_metric_lineage_full(self, metric: Metric) -> None:
        """注册指标完整血缘：表血缘(L3) + 指标间依赖边（best-effort）。

        1. 表级血缘：复用 LineageService.register_metric_from_definition
           解析 definition_json 的 source_table / source_tables，写入指标↔物理底表边。
        2. 指标间依赖血缘：解析 definition_json.dependencies（依赖指标编码列表），
           为每个依赖注册 ``metric:{dep} → metric:{code}`` 边；edge_type 按
           metric.type 映射（composite→COMPOSED_OF / derived→DERIVED_FROM，
           当前枚举未含 COMPOSED_OF 故二者统一 DERIVED_FROM，见 _METRIC_DEP_EDGE_TYPE）；
           atomic 无依赖，跳过。

        幂等：底层 LineageRepository.upsert_edge 按唯一键
        (source/target/edge_type/granularity) 去重，重复注册不产生重复边。
        失败仅告警，不阻断指标创建/发布/更新主流程。

        Args:
            metric: 指标 ORM 实体（读取 metric_code / type / definition_json）。
        """
        from app.services.lineage.repository import LineageRepository
        from app.services.lineage.service import LineageService

        try:
            lineage_svc = LineageService(self._db)
            # 1) 表级血缘（指标 ↔ 物理底表），不在此提交，交由外层事务统一提交
            await lineage_svc.register_metric_from_definition(metric, commit=False)

            # 2) 指标间依赖血缘（仅 derived/composite 有 dependencies）——
            # 表/维度/字段血缘已由 register_metric_from_definition 差异同步处理
            definition = metric.definition_json or {}
            if not isinstance(definition, dict):
                return
            dependencies = definition.get("dependencies") or []
            if not isinstance(dependencies, list) or metric.type == "atomic" or not dependencies:
                return
            edge_type = _METRIC_DEP_EDGE_TYPE.get(metric.type, "DERIVED_FROM")
            repo = LineageRepository(self._db)
            for dep_code in dependencies:
                if not isinstance(dep_code, str) or not dep_code:
                    continue
                await repo.upsert_edge(
                    source_node=f"metric:{dep_code}",
                    target_node=f"metric:{metric.metric_code}",
                    edge_type=edge_type,
                    granularity="L3",
                    provenance="metric_definition",
                    change_reason="metric_dependency",
                )
        except Exception:  # noqa: BLE001 - 血缘注册失败绝不阻断指标主流程
            logger.warning(
                "metric_lineage_register_failed",
                metric_code=metric.metric_code,
                metric_type=metric.type,
                exc_info=True,
            )

    async def _cleanup_metric_lineage(self, metric_code: str) -> None:
        """清理指标相关血缘边（best-effort，软删）。

        指标废弃 / 作废时，沿血缘将其关联边置 deleted_at，避免已失效指标仍参与
        下游影响分析（对齐 TD §12 血缘一致性）。软删保留审计上下文，可随指标恢复
        由血缘团队重新注册。

        Args:
            metric_code: 指标编码（节点 ``metric:{code}``）。
        """
        from app.services.lineage.service import LineageService

        try:
            deleted = await LineageService(self._db).delete_by_node(f"metric:{metric_code}")
            logger.info(
                "metric_lineage_cleaned",
                metric_code=metric_code,
                deleted_edges=deleted,
            )
        except Exception:  # noqa: BLE001 - 血缘清理失败绝不阻断指标废弃/作废主流程
            logger.warning(
                "metric_lineage_cleanup_failed",
                metric_code=metric_code,
                exc_info=True,
            )

    async def _restore_metric_lineage(self, metric_code: str) -> None:
        """恢复指标相关软删血缘边（best-effort，回收站恢复时对称重建）。

        与 ``_cleanup_metric_lineage`` 对称：清除 ``deleted_at`` 使已失效边重新
        参与影响分析，保证恢复的指标血缘立即可见（TD §12 血缘一致性）。

        Args:
            metric_code: 指标编码（节点 ``metric:{code}``）。
        """
        from app.services.lineage.service import LineageService

        try:
            restored = await LineageService(self._db).restore_by_node(f"metric:{metric_code}")
            logger.info(
                "metric_lineage_restored",
                metric_code=metric_code,
                restored_edges=restored,
            )
        except Exception:  # noqa: BLE001 - 血缘恢复失败绝不阻断指标恢复主流程
            logger.warning(
                "metric_lineage_restore_failed",
                metric_code=metric_code,
                exc_info=True,
            )

    async def notify_lineage_impacted_owners(self, source_node: str) -> int:
        """上游数据源结构变更时，沿下游血缘通知受影响指标的 Owner（P1 闭环）。

        查询 ``source_node`` 的下游血缘边，过滤出 ``target_node`` 为 ``metric:`` 的
        受影响指标，反查各指标 owner_id，经 NotifyService.notify_user 定向通知
        「上游数据源结构变更，你的指标 X 可能受影响」。每个 owner 一次性收到其名下
        全部受影响指标清单（去重）。

        触发点由调用方整合（collector 的 schema drift 处理 / app/api/lineage.py），
        本方法仅实现能力；所有 DB / 通知失败均 best-effort，不影响上游变更主流程。

        Args:
            source_node: 发生结构变更的上游节点（如 ``table:db.orders``）。

        Returns:
            成功送达通知的 Owner 数（0 表示无受影响指标或查询失败）。
        """
        from app.db.mysql import async_session_factory
        from app.services.lineage.schemas import LineageImpactParams
        from app.services.lineage.service import LineageService
        from app.services.notify.service import NotifyService

        try:
            edges = await LineageService(self._db).query_impact(
                LineageImpactParams(node=source_node, direction="downstream", max_hops=5)
            )
        except Exception:  # noqa: BLE001 - 影响分析失败不阻断上游变更
            logger.warning(
                "lineage_impact_query_failed", source_node=source_node, exc_info=True
            )
            return 0

        # 收集受影响指标及其 owner（按 code 去重）
        impacted_by_owner: dict[int, list[str]] = {}
        seen_codes: set[str] = set()
        for edge in edges:
            if not edge.target_node.startswith("metric:"):
                continue
            code = edge.target_node[len("metric:") :]
            if code in seen_codes:
                continue
            seen_codes.add(code)
            metric = await self._repo.get_by_code(code)
            if metric is None:
                continue
            impacted_by_owner.setdefault(metric.owner_id, []).append(code)

        if not impacted_by_owner:
            return 0

        # 各 owner 定向通知（独立会话，不污染上游变更事务）
        notified = 0
        for owner_id, codes in impacted_by_owner.items():
            async with async_session_factory() as session:
                try:
                    await NotifyService(session).notify_user(
                        user_id=owner_id,
                        event_type="lineage.change_impacted",
                        title="血缘变更影响",
                        payload={
                            "source_node": source_node,
                            "impacted_metrics": codes,
                            "count": len(codes),
                        },
                    )
                    notified += 1
                except Exception:  # noqa: BLE001 - 单个 owner 通知失败不阻断其余
                    logger.warning(
                        "lineage_impacted_notify_failed",
                        owner_id=owner_id,
                        source_node=source_node,
                        impacted_metrics=codes,
                        exc_info=True,
                    )
        return notified

    async def deprecate_metric(
        self,
        metric_code: str,
        successor_code: str | None,
        actor_id: int,
        role: str,
        user_domain: str | None = None,
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
        # PDP 域权限闸门：deprecate 为写操作，domain_admin 须同域（对齐 update/approve 的
        # check_metric_permission 域校验，修复 domain_admin 可跨域废弃的域隔离漏洞）
        decision = await self._gov_svc().check_metric_permission(
            metric_code=metric_code,
            action="write",
            user_id=actor_id,
            role=role,
            user_domain=user_domain,
            skip_pii_gate=True,
        )
        if not decision.allow:
            raise BusinessError(
                decision.reason or "无权废弃该指标",
                error_code=decision.error_code or "FORBIDDEN",
            )

        # 状态机校验：仅 PUBLISHED 可废弃（对齐 FR-002）
        invalid = MetricStateMachine.validate_transition(metric.status, "DEPRECATED")
        if invalid is not None:
            raise ConflictError(invalid, error_code="INVALID_TRANSITION")

        # 替代指标校验：存在且已发布（对齐 FR-039）。
        # 空字符串视为「未指定替代」——前端未填替代指标时不应报「替代指标不存在:（空）」。
        if successor_code is not None and not str(successor_code).strip():
            successor_code = None
        if successor_code is not None:
            # 自废弃防护：替代指标不得为自身（废弃指标指向自己作替代属语义矛盾，
            # 详情页废弃链会展示"替代指标"指向自身）。
            if str(successor_code) == metric_code:
                raise BusinessError(
                    "替代指标不能为指标自身，请指定其他已发布指标或留空",
                    error_code="VALIDATION_ERROR",
                )
            successor = await self._repo.get_by_code(successor_code)
            if successor is None:
                raise NotFoundError(f"替代指标不存在: {successor_code}")
            if successor.status != "PUBLISHED":
                raise BusinessError(
                    f"替代指标 {successor_code} 未发布，无法作为替代",
                    error_code="VALIDATION_ERROR",
                )

        from datetime import timedelta

        sunset_days = self._settings.metric_sunset_days  # 对齐 TD §13，可配置化覆盖
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

        # 废弃即失效：清理指标相关血缘边（best-effort，避免失效指标仍参与下游影响分析）
        await self._cleanup_metric_lineage(metric_code)

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

    async def promote_metric(self, metric_code: str, actor_id: int) -> Metric:
        """灰度全量发布（EXPERIMENTAL → PUBLISHED，对齐 FR-020）。

        清除 gray_tenant_ids，将指标与版本状态从 EXPERIMENTAL 升为 PUBLISHED，
        发布 metric.promoted 事件 → lineage(Neo4j)/search(ES)/notify。

        Args:
            metric_code: 指标编码。
            actor_id: 操作人 ID。

        Returns:
            全量发布后的指标。

        Raises:
            NotFoundError: 指标不存在。
            ConflictError: 非法状态跃迁（非 EXPERIMENTAL）。
        """
        metric = await self.get_metric(metric_code)

        # PII 合规闸门（COMPL-1）：灰度全量发布到生产前，PII 指标必须已复核
        if metric.pii_flag and not metric.compliance_reviewed:
            raise BusinessError(
                "PII 指标未通过合规复核，禁止全量发布",
                error_code="FORBIDDEN_PII",
                ctx={"metric_code": metric_code},
            )

        # 状态机校验：EXPERIMENTAL→PUBLISHED
        invalid = MetricStateMachine.validate_transition(metric.status, "PUBLISHED")
        if invalid is not None:
            raise ConflictError(invalid, error_code="INVALID_TRANSITION")

        now = datetime.now(UTC)

        # 清除灰度白名单 + 状态升为 PUBLISHED
        updated = await self._repo.update_with_optimistic_lock(
            metric.id,
            metric.row_version,
            status="PUBLISHED",
            gray_tenant_ids=None,
        )

        # 版本状态同步升为 PUBLISHED
        await self._repo.mark_version_published(metric.id, metric.version, now, status="PUBLISHED")

        await self._cache.invalidate(metric_code)

        # 全量发布即正式生效：补全/刷新指标完整血缘（best-effort）
        await self._register_metric_lineage_full(metric)

        # 发布 metric.promoted 事件（对齐 FR-020：lineage+search+notify）
        await self._publish_event(
            "metric.promoted",
            {
                "metric_code": metric_code,
                "domain": metric.domain,
                "version": metric.version,
                "type": metric.type,
                "definition_json": metric.definition_json,
                "promoted_at": now.isoformat(),
            },
            actor_id=str(actor_id),
        )

        logger.info(
            "metric_promoted",
            metric_code=metric_code,
            actor_id=actor_id,
        )
        return updated

    async def rollback_metric(self, metric_code: str, actor_id: int) -> Metric:
        """灰度回滚（EXPERIMENTAL → 回退上一 PUBLISHED 版本，对齐 FR-020）。

        EXPERIMENTAL 版本标记 ARCHIVED，指标状态回到 PUBLISHED，
        effective_version 回退到上一个 PUBLISHED 版本。
        发布 metric.rolled_back 事件 → notify+audit。

        Args:
            metric_code: 指标编码。
            actor_id: 操作人 ID。

        Returns:
            回滚后的指标。

        Raises:
            NotFoundError: 指标不存在。
            ConflictError: 非法状态跃迁 / 无上一 PUBLISHED 版本可回退。
        """
        metric = await self.get_metric(metric_code)

        # 状态机校验：EXPERIMENTAL→PUBLISHED (rollback)
        invalid = MetricStateMachine.validate_transition(metric.status, "PUBLISHED")
        if invalid is not None:
            raise ConflictError(invalid, error_code="INVALID_TRANSITION")

        # 查找上一 PUBLISHED 版本
        versions = await self._repo.list_versions(metric.id)
        prev_published = None
        for v in versions:
            if v.status == "PUBLISHED" and v.version != metric.version:
                prev_published = v
                break

        if prev_published is None:
            raise ConflictError(
                "无上一 PUBLISHED 版本可回退",
                error_code="NO_PREVIOUS_PUBLISHED_VERSION",
            )

        # 将 EXPERIMENTAL 版本标记为 ARCHIVED
        from sqlalchemy import update

        stmt = (
            update(MetricVersion)
            .where(
                MetricVersion.metric_id == metric.id,
                MetricVersion.version == metric.version,
            )
            .values(status="ARCHIVED")
        )
        await self._db.execute(stmt)

        now = datetime.now(UTC)

        # 回退指标状态为 PUBLISHED + 清除灰度白名单 + 回退 effective_version
        updated = await self._repo.update_with_optimistic_lock(
            metric.id,
            metric.row_version,
            status="PUBLISHED",
            gray_tenant_ids=None,
            effective_version=prev_published.version,
        )

        await self._cache.invalidate(metric_code)

        # 发布 metric.rolled_back 事件（对齐 FR-020：notify+audit）
        await self._publish_event(
            "metric.rolled_back",
            {
                "metric_code": metric_code,
                "domain": metric.domain,
                "rolled_back_from_version": metric.version,
                "rolled_back_to_version": prev_published.version,
                "rolled_back_at": now.isoformat(),
            },
            actor_id=str(actor_id),
        )

        logger.info(
            "metric_rolled_back",
            metric_code=metric_code,
            from_version=metric.version,
            to_version=prev_published.version,
            actor_id=actor_id,
        )
        return updated

    async def emergency_publish_metric(
        self,
        metric_code: str,
        request: MetricEmergencyPublishRequest,
        actor_id: int,
        role: str,
    ) -> Metric:
        """紧急发布快通道（DRAFT → PUBLISHED 跳过 REVIEW，对齐 FR-022/FR-023/FR-024）。

        仅 domain_admin 可执行；PII 指标紧急发布仍须合规门禁（不可跳过）；
        合规官不可达时仅 INTERNAL 分级发布。

        Args:
            metric_code: 指标编码。
            request: 紧急发布请求（含 reason/target_version）。
            actor_id: 操作人 ID。
            role: 操作人角色。

        Returns:
            紧急发布后的指标。

        Raises:
            AuthError: 非 domain_admin 角色。
            NotFoundError: 指标不存在。
            ConflictError: 非法状态跃迁。
            BusinessError: PII 未过合规审核。
        """
        # 角色校验：仅 domain_admin / platform_admin 可紧急发布
        if role not in ("domain_admin", "platform_admin"):
            raise AuthError(
                "紧急发布仅 domain_admin / platform_admin 可执行",
                error_code="FORBIDDEN",
                ctx={"role": role},
            )

        metric = await self.get_metric(metric_code)

        # 紧急发布状态校验：仅 DRAFT/REVIEW 可紧急发布（跳过常规 approve 流程）。
        # 不能用 MetricStateMachine.validate_transition("DRAFT", "PUBLISHED")——
        # 该跃迁不在常规矩阵中（非法），而紧急发布语义就是"跳 REVIEW"（FR-022）。
        if metric.status not in ("DRAFT", "REVIEW"):
            raise ConflictError(
                f"紧急发布仅支持 DRAFT/REVIEW 状态，当前 {metric.status}",
                error_code="INVALID_TRANSITION",
            )

        # PII 门禁不可跳过（对齐 FR-024：含 PII 指标紧急发布仍须合规复核）
        if metric.pii_flag and not metric.compliance_reviewed:
            # FR-024 合规官不可达降级路径：查询是否有活跃 compliance_officer
            has_officer = await self._has_active_compliance_officer(metric.domain)
            if has_officer:
                raise BusinessError(
                    "含 PII 指标紧急发布须先通过合规审核（合规门禁不可跳过，FR-024）",
                    error_code="COMPLIANCE_BLOCKED",
                )
            # 合规官不可达：允许 INTERNAL 分级降级发布（FR-024 降级路径）
            if metric.serving_mode != "INTERNAL":
                raise BusinessError(
                    "含 PII 指标紧急发布须先通过合规审核；"
                    "合规官当前不可达，仅允许 INTERNAL 分级降级发布（FR-024）",
                    error_code="COMPLIANCE_UNREACHABLE_DOWNGRADE",
                )
            logger.warning(
                "compliance_officer_unreachable_internal_downgrade",
                metric_code=metric_code,
                domain=metric.domain,
                reason="合规官不可达，降级为 INTERNAL 分级发布",
            )

        # 定位待发布版本
        target_version = request.target_version or metric.version
        version_obj = await self._repo.get_version(metric.id, target_version)
        if version_obj is None:
            raise NotFoundError(f"版本不存在: {target_version}")

        # 同一事务中原子更新：metric.status + 紧急发布标记
        now = datetime.now(UTC)
        updated = await self._repo.update_with_optimistic_lock(
            metric.id,
            metric.row_version,
            status="PUBLISHED",
            approver_id=actor_id,
            effective_version=target_version,
            emergency_publish=True,
            emergency_reason=request.reason,
        )

        # 版本转正
        await self._repo.mark_version_published(metric.id, target_version, now, status="PUBLISHED")

        await self._cache.invalidate(metric_code)

        # 发布 metric.emergency_published 事件（对齐 FR-022：audit EMERGENCY_PUBLISH 标记）
        await self._publish_event(
            "metric.emergency_published",
            {
                "metric_code": metric_code,
                "domain": metric.domain,
                "version": target_version,
                "type": metric.type,
                "emergency_reason": request.reason,
                "emergency_published_at": now.isoformat(),
            },
            actor_id=str(actor_id),
        )

        # 审计 EMERGENCY_PUBLISH 标记
        await self._write_audit(
            actor_id=actor_id,
            action="EMERGENCY_PUBLISH",
            entity_type="metric_definition",
            entity_id=metric_code,
            detail={
                "emergency_reason": request.reason,
                "version": target_version,
                "skipped_review": True,
            },
        )

        logger.info(
            "metric_emergency_published",
            metric_code=metric_code,
            reason=request.reason,
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

        # 软删（作废）即失效：清理指标相关血缘边（best-effort）
        await self._cleanup_metric_lineage(metric_code)

        logger.info("metric_deleted", metric_code=metric_code, actor_id=actor_id)
        return metric

    async def restore_metric(self, metric_code: str, actor_id: int, role: str) -> Metric:
        """恢复软删指标（回收站恢复，仅 DRAFT 且已删状态）。

        清除 deleted_at 使指标重新进入正常列表；血缘边随后续指标更新/发布
        由 ``_register_metric_lineage_full`` 重新注册（对齐 TD §12 血缘一致性）。

        Args:
            metric_code: 指标编码。
            actor_id: 操作人 ID。
            role: 操作人角色。

        Returns:
            恢复后的指标。

        Raises:
            NotFoundError: 指标不存在。
            BusinessError: 指标未处于已删状态 / 非 DRAFT / 无恢复权限。
        """
        metric = await self._repo.get_archived_by_code(metric_code)
        if metric is None:
            raise NotFoundError(f"指标不存在: {metric_code}")
        if metric.deleted_at is None:
            raise BusinessError(
                f"指标 {metric_code} 未处于已删除状态，无需恢复",
                error_code="VALIDATION_ERROR",
            )
        if metric.status != "DRAFT":
            raise BusinessError(
                f"仅 DRAFT 状态的已删指标可恢复，当前状态 {metric.status}",
                error_code="VALIDATION_ERROR",
            )
        # 权限：仅平台管理员或原 owner（对齐删除语义；PDP 由 API 层角色门禁兜底）
        if role != "platform_admin" and metric.owner_id != actor_id:
            raise BusinessError(
                "仅平台管理员或指标原 Owner 可恢复",
                error_code="FORBIDDEN",
            )

        await self._repo.restore_metric(metric.id)
        await self._cache.invalidate(metric_code)
        # 对称恢复血缘：删除时 _cleanup_metric_lineage 软删了相关边，恢复时一并还原
        # （否则恢复的指标血缘为空直到下次编辑/发布，TD §12 血缘一致性）
        await self._restore_metric_lineage(metric_code)
        logger.info("metric_restored", metric_code=metric_code, actor_id=actor_id, role=role)
        return metric

    async def review_compliance(self, metric_code: str, actor_id: int, role: str) -> Metric:
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
        # S-06 修复：非 PII 指标无需合规复核
        if not metric.pii_flag:
            raise BusinessError(
                "非 PII 指标无需合规复核",
                error_code="PII_FLAG_REQUIRED",
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

        Raises:
            NotFoundError: 指标不存在（METRIC_ARCHIVED 表示已因仲裁作废）。
        """
        # 与详情/对比/健康读路径一致：命中「软删 + successor」的作废指标时返回
        # 结构化 METRIC_ARCHIVED（携带胜方指针），而非裸「指标不存在」——详情页
        # 并行加载 versions 时若裸 404 会覆盖友好引导（跨服务一致性）。
        metric = await self._get_metric_for_compare(metric_code)
        return await self._repo.list_versions(metric.id)

    # ---- PENDING_VERSION 版本确认期（FR-007/FR-008）----

    async def confirm_version(self, metric_code: str, version: int, consumer_id: int) -> Metric:
        """消费方确认版本（FR-007）。

        将当前消费方的 PENDING 记录置为 CONFIRMED；当该版本全部消费方确认后，
        版本转正（mark_version_published）并更新 metric.effective_version。

        Args:
            metric_code: 指标编码。
            version: 待确认版本号。
            consumer_id: 消费方用户 ID。

        Returns:
            更新后的指标。

        Raises:
            ConflictError: 无待确认记录或已确认。
        """
        metric = await self.get_metric(metric_code)
        confirmations = await self._repo.get_pending_confirmations(metric.id, version)
        if not confirmations:
            raise ConflictError(
                f"该版本 {version} 无待确认记录", error_code="NO_PENDING_CONFIRMATION"
            )
        mine = next((c for c in confirmations if c.consumer_id == consumer_id), None)
        if mine is None:
            raise ConflictError(
                "当前用户无该版本的待确认记录", error_code="NO_PENDING_CONFIRMATION"
            )
        if mine.status == "REJECTED":
            # 已拒绝的消费方不可再次确认：拒绝决定不可静默撤销，
            # 防止将已 CANCELLED 的版本重新转正（状态机矛盾）。
            raise ConflictError(
                "该版本已被您拒绝，不可再次确认", error_code="NO_PENDING_CONFIRMATION"
            )
        if mine.status == "CONFIRMED":
            return metric  # 幂等：已确认直接返回
        await self._repo.update_confirmation_status(mine.id, "CONFIRMED")

        # 全部确认后版本转正：应用新口径到主表 + 转正版本（同一事务）
        all_confirmed = all(
            c.status in ("CONFIRMED", "TIMEOUT_ACCEPTED") or c.id == mine.id for c in confirmations
        )
        if all_confirmed:
            try:
                return await self._promote_pending_version(metric, version)
            except ConflictError:
                # 乐观锁冲突 → 回滚确认状态，让调用方重试
                await self._repo.update_confirmation_status(mine.id, "PENDING")
                raise
        return metric

    async def auto_accept_timeout(self, metric_id: int, version: int) -> Metric | None:
        """超时自动接受：将仍为 PENDING 的超时确认记录置 TIMEOUT_ACCEPTED，
        全部确认/超时接受后应用新口径并转正（供定时任务调用）。

        Args:
            metric_id: 指标 ID。
            version: 版本号。

        Returns:
            转正后的指标；尚未全部确认时返回 None。
        """
        metric = await self._repo.get_by_id(metric_id)
        if metric is None:
            raise NotFoundError(f"指标不存在: id={metric_id}")
        confirmations = await self._repo.get_pending_confirmations(metric_id, version)
        if not confirmations:
            return None
        for c in confirmations:
            if c.status == "PENDING":
                await self._repo.update_confirmation_status(c.id, "TIMEOUT_ACCEPTED")
        # 重新读取以反映最新状态；全部确认/超时接受即转正
        # （含无 PENDING 可标记但已全部确认的恢复场景：旧缺陷遗留未转正的版本）
        confirmations = await self._repo.get_pending_confirmations(metric_id, version)
        if all(c.status in ("CONFIRMED", "TIMEOUT_ACCEPTED") for c in confirmations):
            return await self._promote_pending_version(metric, version)
        return None

    async def _promote_pending_version(self, metric: Metric, version: int) -> Metric:
        """PENDING_VERSION 全部确认/超时接受后的转正：把版本口径同步到主表。

        旧实现仅置 ``effective_version`` 并标记版本发布，主表 ``definition_json`` /
        ``version`` 仍为旧值——消费方读主表拿到旧口径，破坏性变更经确认后
        永不生效（PENDING_VERSION 全链路空转）。本方法补齐主表同步：

        - 应用版本记录的 ``definition_json``（新口径）；
        - 应用 diff_json 中 top-level 破坏性字段（granularity/unit）的 after 值；
        - 递增主表 ``version`` 至目标版本，标记版本 PUBLISHED，失效缓存。

        Args:
            metric: 已加载的指标对象。
            version: 待转正的版本号。

        Returns:
            转正后的指标。

        Raises:
            NotFoundError: 版本不存在。
            ConflictError: 乐观锁冲突。
        """
        version_obj = await self._repo.get_version(metric.id, version)
        if version_obj is None:
            raise NotFoundError(f"版本不存在: {version}")

        updates: dict[str, Any] = {
            "effective_version": version,
            "version": version,
        }
        if version_obj.definition_json is not None:
            updates["definition_json"] = version_obj.definition_json
        # top-level 破坏性字段：diff_json 的 after 值回写主表
        for field, diff in (version_obj.diff_json or {}).items():
            if field in BREAKING_TOP_LEVEL_FIELDS and isinstance(diff, dict) and "after" in diff:
                updates[field] = diff["after"]

        updated = await self._repo.update_with_optimistic_lock(
            metric.id, metric.row_version, **updates
        )
        await self._repo.mark_version_published(metric.id, version, datetime.now(UTC))
        await self._cache.invalidate(metric.metric_code)
        # 转正后新口径已生效 → 触发血缘差异同步（PENDING 期 update_metric 已延迟注册，
        # 由本处在新口径生效时按版本口径注册——保证血缘始终与「生效口径」一致）
        try:
            await self._register_metric_lineage_full(updated)
        except Exception as exc:  # noqa: BLE001 - best-effort 不阻断转正
            logger.warning(
                "pending_promote_lineage_failed",
                metric_code=metric.metric_code,
                version=version,
                error=str(exc),
            )
        logger.info(
            "pending_version_promoted",
            metric_id=metric.id,
            metric_code=metric.metric_code,
            version=version,
            definition_synced=version_obj.definition_json is not None,
        )
        return updated

    async def reject_version(
        self, metric_code: str, version: int, reason: str, consumer_id: int
    ) -> Metric:
        """消费方拒绝版本（FR-007）。

        任一消费方拒绝则版本取消（REJECTED），旧版本保持 CURRENT，不转正。

        Args:
            metric_code: 指标编码。
            version: 被拒版本号。
            reason: 拒绝原因。
            consumer_id: 消费方用户 ID。

        Returns:
            更新后的指标（保持原有效版本）。

        Raises:
            ConflictError: 无待确认记录。
        """
        metric = await self.get_metric(metric_code)
        confirmations = await self._repo.get_pending_confirmations(metric.id, version)
        if not confirmations:
            raise ConflictError(
                f"该版本 {version} 无待确认记录", error_code="NO_PENDING_CONFIRMATION"
            )
        mine = next((c for c in confirmations if c.consumer_id == consumer_id), None)
        if mine is None:
            raise ConflictError(
                "当前用户无该版本的待确认记录", error_code="NO_PENDING_CONFIRMATION"
            )
        await self._repo.update_confirmation_status(mine.id, "REJECTED", reason=reason)
        # 与 PendingVersionManager.reject 语义对齐：被拒版本置 CANCELLED，
        # 防止后续被确认/超时逻辑错误处理（旧实现只改确认状态，版本滞留 PENDING）
        from sqlalchemy import update as sa_update

        from app.models.metric_version import MetricVersion

        await self._db.execute(
            sa_update(MetricVersion)
            .where(
                MetricVersion.metric_id == metric.id,
                MetricVersion.version == version,
            )
            .values(status="CANCELLED")
        )
        return metric

    async def extend_version(self, metric_code: str, version: int) -> Metric:
        """Owner 请求版本确认延期（FR-008，+7 天，最多延期 1 次）。

        Args:
            metric_code: 指标编码。
            version: 待延期版本号。

        Returns:
            更新后的指标。

        Raises:
            ConflictError: 无待确认记录或已延期满 1 次。
        """
        metric = await self.get_metric(metric_code)
        confirmations = await self._repo.get_pending_confirmations(metric.id, version)
        if not confirmations:
            raise ConflictError(
                f"该版本 {version} 无待确认记录", error_code="NO_PENDING_CONFIRMATION"
            )
        if any(c.extension_count >= 1 for c in confirmations):
            raise ConflictError(
                "版本确认已延期满 1 次，不可再延期", error_code="EXTEND_LIMIT_REACHED"
            )
        for c in confirmations:
            new_deadline = (c.deadline or datetime.now(UTC)) + timedelta(days=7)
            await self._repo.extend_confirmation_deadline(c.id, new_deadline)
        return metric

    # ---- 内部方法 ----

    def _assert_owner_or_admin(self, metric: Metric, actor_id: int, role: str) -> None:
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

    def _assert_reviewer_authorized(
        self,
        metric: Metric,
        actor_id: int,
        role: str,
        user_domain: str | None,
    ) -> None:
        """评审人身份校验：仅被指派评审人可通过/打回指标（TD §13 治理闭环）。

        - ``platform_admin``：始终可审（最终兜底）。
        - ``reviewer_type=user``：仅 ``reviewer_id`` 指定的用户可审。
        - ``reviewer_type=domain``：仅该域 ``domain_admin``/``reviewer`` 角色用户可审。
        - 未指派：``domain_admin`` 兜底可审（保持既有语义）。

        端点角色门禁已放宽至含 ``reviewer``，此处为服务层最终判定；
        跨域指派（域不匹配）由 PDP 的域/授权闸门再行校验，双层防御。

        Args:
            metric: 指标对象。
            actor_id: 操作人 ID。
            role: 操作人角色。
            user_domain: 操作人所属域。

        Raises:
            AuthError: 非被指派评审人。
        """
        if role == "platform_admin":
            return

        if metric.reviewer_type == "user" and metric.reviewer_id is not None:
            if actor_id != metric.reviewer_id:
                raise AuthError(
                    "该指标已指派给指定评审人，仅被指派者可通过/打回",
                    error_code="FORBIDDEN_REVIEWER",
                    ctx={"metric_code": metric.metric_code, "reviewer_id": metric.reviewer_id},
                )
            return

        if metric.reviewer_type == "domain" and metric.reviewer_domain:
            if role not in ("domain_admin", "reviewer"):
                raise AuthError(
                    "该指标已指派给域评审组，仅域管理员/评审员可通过/打回",
                    error_code="FORBIDDEN_REVIEWER",
                    ctx={
                        "metric_code": metric.metric_code,
                        "reviewer_domain": metric.reviewer_domain,
                    },
                )
            if user_domain != metric.reviewer_domain:
                raise AuthError(
                    f"仅 {metric.reviewer_domain} 域评审组成员可评审该指标",
                    error_code="FORBIDDEN_REVIEWER",
                    ctx={"metric_code": metric.metric_code, "user_domain": user_domain},
                )
            return

        # 未指派：域管理员兜底（保持既有"仅管理角色可审"语义）
        if role != "domain_admin":
            raise AuthError(
                "未指派评审人，仅域管理员可评审该指标",
                error_code="FORBIDDEN_REVIEWER",
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
            # 发布即清空历史驳回原因（生命周期闭环）：指标一经发布不再残留
            # "被驳回"历史状态，避免已发布指标仍显示驳回标识。
            reject_reason=None,
            reject_reviewer_id=None,
            rejected_at=None,
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
        # 依赖项用集合比较（顺序无关）
        for field in BREAKING_DEF_FIELDS:
            old_val = old_def.get(field)
            new_val = new_def.get(field)
            if field == "dependencies":
                if set(old_val or []) != set(new_val or []):
                    return True
            elif old_val != new_val:
                return True
        return False

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
            # 依赖项用集合比较（顺序无关）
            if key == "dependencies":
                if set(old_val or []) == set(new_val or []):
                    continue
            elif old_val == new_val:
                continue
            diff[key] = {
                "before": old_val,
                "after": new_val,
                "change_type": ("BREAKING" if key in BREAKING_DEF_FIELDS else "UPDATE"),
            }
        return diff

    # ---- US8: 健康度评分 ----

    async def get_metric_health(self, metric_code: str) -> Any:
        """获取指标健康度评分。

        Args:
            metric_code: 指标编码。

        Returns:
            健康度评分对象。

        Raises:
            NotFoundError: 指标不存在。
        """
        from app.services.semantic.health_scorer import HealthScorer

        metric = await self._repo.get_by_code(metric_code)
        if metric is None:
            # 与详情读路径一致：命中「软删 + successor」的作废指标时返回结构化
            # METRIC_ARCHIVED（携带胜方指针），而非对历史链接直出裸「指标不存在」。
            archived = await self._repo.get_archived_by_code(metric_code)
            if archived is not None and archived.successor_code:
                raise NotFoundError(
                    f"指标已因口径裁决作废: {metric_code}",
                    error_code=ErrorCode.METRIC_ARCHIVED,
                    ctx={
                        "metric_code": metric_code,
                        "successor_code": archived.successor_code,
                        "arbitration_mark": archived.arbitration_mark,
                    },
                )
            raise NotFoundError(f"指标不存在: {metric_code}")
        scorer = HealthScorer(self._db)
        health = await scorer.calculate(metric.id)
        # 红橙指标进整改待办
        if health.level in ("WARNING", "CRITICAL"):
            await self._publish_event(
                "metric.health_critical",
                {
                    "metric_code": metric_code,
                    "score": health.score,
                    "level": health.level,
                    "missing_dimensions": health.missing_dimensions,
                },
                actor_id="system",
            )
        return health

    # ---- US9: 指标对比 ----

    async def _get_metric_for_compare(self, metric_code: str) -> Metric:
        """读取用于对比的指标；对已作废指标返回友好 METRIC_ARCHIVED。

        对比弹窗由冲突仲裁/差异查看触发，关联指标可能已被上一轮仲裁软删作废
        （deleted_at + successor）。此时不应抛裸「指标不存在」，而应复用详情页
        的 METRIC_ARCHIVED 错误码（携带胜方 successor），供前端渲染
        「已作废 → 查看权威」引导，保证冲突/指标跨服务状态一致可读。

        Raises:
            NotFoundError: 指标不存在（METRIC_ARCHIVED 表示已因仲裁作废）。
        """
        metric = await self._repo.get_by_code(metric_code)
        if metric is not None:
            return metric
        archived = await self._repo.get_archived_by_code(metric_code)
        if archived is not None and archived.successor_code:
            raise NotFoundError(
                f"指标已因口径裁决作废: {metric_code}",
                error_code=ErrorCode.METRIC_ARCHIVED,
                ctx={
                    "metric_code": metric_code,
                    "successor_code": archived.successor_code,
                    "arbitration_mark": archived.arbitration_mark,
                },
            )
        raise NotFoundError(f"指标不存在: {metric_code}")

    async def compare_metrics(self, code_a: str, code_b: str) -> dict[str, Any]:
        """两指标关键字段并排对比。

        Args:
            code_a: 指标A编码。
            code_b: 指标B编码。

        Returns:
            并排对比结果，含差异标记。

        Raises:
            NotFoundError: 指标不存在（METRIC_ARCHIVED 表示已因仲裁作废）。
        """
        # 权限校验：需对两指标都有读权限（PII 指标需合规角色，对齐 T049）
        a = await self._get_metric_for_compare(code_a)
        b = await self._get_metric_for_compare(code_b)

        def _diff_level(va: Any, vb: Any) -> str:
            if va == vb:
                return "identical"
            # 简单相似判定：字符串包含关系
            sa, sb = str(va), str(vb)
            if sa in sb or sb in sa:
                return "similar"
            return "different"

        fields = [
            "granularity",
            "unit",
            "currency",
            "aggregation",
            "time_semantics",
            "additivity",
            "dw_layer",
            "metric_tier",
            "serving_mode",
            "freshness",
        ]
        result: dict[str, Any] = {"metrics": [code_a, code_b], "fields": {}}

        for field in fields:
            va = getattr(a, field, None)
            vb = getattr(b, field, None)
            result["fields"][field] = {
                "a": va,
                "b": vb,
                "difference_level": _diff_level(va, vb),
            }

        # 口径定义对比
        def_a = a.definition_json or {}
        def_b = b.definition_json or {}
        expr_a = def_a.get("expression", "")
        expr_b = def_b.get("expression", "")
        result["fields"]["definition"] = {
            "a": def_a,
            "b": def_b,
            "difference_level": _diff_level(expr_a, expr_b),
        }

        # 依赖对比
        dep_a = set(def_a.get("dependencies", []) or [])
        dep_b = set(def_b.get("dependencies", []) or [])
        result["fields"]["dependencies"] = {
            "a": sorted(dep_a),
            "b": sorted(dep_b),
            "intersection": sorted(dep_a & dep_b),
            "only_a": sorted(dep_a - dep_b),
            "only_b": sorted(dep_b - dep_a),
            "difference_level": "identical" if dep_a == dep_b else "different",
        }

        return result

    # ---- US10: 批量注册 ----

    async def batch_register_metrics(
        self,
        request: Any,
        actor_id: int,
    ) -> dict[str, Any]:
        """批量注册指标。

        对齐 spec FR-016：批量注册同样走字典校验，自动推断逻辑与单条注册一致。

        Args:
            request: 批量注册请求(含source_table+measure_columns+domain)。
            actor_id: 操作人ID。

        Returns:
            {batch_id, candidates: [{metric_code, status, validation_errors}]}.
        """
        import uuid

        from app.services.semantic.schemas import MetricCreateRequest

        batch_id = f"batch_{uuid.uuid4().hex[:12]}"
        candidates: list[dict[str, Any]] = []

        # 校验 domain 存在且 active
        await self._validate_domain_active(request.domain)

        # 获取域默认值
        domain_defaults = await self._get_domain_defaults(request.domain)

        for col in request.measure_columns:
            # 使用 auto_fill 引擎生成编码建议
            from app.services.semantic.auto_fill import auto_fill as _auto_fill

            suggested = _auto_fill(
                domain_code=request.domain,
                source_table=request.source_table,
                measure_column=col,
                period="day",
                domain_defaults=domain_defaults,
            )
            code = suggested.get("metric_code_suggestion") or (
                f"{request.domain}_entity_{col.replace('_', '')}_day"
            )
            defaults = suggested.get("defaults", {})

            try:
                create_req = MetricCreateRequest(
                    metric_code=code,
                    name=col,
                    domain=request.domain,
                    type=defaults.get("type", "atomic"),
                    granularity=defaults.get("granularity", "day"),
                    unit=defaults.get("unit", "cnt"),
                    aggregation=defaults.get("aggregation", "SUM"),
                    time_semantics=defaults.get("time_semantics", "PERIOD"),
                    freshness=defaults.get("freshness", "T1"),
                    dw_layer=defaults.get("dw_layer", "DWD"),
                    metric_tier=defaults.get("metric_tier", "T3"),
                    serving_mode=defaults.get("serving_mode", "BATCH_ONLY"),
                    additivity=defaults.get("additivity", "ADDITIVE"),
                    definition_json={"expression": f"SUM({col})", "dependencies": []},
                    source_table=request.source_table,
                    measure_column=col,
                    period="day",
                    batch_id=batch_id,
                )
                await self.create_metric(create_req, owner_id=actor_id)
                candidates.append(
                    {
                        "metric_code": code,
                        "status": "DRAFT",
                        "validation_errors": None,
                    }
                )
            except (BusinessError, ConflictError) as exc:
                candidates.append(
                    {
                        "metric_code": code,
                        "status": "VALIDATION_ERROR",
                        "validation_errors": str(exc),
                    }
                )

        return {"batch_id": batch_id, "candidates": candidates}

    # ---- US11: 消费指南 ----

    async def get_consumption_guide(self, metric_code: str) -> dict[str, Any]:
        """获取指标消费指南（Service层 + 缓存）。

        Args:
            metric_code: 指标编码。

        Returns:
            消费指南字典。

        Raises:
            NotFoundError: 指标不存在。
        """
        # 先查缓存（须用 get_guide 读 metric:guide: 命名空间，与下方 set_guide 对称；
        # 误用 get() 会构造 metric:def:guide:{code}:v0 键，与写入键永不相交 → 缓存恒 miss）
        cached = await self._cache.get_guide(metric_code)
        if cached is not None:
            return cached

        metric = await self.get_metric(metric_code)

        if metric.consumption_guide:
            guide = metric.consumption_guide
        else:
            guide = {
                "metric_code": metric.metric_code,
                "name": metric.name,
                "domain": metric.domain,
                "type": metric.type,
                "granularity": metric.granularity,
                "unit": metric.unit,
                "aggregation": metric.aggregation,
                "time_semantics": metric.time_semantics,
                "serving_mode": metric.serving_mode,
                "recommended_usage": [
                    f"适用 {metric.domain} 域 {metric.granularity} 粒度分析",
                    f"聚合方式为 {metric.aggregation}，"
                    f"注意{'不可' if metric.additivity == 'NON_ADDITIVE' else '可以'}跨维度聚合",
                ],
                "cautions": [],
                "related_metrics": [],
            }
            if metric.pii_flag:
                guide["cautions"].append("该指标包含 PII 数据，使用时需遵守数据合规要求")
            if metric.additivity == "SEMI_ADDITIVE":
                dims = metric.non_additive_dimensions or "未指定"
                guide["cautions"].append(f"半可加指标，不可加维度: {dims}")

        # 缓存结果
        await self._cache.set_guide(metric_code, guide)
        return guide

    # ---- 合规官可达性检查（FR-024 降级路径）----

    async def _has_active_compliance_officer(self, domain: str | None) -> bool:
        """检查指定域是否有活跃的 compliance_officer 用户。

        用于 FR-024 合规官不可达降级路径：若合规官不可达，PII 指标紧急发布
        可降级为 INTERNAL 分级发布。

        Args:
            domain: 指标所属域（None 表示全局查找）。

        Returns:
            是否存在至少一个活跃的 compliance_officer。
        """
        from sqlalchemy import func, select

        from app.models.user import User

        stmt = (
            select(func.count())
            .select_from(User)
            .where(
                User.role == "compliance_officer",
                User.status == "active",
            )
        )
        if domain is not None:
            stmt = stmt.where(User.domain == domain)
        result = await self._db.execute(stmt)
        count = result.scalar() or 0
        return count > 0

    # ------------------------------------------------------------------
    # DATA_SOURCE_DROPPED 状态闭环（TD §12.3 / PRD R5-01、R3-04④）
    #   数据源断连（源表 DROP/不可达）→ PUBLISHED 指标置 DSD；
    #   DSD → PUBLISHED（源恢复/确认误报）；DSD → DEPRECATED（确认退役）。
    # ------------------------------------------------------------------

    async def mark_source_dropped(self, source_ids: list[str], actor_id: int) -> int:
        """数据源 DROP/不可达 → 血缘下游 PUBLISHED 指标批量置 DATA_SOURCE_DROPPED。

        对齐 PRD R3-04④：采集检测到源表 DROP 后调用本方法，沿血缘把引用该
        数据源表的下游指标标记为 DSD（非直接 DEPRECATED，避免误退役），生成
        Owner 待办（7 天处理期）。

        Args:
            source_ids: 已 DROP 的数据源 ID 集合（采集侧确认不可达的源）。
            actor_id: 触发人 ID（采集/运维）。

        Returns:
            被标记为 DSD 的指标数（0 表示无血缘下游指标或均已处理）。

        实现：查血缘 ``table:`` 下游节点，再按 source_id 关联 DBCatalog 过滤——
        精确到「该数据源表」的下游指标，避免误伤同域其他源。best-effort，
        血缘缺失不影响已发布指标继续可用。
        """
        from sqlalchemy import select

        from app.models.data_source import DBCatalog
        from app.services.lineage.schemas import LineageImpactParams
        from app.services.lineage.service import LineageService

        # 1) 数据源 → 该源下的表（entity_name 集合）：仅活跃未删表
        if not source_ids:
            return 0
        stmt = (
            select(DBCatalog.entity_name)
            .where(
                DBCatalog.source_id.in_(source_ids),
                DBCatalog.deleted_at.is_(None),
                DBCatalog.entity_type.in_(["TABLE", "VIEW"]),
            )
        )
        table_rows = (await self._db.execute(stmt)).scalars().all()
        if not table_rows:
            return 0

        # 2) 沿血缘查每张表的下游指标（metric: 节点）
        impacted_codes: set[str] = set()
        for table in table_rows:
            node = f"table:{table}"
            try:
                edges = await LineageService(self._db).query_impact(
                    LineageImpactParams(node=node, direction="downstream", max_hops=5)
                )
            except Exception:
                continue  # best-effort：单表血缘失败不阻断其余
            for e in edges:
                target = getattr(e, "target_node", None)
                target = str(target) if target is not None else ""
                if target.startswith("metric:"):
                    impacted_codes.add(target.removeprefix("metric:"))
        if not impacted_codes:
            return 0

        # 2) 血缘命中的指标需为 PUBLISHED 才标记（已 DSD/DEPRECATED 跳过）
        count = 0
        for code in impacted_codes:
            metric = await self.get_metric(code)
            if metric is None or metric.status != "PUBLISHED":
                continue
            invalid = MetricStateMachine.validate_transition(metric.status, "DATA_SOURCE_DROPPED")
            if invalid is not None:
                continue
            await self._repo.update_with_optimistic_lock(
                metric.id,
                metric.row_version,
                status="DATA_SOURCE_DROPPED",
            )
            await self._cache.invalidate(code)
            await self._publish_event(
                "metric.source_dropped",
                {"metric_code": code, "domain": metric.domain, "source_ids": source_ids},
                actor_id=str(actor_id),
            )
            count += 1
        return count

    async def recover_source_dropped(
        self, metric_code: str, actor_id: int, role: str
    ) -> Metric:
        """DSD → PUBLISHED（源恢复 / 确认误报，对齐 PRD R5-01）。

        Owner 确认源表恢复或标记为误报后，取消 DSD 回到 PUBLISHED。
        """
        metric = await self.get_metric(metric_code)
        self._assert_owner_or_admin(metric, actor_id, role)

        # 仅 DATA_SOURCE_DROPPED 可恢复（同态/其他状态转移不适用本语义）
        if metric.status != "DATA_SOURCE_DROPPED":
            raise ConflictError(
                f"仅 DATA_SOURCE_DROPPED 状态可恢复发布，当前 {metric.status}",
                error_code="INVALID_TRANSITION",
            )

        updated = await self._repo.update_with_optimistic_lock(
            metric.id, metric.row_version, status="PUBLISHED"
        )
        await self._cache.invalidate(metric_code)
        await self._publish_event(
            "metric.source_recovered",
            {"metric_code": metric_code, "domain": metric.domain},
            actor_id=str(actor_id),
        )
        return updated

    async def confirm_deprecate_dropped(
        self, metric_code: str, successor_code: str | None, actor_id: int, role: str
    ) -> Metric:
        """DSD → DEPRECATED（确认退役，对齐 PRD R5-01）。

        Owner 判断源表无法恢复 → 标 DEPRECATED 并填替代指标（可选，但推荐）。
        触发 metric.deprecated 事件 → notify 下游消费方。
        """
        metric = await self.get_metric(metric_code)
        self._assert_owner_or_admin(metric, actor_id, role)

        # 仅 DATA_SOURCE_DROPPED 可确认退役
        if metric.status != "DATA_SOURCE_DROPPED":
            raise ConflictError(
                f"仅 DATA_SOURCE_DROPPED 状态可确认退役，当前 {metric.status}",
                error_code="INVALID_TRANSITION",
            )

        # 替代指标校验（可选）：若填了须为已 PUBLISHED
        if successor_code is not None and not str(successor_code).strip():
            successor_code = None
        if successor_code is not None:
            # 自废弃防护：替代指标不得为自身（语义矛盾，废弃链会展示指向自身）
            if str(successor_code) == metric_code:
                raise BusinessError(
                    "替代指标不能为指标自身，请指定其他已发布指标或留空",
                    error_code="VALIDATION_ERROR",
                )
            successor = await self._repo.get_by_code(successor_code)
            if successor is None:
                raise NotFoundError(f"替代指标不存在: {successor_code}")
            if successor.status != "PUBLISHED":
                raise BusinessError(
                    f"替代指标 {successor_code} 未发布，无法作为替代",
                    error_code="VALIDATION_ERROR",
                )

        from datetime import timedelta

        sunset_days = self._settings.metric_sunset_days
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
        await self._cleanup_metric_lineage(metric_code)
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
        return updated
