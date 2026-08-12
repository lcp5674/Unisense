// Unisense 前端 API 客户端 — 对接真实后端（backend FastAPI，前缀 /api/v1）
// 统一信封：{ code, message, data, trace_id }。鉴权：Bearer token + X-Api-Key（Semantic API）。

import {
  ApiError,
  ConflictListResponse,
  ConflictResponse,
  CurrentUser,
  FavoriteResponse,
  ImpactPreview,
  LineageEdgePage,
  MetricCreateRequest,
  MetricListResponse,
  MetricPublishRequest,
  MetricResponse,
  MetricUpdateRequest,
  MetricVersionResponse,
  API_BASE,
} from "./types";

// ---- 运行配置 ----
// 后端地址：开发时由 Vite dev server 代理到 :8000（见 vite.config.ts），
// 生产构建可通过 VITE_API_BASE 覆盖。X-Api-Key 用于 Semantic API 网关鉴权。
const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, "") || "";
const SEMANTIC_API_KEY: string =
  (import.meta.env.VITE_SEMANTIC_API_KEY as string | undefined) || "dev-semantic-key";

const TOKEN_KEY = "unisense_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}
export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export class UnisenseApiError extends Error {
  code: string;
  traceId: string;
  detail?: Record<string, unknown> | null;
  status: number;
  constructor(message: string, code: string, status: number, traceId: string, detail?: Record<string, unknown> | null) {
    super(message);
    this.name = "UnisenseApiError";
    this.code = code;
    this.status = status;
    this.traceId = traceId;
    this.detail = detail;
  }
}

interface ApiEnvelope<T> {
  code: string;
  message: string;
  data: T;
  trace_id: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Api-Key": SEMANTIC_API_KEY,
    ...(init?.headers as Record<string, string> | undefined),
  };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers,
  });

  if (res.status === 401 || res.status === 403) {
    // 鉴权失效：清 token，交由上层跳登录
    clearToken();
  }

  // 尝试解析统一信封；非 2xx 抛出 UnisenseApiError
  let body: ApiEnvelope<T> | { code: string; message: string; trace_id: string; detail?: unknown } | null = null;
  try {
    body = (await res.json()) as ApiEnvelope<T>;
  } catch {
    if (!res.ok) {
      throw new UnisenseApiError(`请求失败 (HTTP ${res.status})`, "HTTP_ERROR", res.status, "");
    }
  }

  if (!res.ok) {
    const err = (body as { message?: string; code?: string; trace_id?: string; detail?: Record<string, unknown> }) || {};
    throw new UnisenseApiError(
      err.message || `请求失败 (HTTP ${res.status})`,
      err.code || "HTTP_ERROR",
      res.status,
      err.trace_id || "",
      err.detail as Record<string, unknown> | null | undefined,
    );
  }

  return (body as ApiEnvelope<T>).data;
}

// ---- 鉴权 ----
export async function apiLogin(username: string, password: string): Promise<string> {
  const data = await request<{ access_token: string; token_type: string }>(`${API_BASE}/auth/login`, {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  setToken(data.access_token);
  return data.access_token;
}

export async function fetchCurrentUser(): Promise<CurrentUser> {
  return request<CurrentUser>(`${API_BASE}/auth/me`);
}

// ---- 指标定义 ----
export async function listMetrics(params: {
  domain?: string;
  status?: string;
  metric_tier?: string;
  keyword?: string;
  page?: number;
  page_size?: number;
}): Promise<MetricListResponse> {
  const qs = new URLSearchParams();
  if (params.domain) qs.set("domain", params.domain);
  if (params.status) qs.set("status", params.status);
  if (params.metric_tier) qs.set("metric_tier", params.metric_tier);
  if (params.keyword) qs.set("keyword", params.keyword);
  qs.set("page", String(params.page ?? 1));
  qs.set("page_size", String(params.page_size ?? 20));
  return request<MetricListResponse>(`${API_BASE}/metric-definitions?${qs.toString()}`);
}

export async function getMetric(code: string): Promise<MetricResponse> {
  return request<MetricResponse>(`${API_BASE}/metric-definitions/${encodeURIComponent(code)}`);
}

export async function createMetric(req: MetricCreateRequest): Promise<MetricResponse> {
  return request<MetricResponse>(`${API_BASE}/metric-definitions`, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function updateMetric(code: string, req: MetricUpdateRequest): Promise<MetricResponse> {
  return request<MetricResponse>(`${API_BASE}/metric-definitions/${encodeURIComponent(code)}`, {
    method: "PUT",
    body: JSON.stringify(req),
  });
}

export async function publishMetric(code: string, req: MetricPublishRequest): Promise<MetricResponse> {
  return request<MetricResponse>(`${API_BASE}/metric-definitions/${encodeURIComponent(code)}/publish`, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function deprecateMetric(code: string, successor_code: string): Promise<MetricResponse> {
  const qs = new URLSearchParams({ successor_code });
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/deprecate?${qs.toString()}`,
    { method: "POST" },
  );
}

export async function listVersions(code: string): Promise<MetricVersionResponse[]> {
  return request<MetricVersionResponse[]>(`${API_BASE}/metric-definitions/${encodeURIComponent(code)}/versions`);
}

export async function piiReview(code: string): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/pii-review`,
    { method: "POST" },
  );
}

// ---- 冲突 ----
export async function listConflicts(params: {
  status?: string;
  page?: number;
  page_size?: number;
}): Promise<ConflictListResponse> {
  const qs = new URLSearchParams();
  if (params.status) qs.set("status", params.status);
  qs.set("page", String(params.page ?? 1));
  qs.set("page_size", String(params.page_size ?? 20));
  return request<ConflictListResponse>(`${API_BASE}/conflicts?${qs.toString()}`);
}

export async function arbitrateConflict(
  conflictId: string,
  decision: string,
  canonicalMetricCode: string,
): Promise<ConflictResponse> {
  return request<ConflictResponse>(`${API_BASE}/conflicts/${conflictId}/arbitrate`, {
    method: "POST",
    body: JSON.stringify({ decision, canonical_metric_code: canonicalMetricCode }),
  });
}

export async function escalateConflict(conflictId: string, note: string): Promise<ConflictResponse> {
  return request<ConflictResponse>(`${API_BASE}/conflicts/${conflictId}/escalate`, {
    method: "POST",
    body: JSON.stringify({ note }),
  });
}

// ---- 血缘 ----
export async function lineageImpact(params: {
  node: string;
  direction?: "upstream" | "downstream" | "both";
  max_hops?: number;
  page?: number;
  page_size?: number;
}): Promise<LineageEdgePage> {
  const qs = new URLSearchParams();
  qs.set("node", params.node);
  qs.set("direction", params.direction ?? "downstream");
  qs.set("max_hops", String(params.max_hops ?? 5));
  if (params.page) qs.set("page", String(params.page));
  if (params.page_size) qs.set("page_size", String(params.page_size));
  return request<LineageEdgePage>(`${API_BASE}/lineage/impact?${qs.toString()}`);
}

export async function lineageEdges(params: {
  node: string;
  direction?: "upstream" | "downstream" | "both";
  page?: number;
  page_size?: number;
}): Promise<LineageEdgePage> {
  const qs = new URLSearchParams();
  qs.set("node", params.node);
  qs.set("direction", params.direction ?? "both");
  if (params.page) qs.set("page", String(params.page));
  if (params.page_size) qs.set("page_size", String(params.page_size));
  return request<LineageEdgePage>(`${API_BASE}/lineage/edges?${qs.toString()}`);
}

export async function parseLineage(sql: string, dialect?: string): Promise<{ table_edges: number; field_edges: number; graph_written: boolean }> {
  return request(`${API_BASE}/lineage/parse`, {
    method: "POST",
    body: JSON.stringify({ sql, dialect: dialect ?? null, provenance: "sqlglot" }),
  });
}

// 变更影响预览（what-if）
export async function lineageImpactPreview(
  metricCode: string,
  changeType: string,
): Promise<ImpactPreview> {
  return request<ImpactPreview>(`${API_BASE}/lineage/impact-preview`, {
    method: "POST",
    body: JSON.stringify({ metric_code: metricCode, change_type: changeType }),
  });
}

// ---- 收藏（consume 服务）----
export async function listFavorites(): Promise<string[]> {
  return request<string[]>(`${API_BASE}/consume/me/favorites`);
}

export async function addFavorite(metricCode: string): Promise<FavoriteResponse> {
  return request<FavoriteResponse>(`${API_BASE}/consume/me/favorites`, {
    method: "POST",
    body: JSON.stringify({ metric_code: metricCode }),
  });
}

export async function removeFavorite(metricCode: string): Promise<FavoriteResponse> {
  return request<FavoriteResponse>(`${API_BASE}/consume/me/favorites/${encodeURIComponent(metricCode)}`, {
    method: "DELETE",
  });
}

// ---- 驾驶舱 ----
export async function fetchDashboard(): Promise<{
  total_metrics: number;
  published_count: number;
  draft_count: number;
  deprecated_count: number;
  conflict_count: number;
  review_pending_count: number;
  avg_review_hours: number;
  pii_metric_count: number;
  quality_anomaly_count: number;
  top_domains: Array<{ domain: string; count: number }>;
}> {
  return request(`${API_BASE}/semantic/dashboard`);
}

// ---- 消费指南 ----
export async function fetchConsumptionGuide(metricCode: string): Promise<{
  metric_code: string;
  definition: string;
  calculation_logic: string;
  dimensions: Array<{ name: string; description: string; type: string }>;
  usage_examples: Array<{ title: string; sql: string; description: string }>;
  related_metrics: string[];
  faq: Array<{ question: string; answer: string }>;
}> {
  return request(
    `${API_BASE}/semantic/metrics/${encodeURIComponent(metricCode)}/consumption-guide`,
  );
}

// ---- QuickBI Ticket ----
export async function fetchQuickBITicket(params: {
  reportId: string;
  dashboardId?: string;
  params?: Record<string, string>;
}): Promise<{ ticket: string; embed_url: string }> {
  return request(`${API_BASE}/semantic/quickbi/ticket`, {
    method: "POST",
    body: JSON.stringify(params),
  });
}

// ---- 埋点事件 ----
export async function trackEvent(event: {
  event_type: string;
  target_id?: string;
  target_type?: string;
  context?: Record<string, unknown>;
}): Promise<{ event_id: string }> {
  return request(`${API_BASE}/tracking/event`, {
    method: "POST",
    body: JSON.stringify(event),
  });
}

// ---- 资产地图 ----
export async function fetchAssetGraph(params?: {
  domain?: string;
  depth?: number;
  pii_only?: boolean;
}): Promise<{
  nodes: Array<{
    id: string;
    type: string;
    label: string;
    pii?: boolean;
    domain?: string;
    owner?: string;
  }>;
  edges: Array<{ source: string; target: string; type: string }>;
}> {
  const qs = new URLSearchParams();
  if (params?.domain) qs.set("domain", params.domain);
  if (params?.depth) qs.set("depth", String(params.depth));
  if (params?.pii_only) qs.set("pii_only", String(params.pii_only));
  return request(`${API_BASE}/assetmap/graph?${qs.toString()}`);
}

export async function fetchAssetHeatmap(dimension?: string): Promise<{
  buckets: Array<{
    domain: string;
    pii_count: number;
    total: number;
    pii_ratio: number;
  }>;
}> {
  const qs = new URLSearchParams();
  if (dimension) qs.set("dimension", dimension);
  return request(`${API_BASE}/assetmap/heatmap?${qs.toString()}`);
}

export async function fetchAssetOwnerView(ownerId?: number): Promise<{
  owners: Array<{
    owner_id: number;
    owner_name: string;
    metric_count: number;
    pii_count: number;
  }>;
}> {
  const qs = new URLSearchParams();
  if (ownerId) qs.set("owner_id", String(ownerId));
  return request(`${API_BASE}/assetmap/owner-view?${qs.toString()}`);
}

export type { ApiError };
