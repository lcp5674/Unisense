// 类型定义 — 对齐后端 Pydantic Schema（backend/app/services/semantic/schemas.py 等）
// 注意：所有响应均套用统一信封 { code, message, data, trace_id }，见 api.ts ApiEnvelope。

export interface ApiError {
  code: string;
  message: string;
  trace_id: string;
  detail?: Record<string, unknown> | null;
}

export type MetricType = "atomic" | "derived" | "composite";
export type MetricStatus = "DRAFT" | "EXPERIMENTAL" | "PUBLISHED" | "DEPRECATED";
export type MetricTier = "T1" | "T2" | "T3";

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
  pii_flag: boolean;
  compliance_reviewed: boolean;
  effective_version: number | null;
  consumption_guide: Record<string, unknown> | null;
  successor_code: string | null;
  deprecated_at: string | null;
  sunset_until: string | null;
  created_at: string;
  updated_at: string;
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
  metric_code: string;
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

export interface MetricPublishRequest {
  version?: number;
  change_reason: string; // 必填，min_length=4
}

// 冲突（backend/app/services/conflict/schemas.py）
export type ConflictStatus = "OPEN" | "ARBITRATED" | "RULED" | "CLOSED" | "ESCALATED";
export type ConflictType = "NAME_CONFLICT" | "SEMANTIC_DRIFT" | "PII_CONFLICT" | "DEFINITION_DIVERGENCE";

export interface ConflictResponse {
  conflict_id: string;
  type: ConflictType;
  status: ConflictStatus;
  severity: string;
  candidate_metric_code: string;
  existing_metric_code: string;
  description: string;
  detected_at: string;
  resolved_at: string | null;
  created_at: string;
}

export interface ConflictListResponse {
  items: ConflictResponse[];
  total: number;
  page: number;
  page_size: number;
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
}

// 变更影响预览（what-if）——受影响实体为节点 id 数组（与后端 service.impact_preview 一致）
export interface ImpactPreview {
  metric_code: string;
  change_type: string;
  affected_metrics: string[];
  affected_reports: string[];
  affected_consumers: string[];
  risk_level: "low" | "medium" | "high" | "critical";
}

// 收藏（backend/app/api/consume.py）：GET 返回 string[]，POST 返回 FavoriteResponse
export interface FavoriteResponse {
  metric_code: string;
  favorited: boolean;
}

// 当前用户（backend/app/api/auth.py UserInfo）
export interface CurrentUser {
  id: number;
  username: string;
  display_name: string;
  role: string;
  domain: string | null;
  org_id: number;
}

export const API_BASE = "/api/v1";
