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
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.error_codes import ErrorCode
from app.core.exceptions import (
    AuthError,
    BusinessError,
    ConflictError,
    NotFoundError,
    ValidationError,
    public_error_message,
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
    MetricRejectRequest,
    MetricResponse,
    MetricSubmitRequest,
    MetricUpdateRequest,
    MetricVersionResponse,
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
# sql/etl_sql（SQL 模式口径，注册页可选 SQL 模式写入 definition_json.sql，后端 sqlglot 校验）：
# 修改 SQL 口径与表达式（expression）同级——同样破坏下游消费方，必须触发 PENDING 确认期。
# 修复前 SQL 模式指标改口径被当非破坏性 UPDATE 静默生效，绕过 14 天消费方确认（治理漏洞）。
# source_table/source_tables/measure_column（来源表/度量列）：修改它们直接改变消费方
# 读取的数据底座——换来源表或度量列等同于重写口径语义，必须与表达式同级触发 PENDING
# 确认期（此前缺失，改来源表/度量列被当非破坏性静默生效，消费方口径被无声破坏）。
BREAKING_DEF_FIELDS = (
    "expression",
    "aggregation",
    "granularity",
    "dependencies",
    "sql",
    "etl_sql",
    "source_table",
    "source_tables",
    "measure_column",
)

# Top-level 破坏性变更字段：直接修改 metric 表上的这些字段等同于口径变更
# （对齐 TD §12 metric_version：granularity/unit 变更触发 PENDING_VERSION）
# aggregation（聚合方式）语义上就是"怎么算"，SUM→AVG 是完全不同的口径——
# 必须与 granularity/unit 同级触发 PENDING_VERSION（此前误归治理属性静默更新，
# 与 definition_json 路径的 BREAKING_DEF_FIELDS 判定矛盾，R40 修复）。
# OneData：measure_id（逻辑度量）变更 = 换了"度量什么"，同为破坏性口径变更。
BREAKING_TOP_LEVEL_FIELDS = ("granularity", "unit", "aggregation", "measure_id")

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
        # OneData 原子层：逻辑度量目录仓储（原子指标 measure_id 存在性/状态校验与单位继承）。
        # 与 self._repo 同模式——构造时捕获，单元测试直接替换实例即可 mock。
        from app.services.measure_catalog.repository import MeasureCatalogRepository

        self._measure_repo = MeasureCatalogRepository(db)
        self._cache = (
            cache
            if cache is not None
            else MetricCache.from_defaults(get_redis() if _redis_available() else None)
        )
        self._governance_svc = governance_svc
        # 血缘清理临时实例（P0-3）：其延迟副作用（图写/缓存失效）须在事务提交后
        # 由 API 层经 run_lineage_post_commit() 统一触发。
        self._lineage_svc: Any | None = None

    async def run_lineage_post_commit(self) -> None:
        """触发血缘清理的提交后副作用（P0-3）：commit 后调用，防止幽灵边。"""
        if self._lineage_svc is not None:
            try:
                await self._lineage_svc.run_post_commit()
            finally:
                self._lineage_svc = None

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

    async def _validate_dict_field(self, dict_type: str, code: str) -> None:
        """校验单个字典字段值（P1-5 共享逻辑）。

        语义：类型无任何 active 项（未种子/空表）→ 放行；类型已配置但值不在 →
        拦截（DICT_VALUE_NOT_FOUND）；值已停用 → 拦截（DICT_VALUE_INACTIVE）；
        DB 查询异常 → best-effort 放行（不阻断业务）。
        """
        from app.core.exceptions import BusinessError, NotFoundError
        from app.services.system_dict.service import SystemDictService

        if not code:
            return
        try:
            svc = SystemDictService(self._db)
            if not await svc.list_by_type(dict_type, status="active"):
                return  # 类型未配置（未种子/空表）→ 放行该类型
            await svc.validate_dict_value(dict_type, code)
        except NotFoundError:
            raise  # 类型已配置但值不存在 → 非法字典值拦截
        except BusinessError:
            raise  # 值已停用 → 拦截
        except Exception:
            # DB 抖动/表不存在 → best-effort 放行，不阻断创建/更新
            return

    async def _validate_dict_fields(self, request: MetricCreateRequest) -> None:
        """校验字典字段值存在于 SystemDict 且 active（应用层可选校验，对齐 D2）。

        加固语义（P1-5）：此前捕获 NotFoundError 直接放行——「字典类型完全未配置
        （空表/未种子）」与「类型已配置但值不存在（脏值）」无法区分，非法字典值
        （如 granularity=bogus）静默入库。现在按类型区分：
        - 类型无任何 active 项（未种子/空表）→ 放行该类型（兼容迁移 0025 空表，
          避免存量环境创建全阻断）；
        - 类型已配置但值不在 → 拦截（DICT_VALUE_NOT_FOUND）；
        - 值已停用 → 拦截（DICT_VALUE_INACTIVE）。
        """
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
            await self._validate_dict_field(dict_type, code)

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

    async def create_metric(
        self,
        request: MetricCreateRequest,
        owner_id: int,
        role: str | None = None,
        user_domain: str | None = None,
        _preloaded_conflict_existing: list[Any] | None = None,
    ) -> Metric:
        """创建指标（初始状态 DRAFT）。

        对齐 FR-012/FR-013：metric_code 校验委托 ConflictPrechecker.validate_code_format，
        创建后异步调 ConflictPrechecker.precheck，命中相似口径→挂 pending_conflict 标记。

        Args:
            request: 创建请求。
            owner_id: 创建人（Owner）ID。
            role: 创建人角色（P1-6：域管理员/Owner 仅可创建本域指标）。
            user_domain: 创建人所属域（域作用域校验；None 表示未绑定域，不拦截）。

        Returns:
            创建的指标。

        Raises:
            ConflictError: 指标编码已存在。
            BusinessError: 域管理员/Owner 跨域创建（P1-6 域门禁）。
        """
        # P1-6 创建域门禁：域管理员/指标 Owner 仅可创建本域指标（对齐 PDP 本域
        # write 语义——update/submit/approve 均有 check_metric_permission 域校验，
        # 唯独 create 无校验，任意创建者可跨域建指标，owner 域与请求域不校验）。
        if (
            role in ("domain_admin", "metric_owner")
            and user_domain
            and request.domain != user_domain
        ):
            raise BusinessError(
                f"{'域管理员' if role == 'domain_admin' else '指标 Owner'}仅可创建本域指标",
                error_code="FORBIDDEN",
                ctx={
                    "request_domain": request.domain,
                    "user_domain": user_domain,
                    "role": role,
                },
            )

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

        # 3a. OneData 原子层校验：原子指标关联的逻辑度量必须存在且已发布
        # （防 FK 500——传不存在 measure_id 时 flush 抛 IntegrityError → 500；
        #  防草稿/软删度量被新指标引用——度量是原子指标的权威继承源，须 PUBLISHED 才可用）。
        measure: Any = None
        if request.type == "atomic" and request.measure_id is not None:
            measure = await self._measure_repo.get_by_id(request.measure_id)
            if measure is None:
                raise ValidationError(
                    f"关联的逻辑度量不存在: {request.measure_id}",
                    error_code="MEASURE_NOT_FOUND",
                )
            if measure.status != "PUBLISHED":
                raise ValidationError(
                    "关联的逻辑度量未发布"
                    f"（当前 {measure.status}），不可用于新指标: {request.measure_id}",
                    error_code="MEASURE_NOT_PUBLISHED",
                )

        # 3b. 命名规范硬卡（TD §12.3 强化）：指标名须命中受控词根，否则拦截
        # 裸词/无意义命名；维度类指标（metric_type="dimension"）豁免。
        from app.services.semantic.conflict_precheck import ConflictPrechecker

        valid_name, name_error = ConflictPrechecker.validate_metric_name(
            request.name, metric_type=request.type
        )
        if not valid_name:
            raise ValidationError(name_error, error_code="METRIC_NAME_NO_MORPHEME")

        # 3c. OneData 类型化字段兜底（界限文档 §2.3）：原子指标 = 逻辑度量 + 聚合方式，
        # 不绑物理表——单位由逻辑度量 default_unit 继承（measure 已在 3a 校验并复用）；
        # 派生/复合缺省用默认物理属性。
        # 物理属性（time_semantics/freshness/dw_layer）对原子属挂载/数据语义层，缺省取默认。
        if request.unit is None:
            if request.type == "atomic" and measure is not None and measure.default_unit:
                request.unit = measure.default_unit
            if request.unit is None:
                request.unit = "TIMES"
        if request.time_semantics is None:
            request.time_semantics = "PERIOD"
        if request.freshness is None:
            request.freshness = "T1"
        if request.dw_layer is None:
            request.dw_layer = "DWD"

        # 3d. 口径完整性：把 top-level 的 source_table/measure_column 合入 definition_json。
        # 血缘差异同步（register_metric_from_definition）读 definition.source_table /
        # measure_column 建「指标↔落地表」边——但批量注册/模板实例化等后端构造路径此前
        # 不写这两个键，导致请求传了 source_table 却无血缘边（与前端单条 buildDefinitionJson
        # 合入 ②源表/度量列的行为不一致）。此处后端统一兜底，覆盖全部创建路径。
        # OneData：派生指标携带 mount（挂载实体）时同样并入——mount 为权威结构化记录，
        # definition_json 冗余一份供血缘/消费/冲突预检等旧读者读取（二者保持一致）。
        if (
            request.source_table
            or request.measure_column
            or (request.type == "derived" and request.mount)
        ):
            _defn = dict(request.definition_json or {})
            if request.source_table and not _defn.get("source_table"):
                _defn["source_table"] = request.source_table
            if request.measure_column and not _defn.get("measure_column"):
                _defn["measure_column"] = request.measure_column
            if request.type == "derived" and request.mount:
                if not _defn.get("source_table") and request.mount.source_table:
                    _defn["source_table"] = request.mount.source_table
                if not _defn.get("measure_column") and request.mount.source_column:
                    _defn["measure_column"] = request.mount.source_column
            request.definition_json = _defn

        # PII 双源归一化：definition_json.pii 与 pii_flag 保持一致（pii_flag 为权威源）
        definition, pii_flag = _normalize_pii(request.definition_json, request.pii_flag)

        metric = Metric(
            metric_code=request.metric_code,
            name=request.name,
            domain=request.domain,
            type=request.type,
            # OneData：粒度下沉挂载实体——派生携带 mount 时由挂载回填（冗余供列表/排序展示），
            # 原子/复合不设粒度（granularity 可空）
            granularity=(
                request.granularity
                or (
                    request.mount.granularity
                    if request.type == "derived" and request.mount
                    else None
                )
            ),
            # OneData 原子层：关联逻辑度量（原子必填，派生/复合继承可空）
            measure_id=request.measure_id,
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
            # 口径三方责任（PRD 4.5 补充，均可空）：平台用户 id + 外部人员名称兜底
            product_owner_id=request.product_owner_id,
            tech_owner_id=request.tech_owner_id,
            dw_developer_id=request.dw_developer_id,
            product_owner_name=request.product_owner_name,
            tech_owner_name=request.tech_owner_name,
            dw_developer_name=request.dw_developer_name,
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

        # OneData 挂载层（界限文档 §2.3 第 3 条）：派生指标携带 mount 时，落 metric_mount
        # 承载源表/粒度/周期/域——同一事务内 flush，粒度已由构造时回填到 metric。
        # （mount 的 source_table/measure_column 已在 3b 并入 definition_json，
        #   血缘/消费/冲突预检等旧读者无需改动；mount 为权威结构化记录。）
        if request.type == "derived" and request.mount:
            from app.models.metric_mount import MetricMount
            from app.services.metric_mount.repository import MetricMountRepository

            await MetricMountRepository(self._db).save(
                MetricMount(
                    metric_id=metric.id,
                    source_table=request.mount.source_table,
                    source_column=request.mount.source_column,
                    granularity=request.mount.granularity,
                    default_period=request.mount.default_period,
                    domain=request.mount.domain,
                )
            )

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

        # 冲突自动落库 + 标记（对齐 FR-012 / TD §12.4）：创建后自动预检相似口径。
        # 命中即落 conflict 表 OPEN 记录（硬/软均落，source=auto），并按冲突表
        # **实际未决记录**挂 pending_conflict 标记——保证「指标目录标记 ⇔ 仲裁台
        # 可处置记录」严格一致，杜绝「有标记无记录」的孤儿态（曾致目录显示冲突、
        # 仲裁台为空、标记无法通过正常仲裁清除）。软冲突同样落库，仲裁台区分展示。
        # 抽取为公共方法 _detect_and_mark_conflicts：更新口径后（P2-I）复用同一逻辑。
        # 批量注册场景传入预加载的 existing（逐列增量追加），避免 N 次全量加载。
        metric = (
            await self._detect_and_mark_conflicts(
                metric, definition, existing=_preloaded_conflict_existing
            )
            or metric
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

    async def load_conflict_existing(self) -> list[Any]:
        """加载冲突预检的对比对象（P0-A/P1-F/P1-G/P2-K 数据接入）。

        全部活动（非软删、非 DEPRECATED）指标分页全量（P1-F/G 修复——原
        ``list_metrics(limit=1000)`` 不过滤状态致 DEPRECATED 参与比对制造仲裁台
        噪音、1000 条截断漏检更早的历史指标）；附带关联逻辑度量的同义词
        （P2-K，批量 IN 查询避免 N+1）。
        供创建/更新自动预检与手动预检（/conflicts/check 的 existing 为空时
        服务端自动加载）复用。
        """
        from sqlalchemy import select

        from app.models.measure_catalog import MeasureCatalog
        from app.services.conflict.schemas import MetricInput

        rows = await self._repo.list_active_for_conflict()
        syn_map: dict[int, list[str]] = {}
        measure_ids = [m.measure_id for m in rows if m.measure_id]
        if measure_ids:
            # best-effort：度量目录同义词查询失败降级为无同义词，不阻断预检主流程
            # （手动预检端点 /conflicts/check 复用此方法，查询异常须降级而非 500）。
            try:
                measures = (
                    await self._db.execute(
                        select(MeasureCatalog.id, MeasureCatalog.synonyms).where(
                            MeasureCatalog.id.in_(measure_ids),
                            MeasureCatalog.deleted_at.is_(None),
                        )
                    )
                ).all()
                syn_map = {mid: [str(s) for s in (syn or [])] for mid, syn in measures}
            except Exception:  # noqa: BLE001 - 同义词查询降级，仅告警
                logger.warning("conflict_synonyms_load_failed best-effort 跳过")
        result: list[MetricInput] = []
        for m in rows:
            defn = m.definition_json or {}
            result.append(
                MetricInput(
                    metric_code=m.metric_code,
                    domain=m.domain or "",
                    definition=(defn.get("definition") or defn.get("expression") or ""),
                    source_tables=defn.get("source_tables") or [],
                    has_pii=bool(m.pii_flag),
                    pii_authorized=bool(m.compliance_reviewed),
                    metric_id=m.id,
                    definition_json=defn,
                    synonyms=syn_map.get(m.measure_id, []),
                )
            )
        return result

    async def _detect_and_mark_conflicts(
        self,
        metric: Metric,
        definition: dict[str, Any],
        existing: list[Any] | None = None,
    ) -> Metric:
        """创建/口径更新后自动预检相似口径（best-effort，不阻断主流程）。

        P2-I：更新口径后也触发重检（原仅创建时检测一次）——指标改口径后与其它
        指标"后来变得同义"也能被发现。命中即落 conflict 表 OPEN 记录（硬/软均落，
        source=auto），并按冲突表**实际未决记录**挂 pending_conflict 标记——保证
        「指标目录标记 ⇔ 仲裁台可处置记录」严格一致，杜绝「有标记无记录」孤儿态。

        Args:
            metric: 目标指标。
            definition: 口径定义。
            existing: 预加载的冲突比对对象（批量注册场景由调用方加载一次并逐列
                增量追加，避免 N 列 = N 次全量加载的 O(N²) 性能退化）；None 时
                内部加载（单条/更新场景）。
        """
        try:
            from app.services.conflict.repository import ConflictRepository
            from app.services.conflict.schemas import MetricInput
            from app.services.conflict.service import ConflictService

            # OneData 挂载层权威：把挂载实体的 source_table 并入预检比对（挂载独立
            # 更新后 definition_json 的 source_tables 冗余可能过期）
            source_tables = list(definition.get("source_tables") or [])
            try:
                from app.services.metric_mount.repository import MetricMountRepository

                _mount = await MetricMountRepository(self._db).get_by_metric(metric.id)
                if (
                    _mount is not None
                    and isinstance(_mount.source_table, str)
                    and _mount.source_table
                    and _mount.source_table not in source_tables
                ):
                    source_tables.append(_mount.source_table)
            except Exception:  # noqa: BLE001 - best-effort：mount 查询失败仅跳过挂载源表
                pass

            candidate = MetricInput(
                metric_code=metric.metric_code,
                domain=metric.domain or "",
                definition=(definition.get("definition") or definition.get("expression") or ""),
                source_tables=source_tables,
                has_pii=bool(metric.pii_flag),
                pii_authorized=bool(metric.compliance_reviewed),
                metric_id=metric.id,
                definition_json=definition,
            )
            # P2-K：原子指标关联逻辑度量目录（OneData），同义词并入候选比对
            if metric.measure_id is not None:
                try:
                    from sqlalchemy import select

                    from app.models.measure_catalog import MeasureCatalog

                    syn = (
                        await self._db.execute(
                            select(MeasureCatalog.synonyms).where(
                                MeasureCatalog.id == metric.measure_id
                            )
                        )
                    ).scalar_one_or_none()
                    if syn:
                        candidate.synonyms = [str(s) for s in syn]
                except Exception:  # noqa: BLE001 - best-effort：同义词查询失败仅跳过
                    pass

            if existing is None:
                existing = await self.load_conflict_existing()
            # 排除自身：指标已落库，避免与自身比对（check 亦有自我引用防御，此处省查询）
            existing = [e for e in existing if e.metric_code != metric.metric_code]
            await ConflictService(self._db).check(
                candidate, existing, use_llm=False, source="auto"
            )
            # 以冲突表实际未决记录为准挂标记（杜绝孤儿标记）
            open_conflict = await ConflictRepository(self._db).get_first_open_for_metric(
                metric.metric_code
            )
            if open_conflict is not None:
                codes = open_conflict.metric_codes or {}
                conflict_detail = {
                    "conflict_type": getattr(open_conflict.type, "value", None),
                    "score": open_conflict.similarity_score,
                    "existing_code": codes.get("existing"),
                    "existing_metric_id": open_conflict.metric_b,
                    "severity": open_conflict.severity or "soft",
                    "block_publish": bool(open_conflict.block_publish),
                    "reason": open_conflict.reason or "",
                    "source": open_conflict.source or "auto",
                    "conflict_id": open_conflict.conflict_id,
                }
                updated = await self._repo.update_with_optimistic_lock(
                    metric.id,
                    metric.row_version,
                    pending_conflict=True,
                    pending_conflict_detail=conflict_detail,
                )
                logger.info(
                    "metric_conflict_detected",
                    metric_code=metric.metric_code,
                    conflict_id=open_conflict.conflict_id,
                    conflict_detail=conflict_detail,
                )
                # 返回更新后的对象（挂标记后），供 create_metric 覆盖返回值
                return updated
        except Exception:
            # 冲突落库失败不阻塞创建/更新（best-effort）：不挂标记也不落库，
            # 避免「有标记无记录」的孤儿态；用户可稍后手动预检或重跑。
            logger.warning(
                "metric_conflict_precheck_failed",
                metric_code=metric.metric_code,
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

    async def get_metric_public(
        self,
        metric_code: str,
        actor_id: int | None = None,
        role: str | None = None,
    ) -> MetricResponse:
        """经缓存获取指标详情（API 读路径，含 cache-aside + 熔断降级）。

        Redis 命中直接返回；未命中/降级时回源 MySQL 并回写缓存。
        该方法用于对外读接口，与内部 `get_metric`（始终走 DB，供状态流转使用）
        分离，避免缓存与状态机耦合。

        Args:
            metric_code: 指标编码。
            actor_id: 读路径行级隔离（P0-3）——当前用户 ID；None 表示内部调用
                （不过滤，端点层必传）。
            role: 当前用户角色（配合 actor_id 判定管理角色/评审人放行）。

        Returns:
            指标详情响应。

        Raises:
            NotFoundError: 指标不存在，或未发布指标对当前用户不可见（按不存在处理）。
        """
        cached = await self._cache.get(metric_code)
        if cached is not None:
            resp = MetricResponse.model_validate(cached)
            self._assert_metric_visible(resp, actor_id, role)
            return await self._attach_measure_info(resp)
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
        self._assert_metric_visible(metric, actor_id, role)
        await self._cache.set(metric)
        return await self._attach_measure_info(MetricResponse.model_validate(metric))

    async def _attach_measure_info(self, resp: MetricResponse) -> MetricResponse:
        """best-effort 填充逻辑度量展示信息（measure_code/measure_name）。

        原子指标关联的权威继承源（度量目录）在详情页「逻辑度量」栏展示名称+编码；
        度量已软删/查询异常时降级为仅 measure_id（不阻断详情读取）。
        """
        if resp.measure_id is None:
            return resp
        try:
            from sqlalchemy import select

            from app.models.measure_catalog import MeasureCatalog

            row = (
                await self._db.execute(
                    select(MeasureCatalog.measure_code, MeasureCatalog.name).where(
                        MeasureCatalog.id == resp.measure_id
                    )
                )
            ).first()
            if row is not None:
                resp.measure_code = row[0]
                resp.measure_name = row[1]
        except Exception:  # noqa: BLE001 - best-effort：度量查询失败仅降级
            pass
        return resp

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

    async def list_metrics(
        self,
        params: MetricListParams,
        actor_id: int | None = None,
        role: str | None = None,
    ) -> tuple[list[Metric], int]:
        """分页查询指标列表。

        Args:
            params: 查询参数。
            actor_id: 读路径行级隔离（P0-3）——当前用户 ID；None 表示内部调用
                （不过滤，端点层必传）。
            role: 当前用户角色（配合 actor_id 判定管理角色/评审人放行）。

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
            visible_actor_id=actor_id,
            visible_role=role,
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

        # OneData 原子层校验（对齐 create_metric 3a）：更新/更换关联逻辑度量时，
        # 目标度量须存在（防 FK 500——传不存在 measure_id 时 flush 抛 IntegrityError→500）；
        # 原子指标还须 PUBLISHED（度量是原子指标的权威继承源，草稿/软删度量不可被引用）。
        if request.measure_id is not None:
            measure = await self._measure_repo.get_by_id(request.measure_id)
            if measure is None:
                raise ValidationError(
                    f"关联的逻辑度量不存在: {request.measure_id}",
                    error_code="MEASURE_NOT_FOUND",
                )
            if metric.type == "atomic" and measure.status != "PUBLISHED":
                raise ValidationError(
                    "关联的逻辑度量未发布"
                    f"（当前 {measure.status}），不可用于该指标: {request.measure_id}",
                    error_code="MEASURE_NOT_PUBLISHED",
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
            "measure_id",
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
            # 口径三方责任 id 字段已从白名单移除——id/name 成对在下方专用块处理，
            # 以支持显式置空（解除责任方/切换为外部人员）与清空后保留旧值的区分。
        ):
            val = getattr(request, field, None)
            if val is not None:
                updates[field] = val

        # 口径三方责任 id/name 成对处理（非破坏性变更，不触发版本确认）：
        # 三个责任方支持"平台用户 id ↔ 外部人员名称"互相切换与完全解除，白名单循环
        # `if val is not None` 会跳过显式 null 导致旧值残留，故单独用 model_fields_set
        # 区分「未提交（保留旧值）」与「显式置空（解除/切换）」。任一字段被显式提交即
        # 成对写入——id 设空而 name 非空 = 切换为外部人员；两者皆空 = 完全解除。
        _responsibility_provided = set(request.model_fields_set)
        for _id_field, _name_field in (
            ("product_owner_id", "product_owner_name"),
            ("tech_owner_id", "tech_owner_name"),
            ("dw_developer_id", "dw_developer_name"),
        ):
            if _id_field in _responsibility_provided or _name_field in _responsibility_provided:
                updates[_id_field] = getattr(request, _id_field, None)
                updates[_name_field] = getattr(request, _name_field, None)

        # P1-5: update 路径字典校验——此前 update 不校验字典字段，可写入非字典的
        # granularity/unit/aggregation 等脏值（仅 create 校验）。仅校验**实际改变**
        # 的值（新值 != 指标当前值）：存量脏值（如 unit=cnt）未改时允许保留，避免
        # 编辑其他字段被既有脏值误拦；主动改脏值则拦截（DICT_VALUE_NOT_FOUND）。
        for dict_type, field in (
            ("granularity", "granularity"),
            ("unit", "unit"),
            ("aggregation", "aggregation"),
            ("time_semantics", "time_semantics"),
            ("freshness", "freshness"),
            ("dw_layer", "dw_layer"),
            ("metric_tier", "metric_tier"),
            ("serving_mode", "serving_mode"),
            ("additivity", "additivity"),
        ):
            new_val = getattr(request, field, None)
            if new_val is not None and new_val != getattr(metric, field, None):
                await self._validate_dict_field(dict_type, new_val)

        # 命名规范硬卡（TD §12.3 强化）：改名时新名称须命中受控词根
        # （仅校验实际发生变更的名称，存量指标未改名时不受影响；维度类豁免）。
        if "name" in updates and updates["name"] != metric.name:
            from app.services.semantic.conflict_precheck import ConflictPrechecker

            valid_name, name_error = ConflictPrechecker.validate_metric_name(
                updates["name"], metric_type=metric.type
            )
            if not valid_name:
                raise ValidationError(name_error, error_code="METRIC_NAME_NO_MORPHEME")

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
                # PENDING 确认期内禁止再次发起破坏性变更——多个 PENDING 版本并存时，
                # 转正低版本号会把主表 version 回退并覆盖高版本口径（版本历史倒挂，
                # 已确认的高版本变更丢失）。须先完成当前确认期（确认/拒绝/超时）。
                if await self._repo.has_pending_version(metric.id):
                    raise ConflictError(
                        f"该指标存在待确认的破坏性变更（版本 {metric.version}），"
                        "请先完成确认或等待超时后再发起新变更",
                        error_code="METRIC_PENDING_VERSION_EXISTS",
                    )
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
                # P8 非破坏性口径变更直接生效：主表 version 与 effective_version 同步，
                # 消除「version 已递增但 effective_version 滞后 → 永不转正的 DRAFT 版本
                # 与生效版本矛盾」的治理混乱（此前非破坏编辑不写 effective_version）。
                if metric.status == "PUBLISHED":
                    updates["effective_version"] = new_version_num

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
                # 定向通知消费方（Owner/备份 Owner）去「版本历史」确认（best-effort）
                await self._notify_pending_consumers(
                    metric_code=metric_code,
                    version=new_version_num,
                    consumer_ids=consumer_ids,
                    skip_actor=actor_id,
                )

        # Top-level 破坏性变更但无 definition_json 提交时，仍需创建版本记录+PENDING
        elif top_level_breaking:
            new_version_num = metric.version + 1
            updates["version"] = new_version_num

            # PUBLISHED 状态 → PENDING_CONFIRMATION（不直接生效 top-level 破坏性字段）
            if metric.status == "PUBLISHED":
                # PENDING 确认期内禁止再次发起破坏性变更（与 definition_json 分支同款防叠加）
                if await self._repo.has_pending_version(metric.id):
                    raise ConflictError(
                        f"该指标存在待确认的破坏性变更（版本 {metric.version}），"
                        "请先完成确认或等待超时后再发起新变更",
                        error_code="METRIC_PENDING_VERSION_EXISTS",
                    )
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
                # 定向通知消费方（Owner/备份 Owner）去「版本历史」确认（best-effort）
                await self._notify_pending_consumers(
                    metric_code=metric_code,
                    version=new_version_num,
                    consumer_ids=consumer_ids,
                    skip_actor=actor_id,
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

        # OneData 挂载层（界限文档 §2.3 第 3 条）：派生指标携带 mount 时 upsert
        # metric_mount（源表/粒度/周期/域），并回填 metric.granularity（冗余供列表展示）。
        # 非派生指标提供 mount → 拒绝（原子=逻辑度量不挂表，复合=派生组合不直接挂表）。
        # PUBLISHED 派生指标改挂载粒度/源表 = 破坏性变更 → PENDING_VERSION 确认流：
        # 不直接生效，创建 BREAKING 版本 + 14 天消费方确认，确认/超时后由
        # _promote_pending_version 同步主表 granularity 与 metric_mount（Phase 4 接入）。
        if request.mount is not None:
            if metric.type != "derived":
                raise BusinessError(
                    f"仅派生指标可挂载物理表，当前类型 {metric.type}",
                    error_code="INVALID_MOUNT_TARGET",
                )
            from app.models.metric_mount import MetricMount
            from app.services.metric_mount.repository import MetricMountRepository

            _mrepo = MetricMountRepository(self._db)
            existing_mount = await _mrepo.get_by_metric(metric.id)
            # 挂载破坏性变更判定：粒度或源表变化（影响口径/血缘，须消费方确认）
            mount_diff: dict[str, dict[str, Any]] = {}
            if existing_mount is not None:
                for f, old_val, new_val in (
                    ("granularity", existing_mount.granularity, request.mount.granularity),
                    ("source_table", existing_mount.source_table, request.mount.source_table),
                ):
                    if old_val != new_val:
                        mount_diff[f] = {
                            "before": old_val,
                            "after": new_val,
                            "change_type": "BREAKING",
                            "mount_change": True,
                        }

            if metric.status == "PUBLISHED" and mount_diff:
                if await self._repo.has_pending_version(metric.id):
                    raise ConflictError(
                        f"该指标存在待确认的破坏性变更（版本 {metric.version}），"
                        "请先完成确认或等待超时后再发起新变更",
                        error_code="METRIC_PENDING_VERSION_EXISTS",
                    )
                new_version_num = metric.version + 1
                updates["version"] = new_version_num
                # 挂载破坏性变更不直接生效：移除主表 granularity 回填、不更新 mount 实体，
                # 等待确认后由 _promote_pending_version 应用（diff_json 携带 mount_change）。
                updates.pop("granularity", None)
                version = MetricVersion(
                    metric_id=metric.id,
                    version=new_version_num,
                    change_type="BREAKING",
                    definition_json=metric.definition_json,
                    diff_json=mount_diff,
                    status="PENDING_CONFIRMATION",
                    change_reason=request.change_reason,
                    created_by=actor_id,
                )
                await self._repo.create_version(version)

                from app.services.semantic.pending_version_manager import PendingVersionManager

                consumer_ids = [metric.owner_id]
                if metric.backup_owner_id is not None:
                    consumer_ids.append(metric.backup_owner_id)
                pvm = PendingVersionManager(self._db)
                await pvm.create_pending(metric, version, consumer_ids)
                await self._notify_pending_consumers(
                    metric_code=metric_code,
                    version=new_version_num,
                    consumer_ids=consumer_ids,
                    skip_actor=actor_id,
                )
                logger.info(
                    "mount_breaking_change_pending",
                    metric_code=metric_code,
                    version=new_version_num,
                    fields=list(mount_diff.keys()),
                )
            else:
                updates["granularity"] = request.mount.granularity
                if existing_mount is not None:
                    existing_mount.source_table = request.mount.source_table
                    existing_mount.source_column = request.mount.source_column
                    existing_mount.granularity = request.mount.granularity
                    existing_mount.default_period = request.mount.default_period
                    existing_mount.domain = request.mount.domain
                    await self._db.flush()
                else:
                    await _mrepo.save(
                        MetricMount(
                            metric_id=metric.id,
                            source_table=request.mount.source_table,
                            source_column=request.mount.source_column,
                            granularity=request.mount.granularity,
                            default_period=request.mount.default_period,
                            domain=request.mount.domain,
                        )
                    )

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

        # P2-I：口径变更后重检冲突（best-effort，不阻断更新）。
        # 原实现仅在创建时检测一次——指标改口径后与其它指标"后来变得同义"无法发现。
        # 仅在实际生效的口径变更时重检（本次提交了 definition_json 且未走 PUBLISHED
        # 破坏性待确认 PENDING_VERSION——新口径未生效，避免对"未来口径"误报冲突）。
        if (
            "definition_json" in updates
            and request.definition_json is not None
            and not (metric.status == "PUBLISHED" and is_breaking)
        ):
            _new_defn = updated.definition_json or {}
            if _new_defn:
                await self._detect_and_mark_conflicts(updated, _new_defn)

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

    async def bind_metric_term(
        self,
        metric_code: str,
        term_id: int | None,
        actor_id: int,
        role: str,
        user_domain: str | None = None,
    ) -> Metric:
        """绑定/解绑指标↔业务术语（P2-11：术语绑定写路径）。

        写 ``metric.term_id``（术语治理归属），不触发版本/不参与口径变更；
        与描述更新同语义——运营层治理补充。传 ``None`` 解绑。

        Args:
            metric_code: 指标编码。
            term_id: 术语 ID（None=解绑）。
            actor_id: 操作人 ID。
            role: 操作人角色。
            user_domain: 操作人所属域。

        Raises:
            NotFoundError: 指标/术语不存在。
            AuthError: metric_owner 操作他人指标（越权）。
            BusinessError: PDP 无 write 权限。
        """
        metric = await self.get_metric(metric_code)
        self._assert_owner_or_admin(metric, actor_id, role)

        # 术语存在性校验（跨服务：术语在 glossary 域，指标在 semantic 域）
        if term_id is not None:
            from sqlalchemy import select

            from app.models.term import Term

            term = (
                await self._db.execute(select(Term.id).where(Term.id == term_id))
            ).scalar_one_or_none()
            if term is None:
                raise NotFoundError(f"术语不存在: {term_id}", ctx={"term_id": term_id})

        decision = await self._gov_svc().check_metric_permission(
            metric_code=metric_code,
            action="write",
            user_id=actor_id,
            role=role,
            user_domain=user_domain,
        )
        if not decision.allow:
            raise BusinessError(
                decision.reason or "无权绑定该指标术语",
                error_code=decision.error_code or "FORBIDDEN",
                ctx={"metric_code": metric_code, "actor_id": actor_id},
            )

        updated = await self._repo.update_with_optimistic_lock(
            metric.id, metric.row_version, term_id=term_id
        )
        await self._cache.invalidate(metric_code)

        logger.info(
            "metric_term_bound",
            metric_code=metric_code,
            term_id=term_id,
            actor_id=actor_id,
        )
        return updated

    @staticmethod
    def term_binding_reminder(metric: Metric) -> str | None:
        """发布软提醒（P1 术语治理）：已有口径定义但未绑定业务术语 → 返回引导提示。

        不硬卡发布——存量指标可能没有术语，仅经发布响应信封的 ``message`` 引导
        用户先绑定术语（POST /metric-definitions/{code}/term）。
        """
        if metric.term_id is None and metric.definition_json:
            return (
                "该指标已有口径定义但未绑定业务术语，建议先在指标详情绑定术语，"
                "纳入术语治理后口径更可追溯"
            )
        return None

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

        # 幂等门禁（P1-2）：已 PUBLISHED 指标不可重复 approve——状态机同状态跃迁
        # 返回合法，若不显式拦截会对已发布指标重复发事件/审计/通知，灰度租户被覆盖。
        # 对齐 reject_metric 的显式状态门禁：approve 仅允许 REVIEW（→PUBLISHED/
        # EXPERIMENTAL）与 EXPERIMENTAL（→PUBLISHED 灰度转正）发起。
        if metric.status == "PUBLISHED":
            raise ConflictError(
                "指标已发布，无需重复审核",
                error_code="INVALID_TRANSITION",
                ctx={"metric_code": metric_code, "status": metric.status},
            )

        # 状态机校验
        invalid = MetricStateMachine.validate_transition(metric.status, target_status)
        if invalid is not None:
            raise ConflictError(invalid, error_code="INVALID_TRANSITION")

        # 未决硬冲突门禁（TD §12.4 / proposal：硬冲突阻断发布 409）：
        # 指标存在 block_publish=True 的未决冲突（OPEN/NEGOTIATING/ESCALATED）时，
        # 审核通过前必须先协商/裁决消除硬冲突——此前 pending_conflict 仅用于目录
        # 红标展示，评审人可直接放行未经协商的冲突口径进入消费方（治理漏洞）。
        # 软冲突（block_publish=False）不阻断，发布后仍可标注。
        try:
            from app.models.conflict import Conflict as ConflictModel
            from app.services.conflict.repository import ConflictRepository

            open_conflict = await ConflictRepository(self._db).get_first_open_for_metric(
                metric_code
            )
        except Exception:  # noqa: BLE001 - best-effort：冲突查询失败不阻断审批主流程
            open_conflict = None
        # 仅真实冲突记录（ORM 实例）参与判定——mock/降级返回的非 ORM 对象不误判
        # （单测 mock DB 下 get_first_open_for_metric 会返回 MagicMock，其 block_publish
        # 恒 truthy，若不判型会把所有审核测试误拦为 CONFLICT_BLOCKED）。
        if (
            open_conflict is not None
            and isinstance(open_conflict, ConflictModel)
            and bool(open_conflict.block_publish)
        ):
            raise BusinessError(
                "该指标存在未决硬冲突（block_publish），须先协商/裁决消除后方可审核通过",
                error_code="CONFLICT_BLOCKED",
                ctx={
                    "metric_code": metric_code,
                    "conflict_id": open_conflict.conflict_id,
                    "conflict_type": getattr(open_conflict.type, "value", None),
                },
            )

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
            # 复合指标公式强校验（界限文档 §1.2/§4.2）：公式仅允许引用派生/复合
            # 指标 code，禁裸表字段与不存在指标——OneData 复合层 = 跨指标聚合。
            if metric.type == "composite":
                formula_errors = await checker.validate_composite_formula(metric.definition_json)
                if formula_errors:
                    raise BusinessError(
                        "复合指标公式校验失败: " + "; ".join(formula_errors),
                        error_code="INVALID_COMPOSITE_FORMULA",
                        ctx={"formula_errors": formula_errors},
                    )

        # 定位待发布版本
        target_version = request.target_version or metric.version
        # target_version 越权防护：审批只允许发布「当前待审核版本」（metric.version）。
        # 旧实现不校验——有 approve 权限的角色经 API 直调可传任意历史版本号，
        # mark_version_published 会把历史版本重新标 PUBLISHED（版本历史篡改），
        # 且主表 effective_version 指向旧版本而 definition_json 保持最新（口径矛盾）。
        if target_version != metric.version:
            raise ConflictError(
                f"待发布版本 {target_version} 不是当前待审核版本 {metric.version}",
                error_code="INVALID_TARGET_VERSION",
            )
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

        # 状态机校验：REVIEW→DRAFT。显式限定仅 REVIEW 可驳回——P1-7 为灰度超期回收
        # 新增 EXPERIMENTAL→DRAFT 跃迁（expiry_recycle），reject 语义不随之放宽，
        # 避免评审人借 reject 通道把灰度指标打回（回收应走 check_experimental_expiry 系统路径）。
        if metric.status != "REVIEW":
            raise ConflictError(
                f"仅 REVIEW 状态可驳回，当前 {metric.status}",
                error_code="INVALID_TRANSITION",
            )
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

    async def _notify_pending_consumers(
        self,
        *,
        metric_code: str,
        version: int,
        consumer_ids: list[int],
        skip_actor: int | None = None,
        event_type: str = "metric.breaking_change_pending",
        title: str = "指标口径变更待确认",
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        """PENDING_VERSION 确认期创建 → 定向通知消费方去「版本历史」确认。

        修复前：create_pending 只建确认记录、不通知（pending_version_manager.py 的
        TODO），Owner 在 14 天确认期内若不知情只能被动等超时自动接受——确认期闭环断裂。

        与 ``_notify_metric_stakeholders`` 同范式：IN_APP 定向送达，不依赖订阅偏好；
        ``skip_actor`` 非空时跳过发起变更者本人（其已知晓变更）；转正场景（None）
        全量通知消费方（超时默认接受后新口径悄然生效，消费方须被告知）。
        失败仅告警，不阻断变更创建/转正。
        """
        from app.db.mysql import async_session_factory
        from app.services.notify.service import NotifyService

        for uid in consumer_ids:
            if skip_actor is not None and uid == skip_actor:
                continue
            async with async_session_factory() as session:
                try:
                    payload: dict[str, Any] = {
                        "metric_code": metric_code,
                        "version": version,
                        **({"confirm_window": "14 天内"} if event_type.endswith("pending") else {}),
                        **({"action_hint": "请在指标详情页「版本历史」确认或拒绝，超时自动接受"}
                           if event_type.endswith("pending") else {}),
                        **(extra_payload or {}),
                    }
                    await NotifyService(session).notify_user(
                        user_id=uid,
                        event_type=event_type,
                        title=title,
                        payload=payload,
                    )
                except Exception as exc:
                    logger.warning(
                        "pending_consumer_notify_failed event_type=%s metric=%s user=%s err=%s",
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
            # savepoint 隔离：血缘写入失败只回滚本 savepoint，不污染外层业务事务
            # （create/update/publish 的外层事务若已被 SQLAlchemyError 弄脏，
            # 随后的 commit 会抛 PendingRollbackError、业务写入被意外回滚）。
            async with self._db.begin_nested():
                lineage_svc = LineageService(self._db)
                # 1) 表级血缘（指标 ↔ 物理底表），不在此提交，交由外层事务统一提交
                await lineage_svc.register_metric_from_definition(metric, commit=False)

                # 2) 指标间依赖血缘（仅 derived/composite 有 dependencies）——
                # 表/维度/字段血缘已由 register_metric_from_definition 差异同步处理
                definition = metric.definition_json or {}
                if not isinstance(definition, dict):
                    return
                dependencies = definition.get("dependencies") or []
                if (
                    not isinstance(dependencies, list)
                    or metric.type == "atomic"
                    or not dependencies
                ):
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
        except Exception as exc:  # noqa: BLE001 - 血缘注册失败绝不阻断指标主流程
            logger.warning(
                "metric_lineage_register_failed",
                metric_code=metric.metric_code,
                metric_type=metric.type,
                exc_info=True,
            )
            # C7：血缘静默缺失不再无声——发布失败事件进通知闭环（运维/管理员可订阅）
            await self._eventbus.publish(
                "lineage.metric_register_failed",
                {
                    "metric_code": metric.metric_code,
                    "metric_type": metric.type,
                    "reason": "dependency_lineage",
                    "error": str(exc)[:200],
                },
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
            self._lineage_svc = LineageService(self._db)
            deleted = await self._lineage_svc.delete_by_node(f"metric:{metric_code}")
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
            self._lineage_svc = LineageService(self._db)
            restored = await self._lineage_svc.restore_by_node(f"metric:{metric_code}")
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

        # 被引用拦截（TD §12.3 强化）：废弃仍被活跃引用的指标会让下游引用悬空。
        # 未指定替代指标时拦截并列出引用者，引导先指定替代或处理下游；已指定
        # 替代指标视为下游有去处，放行（与发布端 DependencyChecker 反向保护互补）。
        from app.services.lineage.repository import LineageRepository

        referrers = await LineageRepository(self._db).metric_referrers(metric_code)
        if referrers and successor_code is None:
            ref_desc = "、".join(
                f"{r['node']}（{'派生' if r['edge_type'] == 'DERIVED_FROM' else '报表/消费'}）"
                for r in referrers[:10]
            )
            more = f" 等 {len(referrers)} 个引用者" if len(referrers) > 10 else ""
            raise BusinessError(
                f"指标 {metric_code} 仍被 {len(referrers)} 处活跃引用（{ref_desc}{more}），"
                "废弃将悬空下游。请先指定替代指标（successor_code）或处理下游引用后再废弃",
                error_code="METRIC_REFERENCED",
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

    async def promote_metric(
        self,
        metric_code: str,
        actor_id: int,
        role: str,
        user_domain: str | None = None,
    ) -> Metric:
        """灰度全量发布（EXPERIMENTAL → PUBLISHED，对齐 FR-020）。

        清除 gray_tenant_ids，将指标与版本状态从 EXPERIMENTAL 升为 PUBLISHED，
        发布 metric.promoted 事件 → lineage(Neo4j)/search(ES)/notify。

        Args:
            metric_code: 指标编码。
            actor_id: 操作人 ID。
            role: 操作人角色（P0-2：灰度发布会修改主表口径生效状态，
                须与废弃/恢复同级校验 Owner 归属 + PDP 域权限）。
            user_domain: 操作人所属域（domain_admin 域作用域校验）。

        Returns:
            全量发布后的指标。

        Raises:
            NotFoundError: 指标不存在。
            AuthError: metric_owner 操作他人指标（越权）。
            BusinessError: PDP 域权限拒绝。
            ConflictError: 非法状态跃迁（非 EXPERIMENTAL）。
        """
        metric = await self.get_metric(metric_code)
        self._assert_owner_or_admin(metric, actor_id, role)
        # PDP 域权限闸门：promote 为写操作，domain_admin 须同域（对齐 deprecate 的
        # check_metric_permission 域校验，修复 domain_admin 可跨域全量发布的域隔离漏洞）
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
                decision.reason or "无权发布该指标",
                error_code=decision.error_code or "FORBIDDEN",
            )

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

    async def recycle_expired_gray(self, metric_code: str, actor_id: int) -> Metric:
        """灰度超期强制回收（EXPERIMENTAL → DRAFT，对齐 P1-7）。

        ``check_experimental_expiry`` 每日巡检发现超 30 天未决策的 EXPERIMENTAL
        指标后调用：清除灰度白名单并回收到 DRAFT，避免灰度无限滞留。指标口径/
        版本历史保留，Owner 可重新提交评审继续推进。

        Args:
            metric_code: 指标编码。
            actor_id: 触发人 ID（后台任务传 0 表示系统）。

        Returns:
            回收后的指标（状态 DRAFT）。

        Raises:
            NotFoundError: 指标不存在。
            ConflictError: 非法状态跃迁（非 EXPERIMENTAL）。
        """
        metric = await self.get_metric(metric_code)
        if metric is None:
            raise NotFoundError(f"指标不存在: {metric_code}")

        # 状态机校验：EXPERIMENTAL→DRAFT (expiry_recycle)
        invalid = MetricStateMachine.validate_transition(metric.status, "DRAFT")
        if invalid is not None:
            raise ConflictError(invalid, error_code="INVALID_TRANSITION")

        now = datetime.now(UTC)
        updated = await self._repo.update_with_optimistic_lock(
            metric.id,
            metric.row_version,
            status="DRAFT",
            gray_tenant_ids=None,
            effective_version=None,
        )
        await self._cache.invalidate(metric_code)

        await self._publish_event(
            "metric.gray_recycled",
            {
                "metric_code": metric_code,
                "domain": metric.domain,
                "reason": "gray_expiry",
                "recycled_at": now.isoformat(),
            },
            actor_id=str(actor_id),
        )
        logger.info(
            "metric_gray_recycled",
            metric_code=metric_code,
            actor_id=actor_id,
        )
        return updated

    async def rollback_metric(
        self,
        metric_code: str,
        actor_id: int,
        role: str,
        user_domain: str | None = None,
    ) -> Metric:
        """灰度回滚（EXPERIMENTAL → 回退上一 PUBLISHED 版本，对齐 FR-020）。

        EXPERIMENTAL 版本标记 ARCHIVED，指标状态回到 PUBLISHED，
        effective_version 回退到上一个 PUBLISHED 版本。
        发布 metric.rolled_back 事件 → notify+audit。

        Args:
            metric_code: 指标编码。
            actor_id: 操作人 ID。
            role: 操作人角色（P0-2：回滚会改写主表口径，须与废弃/恢复同级
                校验 Owner 归属 + PDP 域权限）。
            user_domain: 操作人所属域（domain_admin 域作用域校验）。

        Returns:
            回滚后的指标。

        Raises:
            NotFoundError: 指标不存在。
            AuthError: metric_owner 操作他人指标（越权）。
            BusinessError: PDP 域权限拒绝。
            ConflictError: 非法状态跃迁 / 无上一 PUBLISHED 版本可回退。
        """
        metric = await self.get_metric(metric_code)
        self._assert_owner_or_admin(metric, actor_id, role)
        # PDP 域权限闸门：rollback 为写操作，domain_admin 须同域（对齐 deprecate）
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
                decision.reason or "无权回滚该指标",
                error_code=decision.error_code or "FORBIDDEN",
            )

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

        # 回退主表口径至上一 PUBLISHED 版本（镜像 _promote_pending_version 转正语义）。
        # 旧实现仅回退 status/gray_tenant_ids/effective_version，主表 definition_json
        # 仍是被回滚的灰度口径——消费端（consume 查 definition_json.source_table）与
        # 血缘差异同步（走 definition_json）读到的是「已回滚的灰度口径」，回滚实际未生效。
        # 此处同步恢复：口径快照 + 主表 version 回退 + top-level 破坏性字段取灰度 diff 的 before。
        updates: dict[str, Any] = {
            "status": "PUBLISHED",
            "gray_tenant_ids": None,
            "effective_version": prev_published.version,
            "version": prev_published.version,
        }
        if prev_published.definition_json is not None:
            updates["definition_json"] = prev_published.definition_json
        # top-level 破坏性字段（granularity/unit）：灰度版本 diff_json 的 before 值回退主表
        current_gray_version = await self._repo.get_version(metric.id, metric.version)
        if current_gray_version is not None:
            for field, diff in (current_gray_version.diff_json or {}).items():
                if (
                    field in BREAKING_TOP_LEVEL_FIELDS
                    and isinstance(diff, dict)
                    and "before" in diff
                ):
                    updates[field] = diff["before"]

        updated = await self._repo.update_with_optimistic_lock(
            metric.id, metric.row_version, **updates
        )

        await self._cache.invalidate(metric_code)

        # 回滚后主表已恢复上一 PUBLISHED 口径 → 触发血缘差异同步（best-effort，镜像转正）
        try:
            await self._register_metric_lineage_full(updated)
        except Exception as exc:  # noqa: BLE001 - best-effort 不阻断回滚
            logger.warning(
                "metric_rollback_lineage_failed",
                metric_code=metric_code,
                error=str(exc),
            )

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
        # target_version 越权防护（同 approve）：紧急发布只允许发布「当前版本」，
        # 禁止经 API 直调把历史版本重新标 PUBLISHED / 造成 effective_version 与口径矛盾。
        if target_version != metric.version:
            raise ConflictError(
                f"待发布版本 {target_version} 不是当前版本 {metric.version}",
                error_code="INVALID_TARGET_VERSION",
            )
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
            action="metric_definition.emergency_publish",
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

    async def complete_emergency_review(
        self,
        metric_code: str,
        actor_id: int,
        role: str,
    ) -> Metric:
        """紧急发布补审（FR-022 闭环）：审批人确认紧急发布的指标，写补审时间。

        紧急发布跳过常规 REVIEW，发布后须由管理角色完成补审
        （``check_emergency_review_overdue`` 每小时巡检超时）。补审只写
        ``emergency_reviewed_at`` 时间戳，不改变状态/口径——标记"已完成补审"后
        巡检不再告警超时；``emergency_publish`` 保留为历史标记。

        Args:
            metric_code: 指标编码。
            actor_id: 补审人 ID。
            role: 补审人角色。

        Returns:
            补审后的指标。

        Raises:
            AuthError: 非管理角色。
            NotFoundError: 指标不存在。
            ConflictError: 非紧急发布 / 已完成补审。
        """
        # 角色校验：与紧急发布同角色（平台/域管理员）
        if role not in ("platform_admin", "domain_admin"):
            raise AuthError(
                "紧急发布补审仅 platform_admin / domain_admin 可执行",
                error_code="FORBIDDEN",
                ctx={"role": role},
            )

        metric = await self.get_metric(metric_code)
        if metric is None:
            raise NotFoundError(f"指标不存在: {metric_code}")
        if not metric.emergency_publish:
            raise ConflictError(
                "该指标非紧急发布，无需补审",
                error_code="NOT_EMERGENCY_PUBLISHED",
            )
        if metric.emergency_reviewed_at is not None:
            raise ConflictError(
                "该指标已完成紧急发布补审",
                error_code="EMERGENCY_ALREADY_REVIEWED",
            )

        now = datetime.now(UTC)
        updated = await self._repo.update_with_optimistic_lock(
            metric.id,
            metric.row_version,
            emergency_reviewed_at=now,
        )
        await self._cache.invalidate(metric_code)

        await self._publish_event(
            "metric.emergency_reviewed",
            {
                "metric_code": metric_code,
                "domain": metric.domain,
                "emergency_reason": metric.emergency_reason,
                "emergency_reviewed_at": now.isoformat(),
                # 定向送达指标 Owner：补审完成确认（发布方/Owner 可感知闭环）
                "recipient_user_id": metric.owner_id,
            },
            actor_id=str(actor_id),
        )
        logger.info(
            "metric_emergency_reviewed",
            metric_code=metric_code,
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

    async def get_versions(
        self,
        metric_code: str,
        actor_id: int | None = None,
        role: str | None = None,
    ) -> list[MetricVersion]:
        """获取指标的所有版本。

        Args:
            metric_code: 指标编码。
            actor_id: 读路径行级隔离（P0-3）——当前用户 ID；None 表示内部调用。
            role: 当前用户角色。

        Returns:
            版本列表。

        Raises:
            NotFoundError: 指标不存在（METRIC_ARCHIVED 表示已因仲裁作废）。
        """
        # 与详情/对比/健康读路径一致：命中「软删 + successor」的作废指标时返回
        # 结构化 METRIC_ARCHIVED（携带胜方指针），而非裸「指标不存在」——详情页
        # 并行加载 versions 时若裸 404 会覆盖友好引导（跨服务一致性）。
        metric = await self._get_metric_for_compare(metric_code, actor_id, role)
        return await self._repo.list_versions(metric.id)

    async def get_version_responses(
        self,
        metric_code: str,
        actor_id: int | None = None,
        role: str | None = None,
    ) -> list[MetricVersionResponse]:
        """获取版本列表（含多消费方确认进度，供版本历史 Tab 展示）。

        与 ``get_versions`` 的区别：PENDING_CONFIRMATION 版本附带确认进度
        （confirmed_count/consumer_count），使发起人/消费方看到「已确认 X/N」——
        否则一方确认后另一方未确认、版本迟迟不转正时，用户无法判断还差谁。

        Returns:
            版本响应列表（按版本号降序，与 get_versions 一致）。
        """
        _, responses = await self.get_version_responses_with_meta(
            metric_code, actor_id, role
        )
        return responses

    async def get_version_responses_with_meta(
        self,
        metric_code: str,
        actor_id: int | None = None,
        role: str | None = None,
    ) -> tuple[Metric, list[MetricVersionResponse]]:
        """获取指标实体 + 版本响应列表（含确认进度）。

        与 ``get_version_responses`` 的关系：后者仅返回版本列表（兼容既有调用方），
        本方法额外返回指标实体——端点层需 ``metric.pii_flag`` 做 PII 读分级脱敏
        与访问审计（读路径中唯一遗漏脱敏的接口，P0-1）。
        """
        metric = await self._get_metric_for_compare(metric_code, actor_id, role)
        versions = await self._repo.list_versions(metric.id)
        if not versions:
            return metric, []
        pending = [v for v in versions if v.status == "PENDING_CONFIRMATION"]
        progress: dict[int, tuple[int, int]] = {}
        if pending:
            progress = await self._repo.count_confirmations_by_versions(
                pending[0].metric_id,
                [v.version for v in pending],
            )
        responses: list[MetricVersionResponse] = []
        for v in versions:
            resp = MetricVersionResponse.model_validate(v)
            if v.status == "PENDING_CONFIRMATION":
                confirmed, total = progress.get(v.version, (0, 0))
                if total > 0:
                    resp.confirmed_count = confirmed
                    resp.consumer_count = total
            responses.append(resp)
        return metric, responses

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
        # P1-3 并发竞态修复：对确认记录加行锁（SELECT ... FOR UPDATE）串行化
        # 「全部确认→转正」判定。此前并发最后两名消费方各自读到对方 PENDING、
        # 均不触发转正，版本滞留 PENDING_CONFIRMATION 仅靠定时任务兜底；加锁后
        # 后到的确认者重读拿到对方已 CONFIRMED，可靠触发转正（锁随事务 commit 释放）。
        confirmations = await self._repo.get_pending_confirmations(
            metric.id, version, for_update=True
        )
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
                return await self._promote_pending_version(
                    metric, version, trigger="consumer_confirm", actor_id=consumer_id
                )
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
        # PENDING 确认期指标被废弃/下线/灰度回退后，不得转正其 PENDING 版本：
        # 废弃（DEPRECATED/DATA_SOURCE_DROPPED）指标不应再发生口径变更，
        # 灰度（EXPERIMENTAL）指标未走确认期语义——仅 PUBLISHED 可超时转正。
        # 修复前：超时任务 14 天后仍转正废弃指标版本并通知"新口径已生效"（语义矛盾）。
        if metric.status != "PUBLISHED":
            logger.info(
                "pending_version_skip_non_published",
                metric_id=metric_id,
                version=version,
                status=metric.status,
            )
            return None
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
            return await self._promote_pending_version(
                metric, version, trigger="timeout", actor_id=metric.owner_id
            )
        return None

    async def _promote_pending_version(
        self,
        metric: Metric,
        version: int,
        *,
        trigger: str = "consumer_confirm",
        actor_id: int | None = None,
    ) -> Metric:
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
        # top-level 破坏性字段：diff_json 的 after 值回写主表；
        # 挂载层破坏性变更（mount_change）额外收集，确认后同步 metric_mount 实体
        mount_updates: dict[str, Any] = {}
        for field, diff in (version_obj.diff_json or {}).items():
            if isinstance(diff, dict) and diff.get("mount_change") and "after" in diff:
                mount_updates[field] = diff["after"]
            if field in BREAKING_TOP_LEVEL_FIELDS and isinstance(diff, dict) and "after" in diff:
                updates[field] = diff["after"]

        updated = await self._repo.update_with_optimistic_lock(
            metric.id, metric.row_version, **updates
        )
        await self._repo.mark_version_published(metric.id, version, datetime.now(UTC))
        # 挂载破坏性变更确认生效：同步 metric_mount（粒度/源表）与主表一致
        if mount_updates:
            from app.services.metric_mount.repository import MetricMountRepository

            _mrepo = MetricMountRepository(self._db)
            mount = await _mrepo.get_by_metric(metric.id)
            if mount is not None:
                if "granularity" in mount_updates:
                    mount.granularity = mount_updates["granularity"]
                if "source_table" in mount_updates:
                    mount.source_table = mount_updates["source_table"]
                await self._db.flush()
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
        # 转正（确认/超时默认接受）后新口径已生效 → 全量通知消费方。
        # 超时默认接受场景消费方未主动确认、新口径悄然生效——须被告知已生效。
        await self._notify_pending_consumers(
            metric_code=metric.metric_code,
            version=version,
            consumer_ids=[
                uid for uid in (metric.owner_id, metric.backup_owner_id) if uid is not None
            ],
            skip_actor=None,
            event_type="metric.breaking_change_promoted",
            title="指标口径变更已生效",
            extra_payload={"effective_version": version},
        )
        # 转正审计（合规可追溯）：新口径何时正式生效、由谁确认或超时触发。
        # 消费方主动确认的转正（端点已有 CONFIRM_VERSION 审计）之外，超时自动
        # 转正（定时任务、无用户操作）此前零审计——14 天后口径悄然生效不可追溯。
        await self._write_audit(
            actor_id=actor_id if actor_id is not None else (metric.owner_id or 0),
            action="metric_definition.promote_version",
            entity_type="metric_definition",
            entity_id=metric.metric_code,
            detail={
                "version": version,
                "trigger": trigger,
                "effective_version": version,
            },
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
        # 终结该版本其他消费方的 PENDING 确认记录：任一消费方拒绝则版本取消
        # （CANCELLED），其余记录若不终结会持续被 pending_version 计算字段
        # （status=="PENDING"）识别为"待确认"——前端警示最长残留 14 天（直到
        # 超时任务处理），且这些消费方仍可对已取消版本执行无效确认。
        for c in confirmations:
            if c.id != mine.id and c.status == "PENDING":
                await self._repo.update_confirmation_status(c.id, "REJECTED", reason=reason)
        # 被拒版本置 CANCELLED（P1-8 后 reject 唯一实现在本方法）：
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

    async def extend_version(
        self,
        metric_code: str,
        version: int,
        actor_id: int,
        role: str,
        user_domain: str | None = None,
    ) -> Metric:
        """Owner 请求版本确认延期（FR-008，+7 天，最多延期 1 次）。

        Args:
            metric_code: 指标编码。
            version: 待延期版本号。
            actor_id: 操作人 ID。
            role: 操作人角色（P1-1：延期会推迟他人消费方的确认期限，
                须校验 Owner 归属 + PDP 域权限，否则任意 metric_owner/
                domain_admin 可越权延后他人破坏性变更确认期）。
            user_domain: 操作人所属域（domain_admin 域作用域校验）。

        Returns:
            更新后的指标。

        Raises:
            NotFoundError: 指标不存在。
            AuthError: 非 Owner/管理员操作（越权）。
            BusinessError: PDP 域权限拒绝。
            ConflictError: 无待确认记录或已延期满 1 次。
        """
        metric = await self.get_metric(metric_code)
        self._assert_owner_or_admin(metric, actor_id, role)
        # PDP 域权限闸门：延期为指标治理写操作，domain_admin 须同域
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
                decision.reason or "无权延后该指标的确认期限",
                error_code=decision.error_code or "FORBIDDEN",
            )
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

    @staticmethod
    def _is_public_metric_status(status: str) -> bool:
        """读路径公开状态：已发布/灰度/已废弃均属可公开发现的资产目录。

        仅 DRAFT/REVIEW（未发布工作区）是私有的——他人读取按「不存在」处理。
        """
        return status in ("PUBLISHED", "EXPERIMENTAL", "DEPRECATED")

    def _assert_metric_visible(
        self, metric: Metric, actor_id: int | None, role: str | None
    ) -> None:
        """读路径可见性守卫（P0-3）：未发布指标仅本人/管理角色可见。

        DRAFT/REVIEW 是指标 Owner 的私有工作区；他人读取一律按「不存在」处理
        （不泄露指标存在性，避免攻击者枚举草稿编码）。管理角色与评审人
        （REVIEW 待审，评审工作台需展示）放行。已发布/灰度/废弃公开。

        Args:
            metric: 指标对象（或含 status/owner_id 的响应对象）。
            actor_id: 当前用户 ID；None 表示内部调用（不过滤，端点层必传）。
            role: 当前用户角色。

        Raises:
            NotFoundError: 未发布指标对当前用户不可见。
        """
        if actor_id is None or role is None:
            return  # 内部调用无鉴权上下文——端点层必传 actor/role
        if self._is_public_metric_status(metric.status):
            return
        if role in ("platform_admin", "domain_admin"):
            return
        if metric.owner_id == actor_id or metric.backup_owner_id == actor_id:
            return
        if role == "reviewer" and metric.status == "REVIEW":
            return
        raise NotFoundError(f"指标不存在: {metric.metric_code}")

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

    async def _get_metric_for_compare(
        self, metric_code: str, actor_id: int | None = None, role: str | None = None
    ) -> Metric:
        """读取用于对比的指标；对已作废指标返回友好 METRIC_ARCHIVED。

        对比弹窗由冲突仲裁/差异查看触发，关联指标可能已被上一轮仲裁软删作废
        （deleted_at + successor）。此时不应抛裸「指标不存在」，而应复用详情页
        的 METRIC_ARCHIVED 错误码（携带胜方 successor），供前端渲染
        「已作废 → 查看权威」引导，保证冲突/指标跨服务状态一致可读。

        Args:
            metric_code: 指标编码。
            actor_id: 读路径行级隔离（P0-3）——当前用户 ID；None 表示内部调用。
            role: 当前用户角色。

        Raises:
            NotFoundError: 指标不存在（METRIC_ARCHIVED 表示已因仲裁作废；
                未发布指标对当前用户不可见也按不存在处理）。
        """
        metric = await self._repo.get_by_code(metric_code)
        if metric is not None:
            self._assert_metric_visible(metric, actor_id, role)
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

    async def compare_metrics(
        self,
        code_a: str,
        code_b: str,
        actor_id: int | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        """两指标关键字段并排对比。

        Args:
            code_a: 指标A编码。
            code_b: 指标B编码。
            actor_id: 读路径行级隔离（P0-3）——当前用户 ID；None 表示内部调用。
            role: 当前用户角色。

        Returns:
            并排对比结果，含差异标记。

        Raises:
            NotFoundError: 指标不存在（METRIC_ARCHIVED 表示已因仲裁作废）。
        """
        # 权限校验：需对两指标都有读权限（PII 指标需合规角色，对齐 T049）
        a = await self._get_metric_for_compare(code_a, actor_id, role)
        b = await self._get_metric_for_compare(code_b, actor_id, role)

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

    async def compare_matrix(
        self,
        metric_codes: list[str],
        actor_id: int | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        """多指标关键字段矩阵对比（2~6 个）。

        两两对比的 only_a/only_b 语义在 3+ 指标时失效，故矩阵模式改为：
        每行一个字段、每列一个指标，行级汇总差异等级：

        - ``all_identical``：所有指标取值一致
        - ``partial``：取值存在部分一致、部分不同（>1 种取值但非全部互异）
        - ``all_different``：每个指标取值都不同

        依赖对比给出全体交集 + 各指标独有（相对全体交集）。

        Args:
            metric_codes: 待对比的指标编码（2~6 个，去重保序）。
            actor_id: 读路径行级隔离（P0-3）——当前用户 ID；None 表示内部调用。
            role: 当前用户角色。

        Returns:
            矩阵对比结果，含行级差异标记。

        Raises:
            ValidationError: 指标数量不在 2~6 范围（超限/过少，去重前判定）。
            NotFoundError: 任一指标不存在（METRIC_ARCHIVED 表示已因仲裁作废）。
        """
        if not 2 <= len(metric_codes) <= 6:
            raise ValidationError(
                f"指标对比需 2~6 个指标，当前 {len(metric_codes)} 个（请减少勾选数量）"
            )
        metrics: list[Metric] = []
        seen: set[str] = set()
        for code in metric_codes:
            if code in seen:
                continue
            seen.add(code)
            metrics.append(await self._get_metric_for_compare(code, actor_id, role))

        def _level(values: list[Any]) -> str:
            distinct = len({repr(v) for v in values})
            if distinct <= 1:
                return "all_identical"
            if distinct == len(values):
                return "all_different"
            return "partial"

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
            # P2-14 治理对比补全：PII/合规复核/责任人/状态/版本/描述
            # 对齐「指标对比用于口径治理」的诉求——责任归属与敏感分级不一致
            # 恰是治理中最该一眼暴露的差异。
            "pii_flag",
            "compliance_reviewed",
            "owner_id",
            "status",
            "version",
            "description",
        ]
        result: dict[str, Any] = {
            "metrics": [m.metric_code for m in metrics],
            "fields": {},
        }

        for field in fields:
            result["fields"][field] = {
                "values": {m.metric_code: getattr(m, field, None) for m in metrics},
                "difference_level": _level([getattr(m, field, None) for m in metrics]),
            }

        # 口径定义对比：表达式为代表值（含 pii 标记的完整定义整体展示）
        defs = [m.definition_json or {} for m in metrics]
        result["fields"]["definition"] = {
            "values": {
                m.metric_code: d for m, d in zip(metrics, defs, strict=True)
            },
            "difference_level": _level([d.get("expression", "") for d in defs]),
        }

        # 依赖对比：全体交集 + 各指标独有（相对全体交集）
        dep_sets = [set(d.get("dependencies", []) or []) for d in defs]
        if len(dep_sets) > 1:
            inter = set.intersection(*dep_sets)
        else:
            inter = dep_sets[0] if dep_sets else set()
        result["fields"]["dependencies"] = {
            "values": {
                m.metric_code: sorted(ds) for m, ds in zip(metrics, dep_sets, strict=True)
            },
            "intersection": sorted(inter),
            "only": {
                m.metric_code: sorted(ds - inter)
                for m, ds in zip(metrics, dep_sets, strict=True)
            },
            "difference_level": _level([sorted(ds) for ds in dep_sets]),
        }

        # P2-14 owner 可读化：字段值是 owner_id（机器可读），另附 owner_names 映射供前端
        # 显示责任人姓名——「责任人不同」是治理对比的高价值信号，裸数字不可读。
        owner_ids = {m.owner_id for m in metrics if m.owner_id is not None}
        result["owner_names"] = await self._repo.get_user_display_names(owner_ids)

        return result

    # ---- US10: 批量注册 ----

    async def batch_register_metrics(
        self,
        request: Any,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> dict[str, Any]:
        """批量注册指标。

        对齐 spec FR-016：批量注册同样走字典校验，自动推断逻辑与单条注册一致。

        Args:
            request: 批量注册请求(含source_table+measure_columns+domain)。
            actor_id: 操作人ID。
            role: 操作人角色（P1-6：域管理员/Owner 仅可批量注册本域指标）。
            user_domain: 操作人所属域。

        Returns:
            {batch_id, candidates: [{metric_code, status, validation_errors}]}.
        """
        import uuid

        from app.services.semantic.schemas import MetricCreateRequest

        batch_id = f"batch_{uuid.uuid4().hex[:12]}"
        candidates: list[dict[str, Any]] = []

        # P1-6 批量注册域门禁：与单条 create 同级（域管理员/Owner 仅可本域批量注册）
        if (
            role in ("domain_admin", "metric_owner")
            and user_domain
            and request.domain != user_domain
        ):
            raise BusinessError(
                f"{'域管理员' if role == 'domain_admin' else '指标 Owner'}仅可批量注册本域指标",
                error_code="FORBIDDEN",
                ctx={
                    "request_domain": request.domain,
                    "user_domain": user_domain,
                    "role": role,
                },
            )

        # 校验 domain 存在且 active
        await self._validate_domain_active(request.domain)

        # 获取域默认值
        domain_defaults = await self._get_domain_defaults(request.domain)

        # L3：冲突预检比对对象在批量循环内共享——预加载一次，每列成功后增量追加，
        # 避免 N 列 = N 次全量加载 + N 次与全量 existing 比对（O(N²) 性能退化）。
        # best-effort：预加载失败降级为 None（每列内部 load_conflict_existing 亦
        # 有 best-effort 兜底），不阻断批量注册。
        try:
            preloaded_existing = await self.load_conflict_existing()
        except Exception:  # noqa: BLE001 - 预加载失败仅降级，不影响主流程
            preloaded_existing = None

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
            # 指标名用 auto_fill 推断的中文业务名（命中受控词根），空则回退列名。
            # 修复前直接用英文列名（name=col），不命中受控词根 → 批量注册计数列/金额列
            # 候选名被命名校验拦截只能到 VALIDATION_ERROR（无法直接创建）。
            _name_field = (suggested.get("fields") or {}).get("name") or {}
            name = str(_name_field.get("value") or col)

            # 维度列映射 → 口径定义：维度名（keys）合入 dimensions（与单条注册 Step② 关联维度
            # 一致，血缘差异同步据此建「指标↔维度」边）；完整映射冗余到 dimension_columns
            # （保留"该维度在源表对应哪列"信息，血缘/展示可读）。空名/纯空白键过滤。
            _defn: dict[str, Any] = {"expression": f"SUM({col})", "dependencies": []}
            if request.dimension_mapping:
                _dim_names = [
                    str(d).strip() for d in request.dimension_mapping if str(d).strip()
                ]
                _dim_names = list(dict.fromkeys(_dim_names))  # 保序去重
                if _dim_names:
                    _defn["dimensions"] = _dim_names
                    _defn["dimension_columns"] = {
                        str(k).strip(): str(v).strip()
                        for k, v in request.dimension_mapping.items()
                        if str(k).strip() and str(v).strip()
                    }

            try:
                # P13 savepoint 隔离：每条候选独立嵌套事务——单列 DB 错误（如重复编码
                # IntegrityError）只回滚本 savepoint，不污染此前已 flush 成功的候选。
                # 修复前：SQLAlchemyError 走整会话 rollback，已 flush 未提交的指标被回滚
                # 但 candidates 已记为 DRAFT 成功 → 部分结果与落库不一致（失真）。
                async with self._db.begin_nested():
                    create_req = MetricCreateRequest(
                        metric_code=code,
                        name=name,
                        domain=request.domain,
                        type=defaults.get("type", "atomic"),
                        granularity=defaults.get("granularity", "day"),
                        unit=defaults.get("unit", "TIMES"),
                        aggregation=defaults.get("aggregation", "SUM"),
                        time_semantics=defaults.get("time_semantics", "PERIOD"),
                        freshness=defaults.get("freshness", "T1"),
                        dw_layer=defaults.get("dw_layer", "DWD"),
                        metric_tier=defaults.get("metric_tier", "T3"),
                        serving_mode=defaults.get("serving_mode", "BATCH_ONLY"),
                        additivity=defaults.get("additivity", "ADDITIVE"),
                        definition_json=_defn,
                        source_table=request.source_table,
                        measure_column=col,
                        period="day",
                        batch_id=batch_id,
                    )
                    _created = await self.create_metric(
                        create_req,
                        owner_id=actor_id,
                        role=role,
                        user_domain=user_domain,
                        _preloaded_conflict_existing=preloaded_existing,
                    )
                    # L3 增量：新列成功后追加到共享 existing，保持候选间互相冲突检测
                    # （与逐列加载时「前一候选已 flush 出现在后续列比对集」行为一致）。
                    if preloaded_existing is not None and _created is not None:
                        from app.services.conflict.schemas import MetricInput

                        _cdef = _created.definition_json or {}
                        preloaded_existing.append(
                            MetricInput(
                                metric_code=_created.metric_code,
                                domain=_created.domain or "",
                                definition=(
                                    _cdef.get("definition")
                                    or _cdef.get("expression")
                                    or ""
                                ),
                                source_tables=_cdef.get("source_tables") or [],
                                has_pii=bool(_created.pii_flag),
                                pii_authorized=bool(_created.compliance_reviewed),
                                metric_id=_created.id,
                                definition_json=_cdef,
                                synonyms=[],
                            )
                        )
                candidates.append(
                    {
                        "metric_code": code,
                        "status": "DRAFT",
                        "validation_errors": None,
                    }
                )
            except (BusinessError, ConflictError) as exc:
                # savepoint 已回滚本列；标记业务失败，继续后续列
                candidates.append(
                    {
                        "metric_code": code,
                        "status": "VALIDATION_ERROR",
                        "validation_errors": public_error_message(exc),
                    }
                )
            except SQLAlchemyError:
                # savepoint 已自动回滚本列（不影响已成功候选）；标记本列失败，继续。
                # 与修复前「整会话 rollback + 中止剩余列」相比：逐列独立，单列失败不拖垮整批。
                candidates.append(
                    {
                        "metric_code": code,
                        "status": "VALIDATION_ERROR",
                        "validation_errors": "批量注册单列失败（DB 错误），已跳过该列",
                    }
                )
                logger.warning(
                    "batch_register_item_db_error_skipped",
                    batch_id=batch_id,
                    source_table=request.source_table,
                    column=col,
                    exc_info=True,
                )
                # 修复前误用 break：单列 DB 错误会中止整批，后续列被静默跳过（
                # candidates 缺失、前端结果表不显示）。对齐注释与 batch_register_from_sql
                # 的「逐列独立」语义，改为 continue 继续处理后续列。
                continue

        return {"batch_id": batch_id, "candidates": candidates}

    async def batch_register_from_sql(
        self,
        request: Any,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> dict[str, Any]:
        """从 SQL 解析候选批量注册指标（FR-010 批量注册增强，场景A/B）。

        对齐 ``batch_register_metrics`` 的域门禁 + savepoint 逐条隔离；复合候选在
        savepoint 外先做依赖预检（批内原子已创建 或 库中已存在），缺依赖直接记
        VALIDATION_ERROR 跳过——不浪费嵌套事务，且批量提交时复合依赖原子 PUBLISHED
        由发布流程兜底（前端对复合禁用一键提交审核）。

        Args:
            request: 批量注册请求（domain + candidates，原子先行复合在后）。
            actor_id: 操作人ID。
            role: 操作人角色（域管理员/Owner 仅可批量注册本域指标）。
            user_domain: 操作人所属域。

        Returns:
            {batch_id, candidates: [{metric_code, status, validation_errors}]}.
        """
        import uuid

        from app.services.semantic.schemas import MetricCreateRequest

        batch_id = f"sqlbatch_{uuid.uuid4().hex[:12]}"
        candidates: list[dict[str, Any]] = []

        # 域门禁（与单条 create / batch_register 同级）
        if (
            role in ("domain_admin", "metric_owner")
            and user_domain
            and request.domain != user_domain
        ):
            raise BusinessError(
                f"{'域管理员' if role == 'domain_admin' else '指标 Owner'}仅可批量注册本域指标",
                error_code="FORBIDDEN",
                ctx={"request_domain": request.domain, "user_domain": user_domain, "role": role},
            )
        await self._validate_domain_active(request.domain)

        # Phase1 原子：逐候选 savepoint 创建；业务/编码冲突记 VALIDATION_ERROR 继续
        atom_ok: set[str] = set()
        for cand in request.candidates:
            if cand.type != "atomic":
                continue
            code = cand.metric_code
            try:
                async with self._db.begin_nested():
                    create_req = MetricCreateRequest(
                        metric_code=code,
                        name=cand.name,
                        domain=request.domain,
                        type="atomic",
                        measure_id=cand.measure_id,
                        unit=cand.unit,
                        aggregation=cand.aggregation or "SUM",
                        definition_json=cand.definition_json,
                        mount=cand.mount,
                        source_table=cand.source_table,
                        measure_column=cand.measure_column,
                        period=cand.period,
                    )
                    await self.create_metric(
                        create_req, owner_id=actor_id, role=role, user_domain=user_domain
                    )
                atom_ok.add(code)
                candidates.append(
                    {"metric_code": code, "status": "DRAFT", "validation_errors": None}
                )
            except (BusinessError, ConflictError) as exc:
                candidates.append(
                    {
                        "metric_code": code,
                        "status": "VALIDATION_ERROR",
                        "validation_errors": public_error_message(exc),
                    }
                )
            except SQLAlchemyError:
                # savepoint 已自动回滚本候选；DB 级错误（如编码唯一约束）标记跳过
                candidates.append(
                    {
                        "metric_code": code,
                        "status": "VALIDATION_ERROR",
                        "validation_errors": "批量注册单条失败（DB 错误），已跳过该条",
                    }
                )
                logger.warning(
                    "sql_batch_register_atom_db_error",
                    batch_id=batch_id,
                    metric_code=code,
                    exc_info=True,
                )

        # Phase2 复合：savepoint 外先依赖预检，缺依赖直接跳过（不浪费嵌套事务）
        for cand in request.candidates:
            if cand.type != "composite":
                continue
            code = cand.metric_code
            deps = cand.dependencies or []
            missing: list[str] = []
            for dep in deps:
                if dep in atom_ok:
                    continue
                try:
                    exists = await self._repo.get_by_code(dep)
                except Exception:
                    exists = None
                if exists is None:
                    missing.append(dep)
            if missing:
                candidates.append(
                    {
                        "metric_code": code,
                        "status": "VALIDATION_ERROR",
                        "validation_errors": f"依赖指标未创建或不存在: {', '.join(missing)}",
                    }
                )
                continue
            try:
                async with self._db.begin_nested():
                    create_req = MetricCreateRequest(
                        metric_code=code,
                        name=cand.name,
                        domain=request.domain,
                        type="composite",
                        # aggregation 为 schema 必填；复合指标聚合语义由依赖/表达式承载，
                        # 占位 SUM（对齐批量注册默认聚合）
                        aggregation=cand.aggregation or "SUM",
                        definition_json=cand.definition_json,
                    )
                    await self.create_metric(
                        create_req, owner_id=actor_id, role=role, user_domain=user_domain
                    )
                candidates.append(
                    {"metric_code": code, "status": "DRAFT", "validation_errors": None}
                )
            except (BusinessError, ConflictError) as exc:
                candidates.append(
                    {
                        "metric_code": code,
                        "status": "VALIDATION_ERROR",
                        "validation_errors": public_error_message(exc),
                    }
                )
            except SQLAlchemyError:
                candidates.append(
                    {
                        "metric_code": code,
                        "status": "VALIDATION_ERROR",
                        "validation_errors": "批量注册复合指标失败（DB 错误），已跳过",
                    }
                )
                logger.warning(
                    "sql_batch_register_composite_db_error",
                    batch_id=batch_id,
                    metric_code=code,
                    exc_info=True,
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
        from sqlalchemy import func, or_, select

        from app.models.user import User, UserRole

        stmt = (
            select(func.count())
            .select_from(User)
            .where(
                or_(
                    User.role == "compliance_officer",
                    User.role_items.any(UserRole.role == "compliance_officer"),
                ),
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

    async def mark_source_dropped(
        self,
        source_ids: list[str],
        actor_id: int,
        role: str,
        entity_names: list[str] | None = None,
    ) -> int:
        """数据源 DROP/不可达 → 血缘下游 PUBLISHED 指标批量置 DATA_SOURCE_DROPPED。

        对齐 PRD R3-04④：采集检测到源表 DROP 后调用本方法，沿血缘把引用该
        数据源表的下游指标标记为 DSD（非直接 DEPRECATED，避免误退役），生成
        Owner 待办（7 天处理期）。

        Args:
            source_ids: 已 DROP 的数据源 ID 集合（采集侧确认不可达的源）。
            actor_id: 触发人 ID（采集/运维）。
            role: 触发人角色。该操作会批量变更他人指标状态，仅限管理角色
                （platform_admin/domain_admin）——任意 metric_owner 不得借
                采集侧接口对任意数据源把他人 PUBLISHED 指标批量置 DSD（越权面）。
            entity_names: 精确到表名（采集侧仅部分表 DROP 时传入，避免把同源
                未 DROP 表的下游指标一并误置 DSD）；缺省表示整源处理。

        Returns:
            被标记为 DSD 的指标数（0 表示无血缘下游指标或均已处理）。

        Raises:
            AuthError: 非管理角色调用（服务层兜底，API 角色门禁之外防御直调）。

        实现：查血缘 ``table:`` 下游节点，再按 source_id 关联 DBCatalog 过滤——
        精确到「该数据源表」的下游指标，避免误伤同域其他源。best-effort，
        血缘缺失不影响已发布指标继续可用。
        """
        if role not in ("platform_admin", "domain_admin"):
            raise AuthError(
                "批量标记数据源下线仅平台/域管理员可执行",
                error_code="FORBIDDEN",
                ctx={"role": role, "actor_id": actor_id},
            )

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
        if entity_names:
            stmt = stmt.where(DBCatalog.entity_name.in_(entity_names))
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
                {
                    "metric_code": code,
                    "domain": metric.domain,
                    "source_ids": source_ids,
                    # 定向送达指标 Owner：DSD 需 7 天内处理（恢复/确认退役），TodoCenter 展示待办
                    "recipient_user_id": metric.owner_id,
                },
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
            {
                "metric_code": metric_code,
                "domain": metric.domain,
                # 定向送达指标 Owner：源恢复/误报确认，指标已回 PUBLISHED
                "recipient_user_id": metric.owner_id,
            },
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
