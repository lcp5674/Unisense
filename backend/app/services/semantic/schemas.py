"""指标 Pydantic Schema 定义。

对齐 TD §3 API 接口规范和 DEV_GUIDE §8a.1（Schema 命名 PascalCase + 后缀）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ---- 请求 Schema ----


def _validate_definition_json(v: dict[str, Any]) -> dict[str, Any]:
    """口径定义结构校验与规范化（FR-07 生产化）。

    1. ``sql``：若提供，用 sqlglot 做语法校验，非法 SQL 拒绝（422）。
    2. ``source_tables``：若提供，规范化为去重字符串数组（指标锚定的数据表，
       与 db_catalog/血缘节点约定一致）。
    3. 仅做校验与规范化，不新增字段、不改变未提供字段。
    """
    sql = v.get("sql")
    if sql is not None:
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("口径 SQL（definition_json.sql）必须为非空字符串")
        try:
            import sqlglot

            sqlglot.parse_one(sql)
        except Exception as exc:  # noqa: BLE001 - sqlglot 语法错误统一 422
            raise ValueError(f"口径 SQL 语法错误: {exc}") from exc

    source_tables = v.get("source_tables")
    if source_tables is not None:
        if not isinstance(source_tables, list):
            raise ValueError("source_tables 必须为数据表名数组")
        seen: set[str] = set()
        cleaned: list[str] = []
        for t in source_tables:
            name = str(t).strip()
            if name and name not in seen:
                seen.add(name)
                cleaned.append(name)
        v["source_tables"] = cleaned
    return v


class MetricCreateRequest(BaseModel):
    """创建指标请求。

    对齐 TD §3 POST /api/v1/metric-definitions。
    """

    metric_code: str | None = Field(
        None,
        max_length=64,
        description="指标编码（4段式，缺省由系统按源表/度量列/周期自动生成）",
    )
    name: str = Field(..., max_length=128, description="指标名称")
    domain: str = Field(..., max_length=64, description="所属域")
    type: Literal["atomic", "derived", "composite"] = Field(
        ..., description="指标类型: atomic/derived/composite"
    )
    granularity: str = Field(..., max_length=64, description="粒度")
    unit: str = Field(..., max_length=32, description="单位")
    currency: str | None = Field(None, max_length=16, description="币种")
    # 与字典种子对齐（9 值）：SUM/AVG/COUNT/COUNT_DISTINCT/LAST_VALUE + MAX/MIN/MEDIAN/PERCENTILE
    aggregation: Literal[
        "SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE", "MAX", "MIN", "MEDIAN", "PERCENTILE"
    ] = Field(
        ...,
        description="聚合方式: SUM/AVG/COUNT/COUNT_DISTINCT/LAST_VALUE/MAX/MIN/MEDIAN/PERCENTILE",
    )
    # 与字典种子对齐（6 值）：PERIOD/YTD/TTM/AVG + MOM/YOY
    time_semantics: Literal["PERIOD", "YTD", "TTM", "AVG", "MOM", "YOY"] = Field(
        ..., description="时间语义: PERIOD/YTD/TTM/AVG/MOM/YOY"
    )
    # 与字典种子对齐（4 值）：REALTIME/T0/T1/HOURLY
    freshness: Literal["REALTIME", "T0", "T1", "HOURLY"] = Field(
        ..., description="新鲜度: REALTIME/T0/T1/HOURLY"
    )
    dw_layer: Literal["ODS", "DWD", "DWS", "ADS", "DM"] = Field(
        ..., description="数仓分层: ODS/DWD/DWS/ADS/DM"
    )
    metric_tier: Literal["T1", "T2", "T3"] = Field("T3", description="指标分级: T1/T2/T3")
    serving_mode: Literal["BATCH_ONLY", "REALTIME_ONLY", "BATCH_REALTIME_DUAL"] = Field(
        "BATCH_ONLY", description="服务模式"
    )
    additivity: Literal["ADDITIVE", "SEMI_ADDITIVE", "NON_ADDITIVE"] = Field(
        "ADDITIVE", description="可加性: ADDITIVE/SEMI_ADDITIVE/NON_ADDITIVE"
    )
    non_additive_dimensions: list[str] | None = Field(None, description="不可加维度列表")
    definition_json: dict[str, Any] = Field(..., description="口径定义")
    pii_flag: bool = Field(False, description="是否含 PII")
    sla: str | None = Field(None, max_length=128, description="SLA 契约")
    # 自动推断辅助字段（FR-010/FR-011）：传入后由 Service 层 auto_fill 补全缺失字段
    source_table: str | None = Field(
        None, max_length=256, description="源表名（用于自动推断编码和数仓层）"
    )
    measure_column: str | None = Field(
        None, max_length=128, description="度量列名（用于自动推断编码和指标类型）"
    )
    period: str | None = Field(
        None, max_length=32, description="统计周期（用于自动推断编码和粒度）"
    )

    @field_validator("metric_code")
    @classmethod
    def validate_code(cls, v: str | None) -> str | None:
        """校验指标编码格式: 域_业务对象_度量_统计周期（4 段式 + 保留词）。

        缺省（None）时由 Service 层按自动生成逻辑补全；显式提供时委托
        ConflictPrechecker.validate_code_format 做严格校验。
        """
        if v is None:
            return v
        from app.services.semantic.conflict_precheck import ConflictPrechecker

        valid, error = ConflictPrechecker.validate_code_format(v)
        if not valid:
            raise ValueError(error)
        return v

    @field_validator("definition_json")
    @classmethod
    def validate_definition(cls, v: dict[str, Any]) -> dict[str, Any]:
        """口径定义：SQL 语法校验 + source_tables 规范化。"""
        return _validate_definition_json(v)


class MetricUpdateRequest(BaseModel):
    """更新指标请求。"""

    name: str | None = Field(None, max_length=128)
    granularity: str | None = Field(None, max_length=64)
    unit: str | None = Field(None, max_length=32)
    definition_json: dict[str, Any] | None = Field(None, description="口径定义")
    sla: str | None = Field(None, max_length=128)
    consumption_guide: dict[str, Any] | None = Field(None, description="消费指南")
    backup_owner_id: int | None = Field(None, description="副 Owner ID")
    change_reason: str = Field(..., min_length=4, description="变更原因")

    @field_validator("definition_json")
    @classmethod
    def validate_definition(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        """口径定义：SQL 语法校验 + source_tables 规范化。"""
        return _validate_definition_json(v) if v is not None else v


class MetricDescriptionUpdateRequest(BaseModel):
    """指标业务描述更新请求（治理补充 TD §12.1，不触发版本/不参与口径变更）。

    传空串表示清除描述。仅 metric_owner / 域管理员 / 平台管理员可操作。
    """

    description: str = Field(
        "", max_length=2000, description="指标业务描述（传空串清除）"
    )


class MetricPublishRequest(BaseModel):
    """发布指标请求（DRAFT → PUBLISHED）。"""

    version: int | None = Field(None, ge=1, description="待发布版本号（缺省为当前版本）")
    change_reason: str = Field(..., min_length=4, description="发布说明")


class MetricDeprecateRequest(BaseModel):
    """废弃指标请求（successor_code 选填，对齐 FR-039/FR-002）。

    替代指标选填：存在「指标因口径失效被下线、无替代」的合法场景。
    为空时表示无替代（后端不校验替代，指标直接废弃）。
    """

    successor_code: str | None = Field(
        default=None,
        max_length=64,
        description="替代指标编码（选填，须为已 PUBLISHED 指标；留空表示无替代）",
    )


class MetricSubmitRequest(BaseModel):
    """提交审核请求（DRAFT → REVIEW，对齐 FR-003）。

    评审指派（TD §13）：可指定评审用户（reviewer_type=user + reviewer_id）或
    域评审组（reviewer_type=domain + reviewer_domain，缺省用指标自身域）。
    均不传则未指派——由域管理员兜底评审。
    """

    change_reason: str = Field(..., min_length=4, description="提交审核说明")
    reviewer_id: int | None = Field(
        None, description="指定评审用户 ID（reviewer_type=user 时必填）"
    )
    reviewer_type: Literal["user", "domain"] | None = Field(
        None, description="评审指派类型: user(指定用户)/domain(域评审组)"
    )
    reviewer_domain: str | None = Field(
        None,
        max_length=64,
        description="域评审组所在域（reviewer_type=domain 时生效，缺省用指标自身域）",
    )

    @field_validator("reviewer_id", "reviewer_domain", mode="after")
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        """空字符串/0 归一为 None，前端未选择时传空串/0 不致校验失败。"""
        if v is None:
            return v
        if isinstance(v, str) and not v.strip():
            return None
        if isinstance(v, int) and v <= 0:
            return None
        return v


# ---- 批量操作 Schema（TD §13 批量治理：提交/通过/打回/下线，逐条收集结果不整体失败）----


class MetricBatchSubmitItem(BaseModel):
    """批量提交审核的单条项（含评审指派）。"""

    metric_code: str = Field(..., max_length=64, description="指标编码")
    change_reason: str = Field(..., min_length=4, description="提交审核说明")
    reviewer_id: int | None = Field(None, description="指定评审用户 ID（reviewer_type=user）")
    reviewer_type: Literal["user", "domain"] | None = Field(
        None, description="评审指派类型: user(指定用户)/domain(域评审组)"
    )
    reviewer_domain: str | None = Field(
        None, max_length=64, description="域评审组所在域（缺省用指标自身域）"
    )


class MetricBatchSubmitRequest(BaseModel):
    """批量提交审核请求。"""

    items: list[MetricBatchSubmitItem] = Field(..., min_length=1, max_length=100)


class MetricBatchApproveRequest(BaseModel):
    """批量审核通过请求（REVIEW → PUBLISHED/EXPERIMENTAL，即批量发布）。"""

    metric_codes: list[str] = Field(..., min_length=1, max_length=100)
    mode: Literal["standard", "experimental"] = Field(
        "standard", description="发布模式: standard(全量)/experimental(灰度)"
    )
    gray_tenant_ids: list[int] | None = Field(None, description="灰度白名单租户 ID")


class MetricBatchRejectRequest(BaseModel):
    """批量审核驳回请求（REVIEW → DRAFT）。"""

    metric_codes: list[str] = Field(..., min_length=1, max_length=100)
    reason: str = Field(..., min_length=4, description="驳回原因")


class MetricBatchDeprecateItem(BaseModel):
    """批量下线（废弃）的单条项。"""

    metric_code: str = Field(..., max_length=64, description="指标编码")
    successor_code: str = Field(..., max_length=64, description="替代指标编码（须已发布）")


class MetricBatchDeprecateRequest(BaseModel):
    """批量下线（废弃）请求。"""

    items: list[MetricBatchDeprecateItem] = Field(..., min_length=1, max_length=100)


class MetricBatchItemResult(BaseModel):
    """批量操作的单条结果（逐条收集，不因单条失败整体回滚）。"""

    metric_code: str
    ok: bool
    message: str = ""


class MetricBatchResponse(BaseModel):
    """批量操作响应。"""

    results: list[MetricBatchItemResult]
    ok_count: int
    fail_count: int


class MetricApproveRequest(BaseModel):
    """审核通过请求（REVIEW → PUBLISHED/EXPERIMENTAL，对齐 FR-004）。"""

    mode: Literal["standard", "experimental"] = Field(
        "standard", description="发布模式: standard(全量)/experimental(灰度)"
    )
    gray_tenant_ids: list[int] | None = Field(
        None, description="灰度白名单租户 ID（仅 experimental 模式）"
    )
    target_version: int | None = Field(None, ge=1, description="待发布版本号（缺省为当前版本）")


class MetricRejectRequest(BaseModel):
    """审核驳回请求（REVIEW → DRAFT，对齐 FR-005）。"""

    reason: str = Field(..., min_length=4, description="驳回原因")


class MetricReviewRequest(BaseModel):
    """评审请求（FR-07，approve → PUBLISHED / reject → DRAFT）。"""

    approved: bool = Field(..., description="评审结论：True 通过并发布，False 打回 DRAFT")
    change_reason: str | None = Field(
        None, description="变更说明（通过时建议附口径变更理由，驳回时可为空）"
    )


class MetricSubmitReviewRequest(BaseModel):
    """提交评审请求（FR-07，DRAFT → REVIEW）。"""

    change_reason: str = Field(..., min_length=4, description="提交评审说明")


class MetricEmergencyPublishRequest(BaseModel):
    """紧急发布请求（DRAFT → PUBLISHED 跳过 REVIEW，对齐 FR-022）。"""

    reason: str = Field(..., min_length=10, description="紧急发布原因")
    target_version: int | None = Field(None, ge=1, description="待发布版本号（缺省为当前版本）")


class VersionConfirmRequest(BaseModel):
    """消费方确认版本请求（对齐 FR-007）。"""

    version: int = Field(..., ge=1, description="版本号")


class VersionRejectRequest(BaseModel):
    """消费方拒绝版本请求（对齐 FR-007）。"""

    version: int = Field(..., ge=1, description="版本号")
    reason: str = Field(..., min_length=4, description="拒绝原因")


class VersionExtendRequest(BaseModel):
    """版本确认延期请求（对齐 FR-008）。"""

    version: int = Field(..., ge=1, description="版本号")


class MetricCompareRequest(BaseModel):
    """指标对比请求（对齐 FR-029）。"""

    metric_codes: list[str] = Field(
        ..., min_length=2, max_length=2, description="待对比的两个指标编码"
    )


class MetricBatchRegisterRequest(BaseModel):
    """批量注册请求（对齐 FR-030）。"""

    source_table: str = Field(..., description="源宽表名")
    measure_columns: list[str] = Field(..., min_length=1, description="度量列列表")
    dimension_mapping: dict[str, str] | None = Field(None, description="维度列映射")
    llm_prefill: bool = Field(True, description="是否使用 LLM 预填（False=手动模式）")
    domain: str = Field(..., max_length=64, description="所属域")


class MetricTemplateCreateRequest(BaseModel):
    """模板创建请求（对齐 FR-041：Schema 校验替代裸 dict）。"""

    code: str | None = Field(
        None,
        max_length=64,
        pattern=r"^tpl_[a-z][a-z0-9_]*$",
        description="模板编码（缺省由系统自动生成 tpl_{domain}_{name} slug）",
    )
    name: str = Field(..., max_length=128, description="模板名称")
    domain: str = Field(..., max_length=64, description="适用域")
    description: str | None = Field(None, description="模板说明")
    defaults_json: dict[str, Any] = Field(default_factory=dict, description="预填字段默认值")
    required_fields: list[str] | None = Field(None, description="必填字段列表")
    type: Literal["atomic", "derived", "composite"] | None = Field(None, description="指标类型预设")
    granularity: str | None = Field(None, max_length=64, description="粒度预设")
    unit: str | None = Field(None, max_length=32, description="单位预设")
    aggregation: str | None = Field(None, max_length=32, description="聚合方式预设")
    time_semantics: str | None = Field(None, max_length=32, description="时间语义预设")
    freshness: str | None = Field(None, max_length=32, description="数据新鲜度预设")
    dw_layer: str | None = Field(None, max_length=32, description="数仓分层预设")
    serving_mode: str | None = Field(None, max_length=32, description="服务模式预设")
    additivity: str | None = Field(None, max_length=32, description="可加性预设")
    metric_tier: str | None = Field(None, max_length=8, description="指标分级预设")
    owner_id: int | None = Field(None, ge=1, description="责任人（Owner）ID")


class MetricListParams(BaseModel):
    """指标列表查询参数。"""

    domain: str | None = None
    status: str | None = None
    metric_tier: str | None = None
    keyword: str | None = None
    # 责任人过滤（资产地图 Owner 视图下钻）
    owner_id: int | None = Field(None, ge=1, description="责任人（Owner）ID 过滤")
    # PII 过滤（热力指标视角下钻：PII 格子 / 非 PII 格子）
    pii_flag: bool | None = Field(None, description="仅 PII / 仅非 PII 指标")
    sort_by: Literal["updated_at", "created_at", "version", "metric_code", "name"] = "updated_at"
    sort_order: Literal["asc", "desc"] = "desc"
    page: int = Field(1, ge=1, le=1000)
    page_size: int = Field(20, ge=1, le=100)


# ---- 响应 Schema ----


class MetricResponse(BaseModel):
    """指标详情响应。"""

    id: int
    metric_code: str
    name: str
    domain: str
    type: str
    granularity: str
    unit: str
    currency: str | None
    aggregation: str
    time_semantics: str
    freshness: str
    sla: str | None
    dw_layer: str
    metric_tier: str
    serving_mode: str
    additivity: str
    non_additive_dimensions: list[str] | None
    definition_json: dict[str, Any]
    version: int
    row_version: int
    status: str
    owner_id: int
    backup_owner_id: int | None
    # 治理追溯：审批人 / 提交人，DB 模型已有，响应透出供目录页显示
    approver_id: int | None = None
    submitted_by: int | None = None
    # 评审指派（TD §13）：提交评审时指定的评审用户/域评审组，审批页据此校验与展示
    reviewer_id: int | None = None
    reviewer_type: str | None = None
    reviewer_domain: str | None = None
    # 指标业务描述（TD §12.1 治理补充，独立于口径/版本，资产地图抽屉展示/编辑）
    description: str | None = None
    description_source: str | None = None
    description_updated_by: int | None = None
    description_updated_at: datetime | None = None
    pii_flag: bool
    compliance_reviewed: bool
    effective_version: int | None
    consumption_guide: dict[str, Any] | None
    successor_code: str | None
    deprecated_at: datetime | None
    # DB 列为 date（models/metric.py），序列化输出 ISO "YYYY-MM-DD"，前端 string 兼容
    sunset_until: date | None
    emergency_publish: bool = False
    emergency_reason: str | None = None
    gray_tenant_ids: list[int] | None = None
    pending_conflict: bool = False
    pending_conflict_detail: dict[str, Any] | None = None
    # 仲裁裁决标记（TD §12.4）：canonical（权威口径）/ coexist（已裁定共存），详情页据此展示
    arbitration_mark: dict[str, Any] | None = None
    # 版本待确认：PUBLISHED+破坏性变更后，消费方需在 14 天内确认
    pending_version: bool = False
    # 健康度信号：列表接口经 metric_health_score 批量回填（无记录时为 None，目录页显示"未评分"）
    health_score: int | None = None
    health_level: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MetricListResponse(BaseModel):
    """指标列表响应。"""

    total: int
    page: int
    page_size: int
    items: list[MetricResponse]


class MetricVersionResponse(BaseModel):
    """指标版本响应。"""

    id: int
    metric_id: int
    version: int
    change_type: str
    definition_json: dict[str, Any]
    diff_json: dict[str, Any] | None
    status: str
    change_reason: str
    created_by: int
    published_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ErrorResponse(BaseModel):
    """统一错误响应。"""

    code: str
    message: str
    trace_id: str
    detail: dict[str, Any] | None = None


class MetricHealthResponse(BaseModel):
    """指标健康度响应（五维评分）。"""

    metric_id: int
    score: int
    level: str
    completeness_score: int
    activity_score: int
    quality_score: int
    owner_response_score: int
    lineage_coverage_score: int
    missing_dimensions: list[str] | None
    calculated_at: datetime

    model_config = {"from_attributes": True}


class MetricSourceDroppedRequest(BaseModel):
    """数据源 DROP → 批量标记下游指标 DSD（采集侧触发，TD §12.3 / PRD R3-04④）。

    source_ids 为采集检测到已 DROP/不可达的数据源 ID 集合。
    """

    source_ids: list[str] = Field(..., min_length=1, max_length=200)
