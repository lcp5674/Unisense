"""OpenAPI 契约展示层中文化。

在 FastAPI 自动生成的 OpenAPI schema 上做文档展示本地化：
- ``info.title/description`` → 中文品牌化说明（含认证/版本指引）。
- ``tags`` → 33 个业务分组的中文名称与分组描述。
- ``operation.summary`` → 英文 summary 映射为中文；未命中的保留原文（不破坏契约）。
- ``components.schemas`` → schema/字段的中文 title（保持 description 与字段名不变，不影响契约）。

设计取舍：
中文化集中在 OpenAPI 生成层而非逐个路由文件改 summary——
- 不侵入 30+ 路由文件（避免并行会话冲突与巨型 diff）。
- 单一事实来源集中在本文档模块，新增接口时在此补充映射即可。
- 不影响 API 契约本身（``summary``/``tags``/``schema.title`` 仅是文档展示字段）。

字段中文 title 仅作为 Swagger UI 展示的次要标题，**契约字段名（properties key）保持英文**，
pydantic 的 ``Field(..., description=...)`` 与现有中文 docstring 继续承担字段语义说明。
"""

from __future__ import annotations

# ---- info 中文说明 ----
_INFO_DESCRIPTION = """Unisense 指标语义中台 — 统一指标语义管理平台的开放 API。

## 认证
除健康检查等公开端点外，接口均需携带 Bearer Token（`Authorization: Bearer <token>`）。
通过 `POST /api/v1/auth/login` 获取令牌；`GET /api/v1/auth/me` 可校验当前会话。

## 版本
当前版本 v0.1.0，接口路径统一以 `/api/v1` 为前缀。

## 说明
本规范由后端路由自动生成（`/openapi.json`），接口变更后自动同步，无需人工维护。
"""

# ---- 业务分组中文映射：tag -> (中文名, 分组描述) ----
# 中文名经 x-displayName 注入，Swagger UI 左侧分组显示中文；description 展示在分组下方。
TAGS_ZH: dict[str, tuple[str, str]] = {
    "health": ("健康检查", "存活/就绪探针、依赖健康态与 Prometheus 指标"),
    "auth": ("认证", "登录认证、令牌签发与当前会话"),
    "audit": ("审计", "操作审计日志查询与导出"),
    "metric-definitions": ("指标定义", "指标注册、口径定义与全生命周期管理"),
    "metric-definitions-stats": ("指标统计", "指标注册统计与复用分析"),
    "collector-source": ("数据源管理", "数据源接入、连通性检测与采集调度"),
    "collector-catalog": ("采集目录", "采集元数据目录与描述治理"),
    "collector-run": ("采集运行", "采集任务与运行日志"),
    "lineage": ("血缘分析", "字段血缘解析、影响分析与健康度"),
    "conflict": ("指标冲突", "同名指标冲突检测与仲裁"),
    "governance": ("数据治理", "敏感数据分级、PII 复核与治理操作"),
    "quality": ("数据质量", "质量规则、观测与事件闭环"),
    "consume": ("指标消费", "指标查询与消费开放 API"),
    "glossary": ("术语管理", "业务术语治理与同义词管理"),
    "dimension": ("维度管理", "维度、成员与维度映射管理"),
    "measure_catalog": ("逻辑度量", "逻辑度量目录与口径管理"),
    "metric_mount": ("挂载实体", "指标挂载实体（OneData 挂载层）管理"),
    "notify": ("通知中心", "消息通知、订阅与事件日志"),
    "observability": ("可观测性", "用户反馈、NPS 与运营指标"),
    "组织管理": ("组织管理", "组织架构与机构管理"),
    "preferences": ("用户偏好", "个人偏好设置"),
    "assetmap": ("资产地图", "数据资产地图、分级与责任人"),
    "recommend": ("智能推荐", "指标/术语关联推荐"),
    "search": ("全局搜索", "资产检索与索引"),
    "ai": ("AI 能力", "AI 问数（NL2SQL）与 LLM 配置"),
    "tracking": ("埋点统计", "前端埋点上报与统计"),
    "semantics": ("语义服务", "指标语义批量解析与推断"),
    "主题域管理": ("主题域管理", "业务主题域与层级管理"),
    "系统字典管理": ("系统字典管理", "系统字典与枚举值管理"),
    "敏感规则配置": ("敏感规则配置", "敏感数据识别规则管理"),
    "feature-flags": ("特性开关", "功能开关管理"),
    "admin/keys": ("密钥管理", "加密密钥轮换与状态"),
    "users": ("用户管理", "用户、角色与权限管理"),
}

# ---- Schema 中文 title 映射：key → 中文 title ----
# 仅作用于 Swagger UI 展示层（pydantic schema 的 title 字段）。
# 字段名（properties key）与 description 不动 → 不影响 API 契约。
# 未命中映射的 schema 保留英文 title，不破坏现有中文 description。
SCHEMA_ZH: dict[str, str] = {
    # ---- 统一响应信封（ApiResponse 类与全部模板实例）----
    "ApiResponse": "统一响应信封",
    # ---- 通用枚举 ----
    "ApiClientStatus": "接入方状态",
    "ConflictStatus": "冲突状态",
    "ConflictType": "冲突类型",
    "SensitivityLevel": "敏感级别（合并枚举）",
    "SensitivityLevelEnum": "敏感级别（DB 兼容枚举）",
    "QualityRuleMode": "质量规则模式",
    "QualityRuleType": "质量规则类型",
    "QualitySeverity": "质量事件严重度",
    "SourceTypeEnum": "数据源类型",
    "EntityTypeEnum": "资产实体类型",
    "FavoriteAssetType": "收藏资产类型",
    "HealthDimension": "健康维度",
    "GrantType": "授权类型",
    "SnapshotGeneratedBy": "快照生成方式",
    # ---- 错误与基础 ----
    "HTTPValidationError": "请求参数校验错误",
    "ValidationError": "字段级校验错误",
    "TokenResponse": "令牌签发响应",
    "RefreshRequest": "刷新令牌请求",
    # ---- 用户/角色/权限 ----
    "LoginRequest": "登录请求",
    "UserAdmin": "管理端用户视图",
    "UserBrief": "用户简要信息",
    "UserInfo": "当前用户信息",
    "UserCreateRequest": "新建用户请求",
    "UserUpdateRequest": "更新用户请求",
    "UserChangePasswordRequest": "修改密码请求",
    "ResetPasswordRequest": "重置密码请求",
    "UserStatusRequest": "用户状态变更请求",
    "UserBatchStatusItem": "批量状态变更项",
    "UserBatchStatusRequest": "批量状态变更请求",
    "UserBatchStatusResult": "批量状态变更结果",
    "UserPermissionUpdateRequest": "用户权限变更请求",
    "RoleCreate": "新建角色请求",
    "RolePermissionUpdate": "角色权限变更请求",
    "PermissionCheckRequest": "权限校验请求",
    # ---- 组织 ----
    "OrganizationCreate": "新建组织请求",
    "OrganizationUpdate": "更新组织请求",
    "OrganizationView": "组织详情视图",
    # ---- 指标定义（核心业务）----
    "MetricCreateRequest": "新建指标请求",
    "MetricUpdateRequest": "更新指标请求",
    "MetricResponse": "指标详情响应",
    "MetricInput": "指标输入（创建/更新通用字段）",
    "MetricListResponse": "指标列表响应",
    "MetricSubmitRequest": "提交审核请求",
    "MetricApproveRequest": "审核通过请求",
    "MetricRejectRequest": "驳回请求",
    "MetricPublishRequest": "发布请求",
    "MetricDeprecateRequest": "废弃请求",
    "MetricBatchApproveRequest": "批量审核请求",
    "MetricBatchDeprecateRequest": "批量废弃请求",
    "MetricBatchDeprecateItem": "批量废弃项",
    "MetricBatchPurgeRequest": "批量清除请求",
    "MetricBatchReactivateRequest": "批量重激活请求",
    "MetricAutoSuggestRequest": "自动建议请求",
    "MetricSuggestDomainRequest": "推荐业务域请求",
    "MetricRefineDefinitionRequest": "细化指标口径请求",
    "MetricEmergencyPublishRequest": "紧急发布请求",
    "MetricDescriptionUpdateRequest": "更新指标描述请求",
    "MetricConsumptionGuideUpdateRequest": "更新消费指南请求",
    "MetricReuseItem": "指标复用项",
    "MetricReuseResponse": "指标复用分析响应",
    "MetricCompareRequest": "指标对比请求",
    "MetricCompareMatrixRequest": "指标对比矩阵请求",
    "MetricDownstreamCheckRequest": "下游影响检查请求",
    "MetricDownstreamCheckResult": "下游影响检查结果",
    "MetricDownstreamReferrer": "下游引用方",
    "MetricSourceDroppedRequest": "指标下线登记请求",
    "MetricTermBindRequest": "指标术语绑定请求",
    "MetricVersionResponse": "指标版本响应",
    "MetricHealthResponse": "指标健康度响应",
    "MetricSqlParseRequest": "SQL 解析请求",
    "MetricSqlTablesRequest": "SQL 依赖表查询请求",
    "MetricSqlBatchRegisterRequest": "SQL 批量注册请求",
    "MetricBatchImportCandidate": "批量导入候选",
    "MetricBatchImportRequest": "批量导入请求",
    "MetricLedgerResponse": "指标账本响应",
    "MetricLedgerDuplicateItem": "指标账本重复项",
    "MetricLedgerZombieItem": "指标账本僵尸项",
    "SqlBatchCreateCandidate": "SQL 批量候选",
    "VersionConfirmRequest": "版本确认请求",
    "VersionExtendRequest": "版本延期请求",
    "VersionRejectRequest": "版本驳回请求",
    # ---- 挂载实体（OneData 挂载层）----
    "MetricMountCreate": "新建挂载实体请求",
    "MetricMountUpdate": "更新挂载实体请求",
    "MetricMountInput": "挂载实体输入",
    "MetricMountResponse": "挂载实体响应",
    "MetricDimensionBind": "指标维度绑定请求",
    # ---- 采集目录与数据源 ----
    "DBCatalogCreateRequest": "新建采集目录请求",
    "DBCatalogResponse": "采集目录响应",
    "DBCatalogListResponse": "采集目录列表响应",
    "DataSourceCreateRequest": "新建数据源请求",
    "DataSourceUpdateRequest": "更新数据源请求",
    "DataSourceResponse": "数据源响应",
    "DataSourceListResponse": "数据源列表响应",
    "DataSourceTypeInfo": "数据源类型信息",
    "ListTablesRequest": "列数据源表请求",
    "DescriptionCoverageResponse": "描述覆盖统计响应",
    "TableCoverageItem": "覆盖明细项",
    "TableDescriptionRequest": "推断表描述请求",
    "TableDescriptionResponse": "推断表描述响应",
    "UpdateDescriptionRequest": "批量更新描述请求",
    "UpdateDescriptionResponse": "批量更新描述响应",
    "InferDescriptionRequest": "推断字段描述请求",
    "InferDescriptionResponse": "推断字段描述响应",
    "InferTableDescriptionResponse": "推断表描述响应",
    "DictInferDescriptionRequest": "字典描述推断请求",
    "CatalogReviewRequest": "目录实体审核请求",
    "CollectRequest": "采集请求",
    "BatchSourceItem": "批量采集项",
    "BatchSourceResult": "批量采集结果",
    "BatchScheduleRequest": "批量调度请求",
    "BatchTestConnectionRequest": "批量连通性测试请求",
    "BatchToggleRequest": "批量启停请求",
    "BatchDeleteRequest": "批量删除请求",
    "BatchInferHistoryEntry": "批量推断历史项",
    "BatchInferHistoryCreate": "批量推断历史创建请求",
    "BatchInferHistoryTable": "批量推断历史表",
    "InferBatchResponse": "批量推断响应",
    "ApplyPiiTemplateRequest": "应用 PII 模板请求",
    # ---- 采集运行 ----
    "CollectionRunResponse": "采集运行响应",
    "CollectionRunListResponse": "采集运行列表响应",
    "ScheduleRequest": "采集调度请求",
    "TestConnectionRequest": "测试连通性请求",
    "TestConnectionResult": "测试连通性结果",
    # ---- 血缘分析 ----
    "LineageParseRequest": "血缘解析请求",
    "LineageParseBatchRequest": "血缘批量解析请求",
    "LineageScanRequest": "血缘扫描请求",
    "LineageCoverageResponse": "血缘覆盖响应",
    "LineageHealthResponse": "血缘健康度响应",
    "LineageEdgeResponse": "血缘边响应",
    "LineageEdgeDetailResponse": "血缘边详情响应",
    "LineageEdgeHistoryResponse": "血缘边历史响应",
    "LineagePathResponse": "血缘路径响应",
    "LineagePathItem": "血缘路径项",
    "LineagePathEdge": "血缘路径边",
    "LineageTerminalsResponse": "血缘端点响应",
    "LineageTerminalItem": "血缘端点项",
    "ManualEdgeCreateRequest": "手工血缘边创建请求",
    "ManualEdgeCreateResponse": "手工血缘边创建响应",
    "EdgeDeleteResult": "血缘边删除结果",
    "CoverageBrokenEdgeItem": "断链覆盖项",
    "CoverageOrphanItem": "孤儿覆盖项",
    "ImpactPreviewRequest": "影响预演请求",
    # ---- 冲突仲裁 ----
    "ConflictCheckRequest": "冲突预检请求",
    "ConflictResolve": "冲突解决请求",
    "ArbitrateRequest": "仲裁请求",
    "EscalateRequest": "升级请求",
    "Body_import_metrics_csv_api_v1_metric_definitions_imports_csv_post": "CSV 批量导入指标请求体",
    # ---- 数据治理 / 质量 ----
    "QualityRuleCreate": "新建质量规则请求",
    "QualityRuleUpdate": "更新质量规则请求",
    "QualityDetectRequest": "质量检测请求",
    "QualityObservationRequest": "质量观测请求",
    "QualityEventAck": "质量事件确认请求",
    "RuleTestRequest": "质量规则测试请求",
    "RuleTestResponse": "质量规则测试响应",
    "RuleTestHit": "质量规则测试命中项",
    "RegexCheckRequest": "正则校验请求",
    "RegexCheckResponse": "正则校验响应",
    "ReclassifySensitivityRequest": "敏感级别重分级请求",
    "BatchSensitivityRequest": "批量敏感级别请求",
    "PiiReviewRequest": "PII 复核请求",
    "PiiValidationRequest": "PII 校验请求",
    "PiiFieldOverrideRequest": "PII 字段覆盖请求",
    "SetMaskingPolicyRequest": "脱敏策略设置请求",
    "ErasureRequestCreate": "数据清除请求创建",
    "ErasureResult": "数据清除结果",
    "SetRetentionRequest": "保留期设置请求",
    "BatchCodesRequest": "批量编码请求",
    "BatchItemResult": "批量项结果",
    "BatchOwnerRequest": "批量责任人请求",
    "BatchRejectRequest": "批量驳回请求",
    "BatchSubmitItem": "批量提交项",
    "BatchSubmitRequest": "批量提交请求",
    "BatchResponse": "批量处理响应",
    "BulkDeprecateRequest": "批量废弃请求",
    "BulkDeprecateItem": "批量废弃项",
    "BulkDeprecateResult": "批量废弃结果",
    # ---- 偏好 / 收藏 / 通知 ----
    "PreferenceItem": "偏好项",
    "PreferenceListResponse": "偏好列表响应",
    "PreferenceUpdate": "偏好更新请求",
    "FavoriteRequest": "收藏请求",
    "FavoriteResponse": "收藏响应",
    "SubscriptionUpsert": "订阅新增/更新请求",
    "DictBatchCreateRequest": "字典批量新建请求",
    "DictBatchDeleteRequest": "字典批量删除请求",
    "DictBatchItem": "字典批量项",
    "DictBatchResult": "字典批量结果",
    "DictBatchStatusRequest": "字典批量状态请求",
    "DictItemCreate": "字典项新建",
    "DictItemResponse": "字典项响应",
    "DictItemUpdate": "字典项更新",
    "DictUnknownNotifyRequest": "字典未知值通知请求",
    "DictUnknownRejectRequest": "字典未知值驳回请求",
    "DictValueCheckItem": "字典值校验项",
    "DictValuesVerifyRequest": "字典值校验请求",
    "DictValuesVerifyResponse": "字典值校验响应",
    "DriftLogResponse": "漂移日志响应",
    "DriftLogListResponse": "漂移日志列表响应",
    # ---- 维度管理 ----
    "DimensionCreate": "新建维度请求",
    "DimensionUpdate": "更新维度请求",
    "DimensionResponse": "维度响应",
    "DimensionExpr": "维度表达式",
    "DimensionMemberCreate": "新建维度成员请求",
    "DimensionMemberUpdate": "更新维度成员请求",
    "DimensionMappingCreate": "新建维度映射请求",
    "DimensionMappingUpdate": "更新维度映射请求",
    "PreviewValuesRequest": "预览维度取值请求",
    # ---- 逻辑度量 ----
    "MeasureCreate": "新建逻辑度量请求",
    "MeasureUpdate": "更新逻辑度量请求",
    "MeasureResponse": "逻辑度量响应",
    "MeasureAutoSuggestRequest": "逻辑度量自动建议请求",
    "MeasureInferSynonymsRequest": "逻辑度量同义词推断请求",
    "ExpectedMeasureIn": "期望逻辑度量输入",
    # ---- 术语管理 ----
    "TermCreate": "新建术语请求",
    "TermUpdate": "更新术语请求",
    "TermNameInfer": "术语名称推断",
    "TermRelationCreate": "术语关系创建请求",
    # ---- 主题域 ----
    "SubjectDomainCreate": "新建主题域请求",
    "SubjectDomainUpdate": "更新主题域请求",
    "SubjectDomainResponse": "主题域响应",
    "SubjectDomainTreeNode": "主题域树节点",
    "SubjectDomainDefaultsUpdate": "主题域默认配置更新请求",
    # ---- 指标消费 / 查询 ----
    "QueryRequest": "指标查询请求",
    "QueryResponse": "指标查询响应",
    "DryRunResponse": "查询预演响应",
    "SnapshotResponse": "指标快照响应",
    "CategoryItem": "分类项",
    # ---- 鉴权 / 密钥 / 通知 ----
    "MigrateRequest": "密钥迁移请求",
    "RotateKeyRequest": "密钥轮换请求",
    "ClientCreateRequest": "新建消费客户端请求",
    "ClientResponse": "消费客户端响应",
    "ClientCreatedResponse": "消费客户端创建响应",
    "GrantCreate": "新建授权请求",
    "GrantBatchRequest": "批量授权请求",
    "ReviewApproveRequest": "审核通过请求",
    "ReviewRejectRequest": "审核驳回请求",
    "ReviewSubmitRequest": "提交审核请求",
    "RejectRequest": "驳回请求",
    "ReconciliationRun": "执行口径对账请求",
    "ReconciliationSubmit": "提交口径对账请求",
    "ReconciliationConfirm": "确认口径对账请求",
    "ReconciliationReview": "审核口径对账请求",
    # ---- 反馈 / NPS / 埋点 ----
    "FeedbackCreate": "反馈创建请求",
    "FeedbackClarifyRequest": "反馈追问请求",
    "FeedbackStatusUpdateRequest": "反馈状态更新请求",
    "NpsSubmitRequest": "NPS 提交请求",
    "TrackEventRequest": "埋点事件请求",
    "TrackEventResponse": "埋点事件响应",
    "TrackingStatsResponse": "埋点统计响应",
    # ---- 质量基准 ----
    "BenchmarkBind": "绑定质量基准请求",
    "BenchmarkImport": "导入质量基准请求",
    # ---- AI / 事件 ----
    "NL2SQLRequest": "AI 问数请求",
    "LlmConfigPayload": "LLM 配置请求体",
    "LlmConfigTestRequest": "LLM 配置测试请求",
    "LlmModelsRequest": "LLM 模型列表请求",
    "EventPublish": "事件发布请求",
    # ---- 敏感规则 ----
    "SensitiveRuleCreate": "新建敏感规则请求",
    "SensitiveRuleUpsert": "新增/更新敏感规则请求",
    "SensitiveRuleItem": "敏感规则项",
    # ---- 评估/样本 ----
    "EvalSampleIn": "评估样本输入",
    "EvalSamplePreviewIn": "评估样本预览输入",
    "EvalSampleUpdate": "评估样本更新请求",
    "FeatureFlagUpdate": "特性开关更新请求",
}

# ---- 通用字段 title 中文化（高频字段，幂等且不影响 description）----
# 仅修改 schema.properties[*].title，**保留字段名（key）与 description**。
# 未命中保留 Pydantic 自动生成的英文 title（如 "Code" → "Code"）。
FIELD_TITLE_ZH: dict[str, str] = {
    # ApiResponse 信封字段
    "code": "业务码",
    "message": "提示信息",
    "data": "业务数据",
    "trace_id": "链路追踪 ID",
    # 通用主键/外键
    "id": "ID",
    "uuid": "UUID",
    # 通用元数据
    "name": "名称",
    "code_": "编码",  # 罕见重名保护
    "code_name": "编码名称",
    "status": "状态",
    "type": "类型",
    "label": "标签",
    "description": "描述",
    "remark": "备注",
    "notes": "备注",
    "value": "值",
    "key": "键",
    "sort_order": "排序",
    "is_active": "是否启用",
    "is_default": "是否默认",
    "is_system": "是否系统",
    "enabled": "是否启用",
    # 时间字段
    "created_at": "创建时间",
    "updated_at": "更新时间",
    "deleted_at": "删除时间",
    "submitted_at": "提交时间",
    "approved_at": "审核通过时间",
    "rejected_at": "驳回时间",
    "deprecated_at": "废弃时间",
    "reactived_at": "重激活时间",
    "effective_at": "生效时间",
    "expired_at": "失效时间",
    "last_active_at": "最后活跃时间",
    "created_by": "创建人",
    "updated_by": "更新人",
    # 指标相关
    "metric_code": "指标编码",
    "metric_name": "指标名称",
    "metric_id": "指标 ID",
    "metric_type": "指标类型",
    "aggregation": "聚合方式",
    "granularity": "粒度",
    "period": "周期",
    "unit": "单位",
    "expression": "表达式",
    "definition": "口径定义",
    "definition_json": "口径定义（JSON）",
    "additivity": "可加性",
    "default_period": "默认周期",
    "domain": "业务域",
    "sub_domain": "子业务域",
    "owner_id": "责任人 ID",
    "product_owner_id": "产品责任方 ID",
    "tech_owner_id": "技术责任方 ID",
    "data_owner_id": "数仓责任方 ID",
    "business_owner_id": "业务责任方 ID",
    "source_table": "源表",
    "source_column": "源列",
    "source_id": "数据源 ID",
    "source_type": "数据源类型",
    "row_version": "乐观锁版本",
    "reason": "原因",
    "page_size": "每页条数",
    "page_no": "页码",
    "page": "页码",
    "items": "条目列表",
    "total": "总数",
    "total_count": "总数",
    "action": "动作",
    "version": "版本",
    "version_id": "版本 ID",
    "force": "是否强制",
    "is_force": "是否强制",
    "entity_name": "实体名",
    "entity_type": "实体类型",
    "search": "搜索关键字",
    "keyword": "搜索关键字",
    "filter": "过滤条件",
    "sort_by": "排序字段",
    "sort_order_": "排序方向",  # 重名保护
    "sort_field": "排序字段",
    "date_range": "日期范围",
    "start_date": "起始日期",
    "end_date": "结束日期",
}

# ---- ApiResponse[X] 模板标题前缀 ----
_API_RESPONSE_TITLE_ZH = "统一响应信封"
SUMMARY_ZH: dict[str, str] = {
    "Ack Event": "确认质量事件",
    "Add Favorite": "添加收藏",
    "Add Manual Edge": "新增手工血缘边",
    "Api Metrics": "API 指标",
    "Apply Pii Template": "应用 PII 模板",
    "Approve Term": "审核通过术语",
    "Arbitrate Conflict": "仲裁冲突",
    "Assign Owner": "指定责任人",
    "Batch Assign Owner": "批量指定责任人",
    "Batch Delete Data Sources": "批量删除数据源",
    "Batch Grants": "批量授权",
    "Batch Reclassify": "批量重分级",
    "Batch Schedule Data Sources": "批量调度数据源",
    "Batch Set Rule Confidence": "批量设置规则置信度",
    "Batch Set Rule Status": "批量设置规则状态",
    "Batch Set User Status": "批量设置用户状态",
    "Batch Test Data Sources": "批量测试数据源",
    "Batch Toggle Data Sources": "批量启停数据源",
    "Bind Benchmark": "绑定质量基准",
    "Bind Metric Dimension": "绑定指标维度",
    "Bulk Deprecate": "批量废弃",
    "Cancel Collection Job": "取消采集任务",
    "Catalog Summary": "目录汇总",
    "Change My Password": "修改我的密码",
    "Check Conflict": "冲突预检",
    "Check Permission": "校验权限",
    "Check Source Connection": "检测数据源连通性",
    "Clarify Feedback": "反馈追问",
    "Classification Rescan": "分级重扫描",
    "Classification Summary": "分级汇总",
    "Clear Batch Infer History": "清空批量推断历史",
    "Close Conflict": "关闭冲突",
    "Close Event": "关闭质量事件",
    "Collect Now": "立即采集",
    "Collect Source": "采集数据源",
    "Collect Source Async": "异步采集数据源",
    "Collection Run Summary": "采集运行汇总",
    "Confirm Reconciliation": "确认对账结果",
    "Confirm Repair": "确认修复",
    "Confirm Stale": "确认失效边",
    "Confirm Version": "确认指标版本",
    "Coverage": "血缘覆盖统计",
    "Coverage Broken": "断链覆盖统计",
    "Coverage Orphans": "孤儿覆盖统计",
    "Create Batch Infer History": "记录批量推断历史",
    "Create Client": "创建消费客户端",
    "Create Data Source": "新建数据源",
    "Create Dimension": "新建维度",
    "Create Event": "上报埋点事件",
    "Create Grant": "新建授权",
    "Create Llm Config": "新建 LLM 配置",
    "Create Mapping": "新建维度映射",
    "Create Measure": "新建逻辑度量",
    "Create Member": "新建维度成员",
    "Create Mount": "新建挂载实体",
    "Create Organization": "新建组织",
    "Create Relation": "新建术语关系",
    "Create Role": "新建角色",
    "Create Rule": "新建质量规则",
    "Create Term": "新建术语",
    "Create User": "新建用户",
    "Del Favorite": "删除收藏",
    "Delete All Notifications": "清空全部通知",
    "Delete Data Source": "删除数据源",
    "Delete Edges By Node": "按节点删除血缘边",
    "Delete Llm Config": "删除 LLM 配置",
    "Delete Mapping": "删除维度映射",
    "Delete Member": "删除维度成员",
    "Delete Mount": "删除挂载实体",
    "Delete Notification": "删除通知",
    "Delete Preference": "删除偏好设置",
    "Delete Role": "删除角色",
    "Delete Rule": "删除质量规则",
    "Delete Single Edge": "删除单条血缘边",
    "Deprecate Dimension": "废弃维度",
    "Deprecate Measure": "废弃逻辑度量",
    "Deprecate Member": "废弃维度成员",
    "Deprecate Term": "废弃术语",
    "Detect": "触发质量检测",
    "Dry Run": "查询预演（Dry Run）",
    "Dry Run Batch Grants": "批量授权预演",
    "Edge Detail": "血缘边详情",
    "Escalate Conflict": "冲突升级",
    "Export Audit Logs": "导出审计日志",
    "Export Pii Csv": "导出 PII 资产 CSV",
    "Export Tables": "导出表清单",
    "Fetch Llm Models": "拉取 LLM 模型列表",
    "Force Close Conflict": "强制关闭冲突",
    "Get Catalog Detail": "目录详情",
    "Get Collection Job": "采集任务详情",
    "Get Collection Run": "采集运行详情",
    "Get Collection Run Logs": "采集运行日志",
    "Get Data Source": "数据源详情",
    "Get Description Coverage": "描述覆盖统计",
    "Get Dimension": "维度详情",
    "Get Entity Detail": "资产实体详情",
    "Get Favorite Details": "收藏详情",
    "Get Favorites": "我的收藏列表",
    "Get Graph": "资产图谱",
    "Get Health": "数据源健康状态",
    "Get Heatmap": "资产热力图",
    "Get Heatmap Matrix": "资产热力矩阵",
    "Get Llm Config": "查询 LLM 配置",
    "Get Llm Config Secret": "查询 LLM 配置密钥",
    "Get Measure": "逻辑度量详情",
    "Get Mount": "挂载实体详情",
    "Get Owner View": "责任人视图",
    "Get Rule": "质量规则详情",
    "Get Semantic": "指标语义信息",
    "Get Source Overview": "数据源概览",
    "Get Stats": "埋点统计",
    "Get Term": "术语详情",
    "Get User Permissions": "查询用户权限",
    "Get Watermark": "数据水位",
    "Health Summary": "健康汇总",
    "Impact": "血缘影响分析",
    "Impact Preview": "影响预演",
    "Import Benchmark": "导入质量基准",
    "Infer Column Description": "推断字段描述",
    "Infer Descriptions Batch": "批量推断描述",
    "Infer Table Description": "推断表描述",
    "Infer Term Suggestion": "推断术语建议",
    "Issue Token": "签发客户端令牌",
    "Key Status": "密钥状态",
    "Lineage Export": "血缘导出",
    "Lineage Graph": "血缘图谱",
    "Lineage Health": "血缘健康度",
    "Lineage Metrics": "血缘指标",
    "Lineage Path": "血缘路径",
    "Lineage Path Terminals": "血缘路径端点",
    "List Action Registry": "动作注册表",
    "List Admin Users": "管理端用户列表",
    "List Audit Logs": "审计日志列表",
    "List Batch Infer History": "批量推断历史列表",
    "List Benchmarks": "质量基准列表",
    "List Catalog Databases": "目录库列表",
    "List Catalogs": "目录列表",
    "List Categories": "敏感规则分类",
    "List Channel Runs": "通道运行记录",
    "List Channels": "采集通道列表",
    "List Clients": "消费客户端列表",
    "List Collection Jobs": "采集任务列表",
    "List Collection Runs": "采集运行列表",
    "List Conflicts": "冲突列表",
    "List Data Sources": "数据源列表",
    "List Databases": "库列表",
    "List Dimension Metrics": "维度绑定指标列表",
    "List Dimensions": "维度列表",
    "List Drift Logs": "漂移日志列表",
    "List Edges": "血缘边列表",
    "List Event Logs": "事件日志列表",
    "List Event Types": "订阅事件类型列表",
    "List Events": "质量事件列表",
    "List Feature Flags": "特性开关列表",
    "List Feedback": "反馈列表",
    "List Grants": "授权列表",
    "List Mappings": "维度映射列表",
    "List Measures": "逻辑度量列表",
    "List Members": "维度成员列表",
    "List Metric Dimensions": "指标维度绑定列表",
    "List Metric Snapshots": "指标快照列表",
    "List Mounts": "挂载实体列表",
    "List Nodes": "血缘节点列表",
    "List Notifications": "通知列表",
    "List Organizations": "组织列表",
    "List Pii Assets": "PII 资产列表",
    "List Pii Templates": "PII 模板列表",
    "List Preferences": "偏好设置列表",
    "List Reconciliation Records": "对账记录列表",
    "List Reconciliations": "口径对账列表",
    "List Role Options": "角色选项列表",
    "List Role Permissions": "角色权限列表",
    "List Rules": "质量规则列表",
    "List Rulings": "仲裁裁定列表",
    "List Source Catalogs": "数据源目录列表",
    "List Source Types": "数据源类型列表",
    "List Stale": "失效血缘边列表",
    "List Subscriptions": "订阅列表",
    "List Tables": "表列表",
    "List Term Relations": "术语关系列表",
    "List Terms": "术语列表",
    "List Users": "用户列表",
    "Login": "登录",
    "Logout": "登出",
    "Mark All Read": "全部标为已读",
    "Mark Failed": "标记投递失败",
    "Mark Handled": "标记已处理",
    "Mark Read": "标记已读",
    "Mark Sent": "标记已发送",
    "Me": "当前登录用户",
    "Metric Dimension Summary": "指标维度汇总",
    "Metric Summary": "指标汇总",
    "Migrate Secrets": "迁移加密密钥",
    "My Assets": "我的资产",
    "My Permissions": "我的权限",
    "Nl2Sql": "AI 问数（NL2SQL）",
    "Notification Metrics": "通知指标",
    "Nps Stats": "NPS 统计",
    "Orphan Assets": "孤儿资产",
    "Overview Metrics": "运营总览指标",
    "Parse Lineage": "解析血缘 SQL",
    "Parse Lineage Batch": "批量解析血缘",
    "Pii Overview": "PII 概览",
    "Pii Review": "PII 复核",
    "Pii Validate": "PII 校验",
    "Preview Dimension Values": "预览维度取值",
    "Publish All Members": "批量发布维度成员",
    "Publish Dimension": "发布维度",
    "Publish Event": "发布业务事件",
    "Publish Measure": "发布逻辑度量",
    "Publish Member": "发布维度成员",
    "Publish Term": "发布术语",
    "Quality Events List": "质量事件列表（运营）",
    "Quality Metrics": "质量指标",
    "Query": "指标查询",
    "Query Metric Internal": "指标查询（内部）",
    "Recent Changes": "最近变更",
    "Reclassify Sensitivity": "敏感级别重分级",
    "Recommend Metrics": "推荐指标",
    "Recommend Terms": "推荐术语",
    "Record Observation": "记录质量观测",
    "Refresh": "刷新令牌",
    "Refresh Entity": "刷新采集实体",
    "Register Catalog": "注册采集目录",
    "Reject Term": "驳回术语",
    "Reject Version": "驳回指标版本",
    "Related Metrics": "关联指标推荐",
    "Remove Pii Override": "移除 PII 覆盖",
    "Reopen Conflict": "重新打开冲突",
    "Reset Password": "重置密码",
    "Reset Role Permissions": "重置角色权限",
    "Resolve Conflict": "解决冲突",
    "Resolve Event": "解决质量事件",
    "Response Time Stats": "响应耗时统计",
    "Restore Stale": "恢复失效边",
    "Retry Delivery": "重试投递",
    "Review Catalog Entity": "审核目录实体",
    "Review Reconciliation": "审核对账结果",
    "Revoke Grant": "撤销授权",
    "Rotate Key": "轮换密钥",
    "Run Detail": "血缘运行详情",
    "Run Reconciliation": "执行口径对账",
    "Scan Lineage Directory": "扫描血缘目录",
    "Schedule Collection": "配置采集调度",
    "Search Assets": "搜索资产",
    "Set Masking Policy": "设置脱敏策略",
    "Set Retention": "设置保留周期",
    "Set Role Permissions": "设置角色权限",
    "Set Rule Status": "设置规则状态",
    "Set User Permissions": "设置用户权限",
    "Set User Status": "设置用户状态",
    "Stream Collection Job": "采集任务实时流",
    "Submit Feedback": "提交反馈",
    "Submit Nps": "提交 NPS 评分",
    "Submit Reconciliation": "提交口径对账",
    "Submit Term": "提交术语审核",
    "Sync Metric Consumers": "同步指标消费方",
    "Test Connection": "测试连通性",
    "Test Llm Config": "测试 LLM 配置",
    "Test Rule": "测试敏感规则",
    "Unbind Metric Dimension": "解绑指标维度",
    "Unread Count": "未读通知数",
    "Update Column Description": "更新字段描述",
    "Update Data Source": "更新数据源",
    "Update Dimension": "更新维度",
    "Update Feature Flag": "更新特性开关",
    "Update Feedback Status": "更新反馈状态",
    "Update Llm Config": "更新 LLM 配置",
    "Update Mapping": "更新维度映射",
    "Update Measure": "更新逻辑度量",
    "Update Member": "更新维度成员",
    "Update Mount": "更新挂载实体",
    "Update Organization": "更新组织",
    "Update Rule": "更新质量规则",
    "Update Table Description": "更新表描述",
    "Update Term": "更新术语",
    "Update User": "更新用户",
    "Upsert Pii Override": "新增/更新 PII 覆盖",
    "Upsert Preference": "新增/更新偏好设置",
    "Upsert Subscription": "新增/更新订阅",
    "Validate Regex": "校验正则表达式",
}


def localize_openapi(schema: dict[str, object]) -> dict[str, object]:
    """对 FastAPI 生成的 OpenAPI schema 做展示层中文化（原地修改并返回）。

    Args:
        schema: ``app.openapi()`` 生成的 OpenAPI 3.x 字典。

    Returns:
        中文化后的同一字典（便于链式调用）。
    """
    # ---- info ----
    info = schema.setdefault("info", {})
    info["title"] = "Unisense 指标语义中台"
    info["description"] = _INFO_DESCRIPTION

    # ---- tags：中文名（name 中文化 + x-displayName 供兼容工具）+ 分组描述 ----
    # Swagger UI 5.x 渲染分组标题时使用 tag 的 name（不支持 x-displayName），
    # 因此直接把 name 替换为中文，并同步替换 operation.tags 引用，保证分组正确。
    # 幂等：已中文化的 tag（name 为中文）不再处理，避免二次调用清空 description。
    tag_name_zh: dict[str, str] = {}
    for tag in schema.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        name = str(tag.get("name", ""))
        zh_name, description = TAGS_ZH.get(name, (None, None))
        if zh_name:
            tag_name_zh[name] = zh_name
            tag["name"] = zh_name
            tag["description"] = description
            tag["x-displayName"] = zh_name

    # ---- operation.tags：引用同步替换为中文名 ----
    for path_item in schema.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            tags = operation.get("tags")
            if isinstance(tags, list):
                operation["tags"] = [tag_name_zh.get(t, t) for t in tags]

    # ---- operation.summary：英文 → 中文 ----
    for path_item in schema.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            summary = operation.get("summary")
            if isinstance(summary, str) and summary in SUMMARY_ZH:
                operation["summary"] = SUMMARY_ZH[summary]

    # ---- schemas：title 中文化（保持字段名/description/契约不变）----
    _localize_schemas(schema)

    return schema


def _localize_schemas(schema: dict[str, object]) -> None:
    """对 ``components.schemas`` 做展示层中文化（原地修改）。

    处理范围：
    - ``ApiResponse`` 类：title → 「统一响应信封」
    - ``ApiResponse[X]`` 模板实例：title → 「统一响应信封[X]」
    - ``SCHEMA_ZH`` 命中的核心业务 schema：title → 中文
    - 字段 title：命中 ``FIELD_TITLE_ZH`` 改为中文，否则保留
      Pydantic 自动生成的英文 title（字段名/description 不受影响）。

    幂等：二次调用时，若 title 已是中文则跳过，不会清空 description。
    """
    schemas = schema.get("components", {}).get("schemas")
    if not isinstance(schemas, dict):
        return

    import re

    api_response_inner_re = re.compile(r"^ApiResponse_(.+)_$")

    for key, sc in schemas.items():
        if not isinstance(sc, dict):
            continue

        title = sc.get("title")
        zh_title: str | None = None

        if key == "ApiResponse":
            zh_title = _API_RESPONSE_TITLE_ZH
        elif key in SCHEMA_ZH:
            zh_title = SCHEMA_ZH[key]
        elif key.startswith("ApiResponse_") and key.endswith("_"):
            # ApiResponse[X] 模板实例
            inner_match = api_response_inner_re.match(key)
            if inner_match:
                inner_key = inner_match.group(1)
                inner_zh = SCHEMA_ZH.get(inner_key)
                inner_label = inner_zh if inner_zh else inner_key
                zh_title = f"{_API_RESPONSE_TITLE_ZH}[{inner_label}]"

        if zh_title and title != zh_title:
            sc["title"] = zh_title

        # 字段 title 中文化（保留 description 与字段名）
        properties = sc.get("properties")
        if not isinstance(properties, dict):
            continue
        for field_name, prop in properties.items():
            if not isinstance(prop, dict):
                continue
            zh_field_title = FIELD_TITLE_ZH.get(field_name)
            if zh_field_title and prop.get("title") != zh_field_title:
                prop["title"] = zh_field_title
