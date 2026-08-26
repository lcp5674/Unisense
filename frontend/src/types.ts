// 类型定义 — 对齐后端 Pydantic Schema（backend/app/services/semantic/schemas.py 等）
// 注意：所有响应均套用统一信封 { code, message, data, trace_id }，见 api.ts ApiEnvelope。

export interface ApiError {
  code: string;
  message: string;
  trace_id: string;
  detail?: Record<string, unknown> | null;
}

export type MetricType = "atomic" | "derived" | "composite";
export type MetricStatus = "DRAFT" | "EXPERIMENTAL" | "REVIEW" | "PUBLISHED" | "DEPRECATED" | "DATA_SOURCE_DROPPED";
export type MetricTier = "T1" | "T2" | "T3";

// 作废指标详情（GET /metric-definitions/{code}/archived）：完整历史口径 + 裁决指针
export interface ArchivedMetricResponse {
  metric: MetricResponse;
  successor_code: string | null;
  arbitration_mark: Record<string, unknown> | null;
}

export interface MetricResponse {
  id: number;
  metric_code: string;
  name: string;
  domain: string;
  type: MetricType;
  /** OneData：粒度已下沉挂载实体（metric_mount），存量/派生回填可空 */
  granularity: string | null;
  /** OneData 原子层：关联逻辑度量 ID（原子必填；派生/复合继承可空；旧后端缺省） */
  measure_id?: number | null;
  /** 逻辑度量展示信息（backend best-effort 填充）：详情页「逻辑度量」栏展示名称+编码 */
  measure_code?: string | null;
  measure_name?: string | null;
  unit: string;
  currency: string | null;
  aggregation: string;
  time_semantics: string;
  freshness: string;
  sla: string | null;
  dw_layer: string;
  metric_tier: MetricTier;
  serving_mode: string;
  additivity: string;
  non_additive_dimensions: string[] | null;
  definition_json: Record<string, unknown>;
  version: number;
  row_version: number;
  status: MetricStatus;
  owner_id: number;
  backup_owner_id: number | null;
  /** 口径三方责任（PRD 4.5 补充）：产品需求方/技术方/数仓开发（user.id，均可空） */
  product_owner_id?: number | null;
  tech_owner_id?: number | null;
  dw_developer_id?: number | null;
  /** 外部人员名称兜底（责任方非平台用户时直接填名称，id 为空）：展示优先级 id>name */
  product_owner_name?: string | null;
  tech_owner_name?: string | null;
  dw_developer_name?: string | null;
  approver_id: number | null;
  submitted_by: number | null;
  /** 评审指派（TD §13）：提交评审时指定的评审用户/域评审组，审批页据此校验与展示 */
  reviewer_id?: number | null;
  reviewer_type?: "user" | "domain" | null;
  reviewer_domain?: string | null;
  /** 驳回可追溯（FR-005 闭环）：DRAFT 详情页展示"上次驳回原因"引导提交人修改后重提 */
  reject_reason?: string | null;
  reject_reviewer_id?: number | null;
  rejected_at?: string | null;
  /** 审核通过时间（「我审过的」视图：由 list 接口从生效版本 published_at 填充，驳回场景用 rejected_at） */
  approved_at?: string | null;
  pii_flag: boolean;
  compliance_reviewed: boolean;
  /** 关联业务术语 ID（P2-11：术语治理归属，null=未绑定） */
  term_id: number | null;
  /** P0-C：批量注册批次 ID（可空）——列表/详情/审核页展示批次可回溯整批 */
  batch_id?: string | null;
  /** P1-1（第六轮）：原始口径 SQL（批量创建透传落库）——详情页反查 batch_id →
      整句口径原文；此前 MetricResponse 未声明该字段 API 永不返回（"写而不读"） */
  raw_sql?: string | null;
  effective_version: number | null;
  consumption_guide: Record<string, unknown> | null;
  successor_code: string | null;
  deprecated_at: string | null;
  sunset_until: string | null;
  emergency_publish: boolean;
  emergency_reason: string | null;
  /** 紧急发布补审时间（null=未补审；紧急发布须 24h 内补审，FR-022 闭环） */
  emergency_reviewed_at: string | null;
  gray_tenant_ids: number[] | null;
  pending_conflict: boolean;
  pending_conflict_detail: Record<string, unknown> | null;
  /** 仲裁裁决标记（TD §12.4）：canonical=权威口径 / coexist=已裁定共存，详情页据此展示；
   *   rename_required=true 表示仲裁要求该指标改名（同名不同义区分），Owner 改名后清除 */
  arbitration_mark?: {
    status: "canonical" | "coexist";
    conflict_id?: string;
    decision?: string;
    ruled_at?: string;
    opposite_code?: string;
    rename_required?: boolean;
    rename_opposite_code?: string;
    resolved_at?: string;
  } | null;
  pending_version: boolean;
  /** 指标业务描述（治理补充 TD §12.1，资产地图/详情展示与编辑） */
  description?: string | null;
  description_source?: string | null;
  description_updated_by?: number | null;
  description_updated_at?: string | null;
  /** 健康度信号（列表接口批量回填，无评分记录时为 null）：score 0-100 / level EXCELLENT|GOOD|WARNING|CRITICAL */
  health_score?: number | null;
  health_level?: string | null;
  created_at: string;
  updated_at: string;
}

// 指标健康度（backend health_scorer.py：五维加权，>=85 EXCELLENT / >=70 GOOD / >=55 WARNING / <55 CRITICAL）
export interface MetricHealth {
  metric_id: number;
  score: number;
  level: "EXCELLENT" | "GOOD" | "WARNING" | "CRITICAL";
  completeness_score: number;
  activity_score: number;
  quality_score: number;
  owner_response_score: number;
  lineage_coverage_score: number;
  missing_dimensions: string[] | null;
  calculated_at: string;
}

// 两指标并排对比（backend compare_metrics：fields.difference_level ∈ identical/similar/different）
export interface MetricCompareField {
  a: unknown;
  b: unknown;
  difference_level: "identical" | "similar" | "different";
}
export interface MetricCompareDeps {
  a: string[];
  b: string[];
  intersection: string[];
  only_a: string[];
  only_b: string[];
  difference_level: "identical" | "different";
}
export interface MetricCompareResult {
  metrics: [string, string];
  fields: Record<string, MetricCompareField | MetricCompareDeps | undefined>;
}

// 多指标矩阵对比（backend compare_matrix：行级 all_identical/partial/all_different）
export interface MetricCompareMatrixField {
  values: Record<string, unknown>;
  difference_level: "all_identical" | "partial" | "all_different";
}
export interface MetricCompareMatrixDeps {
  values: Record<string, string[]>;
  intersection: string[];
  only: Record<string, string[]>;
  difference_level: "all_identical" | "partial" | "all_different";
}
export interface MetricCompareMatrixResult {
  metrics: string[];
  fields: Record<string, MetricCompareMatrixField | MetricCompareMatrixDeps | undefined>;
  /** owner_id → 责任人显示名（P2-14 治理对比可读化） */
  owner_names?: Record<number, string>;
}

// 只读用户摘要（backend GET /auth/users，Owner 责任链渲染用）
export interface UserBrief {
  id: number;
  username: string;
  display_name: string;
  role: string;
  domain: string | null;
  status: string;
}

// 用户管理视图（backend /api/v1/users，platform_admin 专属）
export interface AdminUser {
  id: number;
  username: string;
  email: string;
  display_name: string;
  role: string;
  roles: string[];
  domain: string | null;
  org_id: number | null;
  org_name: string | null;
  status: string;
  last_login_at: string | null;
  created_at: string | null;
}

export interface AdminUserListResponse {
  total: number;
  page: number;
  page_size: number;
  items: AdminUser[];
}

export interface UserBatchStatusItem {
  user_id: number;
  username: string | null;
  ok: boolean;
  error_code: string | null;
  message: string | null;
}

export interface UserBatchStatusResult {
  succeeded: UserBatchStatusItem[];
  failed: UserBatchStatusItem[];
}

export interface UserCreateRequest {
  username: string;
  email: string;
  display_name: string;
  role: string;
  roles?: string[];
  domain?: string | null;
  org_id?: number | null;
  password: string;
}

export interface UserUpdateRequest {
  display_name: string;
  email: string;
  role: string;
  roles?: string[];
  domain?: string | null;
  org_id?: number | null;
}

export interface MetricListResponse {
  total: number;
  page: number;
  page_size: number;
  items: MetricResponse[];
}

export interface MetricVersionResponse {
  id: number;
  metric_id: number;
  version: number;
  change_type: string;
  definition_json: Record<string, unknown>;
  diff_json: Record<string, unknown> | null;
  status: string;
  change_reason: string;
  created_by: number;
  published_at: string | null;
  created_at: string;
  /** PENDING_CONFIRMATION 版本的确认截止时间（14 天 + 延期），超时自动接受 */
  pending_deadline?: string | null;
  /** 多消费方确认进度：已确认 X / 共 N 个消费方（仅待确认版本填充） */
  confirmed_count?: number | null;
  consumer_count?: number | null;
}

export interface MetricCreateRequest {
  metric_code?: string;
  name: string;
  domain: string;
  type: MetricType;
  /** OneData：单位由逻辑度量继承/后端默认——原子不传，派生/复合可选 */
  unit?: string | null;
  currency?: string | null;
  aggregation: "SUM" | "AVG" | "COUNT" | "COUNT_DISTINCT" | "LAST_VALUE";
  /** OneData：物理属性由挂载/默认承载——原子不传，派生/复合可选 */
  time_semantics?: "PERIOD" | "YTD" | "TTM" | "AVG" | null;
  freshness?: "REALTIME" | "T1" | "HOURLY" | null;
  dw_layer?: "ODS" | "DWD" | "DWS" | "ADS" | "DM" | null;
  metric_tier?: MetricTier;
  serving_mode?: "BATCH_ONLY" | "REALTIME_ONLY" | "BATCH_REALTIME_DUAL";
  additivity?: "ADDITIVE" | "SEMI_ADDITIVE" | "NON_ADDITIVE";
  non_additive_dimensions?: string[] | null;
  definition_json: Record<string, unknown>;
  /** OneData：粒度已下沉挂载实体——原子/复合不设，派生由 mount 承载 */
  granularity?: string | null;
  /** OneData 原子层：关联逻辑度量 ID（原子指标必填，度量格式/单位/小数位继承） */
  measure_id?: number | null;
  /** OneData 挂载层：派生指标携带源表/列/粒度/周期/域，服务端自动落 metric_mount */
  mount?: MetricMountInput | null;
  pii_flag?: boolean;
  sla?: string | null;
  /** 消费指南（选填）：创建时随指标落库（guide_source=manual），三组字符串数组 */
  consumption_guide?: ConsumptionGuidePayload | null;
  /** 口径三方责任（PRD 4.5 补充，均可空）：产品需求方/技术方/数仓开发 */
  product_owner_id?: number | null;
  tech_owner_id?: number | null;
  dw_developer_id?: number | null;
  /** 外部人员名称兜底（责任方非平台用户时直接填名称，id 为空）：展示优先级 id>name */
  product_owner_name?: string | null;
  tech_owner_name?: string | null;
  dw_developer_name?: string | null;
}

export interface MetricUpdateRequest {
  name?: string;
  granularity?: string | null;
  unit?: string;
  /** OneData 原子层：更换逻辑度量 = 破坏性口径变更 */
  measure_id?: number | null;
  /** OneData 挂载层：派生指标携带则 upsert metric_mount */
  mount?: MetricMountInput | null;
  currency?: string; // 治理属性（非破坏性，不触发版本递增）
  aggregation?: string; // 治理属性：聚合方式
  time_semantics?: string; // 治理属性：时间语义
  freshness?: string; // 治理属性：新鲜度
  dw_layer?: string; // 治理属性：数仓分层
  metric_tier?: string; // 治理属性：指标分级
  serving_mode?: string; // 治理属性：服务模式
  additivity?: string; // 治理属性：可加性
  non_additive_dimensions?: string[]; // 治理属性：不可加维度
  definition_json?: Record<string, unknown>;
  sla?: string | null;
  /** 消费指南（选填）：创建时随指标落库（guide_source=manual），三组字符串数组 */
  consumption_guide?: ConsumptionGuidePayload | null;
  backup_owner_id?: number | null;
  /** 口径三方责任（非破坏性变更，不触发版本确认）：产品需求方/技术方/数仓开发 */
  product_owner_id?: number | null;
  tech_owner_id?: number | null;
  dw_developer_id?: number | null;
  /** 外部人员名称兜底（责任方非平台用户时直接填名称，id 为空）：展示优先级 id>name */
  product_owner_name?: string | null;
  tech_owner_name?: string | null;
  dw_developer_name?: string | null;
  change_reason: string; // 必填，min_length=4
  row_version?: number; // 跨请求乐观锁：编辑时回传当前版本号，他人已改则 409 拒绝
}

// 仲裁改名建议（backend POST /metric-definitions/{code}/suggest-rename）
export interface RenameSuggestItem {
  name: string;
  reason: string;
  source: "llm" | "rule";
}

export interface RenameSuggestResponse {
  suggestions: RenameSuggestItem[];
  current_name: string;
}

// 批量注册指标（backend POST /metric-definitions/batch-register，对齐 FR-030）
// 请求体对齐 backend/app/services/semantic/schemas.py MetricBatchRegisterRequest
export interface MetricBatchRegisterRequest {
  /** 源宽表名（必填） */
  source_table: string;
  /** 度量列列表（至少 1 个，按列批量创建 DRAFT 指标） */
  measure_columns: string[];
  /** 维度列映射，可选（如 { date: dt, shop: shop_id }） */
  dimension_mapping?: Record<string, string> | null;
  /** 是否使用 LLM 预填（默认 true；False=纯规则手动模式） */
  llm_prefill?: boolean;
  /** 所属域（必填，须为 active 域） */
  domain: string;
}

/** 批量注册结果中的单条候选（成功=DRAFT，失败=VALIDATION_ERROR） */
export interface MetricBatchRegisterCandidate {
  metric_code: string;
  status: "DRAFT" | "VALIDATION_ERROR";
  validation_errors: string | null;
}

/** 批量注册响应 data 结构（对齐 service.batch_register_metrics 返回值） */
export interface MetricBatchRegisterResult {
  batch_id: string;
  candidates: MetricBatchRegisterCandidate[];
}

// ---- 批量治理（TD §13：提交/通过/打回/下线，逐条收集结果；后端统一 app/api/batch_common）----

/** 批量提交审核单条项（含评审指派），指标/逻辑度量/维度/术语共用 */
export interface BatchSubmitItem {
  code: string;
  change_reason: string;
  reviewer_id?: number | null;
  reviewer_type?: "user" | "domain" | null;
  reviewer_domain?: string | null;
}

/** 批量操作单条结果（code 为实体编码） */
export interface BatchItemResult {
  code: string;
  ok: boolean;
  message: string;
}

/** 批量操作响应 data 结构（四模块统一） */
export interface BatchResult {
  results: BatchItemResult[];
  ok_count: number;
  fail_count: number;
}

// 冲突（backend/app/services/conflict/schemas.py）
// 后端枚举见 models/conflict.py：状态 OPEN/NEGOTIATING/ESCALATED/RULED/CLOSED；
// 类型 same_name_diff_def/same_def_diff_name/grain_unit/cross_domain_same_def/version_conflict/pii
export type ConflictStatus = "OPEN" | "NEGOTIATING" | "ESCALATED" | "RULED" | "CLOSED";
export type ConflictType =
  | "same_name_diff_def"
  | "same_def_diff_name"
  | "grain_unit"
  | "cross_domain_same_def"
  | "version_conflict"
  | "pii";

export interface ConflictResponse {
  conflict_id: string;
  type: ConflictType;
  status: ConflictStatus;
  conflict_type: string;
  // 后端 metric_a/b 为指标主键 id（int | null），指标编码请读扁平字段 candidate/existing_metric_code
  metric_a: number | null;
  metric_b: number | null;
  similarity_score: number;
  metric_codes: string[];
  decision_json: Record<string, unknown> | null;
  severity?: string;
  // 治理字段（后端迁移 0090）：仲裁台据此区分软/硬冲突并提示来源/原因
  source?: string;
  reason?: string;
  block_publish?: boolean;
  candidate_metric_code?: string;
  existing_metric_code?: string;
  description?: string;
  detected_at?: string;
  resolved_at?: string | null;
  created_at?: string;
}

export interface ConflictListResponse {
  items: ConflictResponse[];
  total: number;
  page: number;
  page_size: number;
}

// 冲突预检（POST /conflicts/check，backend/app/services/conflict/schemas.py）
export interface ConflictMetricInput {
  metric_code: string;
  domain?: string;
  definition?: string;
  source_tables?: string[];
  has_pii?: boolean;
  pii_authorized?: boolean;
  metric_id?: number | null;
}

export interface ConflictCheckRequest {
  candidate: ConflictMetricInput;
  existing?: ConflictMetricInput[];
}

export interface ConflictDetection {
  conflict_type: ConflictType;
  score: number;
  existing_code: string;
  existing_metric_id?: number | null;
  severity: string;
  block_publish: boolean;
  reason?: string;
  llm_confirmed?: boolean;
}

export interface ConflictCheckResult {
  detections: ConflictDetection[];
}

// 裁决记录（知识库条目，GET /conflicts/{id}/rulings，backend/app/services/conflict/schemas.py）
export interface RulingRecord {
  id: number;
  conflict_id: string;
  metric_codes: Record<string, unknown> | null;
  dispute_desc: string | null;
  decision: string | null;
  reason: string | null;
  arbitrator_id: number | null;
  decided_at: string | null;
}

// 血缘边（backend/app/services/lineage/schemas.py）
export interface LineageEdge {
  id: number;
  source_node: string;
  target_node: string;
  edge_type: string;
  granularity: string;
  confidence: number;
  provenance: string;
  pii_inherited?: boolean;
}

// 血缘影响分析/边列表分页响应（T4 起后端返回 {items,total,...}）
export interface LineageEdgePage {
  items: LineageEdge[];
  total: number;
  page: number;
  page_size: number;
  has_more?: boolean;
  /** 当前页节点的基础元数据（后端 /impact 与 /edges 携带，与 /lineage/graph 节点结构对齐） */
  nodes?: LineageNodeInfo[];
}

// 血缘节点基础元数据（影响分析/边列表响应的 nodes 字段）——供血缘查询/影响分析
// 图谱点击节点在侧边栏展示具体信息（指标详情/表详情），并使节点具备域/PII 属性
// （按业务域着色、PII 红色描边，与血缘图谱交互一致）。
export interface LineageNodeInfo {
  id: string;
  type: string;
  label: string;
  /** db_catalog 主键（仅表/视图节点有值，用于表详情直达） */
  entity_id?: number | null;
  pii?: boolean;
  domain?: string | null;
  owner?: string | null;
}

// SQL 血缘解析结果（后端 lineage/schemas.py 的 LineageParseResponse）——
// 计数字段 + 本次解析的表级/字段级边明细（供解析页面当页展示）
export interface LineageTableEdge {
  source: string;
  target: string;
}

export interface LineageFieldEdge {
  source_table: string;
  source_column: string | null;
  target_table: string;
  target_column: string;
  expression: string | null;
}

// 只读查询（纯 SELECT 无落点）读取的上游依赖清单（方案 B）
export interface UpstreamDeps {
  tables: string[];
  fields: string[];
}

export interface ParseLineageResult {
  table_edges: number;
  field_edges: number;
  graph_written: boolean;
  table_lineage: LineageTableEdge[];
  field_lineage: LineageFieldEdge[];
  upstream_deps?: UpstreamDeps | null;
}

// 血缘图谱节点/边（后端 lineage/graph 端点返回，与资产地图图谱结构对齐，
// 可被 assetmap/AssetGraph 力导向图组件直接消费）
export interface LineageGraphNode {
  id: string;
  type: string;
  label: string;
  entity_id?: number;
  pii?: boolean;
  domain?: string;
  owner?: string;
}

export interface LineageGraphEdge {
  source: string;
  target: string;
  type: string;
}

export interface LineageGraphData {
  nodes: LineageGraphNode[];
  edges: LineageGraphEdge[];
}

// 变更影响预览（what-if）——后端 lineage/schemas.py 的 ImpactPreviewResponse
export interface ImpactPreview {
  affected_metrics: { metric_code: string; change_type: string }[];
  affected_tables: string[];
  affected_consumers: string[];
  risk_level: "low" | "medium" | "high" | "critical";
}

// 血缘采集通道运行记录（后端 lineage/schemas.py 的 LineageIngestRunResponse）
export interface LineageIngestRun {
  id: number;
  source: string;
  run_at: string;
  status: string;
  total_edges: number;
  added_count: number;
  updated_count: number;
  missing_count: number;
  stale_flagged_count: number;
  restored_count: number;
  error?: string | null;
  // 本次运行详情快照（点击运行历史行时从 /lineage/runs/{id} 拉取）：
  // SQL 解析含 sql/dialect/target_table/table_lineage/field_lineage；批量采集含 added_edges/updated_edges
  detail?: LineageRunDetail | null;
}

// 采集运行详情快照（后端 LineageIngestRunResponse.detail）
export interface LineageRunDetail {
  kind: "sql_parse" | "batch";
  sql?: string;
  dialect?: string | null;
  target_table?: string | null;
  source_node?: string | null;
  actor_id?: number;
  table_lineage?: LineageTableEdge[];
  field_lineage?: LineageFieldEdge[];
  added_edges?: string[][];
  updated_edges?: string[][];
  [key: string]: unknown;
}

// 血缘采集通道总览（后端 LineageChannelResponse）
export interface LineageChannel {
  source: string;
  edge_count: number;
  node_count: number;
  stale_count: number;
  last_run?: LineageIngestRun | null;
}

// 失效队列边（后端 StaleEdgeResponse）
export interface StaleEdge {
  id: number;
  source_node: string;
  target_node: string;
  edge_type: string;
  granularity: string;
  confidence: number;
  provenance: string;
  missing_count: number;
  stale_since?: string | null;
}

// 血缘候选节点（后端 lineage/schemas.py 的 LineageNodeResponse）——
// 影响分析/血缘查询选项框的预加载与关键词搜索
export interface LineageNode {
  id: string;
  label: string;
  type: "table" | "metric" | "field" | "external" | "other";
  count: number;
}

// 手动登记血缘边结果（POST /lineage/edges/manual）。
export interface ManualLineageEdgeResult {
  edge: LineageEdge;
  created: boolean;
}

// 血缘覆盖率治理（backend/app/api/lineage.py /coverage）——
// 指标/表血缘完整度统计看板。
export interface LineageCoverage {
  metric_total: number;
  metric_with_lineage: number;
  metric_orphan: number;
  table_total: number;
  table_no_downstream: number;
  edge_total: number;
  broken_edges: number;
}

// 孤立指标明细（无任何血缘边的指标）。后端仅返回 {metric_code, domain}，
// name/status 为容错读入（部分实现可能附带），前端偏好字段可选。
export interface CoverageOrphanItem {
  metric_code: string;
  name?: string;
  domain?: string | null;
  status?: string;
}

// 断链边明细（source 节点对应目录/指标实体已不存在）。
export interface CoverageBrokenEdgeItem {
  id: number;
  source_node: string;
  target_node: string;
  edge_type: string;
  granularity?: string;
  confidence?: number;
  provenance: string;
}

// 孤立指标 / 断链边列表（孤立/断链端点兼容「纯数组」与「{items,total}」两种响应，
// api 层归一化为统一形状，UI 只消费本结构）。
export interface CoverageOrphanList {
  items: CoverageOrphanItem[];
  total: number;
}
export interface CoverageBrokenEdgeList {
  items: CoverageBrokenEdgeItem[];
  total: number;
}

// 血缘边变更历史快照项（backend lineage/schemas.py LineageEdgeHistoryResponse）。
// before_value/changed_at 为契约中的别名，后端实际返回 source/target/.../change_reason/created_at，
// api 层归一化时把 created_at 映射到 changed_at，before_value 保留可选。
export interface LineageEdgeHistoryItem {
  id?: number;
  before_value?: string;
  change_reason?: string;
  changed_at?: string;
  created_at?: string;
  source_node?: string;
  target_node?: string;
  edge_type?: string;
}

// 血缘边详情（backend /lineage/edges/{edge_id}）：后端响应嵌套在 .edge 下并携带独立 history，
// api 层归一化为扁平结构，UI 只消费本形状。
export interface LineageEdgeDetail {
  id: number;
  source_node: string;
  target_node: string;
  edge_type: string;
  granularity?: string;
  confidence?: number;
  provenance?: string;
  pii_inherited?: boolean;
  created_at?: string;
  history: LineageEdgeHistoryItem[];
}

// 收藏（backend/app/api/consume.py）：POST/DELETE 返回 FavoriteResponse
export interface FavoriteResponse {
  asset_type: string;
  asset_id: string;
  pinned: boolean;
}

// 当前用户（backend/app/api/auth.py UserInfo）
export interface CurrentUser {
  id: number;
  username: string;
  display_name: string;
  role: string;
  domain: string | null;
  /** 所属域中文名（后端 /auth/me 回填，个人中心展示） */
  domain_name?: string | null;
  org_id: number;
  /** 组织中文名（后端 /auth/me 回填，个人中心展示） */
  org_name?: string | null;
  /** 是否首次登录需强制改密（后端登录/me 响应携带，前端据此弹不可关闭的改密弹窗） */
  must_change_password?: boolean;
}

// 用户偏好（backend /api/v1/me/preferences，key → JSON value，按用户持久化）
export interface UserPreferenceItem {
  key: string;
  value: unknown;
}
export interface UserPreferenceList {
  items: UserPreferenceItem[];
  total: number;
}

// ============================================================================
// 语义服务（backend /api/v1/semantics/*）
// ============================================================================

export interface AssetStat {
  total: number;
  by_status: Record<string, number>;
}

/**
 * Owner 名下单类资产统计：新版后端返回 `{ total, by_status }`；
 * 旧版后端仅返回纯数字（只有 metrics 是对象，其余资产是 count）。
 * 兼容 union——Dashboard 消费时须经 normalizeOwnerStat 归一化，勿直接取 .by_status。
 */
export type OwnerAssetStat = AssetStat | number;

export interface DashboardData {
  total: number;
  by_status: Record<string, number>;
  by_tier: Record<string, number>;
  by_domain: Record<string, number>;
  pii_count: number;
  pii_ratio: number;
  /** Owner 责任分布（跨资产）：指标/数据表/数据源/维度/术语/指标模板按责任人聚合 */
  by_owner?: Record<
    number,
    {
      name: string;
      /** 跨资产总计（指标+数据表+数据源+维度+术语+模板） */
      total: number;
      metrics: OwnerAssetStat;
      tables: OwnerAssetStat;
      sources: OwnerAssetStat;
      dimensions: OwnerAssetStat;
      terms: OwnerAssetStat;
      templates: OwnerAssetStat;
    }
  >;
  /** 质量健康：严重级分布 + 待处理（OPEN+ACK） */
  quality?: { total: number; by_severity: Record<string, number>; pending: number };
  /** 合规：复核率 */
  compliance?: { total: number; reviewed: number; pending: number; reviewed_ratio: number };
  /** 冲突风险：待仲裁 + 升级中 */
  conflict?: { total: number; open: number; escalated: number; by_status: Record<string, number> };
  /** 新鲜度：近 30 天更新 */
  freshness?: { total: number; updated_30d: number; updated_30d_ratio: number };
  /** 全资产总览：指标/数据表/数据源/维度/术语/指标模板/采集任务/数据字典 */
  assets?: {
    metric: AssetStat;
    table: AssetStat;
    source: AssetStat;
    dimension: AssetStat;
    term: AssetStat;
    template: AssetStat;
    collection_task: AssetStat;
    system_dict: AssetStat;
  };
}

export interface MetricTemplate {
  id: number;
  code: string;
  name: string;
  domain: string;
  description: string | null;
  defaults_json: Record<string, unknown>;
  required_fields: string[] | null;
  type: string;
  granularity: string;
  unit: string;
  aggregation: string;
  time_semantics: string;
  freshness: string;
  dw_layer: string;
  serving_mode: string;
  additivity: string;
  metric_tier: string;
  /** OneData 原子层：逻辑度量预设（原子指标实例化时继承度量格式/单位/小数位） */
  measure_id: number | null;
  /** OneData 挂载层：挂载实体预设（派生指标实例化时落 metric_mount） */
  mount: MetricMountInput | null;
  /** 口径三方责任预设（实例化时作为指标默认责任方，均可空） */
  product_owner_id: number | null;
  tech_owner_id: number | null;
  dw_developer_id: number | null;
  product_owner_name: string | null;
  tech_owner_name: string | null;
  dw_developer_name: string | null;
  is_active: boolean;
  owner_id: number | null;
  created_by: number;
  /** 模板版本号（内容变更递增，P2-13 编辑闭环） */
  version: number;
}

export interface ConsumptionGuideResponse {
  metric_code: string;
  name: string;
  domain: string;
  type: string;
  granularity: string;
  unit: string;
  aggregation: string;
  time_semantics: string;
  serving_mode: string;
  recommended_usage: string[];
  cautions: string[];
  related_metrics: string[];
  /** 指南来源：auto=自动生成 / manual=人工维护 */
  guide_source?: "auto" | "manual" | null;
  /** 人工维护时间（自动生成时为 null/缺省） */
  guide_updated_at?: string | null;
}

/** 消费指南三组列表（人工维护的请求/落库结构） */
export interface ConsumptionGuidePayload {
  recommended_usage: string[];
  cautions: string[];
  related_metrics: string[];
}

/** 更新消费指南请求（独立于指标状态机；row_version 为可选乐观锁） */
export interface MetricConsumptionGuideUpdateRequest {
  recommended_usage: string[];
  cautions: string[];
  related_metrics: string[];
  row_version?: number;
}

// ============================================================================
// 消费服务（backend /api/v1/consume/*）
// ============================================================================

/** 主数据审核流共享字段（对齐后端 ReviewFieldsMixin / 指标审核流 TD §13）。
 *  逻辑度量/维度/术语三类主数据统一走 DRAFT→REVIEW→PUBLISHED→DEPRECATED，字段名一致。 */
export interface ReviewFields {
  /** 提交评审人 ID（approve/reject 时禁止自审） */
  submitted_by?: number | null;
  /** 审核通过人 ID */
  approver_id?: number | null;
  /** 指定评审用户 ID（reviewer_type=user 时生效） */
  reviewer_id?: number | null;
  /** 评审指派类型: user(指定用户)/domain(域评审组) */
  reviewer_type?: string | null;
  /** 评审团队所在域（reviewer_type=domain 时生效） */
  reviewer_domain?: string | null;
  /** 最近一次审核驳回原因（引导修改后重提） */
  reject_reason?: string | null;
  /** 驳回审核人 ID */
  reject_reviewer_id?: number | null;
  /** 驳回时间 */
  rejected_at?: string | null;
  /** 最近审核时间（approve/reject 时写入） */
  reviewed_at?: string | null;
}

export interface DimensionExpr {
  name: string;
  value: string | number;
}

export interface QueryRequest {
  metric_code: string;
  dimensions?: DimensionExpr[];
  date_range: string;
  granularity?: string | null;
  comparison?: string | null;
  accept_stale?: boolean;
  params?: Record<string, unknown>;
}

export interface DryRunResponse {
  metric_code: string;
  status: string;
  checks: Array<Record<string, unknown>>;
  execution_plan: Record<string, unknown>;
  meta: Record<string, unknown>;
}

export interface QueryResponse {
  metric_code: string;
  degraded: boolean;
  data: Record<string, unknown> | null;
  execution_plan: Record<string, unknown>;
  meta: Record<string, unknown>;
}

export interface ClientCreateRequest {
  client_id?: string;
  secret: string;
  scope_domain?: string | null;
  metric_whitelist?: string[] | null;
  qps?: number;
  daily_quota?: number;
}

export interface ClientCreatedResponse {
  client_id: string;
  scope_domain: string | null;
  metric_whitelist: string[] | null;
  qps: number;
  daily_quota: number;
  status: string;
  secret: string;
}

export interface ClientResponse {
  client_id: string;
  scope_domain: string | null;
  metric_whitelist: string[] | null;
  qps: number;
  daily_quota: number;
  status: string;
}

export interface SnapshotResponse {
  id: number;
  metric_code: string;
  version: number;
  dims: Record<string, unknown>;
  date_range: string;
  value_json: Record<string, unknown>;
  quality_flag: string | null;
  generated_at: string;
  generated_by: string;
}

// ============================================================================
// 维度服务（backend /api/v1/dimensions/*）
// ============================================================================

export interface Dimension extends ReviewFields {
  id: number;
  dim_code: string;
  name: string;
  domain: string;
  type: string;
  description: string | null;
  owner_id: number;
  status: string;
  created_at: string;
  updated_at: string;
  /** 绑定指标数（列表接口批量回填，默认 0；兼容旧后端缺省场景） */
  metric_count?: number;
}

export interface DimensionMapping {
  id: number;
  source_dim_code: string;
  target_dim_code: string;
  mapping_type: string;
  expression: string | null;
  created_by: number;
  created_at: string;
}

export interface Reconciliation {
  id: number;
  metric_id: number;
  /** 对账指标编码/名称（后端联查回填，旧后端可能缺省 → 前端回退 #metric_id） */
  metric_code?: string | null;
  metric_name?: string | null;
  dim_code: string | null;
  expected_expr: string;
  actual_expr: string;
  status: string;
  diff_summary: string | null;
  reviewed_by: number | null;
  created_at: string;
  reviewed_at: string | null;
}

export interface DimensionMember {
  id: number;
  dim_code: string;
  member_code: string;
  member_name: string;
  parent_code: string | null;
  path: string | null;
  attributes: Record<string, unknown> | null;
  status: string;
  created_at: string;
}

/** 指标-维度绑定关系（POST /dimensions/{dim_code}/metrics） */
export interface MetricDimension {
  id: number;
  metric_id: number;
  dim_code: string;
  role: string;
  default_member: string | null;
  /** 维度当前状态（DRAFT/PUBLISHED/DEPRECATED）——指标详情「关联维度」展示维度废弃态 */
  dim_status?: string | null;
}

/** 按维度查绑定指标（GET /dimensions/{dim_code}/metrics，role 为 PARTITION/SPLICE/FILTER） */
export interface DimensionMetricBinding {
  metric_id: number;
  metric_code: string;
  metric_name: string;
  role: string;
  default_member: string | null;
  metric_status: string;
}

// ============================================================================
// 逻辑度量目录（backend /api/v1/measure-catalogs/*，OneData 原子层）
// 原子指标 = 逻辑度量 + 基础统计粒度（日），不含业务限定与时间周期，不绑物理表；
// 度量格式/单位/小数位/源头系统/同义词由度量目录定义，原子指标继承（PRD FR-02-08）。
// ============================================================================

/** 度量格式（字典化：种子值为 AMOUNT/RATIO/NUMERIC，可经「系统设置 → 字典管理」自定义扩展，extra 携带默认单位/小数位） */
export type MeasureFormat = string;

/** 度量分类（字典化：种子值为 FLOW/FEE/DRUG/MEDICAL_INSURANCE/EFFICIENCY/QUALITY/OTHER，可经「系统设置 → 字典管理」自定义扩展） */
export type MeasureCategory = string;

export interface MeasureCatalog extends ReviewFields {
  id: number;
  measure_code: string;
  name: string;
  description: string | null;
  measure_format: MeasureFormat;
  /** 默认单位（金额:元/比率:小数/数值:自定义） */
  default_unit: string;
  /** 默认小数位数（金额2/比率4/数值按需，null=未定） */
  default_decimal_places: number | null;
  /** 源头系统（业务系统术语多值） */
  source_system: string[] | null;
  synonyms: string[] | null;
  /** 度量分类（FLOW/FEE/DRUG/MEDICAL_INSURANCE/EFFICIENCY/QUALITY/OTHER） */
  category: MeasureCategory;
  /** 统计口径（业务侧如何计算） */
  stat_caliber: string | null;
  domain: string;
  owner_id: number;
  status: string;
  created_at: string;
  updated_at: string;
}

/** 度量格式展示文案（对齐 PRD FR-02-08） */
export const MEASURE_FORMAT_LABEL: Record<MeasureFormat, string> = {
  AMOUNT: "金额",
  RATIO: "比率",
  NUMERIC: "数值",
};

/** 度量分类展示文案 */
export const MEASURE_CATEGORY_LABEL: Record<MeasureCategory, string> = {
  FLOW: "流量类",
  FEE: "费用类",
  DRUG: "药品类",
  MEDICAL_INSURANCE: "医保类",
  EFFICIENCY: "效率类",
  QUALITY: "质量类",
  OTHER: "其他",
};

/** 逻辑度量 AI 推断：单字段结果（值 + 来源 + 置信度 + 理由） */
export interface SuggestField {
  value: string | number | string[] | null;
  source: "llm" | "rule";
  confidence: number;
  reason: string;
}

export interface MeasureSuggestResult {
  fields: Record<string, SuggestField>;
}

// ============================================================================
// 指标挂载实体（backend /api/v1/metric-mounts/*，OneData 挂载层）
// 派生指标 = 原子指标 + 业务限定 + 时间周期 + 挂载；粒度从 metric 下沉到挂载（界限文档 §2.3 第 3 条）。
// ============================================================================

export interface MetricMount {
  id: number;
  metric_id: number;
  source_table: string;
  source_column: string;
  granularity: string;
  default_period: string | null;
  domain: string;
  /** 所属指标编码/名称/类型（列表接口 LEFT JOIN 回填） */
  metric_code?: string | null;
  metric_name?: string | null;
  metric_type?: string | null;
  created_at: string;
  updated_at: string;
}

/** 挂载实体输入（指标创建/更新请求内嵌，不含 metric_id——由服务端以指标 id 落库） */
export interface MetricMountInput {
  source_table: string;
  source_column: string;
  granularity: string;
  default_period?: string | null;
  domain: string;
}

// ============================================================================
// 术语表（backend /api/v1/terms/*）
// ============================================================================

export interface GlossaryTerm extends ReviewFields {
  id: number;
  term_code: string;
  name: string;
  definition: string;
  domain: string;
  synonyms: unknown[];
  boundary: string | null;
  status: string;
  owner_id: number;
  version?: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface GlossaryConflict {
  id: number;
  term_id: number;
  conflict_type: string;
  ref_term_id: number | null;
  ref_metric_id: number | null;
  status: string;
  resolver: number | null;
  created_at: string | null;
}

// 术语关系（backend POST /terms/{term_code}/relations，对齐 TermRelationResponse）
export interface TermRelation {
  id: number;
  source_term_id: number;
  target_term_id: number;
  relation_type: string;
  declared_by: number | null;
  source_type: string;
  confirmed_at: string | null;
}

// 术语关系图谱元素（backend GET /terms/{term_code}/relations 返回）
// direction：outgoing=本术语→对端（下游）；incoming=对端→本术语（上游）
export interface TermRelationViewItem {
  relation_type: string;
  direction: "outgoing" | "incoming";
  peer: {
    id: number;
    term_code: string;
    name: string;
    domain: string | null;
    status: string;
  };
}

// 术语关系图谱响应（GET /terms/{term_code}/relations 的 data 结构）
export interface TermRelationView {
  items: TermRelationViewItem[];
  total: number;
}

// ============================================================================
// 治理（backend /api/v1/governance + grants + roles + pii + erasure）
// ============================================================================

export interface RoleResponse {
  id: number;
  name: string;
  description: string | null;
}

export interface RolePermissionItem {
  role: string;
  default_actions: string[];
  custom_actions: string[] | null;
  effective_actions: string[];
  ui_default_actions: string[];
  ui_custom_actions: string[] | null;
  ui_effective_actions: string[];
  protected: boolean;
  is_custom: boolean;
}

/** 动作点注册表项（GET /governance/action-registry，角色管理可视化配置数据源）。 */
export interface ActionRegistryItem {
  action: string;
  module: string;
  label: string;
  description: string;
}

/** 角色行下拉选项（GET /roles/options，授权管理「角色」下拉 id→name）。 */
export interface RoleOption {
  id: number;
  name: string;
  is_custom: boolean;
}

/** 用户按钮权限点视图（角色继承 + 直挂并集，GET /users/{id}/permissions）。 */
export interface UserPermissionResponse {
  user_id: number;
  role: string;
  role_actions: string[];
  direct_actions: string[];
  effective_actions: string[];
}

export interface OrganizationView {
  id: number;
  name: string;
  code: string;
  status: string;
  domain: string | null;
  user_count: number;
  created_at: string | null;
}

export interface GrantResponse {
  id: number;
  user_id: number;
  role_id: number | null;
  domain: string | null;
  metric_whitelist: string[] | null;
  grant_type: string;
  status: string;
  row_level: boolean;
  expires_at: string | null;
  granted_by: number | null;
  reason: string | null;
}

export interface GrantCreate {
  user_id: number;
  role_id?: number | null;
  domain?: string | null;
  metric_whitelist?: string[] | null;
  grant_type?: string;
  row_level?: boolean;
  expires_at?: string | null;
  reason?: string | null;
}

export interface GrantBatchItem {
  user_id: number;
  domain: string | null;
  action: string;
  ok: boolean;
  detail: string;
}

export interface GrantBatchResult {
  dry_run: boolean;
  operation: string;
  affected_users: number;
  affected_metrics: number;
  succeeded: number;
  failed: number;
  items: GrantBatchItem[];
}

export interface PiiReviewResult {
  metric_code: string;
  decision: string;
  compliance_reviewed: boolean;
  sensitivity_level: string;
  masking_policy: string;
  reviewer_id: number;
  reviewed_at: string;
  secondary_validation: Record<string, unknown> | null;
}

// ---- 敏感规则配置台（backend /api/v1/sensitive-rules/*）----
export interface SensitiveRuleItem {
  rule_id: string;
  label: string;
  category: string;
  category_label: string;
  name_re: string;
  sample_re: string | null;
  confidence: number;
  pii: boolean;
  source: "builtin" | "custom";
  status: "active" | "inactive";
  updated_at: string | null;
}

export interface SensitiveRuleCategory {
  category: string;
  label: string;
  pii: boolean;
}

export interface SensitiveRuleCreate {
  rule_id?: string | null;
  label: string;
  category: string;
  name_re: string;
  sample_re?: string | null;
  confidence?: number;
  pii?: boolean;
}

export type SensitiveRuleUpdate = Omit<SensitiveRuleCreate, "rule_id">;

export interface RegexCheckResult {
  valid: boolean;
  error: string | null;
}

export interface SensitiveRuleTestRequest {
  entity_name?: string;
  column_name: string;
  sample_value?: string | null;
  comment?: string | null;
}

export interface SensitiveRuleTestHit {
  column: string;
  category: string;
  category_label: string;
  rule: string;
  confidence: number;
  matched_by: string;
  pii: boolean;
}

export interface SensitiveRuleTestResponse {
  sensitivity_level: string;
  hits: SensitiveRuleTestHit[];
}

export interface PermissionSnapshot {
  user_id: number;
  role: string;
  home_domain: string | null;
  allowed_actions: string[];
  /** UI 权限点（模块:功能），前端 usePermission 消费；默认+role_permission 覆盖合并 */
  ui_actions: string[];
  granted_domains: string[];
  metric_whitelist: string[];
  row_level_restricted: boolean;
  grants: GrantResponse[];
  expiring_soon: GrantResponse[];
}

export interface PermissionCheckResult {
  allow: boolean;
  reason: string;
  error_code: string;
  restricted: boolean;
  masking: string;
}

export interface ClassificationRescanResult {
  scanned: number;
  changed: number;
  pii_found: number;
  degraded: number;
  model_version: string;
  items: Array<{
    catalog_id: number;
    entity_name: string;
    sensitivity_before: string;
    sensitivity_after: string;
    pii_columns: unknown[];
    degraded: boolean;
  }>;
}

export interface ErasureResult {
  subject_user_id: number;
  status: string;
  token_prefix: string;
  affected_rows: number;
  requested_at: string;
}

// ============================================================================
// 质量（backend /api/v1/quality/*）
// ============================================================================

export interface QualityRule {
  id: number;
  metric_id: number;
  rule_type: string;
  threshold: Record<string, unknown>;
  rule_mode: string;
  severity: string;
  enabled: boolean;
  notify_targets: Record<string, unknown> | null;
  created_by: number;
  created_at: string | null;
}

export interface QualityRuleCreate {
  metric_id: number;
  rule_type: string;
  threshold: Record<string, unknown>;
  rule_mode?: string;
  severity?: string;
  enabled?: boolean;
  notify_targets?: Record<string, unknown> | null;
}

export interface QualityEvent {
  id: number;
  metric_id: number;
  level: string;
  rule_type: string;
  obs_value: number | null;
  threshold: number | null;
  status: string;
  created_at: string | null;
  ack_note: string | null;
  ack_by: number | null;
  ack_at: string | null;
  resolved_by: number | null;
  resolved_at: string | null;
  closed_by: number | null;
  closed_at: string | null;
  repair_suggestion: Record<string, unknown> | null;
}

export interface QualityObservation {
  id: number;
  metric_id: number;
  metric_code: string;
  source_id: string | null;
  obs_time: string;
  value: number;
  dims: Record<string, unknown> | null;
}

export interface QualityBenchmark {
  id: number;
  source_id: string;
  metric_code: string;
  bench_date: string;
  dims: Record<string, unknown> | null;
  bench_value: number;
  provider: string;
  tolerance_pct: number | null;
  imported_by: number;
  created_at: string | null;
}

export interface ReconciliationRecord {
  id: number;
  benchmark_id: number;
  metric_code: string;
  metric_value: number;
  bench_value: number;
  diff_pct: number;
  window: string | null;
  status: string;
  owner_note: string | null;
  decision: string | null;
  confirmed_by: number | null;
  checked_at: string | null;
  created_at: string | null;
}

// ============================================================================
// 通知（backend /api/v1/notify/*）
// ============================================================================

export interface Notification {
  id: number;
  subscriber_id: number;
  channel: string;
  template_code: string | null;
  title: string;
  body: string | null;
  payload: Record<string, unknown> | null;
  status: string;
  send_at: string | null;
  sent_at: string | null;
  ref_type: string | null;
  ref_id: number | null;
  created_at: string;
  /** 已读时间；null 表示未读（Badge 未读计数 / 未读高亮依据） */
  read_at: string | null;
  /** 操作人 ID（谁发起的操作；null=系统/定时任务） */
  actor_id: number | null;
  /** 操作人姓名快照（服务端解析，历史通知稳定展示） */
  actor_name: string | null;
  /** 最近一次投递失败原因（仅 FAILED 状态有值，重试成功后清空） */
  last_error: string | null;
  /** 待办已处理时间；null 表示未处理（「仅待处理」筛选排除非空项） */
  handled_at: string | null;
}

export interface NotifyEventLog {
  id: number;
  event_type: string;
  source: string | null;
  payload: Record<string, unknown> | null;
  level: string;
  notified: boolean;
  created_at: string;
  actor_id: number | null;
  actor_name: string | null;
}

export interface SubscriptionPref {
  id: number;
  user_id: number;
  channel: string;
  event_type: string | null;
  /** 资产维度订阅（按指标/源表 watch）：asset_type 非空时 event_type 为 null */
  asset_type?: string | null;
  asset_id?: string | null;
  enabled: boolean;
  threshold: number | null;
  created_at: string;
}

// ============================================================================
// 可观测（backend /api/v1/observability/*）
// ============================================================================

export interface Feedback {
  id: number;
  user_id: number;
  target_type: string;
  target_id: string | null;
  /** 反馈对象名称（服务端批量解析；对象失效/删除时为 null，前端标记「已失效」） */
  target_name?: string | null;
  rating: number | null;
  comment: string | null;
  /** 反馈分类（运营按类分派）：bug/feature/improvement/question/praise */
  category: string;
  /** 反馈优先级（排期与 SLA 依据）：high/medium/low */
  priority: string;
  /** 反馈来源页面 URL（提交时自动捕获） */
  source_url: string | null;
  /** NPS 评分（0-10，仅 NPS 采集记录有值） */
  nps_score: number | null;
  /** 采纳闭环状态：pending/in_progress/adopted/rejected/clarifying */
  status: string;
  /** 质疑澄清（质疑→澄清→修订闭环）：提交人在 clarifying 状态补充的说明 */
  clarification?: string | null;
  clarified_at?: string | null;
  resolution_note: string | null;
  resolver_id: number | null;
  resolved_at: string | null;
  created_at: string;
}

/** NPS 分布统计（GET /observability/nps/stats） */
export interface NpsStats {
  total: number;
  promoters: number;
  passives: number;
  detractors: number;
  score: number;
}

/** 可观测中心最近质量事件明细行（GET /observability/quality-events） */
export interface QualityEventItem {
  id: number;
  level: string;
  status: string;
  rule_type: string;
  obs_value: number | null;
  threshold: number | null;
  metric_id: number;
  metric_name: string | null;
  metric_code: string | null;
  /** 指标所属域（资产归属上下文） */
  metric_domain?: string | null;
  ack_note: string | null;
  ack_by: number | null;
  /** 确认人用户名（display_name 优先回落 username） */
  ack_by_name?: string | null;
  ack_at: string | null;
  resolved_by: number | null;
  resolved_by_name?: string | null;
  resolved_at: string | null;
  closed_by: number | null;
  closed_by_name?: string | null;
  closed_at: string | null;
  repair_suggestion: Record<string, unknown> | null;
  created_at: string | null;
}

export interface ObsMetricsQuality {
  by_level: Record<string, number>;
  by_status: Record<string, number>;
  total: number;
}

export interface ObsMetricsNotifications {
  by_status: Record<string, number>;
  event_total: number;
  event_notified: number;
}

/** 平台运营总览（backend GET /observability/overview） */
export interface ObsOverview {
  sources: {
    by_health: Record<string, number>;
    total: number;
  };
  backlog: {
    open_conflicts: number;
    pending_quality_events: number;
    review_metrics: number;
    open_escalations: number;
  };
  assets: {
    metrics_by_status: Record<string, number>;
    terms: number;
    dimensions: number;
    domains: number;
    sources: number;
  };
  clients: {
    total: number;
    active: number;
  };
  /** 系统健康：核心依赖实时态 + 采集链路（熔断/失败/新鲜度是运维第一信号） */
  system: {
    dependencies: {
      by_status: Record<string, number>;
      circuit_open: number;
      total: number;
      items: Array<{
        dependency_type: string;
        dependency_id: string;
        status: string;
        circuit_state: string;
        consecutive_failures: number;
        latency_p95_ms: number | null;
        error_rate_pct: number;
        last_check_at: string | null;
      }>;
    };
    collection: {
      by_status: Record<string, number>;
      total: number;
      running: number;
      failed: number;
      success_rate_pct: number;
      last_collected_at: string | null;
    };
  };
  /** 资产质量：指标健康度分布 + 血缘健康 */
  quality: {
    metric_health: {
      by_level: Record<string, number>;
      total_scored: number;
      coverage_pct: number;
      avg_score: number;
      top_risk: Array<{
        metric_id: number;
        /** 指标名/编码（后端 JOIN Metric 随行返回，供「低健康指标」展示业务名称而非裸 ID） */
        metric_name: string | null;
        metric_code: string | null;
        score: number;
        level: string;
        missing_dimensions: string[] | null;
      }>;
    };
    lineage: {
      edges: number;
      stale: number;
      ingest_success: number;
      last_ingest_at: string | null;
    };
  };
  /** 风险雷达：PII 待复核 / 授权即将到期 / 近 7 天 Schema 漂移 */
  risks: {
    pii_review_pending: number;
    grants_expiring_soon: number;
    schema_drift_7d: number;
  };
  /** 近 7 天趋势：指标新增 / 采集运行 按天聚合 */
  trends: {
    days: number;
    metrics_created: Array<{ date: string; count: number }>;
    collections: Array<{ date: string; count: number }>;
  };
}

// ============================================================================
// 推荐（backend /api/v1/recommend/*）
// ============================================================================

export interface RecommendItem {
  metric_id: string;
  via?: string;
  score?: number;
  edge_type: string;
  from?: string;
  reason?: string;
}

export interface RecommendTermsResponse {
  items: GlossaryTerm[];
  total: number;
}

// ============================================================================
// AI 助手（backend /api/v1/ai/*）
// ============================================================================

export interface NL2SQLResult {
  anchored: string[];
  sql: string;
  params: Record<string, unknown>;
  safe: boolean;
  notes: string[];
  method: string;
  execute: boolean;
  execute_result?: { rows: unknown[]; total: number; elapsed_ms: number };
  execute_error?: string;
}

// LLM 平台配置（backend /api/v1/ai/config，多实例轮询路由）
export interface LlmConfigItem {
  id: number | null;
  name: string;
  provider: string;
  base_url: string;
  model: string;
  has_api_key: boolean;
  timeout: number;
  enabled: boolean;
  priority: number;
  source: "db" | "env" | "none";
  can_edit: boolean;
  updated_by: number | null;
  updated_at: string | null;
}

export interface LlmConfigList {
  items: LlmConfigItem[];
  strategy: string;
  effective: Record<string, unknown> & {
    provider: string;
    base_url: string;
    model: string;
    source: "db" | "env" | "none";
  };
  can_edit: boolean;
}

export interface LlmConfigPayload {
  name: string;
  provider: string;
  base_url: string;
  model: string;
  api_key?: string;
  timeout: number;
  enabled: boolean;
  priority: number;
}

export interface LlmConfigTestResult {
  ok: boolean;
  latency_ms: number;
  model: string;
  error: string;
  detail?: Record<string, unknown> | null;
  /** GET /models 返回的可用模型列表（连通成功时） */
  models?: string[];
  /** 真实推理探测是否通过（true=可推理；false=网关可达但模型不可推理；undefined=未执行） */
  chat?: boolean;
}

export interface LlmConfigSecret {
  id: number;
  api_key: string;
}

// 一键获取模型列表结果（backend /api/v1/ai/config/models）
export interface LlmModelsResult {
  models: string[];
  supported: boolean;
  error: string;
  latency_ms: number;
}

// ============================================================================
// 审计日志（backend /api/v1/audit）
// ============================================================================

export interface AuditEntry {
  id: number;
  actor_id: number;
  action: string;
  entity_type: string;
  entity_id: string;
  detail_json: Record<string, unknown> | null;
  ip: string;
  trace_id: string;
  pii_access: boolean;
  archived: boolean;
  created_at: string;
  /** 后端 enrich：站在用户角度的中文描述（如「发布了指标定义（版本=v2）」） */
  action_desc?: string;
  /** 后端 enrich：操作人显示名（联查 user，回退「用户 #id」） */
  actor_display?: string;
}

// ============================================================================
// 埋点统计（backend /api/v1/tracking/stats，platform_admin/domain_admin）
// ============================================================================

/** 埋点统计单行（按 group_by 分组聚合的结果）。 */
export interface TrackingStatsRow {
  group_key: string;
  event_count: number;
  unique_actors: number;
}

/** 埋点统计响应（backend tracking.py TrackingStatsResponse）。 */
export interface TrackingStatsResponse {
  stats: TrackingStatsRow[];
  /** 当前过滤条件下全量去重用户数（不随分组变化）；缺失时前端降级为各组 unique_actors 之和 */
  total_unique_actors?: number;
}

/** 埋点统计允许的分组字段（对齐后端 _GROUP_BY_ALLOWED 白名单）。 */
export type TrackingGroupBy = "event_type" | "target_type" | "actor_id";

// ============================================================================
// 采集器（backend /api/v1/data-sources + /api/v1/catalogs）
// ============================================================================

export type SourceType =
  | "mysql"
  | "postgres"
  | "hive"
  | "spark"
  | "doris"
  | "clickhouse"
  | "kafka"
  | "starrocks";

export interface SourceTypeInfo {
  source_type: string;
  label: string;
  default_port: number;
  supports_database: boolean;
  supports_schema: boolean;
  description: string;
}

export interface DataSource {
  source_id: string;
  name: string;
  source_type: SourceType;
  domain: string;
  cluster_id: string | null;
  coverage: number;
  health_status: string;
  connection_config_present: boolean;
  /** 明文连接配置：仅详情接口返回（供编辑回显）；列表接口为 null（脱敏） */
  connection_config?: Record<string, unknown> | null;
  /** 目标数据库列表（多库采集；null=全部库/单库配置） */
  databases?: string[] | null;
  schedule_cron: string | null;
  /** 是否启用定时调度（停用后保留 cron 配置但不触发，源仍可手动采集） */
  schedule_enabled: boolean;
  collection_mode: string;
  enabled: boolean;
  created_by: number | null;
  created_at: string;
  updated_at: string;
  // ---- 三期治理字段 ----
  owner_id?: number | null;
  owner_name?: string | null;
  description?: string | null;
  include_patterns?: string[] | null;
  exclude_patterns?: string[] | null;
  health_metrics?: Record<string, unknown> | null;
  degraded_since?: string | null;
  /** 资源配额（max_concurrency/max_scan_rows） */
  quota?: Record<string, unknown>;
  // ---- 列表信号（list_sources 批量回填）----
  table_count?: number | null;
  pii_count?: number | null;
  last_collected_at?: string | null;
  drift_count?: number | null;
  scanned_count?: number | null;
  failed_count?: number | null;
}

export interface DataSourceListResponse {
  items: DataSource[];
  total: number;
  page: number;
  page_size: number;
}

export interface BatchSourceItem {
  source_id: string;
  name: string | null;
  ok: boolean;
  error_code: string | null;
  message: string | null;
}

export interface BatchSourceResult {
  succeeded: BatchSourceItem[];
  failed: BatchSourceItem[];
}

export interface BatchToggleRequest {
  source_ids: string[];
  enabled: boolean;
}

export interface BatchDeleteRequest {
  source_ids: string[];
}

export interface DataSourceCreateRequest {
  /** 不传时由系统按 类型_库|域 自动生成 */
  source_id?: string | null;
  name: string;
  source_type: SourceType;
  connection_config: Record<string, unknown>;
  domain: string;
  cluster_id?: string | null;
  /** 目标数据库列表（多库采集；null/空=全部库/单库配置） */
  databases?: string[] | null;
  /** 默认采集模式（FULL 全量 / INCREMENTAL 增量），默认 FULL */
  collection_mode?: string;
  /** 表级白名单（fnmatch；可视化选表自动生成 库.表/库.*，亦可高级模式手填） */
  include_patterns?: string[];
  /** 表级黑名单（fnmatch） */
  exclude_patterns?: string[];
}

export interface DataSourceUpdateRequest {
  /** PATCH 语义：全部字段可选，仅更新传入项；source_id 不可变更 */
  name?: string;
  source_type?: SourceType;
  connection_config?: Record<string, unknown>;
  domain?: string;
  cluster_id?: string | null;
  /** 停用/启用：undefined 表示不修改 */
  enabled?: boolean;
  // ---- 三期治理字段 ----
  owner_id?: number | null;
  description?: string | null;
  include_patterns?: string[] | null;
  exclude_patterns?: string[] | null;
  /** 资源配额（max_concurrency/max_scan_rows） */
  quota?: Record<string, unknown>;
  /** 目标数据库列表；[] 表示清空（采集全部库），undefined 表示不修改 */
  databases?: string[] | null;
  /** 默认采集模式（FULL/INCREMENTAL），undefined 表示不修改 */
  collection_mode?: string;
}

/** 数据源资产规模概览（GET /data-sources/{id}/overview） */
export interface SourceOverview {
  source_id: string;
  entity_types: Record<string, number>;
  by_sensitivity: Record<string, number>;
  total_fields: number;
  drift_count: number;
  coverage: number;
  last_collected_at: string | null;
  scanned_count: number;
  failed_count: number;
}

export interface TestConnectionResult {
  ok: boolean;
  source_type: string;
  latency_ms: number | null;
  error: string | null;
  detail: Record<string, unknown> | null;
}

export interface DBCatalog {
  /** db_catalog 主键（列表/详情接口均返回；LLM 推断/编辑描述依赖） */
  id?: number;
  source_id: string;
  entity_name: string;
  entity_type: string;
  schema_def: Record<string, unknown>;
  etl_sql: string | null;
  sensitivity_level: string;
  owner_id: number | null;
  upstream_signature: string;
  content_signature: string | null;
  schema_incomplete: boolean;
  /** 数据源维度展示信息：源是否已删除 / 源名称 */
  source_deleted?: boolean;
  source_name?: string | null;
  /** 业务域（经数据源继承回填）与责任人展示名 */
  domain?: string | null;
  owner_name?: string | null;
  /** 表级业务描述（治理补全，TD §12.1） */
  description?: string | null;
  description_source?: DescriptionSource | null;
  description_updated_by?: number | null;
  description_updated_at?: string | null;
  /** 元数据实体最近更新时间（采集刷新/治理补全时更新；资产目录「最近更新」列用） */
  updated_at?: string | null;
}

export interface CollectResult {
  source_id: string;
  scanned: number;
  registered: number;
  pii_registered: number;
  failed_count: number;
  failed_specs: Array<{ entity_name: string; error: string }>;
  coverage: number;
  mode: string;
  drift_count: number;
  drift_events: Array<{ entity_name: string; change_type: string }>;
  deprecated_count?: number;
  /** 源表 DROP → 血缘下游指标置 DATA_SOURCE_DROPPED 的数量（采集侧自动触发） */
  dsd_count?: number;
  /** 本次临时/数据源白黑名单过滤跳过的表数（方案 B） */
  filtered_count?: number;
  /** 被过滤跳过的表名（方案 B） */
  filtered_names?: string[];
  /** 本次采集到的实体明细（表名 + 敏感度 + 漂移标记） */
  entities?: Array<{
    entity_name: string;
    sensitivity_level: string;
    drifted: boolean;
    change_type: string | null;
  }>;
}

/** 数据源实例下的非系统数据库列表。 */
export interface ListDatabasesResult {
  databases: string[];
  source_type: string;
}

/** 数据源实例下按库分组的表名列表（级联选表）。 */
export interface ListTablesResult {
  tables: Record<string, string[]>;
  source_type: string;
}

/** 异步立即采集（collect-now）返回。 */
export interface CollectNowResult {
  job_id: string;
  status: string;
  mode?: string;
}

/** 采集任务 SSE 进度快照（detail.progress）。 */
export interface CollectionProgress {
  phase?: string;
  message?: string;
  messages?: string[];
  index?: number | null;
  total?: number | null;
  entity_name?: string | null;
  scanned?: number | null;
  sensitivity?: string | null;
}

export interface ScheduleResult {
  scheduled: boolean;
  cron: string;
  /** 采集模式（未传时可能为 null，保持数据源现有模式） */
  mode?: string | null;
  /** 是否启用定时调度（缺省时保持当前状态） */
  schedule_enabled?: boolean;
}

export interface JobStatus {
  job_id: string;
  source_id?: string;
  actor_id?: number;
  status: string;
  detail: Record<string, unknown>;
}

export interface Watermark {
  source_id: string;
  last_collected_at: string | null;
  mode: string;
  scanned_count: number;
  failed_count: number;
}

export interface SourceHealth {
  source_id: string;
  health_status: string;
  last_collected_at: string | null;
  last_error: string | null;
  last_health_check: string | null;
  uptime_check: boolean;
  /** 三期：DEGRADED（黄态）健康指标与降级起始时间 */
  health_metrics?: Record<string, unknown> | null;
  degraded_since?: string | null;
}

/** 异步采集任务（采集任务中心）。 */
export interface CollectionJob {
  job_id: string;
  source_id?: string;
  actor_id?: number;
  status: string;
  detail?: Record<string, unknown>;
  created_at?: string | null;
  /** 任务来源：manual 手动触发 / scheduled 定时调度（由 job_id 前缀推导）。 */
  kind?: "manual" | "scheduled";
}

/** 一次采集运行的持久化历史记录（采集记录页主视图，TD §12.1）。 */
export interface CollectionRun {
  id: number;
  source_id: string;
  source_name?: string | null;
  job_id?: string | null;
  /** 触发方式：manual 手动 / scheduled 定时 */
  trigger: "manual" | "scheduled" | string;
  /** 请求采集模式 */
  mode: string;
  /** 实际执行模式（增量降级为全量后回填） */
  effective_mode?: string | null;
  status: "RUNNING" | "COMPLETED" | "FAILED" | string;
  actor_id?: number | null;
  actor_name?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  /** 耗时（秒） */
  duration_seconds?: number | null;
  scanned: number;
  registered: number;
  pii_registered: number;
  failed_count: number;
  drift_count: number;
  deprecated_count: number;
  /** 源表 DROP → 血缘下游指标置 DATA_SOURCE_DROPPED 的数量（采集侧自动触发） */
  dsd_count?: number;
  coverage?: number | null;
  error?: string | null;
  /** 明细（详情接口返回）：failed_specs / drift_events / degrade_reason */
  detail?: {
    failed_specs?: Array<{ entity_name: string; error: string }>;
    drift_events?: Array<{ entity_name: string; change_type: string }>;
    degrade_reason?: string | null;
    dsd_count?: number;
  } | null;
}

// ============================================================================
// 资产地图（backend /api/v1/assetmap/*）
// ============================================================================

export interface AssetCatalogSummary {
  total: number;
  by_entity_type: Record<string, number>;
  by_sensitivity: Record<string, number>;
  orphan_assets: number;
}

export interface AssetClassificationSummary {
  by_sensitivity: Record<string, number>;
}

export interface AssetMetricSummary {
  by_domain: Record<string, number>;
  by_status: Record<string, number>;
}

// 指标体系聚合（GET /assetmap/metric-dimensions）：8 类维度分布 + PII 合规率
export interface AssetMetricDimensionSummary {
  total: number;
  by_type: Record<string, number>;
  by_granularity: Record<string, number>;
  by_dw_layer: Record<string, number>;
  by_metric_tier: Record<string, number>;
  by_unit: Record<string, number>;
  by_currency: Record<string, number>;
  by_aggregation: Record<string, number>;
  by_time_semantics: Record<string, number>;
  by_freshness: Record<string, number>;
  by_serving_mode: Record<string, number>;
  by_additivity: Record<string, number>;
  by_status: Record<string, number>;
  by_domain: Record<string, number>;
  pii_compliance: {
    pii_total: number;
    pii_reviewed: number;
    pii_unreviewed: number;
    review_rate: number;
  };
}

export interface AssetTableItem {
  /** 目录主键（db_catalog.id），用于实体详情下钻；后端 to_dict 保留 id */
  id?: number;
  source_id: string;
  entity_name: string;
  entity_type: string;
  /** 敏感级别；历史 to_dict 曾剥离该字段，按可空处理（渲染端防御） */
  sensitivity_level?: string | null;
  owner_id: number | null;
  /** 责任人展示名（后端批量回填，display_name 优先） */
  owner_name?: string | null;
  /** 源名称（后端批量回填） */
  source_name?: string | null;
  /** 业务域（经 data_source 继承回填） */
  domain?: string | null;
  /** 表级业务描述 */
  description?: string | null;
  schema_incomplete: boolean;
  etl_sql?: string | null;
  /** 新鲜度字段（后端 to_dict 透传 created_at/updated_at） */
  created_at?: string | null;
  updated_at?: string | null;
}

/** 字段描述来源 */
export type DescriptionSource = "manual" | "llm" | "schema";

/** Schema 字段结构（含优先级合并后的描述与来源标记） */
export interface SchemaColumn {
  name: string;
  type?: string;
  comment?: string;
  /** 优先级合并后最终展示描述（manual > llm > schema_json comment） */
  description?: string;
  /** 描述来源标记 */
  description_source?: DescriptionSource | null;
  nullable?: boolean;
  default?: string;
}

/** 独立字段描述记录（对应 column_descriptions 表） */
export interface ColumnDescription {
  catalog_id: number;
  column_name: string;
  description: string;
  source: DescriptionSource;
  updated_by: number | null;
  updated_at: string;
}

// 实体详情（GET /api/v1/assetmap/entities/{entity_id}，A1 新增端点）
export interface AssetEntityDetail {
  id: number;
  entity_name: string;
  entity_type: string;
  source_id: string;
  /** 源名称（经 data_source 回填） */
  source_name?: string | null;
  /** 业务域（经 data_source 继承回填） */
  domain?: string | null;
  sensitivity_level: string | null;
  owner_id: number | null;
  /** 责任人展示名（display_name 优先） */
  owner_name?: string | null;
  /** 字段数（schema_summary 为 list 时的长度） */
  column_count?: number | null;
  schema_incomplete: boolean;
  content_signature: string | null;
  /** schema 摘要：结构化字段列表或字符串/null */
  schema_summary?: SchemaColumn[] | string | null;
  /** 表级业务描述（治理补全，TD §12.1） */
  description?: string | null;
  description_source?: DescriptionSource | null;
  description_updated_at?: string | null;
  /** 血缘相关边数（表/字段级别） */
  lineage_count?: number;
  /** 血缘边明细列表（生产化增强） */
  lineage_edges?: Array<{
    source: string;
    target: string;
    edge_type: string;
    granularity?: string;
    confidence?: number;
    provenance?: string;
  }>;
  /** 关联指标（血缘下游 metric: 节点） */
  related_metrics?: Array<{ metric_node: string; edge_type: string }>;
  /** 源健康状态（生产化增强） */
  source_health?: {
    health_status: string;
    last_health_check: string | null;
    source_name: string | null;
  };
  /** 是否含 PII */
  pii_flag?: boolean;
  // ---- PII 合规增强：字段级明细 / 合规状态 / 脱敏 / 保留期 ----
  /** 字段级 PII 命中明细（列名/类别/规则/置信度/人工标注） */
  pii_fields?: AssetPiiField[];
  /** 活跃 PII 字段数（排除误报标注） */
  pii_field_count?: number;
  pii_categories?: string[];
  /** 字段级人工标注列表 */
  pii_overrides?: AssetPiiOverride[];
  /** 表级合规复核状态 */
  compliance_reviewed?: boolean;
  compliance_reviewed_by?: number | null;
  compliance_reviewed_at?: string | null;
  /** 脱敏策略（none/mask/hash/deny） */
  masking_policy?: string | null;
  /** 保留期（天）与合法性基础 */
  retention_days?: number | null;
  legal_basis?: string | null;
  retention_expires_at?: string | null;
  /** 保留期是否临近到期（30 天内） */
  retention_expiring?: boolean;
  etl_sql?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface AssetOwnerView {
  owner_id: number;
  owner_name?: string | null;
  role?: string | null;
  domain?: string | null;
  metrics: {
    total: number;
    published: number;
    draft: number;
    pii_count: number;
    by_domain: Record<string, number>;
    by_type: Record<string, number>;
    by_metric_tier: Record<string, number>;
    snapshot_covered: number;
    todo: {
      pii_unreviewed: number;
      deprecated_without_successor: number;
    };
  };
  catalogs: {
    total: number;
    items: Array<{
      id: number;
      entity_name: string;
      entity_type: string;
      sensitivity_level: string | null;
      source_id: string;
      source_name?: string | null;
      owner_name?: string | null;
      updated_at: string | null;
    }>;
  };
}

// 二维热力矩阵（GET /assetmap/heatmap-matrix?asset_type=）
// catalog 视角：sensitivity 为敏感级枚举；metric 视角：sensitivity 为 INTERNAL/PII
export interface AssetHeatmapMatrix {
  cells: Array<{
    domain: string;
    sensitivity: string;
    count: number;
    pii_count: number;
  }>;
  columns: string[];
}

// ---- 产品补充（FR-18 生产化）：搜索 / 健康 / PII / 变更 / 我的资产 ----

// 全局搜索（GET /assetmap/search）——富化：源/责任人/描述/字段数 + 指标口径字段
export interface AssetSearchItem {
  type: "catalog" | "field" | "metric";
  id: number;
  name: string;
  entity_type: string;
  sensitivity_level: string | null;
  domain: string | null;
  owner_id: number | null;
  owner_name?: string | null;
  source_id?: string | null;
  source_name?: string | null;
  description?: string | null;
  column_count?: number | null;
  updated_at?: string | null;
  status: string | null;
  /** 以下为指标专属字段（type=metric 时有值） */
  metric_type?: string;
  granularity?: string;
  unit?: string;
  aggregation?: string;
  time_semantics?: string;
  freshness?: string;
  dw_layer?: string;
  metric_tier?: string;
  additivity?: string;
  serving_mode?: string;
}

// 资产健康视图（GET /assetmap/health）——升级：评分 + 9 项体检
export interface AssetHealthSummary {
  score: number;
  level: "excellent" | "good" | "fair" | "poor";
  checks: Array<{
    key: string;
    count: number;
    deduct: number;
    field_total?: number;
  }>;
  unhealthy_sources: Array<{ source_id: string; name: string; health_status: string }>;
  schema_incomplete: Array<{ id: number; entity_name: string; source_id: string }>;
  orphan_assets: number;
  stale_assets: Array<{ id: number; entity_name: string; updated_at: string }>;
  stale_days: number;
  pii_unreviewed: Array<{ metric_code: string; name: string; owner_id: number | null }>;
  metrics_without_snapshot: Array<{ metric_code: string; name: string }>;
  deprecated_without_successor: Array<{ metric_code: string; name: string }>;
}

// PII 合规视图（GET /assetmap/pii）——增强：类别分布 + 风险计数（无主/待复核/已复核）
export interface AssetPiiOverview {
  by_sensitivity: Record<string, number>;
  by_domain: Record<string, number>;
  pii_metric_count: number;
  pii_catalog_count: number;
  /** 字段级 PII 类别分布（ID_CARD/PHONE/...） */
  by_category: Record<string, number>;
  /** 无主 PII 目录数（最高优先级合规风险） */
  unowned_pii: number;
  /** 待复核 PII 总数（目录 + 指标） */
  unreviewed_pii: number;
  unreviewed_catalog: number;
  unreviewed_metric: number;
  /** 已复核 PII 目录数 */
  reviewed_pii: number;
}

// PII 合规增强：PII 资产明细 / 字段级命中 / 人工标注 / 行业模板
export interface AssetPiiField {
  column: string;
  category: string;
  rule: string;
  confidence: number;
  matched_by: string;
  /** 人工标注：True=误报非 PII；False=确认是 PII */
  suppressed?: boolean;
  override_reason?: string | null;
}

export interface AssetPiiAssetItem {
  id: number;
  entity_name: string;
  entity_type: string;
  source_id: string;
  source_name?: string | null;
  domain?: string | null;
  sensitivity_level: string;
  owner_id: number | null;
  owner_name?: string | null;
  compliance_reviewed: boolean;
  masking_policy?: string | null;
  pii_field_count: number;
  categories: string[];
  pii_fields?: AssetPiiField[];
  updated_at: string;
}

export interface AssetPiiTemplate {
  id: string;
  name: string;
  description: string;
  sensitive_categories: string[];
}

export interface AssetPiiOverride {
  column: string;
  suppressed: boolean;
  reason?: string | null;
}

// 变更追踪（GET /assetmap/changes）——富化：变更类型/责任人/版本 + drift 明细
export interface AssetChangeItem {
  id: number;
  entity_name: string;
  entity_type: string;
  sensitivity_level: string;
  owner_id: number | null;
  owner_name?: string | null;
  source_id: string;
  source_name?: string | null;
  created_at: string | null;
  updated_at: string;
  change_type: "created" | "updated";
}
export interface AssetChangeMetric {
  metric_code: string;
  name: string;
  status: string;
  domain: string;
  pii_flag: boolean;
  version: number;
  description?: string | null;
  owner_id: number | null;
  owner_name?: string | null;
  change_type: "created" | "updated" | "deprecated";
  updated_at: string;
}
export interface AssetDriftItem {
  id: number;
  source_id: string;
  entity_name: string;
  change_type: string;
  diff_json: Record<string, unknown> | null;
  created_at: string;
}
export interface AssetChanges {
  catalogs: AssetChangeItem[];
  metrics: AssetChangeMetric[];
  drift: AssetDriftItem[];
  days: number;
}

// 我的资产（GET /assetmap/my-assets）——富化：统计卡/口径摘要/快照覆盖/待认领
export interface AssetMyAssets {
  owner_id: number;
  catalogs: Array<{
    id: number;
    entity_name: string;
    entity_type: string;
    sensitivity_level: string;
    source_id: string;
    source_name?: string | null;
    owner_name?: string | null;
    description?: string | null;
    column_count?: number | null;
    updated_at?: string | null;
  }>;
  metrics: Array<{
    metric_code: string;
    name: string;
    status: string;
    domain: string;
    pii_flag: boolean;
    type?: string;
    granularity?: string;
    unit?: string;
    metric_tier?: string;
    description?: string | null;
    updated_at?: string | null;
  }>;
  summary: {
    catalog_count: number;
    metric_count: number;
    draft_count: number;
    pii_count: number;
    snapshot_covered: number;
    snapshot_total: number;
  };
  claimable_orphans: number;
}

export const API_BASE = "/api/v1";

// ============================================================================
// 主题域管理（backend /api/v1/domains/*）
// ============================================================================

export interface SubjectDomain {
  id: number;
  code: string;
  name: string;
  parent_id: number | null;
  level: number;
  path: string | null;
  sort_order: number;
  status: string;
  defaults_json: Record<string, unknown>;
  description: string | null;
  owner_id: number;
  metric_count: number;
  created_at: string;
  updated_at: string;
}

export interface SubjectDomainTreeNode {
  id: number;
  code: string;
  name: string;
  parent_id: number | null;
  level: number;
  sort_order: number;
  status: string;
  metric_count: number;
  dimension_count?: number;
  children: SubjectDomainTreeNode[];
}

export interface SubjectDomainCreateRequest {
  /** 域编码：可选，缺省由后端按显示名自动生成 */
  code?: string;
  name: string;
  parent_id?: number | null;
  sort_order?: number;
  description?: string | null;
  /** 域管理员：可选，缺省由后端以创建人认证身份覆盖（PLAT-2） */
  owner_id?: number;
  defaults_json?: Record<string, unknown>;
}

export interface SubjectDomainUpdateRequest {
  name?: string;
  sort_order?: number;
  description?: string | null;
  owner_id?: number;
  defaults_json?: Record<string, unknown>;
}

// ============================================================================
// 系统字典管理（backend /api/v1/dicts/*）
// ============================================================================

export interface SystemDictItem {
  id: number;
  dict_type: string;
  code: string;
  label: string;
  sort_order: number;
  status: string;
  description: string | null;
  /** 扩展属性（JSON）：如度量格式的默认单位/小数位 {"unit":"元","decimal":2} */
  extra: Record<string, unknown> | null;
  ref_count: number;
  created_at: string;
  updated_at: string;
}

export interface DictItemCreateRequest {
  /** 编码；缺省由后端按显示名自动生成英文名 */
  code?: string;
  label: string;
  sort_order?: number;
  description?: string | null;
  /** 扩展属性（JSON）：如度量格式的默认单位/小数位 */
  extra?: Record<string, unknown> | null;
}

export interface DictItemUpdateRequest {
  label?: string;
  sort_order?: number;
  description?: string | null;
  extra?: Record<string, unknown> | null;
}

/** 批量操作结果单项（207 语义，逐项标注成败原因） */
export interface DictBatchItem {
  code: string;
  label?: string | null;
  ok: boolean;
  error_code?: string | null;
  message?: string | null;
}

/** 批量操作结果（succeeded + failed 分桶） */
export interface DictBatchResult {
  succeeded: DictBatchItem[];
  failed: DictBatchItem[];
}

/** 字典值校验项（dict_type + value） */
export interface DictValueCheckItem {
  dict_type: string;
  value: string;
}

/** 批量校验字典值响应：未收录列表 */
export interface DictValuesVerifyResponse {
  unknown: DictValueCheckItem[];
}

/** 无收录权限用户保存未收录值时，通知管理员收录/打回请求 */
export interface DictUnknownNotifyRequest {
  metric_code?: string | null;
  values: DictValueCheckItem[];
  note?: string | null;
}

/** 字典收录申请打回请求（管理员操作） */
export interface DictUnknownRejectRequest {
  notification_id: number;
  reason?: string | null;
}

// ============================================================================
// 自动推断（backend /api/v1/metric-definitions/auto-suggest）
// ============================================================================

export interface AutoSuggestRequest {
  domain_code: string;
  source_table?: string | null;
  measure_column?: string | null;
  period?: string | null;
  sql?: string | null;
  /** 是否启用 LLM 全字段推断（默认走程序规则推断；LLM 产出经枚举白名单校验兜底）。 */
  use_llm?: boolean;
}

/** 单个推断字段：含取值、来源、置信度与理由，便于前端展示来源徽标。 */
export interface SuggestionField {
  value: unknown;
  source: string;
  confidence: number;
  reason?: string;
}

export interface AutoSuggestResponse {
  metric_code_suggestion: string | null;
  segments: {
    domain: string;
    biz_object: string | null;
    measure: string | null;
    period: string | null;
  };
  /** 13 字段推断结果（name/type/granularity/unit/aggregation/time_semantics/freshness/dw_layer/additivity/serving_mode/metric_tier/definition_json/definition_mode）。 */
  fields: Record<string, SuggestionField>;
  definition_json: Record<string, unknown> | null;
  definition_mode: string | null;
  /** 向后兼容：旧式域默认/规则默认值聚合。 */
  defaults?: Record<string, unknown>;
  /** 血缘推断关联表（向后兼容：上游依赖 + 下游使用并集，新代码请用下述方向拆分字段）。 */
  related_tables?: string[];
  /** 血缘推断：上游依赖表（加工出源表的表，填入「依赖表（上游）」）。 */
  source_tables?: string[];
  /** 血缘推断：下游使用表（消费源表的表，填入「使用表（下游）」）。 */
  downstream_tables?: string[];
  /** SQL 解析出的度量列清单（含聚合方式/来源表/原始表达式）——供用户确认推断
   *  是否真正识别成功（多度量脚本不再只对用户黑盒展示首个度量）。 */
  parsed_measures?: {
    column: string;
    agg: string;
    alias?: string | null;
    table?: string | null;
    sunk?: boolean;
    expression?: string | null;
  }[];
  /** 逻辑度量推荐（信息最大化）：按度量列名匹配已发布逻辑度量目录，供原子指标
   *  一键继承 measure_id。尽力而为——无匹配为空数组，不阻断推断。 */
  measure_suggestions?: MeasureSuggestion[];
}

/** SQL 推断推荐的逻辑度量候选（OneData 原子层继承源：原子指标 = 逻辑度量 + 聚合）。 */
export interface MeasureSuggestion {
  id: number;
  measure_code: string;
  name: string;
  measure_format: MeasureFormat;
  default_unit: string;
  /** 匹配置信度（0~1：列名与度量编码/同义词相等=1，包含关系=0.7） */
  confidence: number;
  reason: string;
}

/** 业务域建议候选（FR-010 域建议增强：反向定位/LLM 兜底）。 */
export interface DomainSuggestionCandidate {
  code: string;
  name: string;
  confidence: number;
  /** 来源：catalog（采集目录）/ mount（挂载实体）/ llm（AI 推断）。 */
  source: string;
  reason?: string;
}

/** 业务域建议响应（suggest-domain 端点四态）。 */
export interface DomainSuggestionResponse {
  /** unique（唯一命中）/ multiple（多候选）/ llm（AI 推断）/ none（无法建议）。 */
  status: "unique" | "multiple" | "llm" | "none";
  domain: DomainSuggestionCandidate | null;
  candidates: DomainSuggestionCandidate[];
  /** 命中归属的表（空=表未被采集，可提示）。 */
  matched_tables: string[];
}

// ============================================================================
// SQL 批量解析/注册（backend /parse-sql-batch / /batch-register-from-sql，FR-010 批量注册增强）
// ============================================================================

/** SQL 批量解析请求（场景A 多语句切分 / 场景B 单语句多度量拆分）。 */
export interface SqlParseRequest {
  /** 大段 SQL 脚本（含多个指标） */
  sql: string;
  /** 切分模式：semicolon（引号感知 ;）/ statement（CTE/INSERT 语义）/ custom（用户自定义规则） */
  split_mode?: "semicolon" | "statement" | "custom";
  /** 自定义切分规则：{delimiters: string[], start_markers: string[]} */
  custom_rules?: { delimiters?: string[]; start_markers?: string[] } | null;
  /** 显式指定域（缺省自动建议） */
  domain_code?: string | null;
  /** 单语句多度量时是否合成复合指标候选 */
  synthesize_composite?: boolean;
  /** 显式 LLM 模式：对规则候选做一次 LLM 批量补全（中文名/周期/非度量过滤）+ 规范收敛 */
  use_llm?: boolean;
}

/** SQL 批量解析候选（前端勾选微调后提交创建）。 */
export interface SqlBatchCandidate {
  /** 稳定标识：{语句序号}:{度量列}（原子）/{语句序号}:composite（复合） */
  key: string;
  metric_code: string;
  name: string;
  type: MetricType;
  source_table: string | null;
  measure_column: string | null;
  aggregation: string | null;
  period: string | null;
  unit: string | null;
  granularity: string | null;
  /** 口径定义（原子：expression 模式；派生/复合：expression+dependencies） */
  definition_json: Record<string, unknown>;
  definition_mode: string;
  /** 派生/复合候选的依赖指标编码 */
  dependencies?: string[] | null;
  /** 派生/复合候选的计算表达式（前端在线编辑，如 {a} / {b}；提交合入 definition_json.expression） */
  calc_expression?: string | null;
  /** OneData 原子层：候选关联逻辑度量（SQL 无法推断恒空，前端选择器关联后透传） */
  measure_id?: number | null;
  /** 口径溯源：候选所属语句原始 SQL（批量创建透传落 Metric.raw_sql） */
  raw_sql?: string | null;
  statement_index: number;
  /** P2-10：语句级建议域（整段域建议为多域/无域时后端逐语句反查；与生效域可能不同） */
  suggested_domain_code?: string | null;
  /** P2-2：候选来源（rule=规则层可靠产出 / llm=LLM 兜底提取，需人工复核） */
  source?: "rule" | "llm" | null;
  /** A-1/2：CASE/窗口/下沉子查询口径需人工核对（expression 非简单 SUM(col)） */
  needs_review?: boolean;
  /** P0-2：口径三方责任（复合候选批量创建补齐；原子通常随创建人/域默认） */
  product_owner_id?: number | null;
  tech_owner_id?: number | null;
  dw_developer_id?: number | null;
  product_owner_name?: string | null;
  tech_owner_name?: string | null;
  dw_developer_name?: string | null;
}

/** SQL 批量解析语句摘要（前端 Collapse 分组标题）。 */
export interface SqlStatementMeta {
  index: number;
  sql: string;
  source_tables: string[];
  measure_count: number;
  group_by: string[];
  /** P2-10：语句级建议域编码（未建议为 null） */
  suggested_domain?: string | null;
}

/** SQL 批量解析响应（parse-sql-batch 端点）。 */
export interface SqlBatchParseResult {
  statements: SqlStatementMeta[];
  candidates: SqlBatchCandidate[];
  /** 无聚合度量列的语句（跳过原因）。 */
  skipped: { index: number; sql: string; reason: string }[];
  /** 域建议（未显式指定域时返回四态；已指定=status user）。 */
  domain: {
    code: string | null;
    name: string | null;
    status: string;
    confidence: number | null;
    candidates: DomainSuggestionCandidate[];
    matched_tables: string[];
  } | null;
}

/** SQL 批量创建候选（勾选微调后提交，创建端纯写不重跑 LLM）。 */
export interface SqlBatchRegisterCandidate {
  key: string;
  metric_code: string;
  name: string;
  type: MetricType;
  source_table?: string | null;
  measure_column?: string | null;
  aggregation?: string | null;
  unit?: string | null;
  period?: string | null;
  /** P1-5：粒度（推断产出，批量创建落库；旧式物理来源承载） */
  granularity?: string | null;
  measure_id?: number | null;
  definition_json: Record<string, unknown>;
  /** 口径溯源：候选所属语句原始 SQL（透传落 Metric.raw_sql） */
  raw_sql?: string | null;
  dependencies?: string[] | null;
  mount?: MetricMountInput | null;
}

/** SQL 批量创建请求（batch-register-from-sql 端点）。 */
export interface SqlBatchRegisterRequest {
  domain: string;
  candidates: SqlBatchRegisterCandidate[];
}

// ============================================================================
// SQL 智能推断评测（backend /metric-definitions/sql-infer-eval，解析成功率可视化）
// ============================================================================

/** 评测集单条用例结果。 */
export interface SqlInferEvalCase {
  case_id: string;
  dialect: string;
  exact: boolean;
  measure_precision: number | null;
  measure_recall: number | null;
  table_precision: number | null;
  table_recall: number | null;
  period_match: boolean | null;
  /** 完整实际解析结果（期望 vs 实际对照展示；历史记录可能缺失 → 可选）。 */
  pred_measures?: string[];
  pred_tables?: string[];
  /** 结构化实际度量（与 expected_measures_detail 对称，逐字段展示；历史记录可能缺失）。 */
  pred_measures_detail?: SqlInferEvalExpectedMeasure[];
  extra_measures: string[];
  missing_measures: string[];
  extra_tables: string[];
  missing_tables: string[];
  pred_period: string | null;
  expected_period: string | null;
}

/** 评测集成功率报告（实时计算，确定性）。 */
export interface SqlInferEvalReport {
  total: number;
  exact_count: number;
  exact_rate: number;
  measure_precision: number | null;
  measure_recall: number | null;
  table_precision: number | null;
  table_recall: number | null;
  period_match_rate: number | null;
  cases: SqlInferEvalCase[];
}

/** 评测运行历史记录（成功率趋势）。 */
export interface SqlInferEvalRunSummary {
  id: number;
  ran_at: string | null;
  total: number;
  exact_count: number;
  exact_rate: number;
  measure_precision: number | null;
  measure_recall: number | null;
  table_precision: number | null;
  table_recall: number | null;
  period_match_rate: number | null;
  elapsed_ms: number;
  actor_id: number | null;
}

/** 评测期望度量（结构化，CRUD 弹窗回填用）。 */
export interface SqlInferEvalExpectedMeasure {
  column: string;
  agg: string | null;
  alias?: string | null;
  table?: string | null;
}

/** 评测集样本（前端逐样本展示 SQL/期望画像/说明/来源标记）。 */
export interface SqlInferEvalSample {
  case_id: string;
  dialect: string;
  note: string;
  sql: string;
  expected_measures: string[];
  expected_measures_detail: SqlInferEvalExpectedMeasure[];
  expected_tables: string[];
  expected_period: string;
  /** builtin=内置基线（只读）；custom=自定义可管理 */
  source: "builtin" | "custom";
}

/** 自定义评测样本（CRUD 返回，含 DB 行信息）。 */
export interface EvalSample {
  id: number;
  case_id: string;
  dialect: string;
  sql: string;
  expected_measures: SqlInferEvalExpectedMeasure[];
  expected_tables: string[];
  expected_period: string;
  note: string;
  enabled: boolean;
  is_builtin: boolean;
  created_by: number | null;
}

/** 评测样本创建/更新请求。 */
export interface EvalSampleIn {
  case_id: string;
  dialect: string;
  sql: string;
  expected_period: string;
  expected_measures?: SqlInferEvalExpectedMeasure[];
  expected_tables?: string[];
  note?: string;
}

/** 评测样本即时解析预览结果（POST /sql-infer-eval/samples/preview）。 */
export interface EvalSamplePreview {
  measures: SqlInferEvalExpectedMeasure[];
  source_tables: string[];
  period: string | null;
}

/** 评测页数据（GET /sql-infer-eval）。 */
export interface SqlInferEvalData {
  report: SqlInferEvalReport;
  history: SqlInferEvalRunSummary[];
  latest_run: SqlInferEvalRunSummary | null;
  latest_run_cases: SqlInferEvalCase[];
  dataset: SqlInferEvalSample[];
}

/** 评测运行结果（POST /sql-infer-eval/run）。 */
export interface SqlInferEvalRunResult {
  report: SqlInferEvalReport;
  run_id: number;
}

// ============================================================================
// 全局聚合搜索（backend /api/v1/search，FR-18 全局搜索栏）
// ============================================================================

export type GlobalSearchType =
  | "metric"
  | "dimension"
  | "term"
  | "template"
  | "data_source"
  | "catalog"
  | "field"
  | "subject_domain"
  | "measure";

export interface GlobalSearchItem {
  type: GlobalSearchType;
  id: number;
  /** 资源编码：metric_code/dim_code/term_code/template.code/source_id/entity_name/列名/域 code */
  code: string;
  name: string;
  domain: string | null;
  status: string | null;
  /** 不同类型附带的上下文（源、表名、敏感度、实体类型、分级等） */
  source_id?: string | null;
  table_name?: string | null;
  entity_type?: string | null;
  sensitivity_level?: string | null;
  pii_flag?: boolean;
  source_type?: string | null;
  level?: number;
}

export interface GlobalSearchResponse {
  groups: Record<GlobalSearchType, GlobalSearchItem[]>;
  total: number;
}

