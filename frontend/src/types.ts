// 类型定义 — 对齐后端 Pydantic Schema（backend/app/services/semantic/schemas.py 等）
// 注意：所有响应均套用统一信封 { code, message, data, trace_id }，见 api.ts ApiEnvelope。

export interface ApiError {
  code: string;
  message: string;
  trace_id: string;
  detail?: Record<string, unknown> | null;
}

export type MetricType = "atomic" | "derived" | "composite";
export type MetricStatus = "DRAFT" | "EXPERIMENTAL" | "REVIEW" | "PUBLISHED" | "DEPRECATED";
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
  granularity: string;
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
  approver_id: number | null;
  submitted_by: number | null;
  /** 评审指派（TD §13）：提交评审时指定的评审用户/域评审组，审批页据此校验与展示 */
  reviewer_id?: number | null;
  reviewer_type?: "user" | "domain" | null;
  reviewer_domain?: string | null;
  pii_flag: boolean;
  compliance_reviewed: boolean;
  effective_version: number | null;
  consumption_guide: Record<string, unknown> | null;
  successor_code: string | null;
  deprecated_at: string | null;
  sunset_until: string | null;
  emergency_publish: boolean;
  emergency_reason: string | null;
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
  domain?: string | null;
  org_id?: number | null;
  password: string;
}

export interface UserUpdateRequest {
  display_name: string;
  email: string;
  role: string;
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
}

export interface MetricCreateRequest {
  metric_code?: string;
  name: string;
  domain: string;
  type: MetricType;
  granularity: string;
  unit: string;
  currency?: string | null;
  aggregation: "SUM" | "AVG" | "COUNT" | "COUNT_DISTINCT" | "LAST_VALUE";
  time_semantics: "PERIOD" | "YTD" | "TTM" | "AVG";
  freshness: "REALTIME" | "T1" | "HOURLY";
  dw_layer: "ODS" | "DWD" | "DWS" | "ADS" | "DM";
  metric_tier?: MetricTier;
  serving_mode?: "BATCH_ONLY" | "REALTIME_ONLY" | "BATCH_REALTIME_DUAL";
  additivity?: "ADDITIVE" | "SEMI_ADDITIVE" | "NON_ADDITIVE";
  non_additive_dimensions?: string[] | null;
  definition_json: Record<string, unknown>;
  pii_flag?: boolean;
  sla?: string | null;
}

export interface MetricUpdateRequest {
  name?: string;
  granularity?: string;
  unit?: string;
  definition_json?: Record<string, unknown>;
  sla?: string | null;
  consumption_guide?: Record<string, unknown>;
  backup_owner_id?: number | null;
  change_reason: string; // 必填，min_length=4
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

export interface MetricPublishRequest {
  version?: number;
  change_reason: string; // 必填，min_length=4
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

// ---- 批量治理（TD §13：提交/通过/打回/下线，逐条收集结果）----

/** 批量提交审核单条项（含评审指派） */
export interface MetricBatchSubmitItem {
  metric_code: string;
  change_reason: string;
  reviewer_id?: number | null;
  reviewer_type?: "user" | "domain" | null;
  reviewer_domain?: string | null;
}

/** 批量操作单条结果 */
export interface MetricBatchItemResult {
  metric_code: string;
  ok: boolean;
  message: string;
}

/** 批量操作响应 data 结构 */
export interface MetricBatchResult {
  results: MetricBatchItemResult[];
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
      metrics: { total: number; by_status: Record<string, number> };
      tables: number;
      sources: number;
      dimensions: number;
      terms: number;
      templates: number;
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
  is_active: boolean;
  owner_id: number | null;
  created_by: number;
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
}

// ============================================================================
// 消费服务（backend /api/v1/consume/*）
// ============================================================================

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

export interface Dimension {
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
// 术语表（backend /api/v1/terms/*）
// ============================================================================

export interface GlossaryTerm {
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
}

export interface NotifyEventLog {
  id: number;
  event_type: string;
  source: string | null;
  payload: Record<string, unknown> | null;
  level: string;
  notified: boolean;
  created_at: string;
}

export interface SubscriptionPref {
  id: number;
  user_id: number;
  channel: string;
  event_type: string;
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
  /** 采纳闭环状态：pending/in_progress/adopted/rejected */
  status: string;
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
  metric_id: number;
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
  schedule_cron: string | null;
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
  mode: string;
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
  coverage?: number | null;
  error?: string | null;
  /** 明细（详情接口返回）：failed_specs / drift_events / degrade_reason */
  detail?: {
    failed_specs?: Array<{ entity_name: string; error: string }>;
    drift_events?: Array<{ entity_name: string; change_type: string }>;
    degrade_reason?: string | null;
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

// PII 合规视图（GET /assetmap/pii）
export interface AssetPiiOverview {
  by_sensitivity: Record<string, number>;
  by_domain: Record<string, number>;
  pii_metric_count: number;
  pii_catalog_count: number;
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
}

export interface DictItemUpdateRequest {
  label?: string;
  sort_order?: number;
  description?: string | null;
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
  | "subject_domain";

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

