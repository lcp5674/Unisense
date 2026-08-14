// Unisense 前端 API 客户端 — 对接真实后端（backend FastAPI，前缀 /api/v1）
// 统一信封：{ code, message, data, trace_id }。鉴权：Bearer token + X-Api-Key（Semantic/Consume API）。
// 注意：语义服务后端路由为 /semantics（复数），前端统一调用此处封装。

import {
  ApiError,
  AssetCatalogSummary,
  AssetChanges,
  AssetClassificationSummary,
  AssetEntityDetail,
  AssetHealthSummary,
  AssetMetricSummary,
  AssetMyAssets,
  AssetOwnerView,
  AssetPiiOverview,
  AssetSearchItem,
  AssetTableItem,
  AuditEntry,
  AutoSuggestRequest,
  AutoSuggestResponse,
  ClientCreateRequest,
  ClientCreatedResponse,
  ConflictCheckRequest,
  ConflictCheckResult,
  ClientResponse,
  CollectNowResult,
  CollectResult,
  CollectionProgress,
  ConflictListResponse,
  ConflictResponse,
  ConsumptionGuideResponse,
  CurrentUser,
  DashboardData,
  DataSource,
  DataSourceCreateRequest,
  DataSourceListResponse,
  DataSourceUpdateRequest,
  DBCatalog,
  DictItemCreateRequest,
  DictItemUpdateRequest,
  Dimension,
  DimensionMapping,
  DimensionMember,
  DimensionExpr,
  DryRunResponse,
  ErasureResult,
  FavoriteResponse,
  Feedback,
  GlossaryConflict,
  GlossaryTerm,
  GlobalSearchResponse,
  GrantBatchResult,
  GrantCreate,
  GrantResponse,
  ImpactPreview,
  LineageChannel,
  LineageEdgePage,
  LineageIngestRun,
  ListDatabasesResult,
  MetricCreateRequest,
  MetricListResponse,
  MetricCompareResult,
  MetricHealth,
  MetricPublishRequest,
  MetricResponse,
  MetricTemplate,
  MetricUpdateRequest,
  MetricVersionResponse,
  NL2SQLResult,
  LlmConfig,
  LlmConfigTestResult,
  Notification,
  NotifyEventLog,
  ObsMetricsNotifications,
  ObsMetricsQuality,
  PermissionCheckResult,
  PermissionSnapshot,
  PiiReviewResult,
  QualityBenchmark,
  QualityEvent,
  QualityObservation,
  QualityRule,
  QualityRuleCreate,
  QueryRequest,
  QueryResponse,
  RecommendItem,
  Reconciliation,
  ReconciliationRecord,
  RoleResponse,
  ScheduleResult,
  SnapshotResponse,
  CollectionJob,
  SourceHealth,
  SourceType,
  SourceTypeInfo,
  StaleEdge,
  SubscriptionPref,
  SubjectDomain,
  SubjectDomainCreateRequest,
  SubjectDomainTreeNode,
  SubjectDomainUpdateRequest,
  SystemDictItem,
  TestConnectionResult,
  UserBrief,
  UserPreferenceItem,
  UserPreferenceList,
  Watermark,
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

// 消费服务客户端访问令牌（role=consume 的 JWT），由 /consume/api-clients/{id}/token 签发
const CONSUME_TOKEN_KEY = "unisense_consume_token";
export function getConsumeToken(): string | null {
  return localStorage.getItem(CONSUME_TOKEN_KEY);
}
export function setConsumeToken(token: string): void {
  localStorage.setItem(CONSUME_TOKEN_KEY, token);
}
export function clearConsumeToken(): void {
  localStorage.removeItem(CONSUME_TOKEN_KEY);
}

// 后端 error_code → 中文可读描述（供全站错误提示展示，避免英文技术码直出给业务用户）
const ERROR_CODE_ZH: Record<string, string> = {
  AUTH_TOKEN_MISSING: "未登录或登录状态缺失",
  AUTH_TOKEN_EXPIRED: "登录已过期，请重新登录",
  AUTH_TOKEN_INVALID: "登录状态无效",
  AUTH_INVALID_CREDENTIALS: "用户名或密码错误",
  AUTH_APIKEY_MISSING: "缺少访问密钥（X-Api-Key）",
  AUTH_APIKEY_INVALID: "访问密钥无效或已吊销",
  FORBIDDEN: "您无权执行该操作",
  FORBIDDEN_DOMAIN: "您无权访问该数据域",
  FORBIDDEN_METRIC: "您无权访问该指标",
  FORBIDDEN_DIMENSION: "您无权访问该维度",
  FORBIDDEN_PII: "无权访问含个人信息的数据",
  FORBIDDEN_DEPRECATED: "该指标已废弃，无法操作",
  RATE_LIMITED: "请求过于频繁，请稍后再试",
  DEPENDENCY_DEGRADED_ENGINE: "查询引擎暂不可用，请稍后再试",
  INJECTION_DETECTED: "检测到非法输入，已拦截",
  UNSAFE_QUERY: "查询包含危险语句，已拒绝",
  SELF_REVIEW_BLOCKED: "不能审核自己提交的指标",
  INVALID_TRANSITION: "当前状态不允许该操作",
  NOT_FOUND: "资源不存在或已被删除",
  VALIDATION_ERROR: "输入校验未通过",
  INTERNAL_ERROR: "系统内部错误，请稍后重试",
  DEPENDENCY_UNPUBLISHED: "依赖指标尚未发布",
  CONFLICT: "存在冲突，需协商或裁决后继续",
  PII_REVIEW_REQUIRED: "该指标含个人信息，需先完成合规复核",
  GRANULARITY_VIOLATION: "查询粒度与指标定义不符",
  CIRCULAR_DEPENDENCY: "检测到循环依赖，已拒绝",
};

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

  /** 错误码的中文可读描述；未收录时回退为原始错误码。 */
  get codeZh(): string {
    return ERROR_CODE_ZH[this.code] ?? this.code;
  }
}

interface ApiEnvelope<T> {
  code: string;
  message: string;
  data: T;
  trace_id: string;
}

interface RequestOptions extends RequestInit {
  /** 消费服务调用：使用 API 客户端令牌而非用户 JWT */
  consumeAuth?: boolean;
  /** consumeAuth 模式下，无消费令牌时回落登录用户 JWT（仅快照等双通道只读端点） */
  consumeFallbackUser?: boolean;
}

async function request<T>(path: string, init?: RequestOptions): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Api-Key": SEMANTIC_API_KEY,
    ...(init?.headers as Record<string, string> | undefined),
  };
  if (init?.consumeAuth) {
    const consumeToken = getConsumeToken();
    let bearer: string | null = consumeToken;
    if (!bearer && init?.consumeFallbackUser) bearer = getToken();
    if (bearer) headers["Authorization"] = `Bearer ${bearer}`;
  } else {
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }

  const { consumeAuth: _consumeAuth, consumeFallbackUser: _consumeFallbackUser, ...restInit } = init ?? {};

  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...restInit,
    headers,
  });

  if (res.status === 401 || res.status === 403) {
    // 鉴权失效：清 token，交由上层跳登录（消费令牌失效仅清除消费令牌）
    if (init?.consumeAuth) {
      clearConsumeToken();
    } else {
      clearToken();
    }
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

function pageQs(params: Record<string, string | number | undefined>): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }
  return qs.toString();
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

// ---- 用户偏好（按用户持久化，如侧边栏折叠态）----
export async function fetchPreferences(): Promise<Record<string, unknown>> {
  const data = await request<UserPreferenceList>(`${API_BASE}/me/preferences`);
  const map: Record<string, unknown> = {};
  for (const item of data.items) map[item.key] = item.value;
  return map;
}

export async function setPreference(key: string, value: unknown): Promise<void> {
  await request<UserPreferenceItem>(`${API_BASE}/me/preferences/${encodeURIComponent(key)}`, {
    method: "PUT",
    body: JSON.stringify({ value }),
  });
}

export async function deletePreference(key: string): Promise<void> {
  await request<UserPreferenceItem>(`${API_BASE}/me/preferences/${encodeURIComponent(key)}`, {
    method: "DELETE",
  });
}

// ---- 指标定义 ----
export async function listMetrics(params: {
  domain?: string;
  status?: string;
  metric_tier?: string;
  keyword?: string;
  sort_by?: "updated_at" | "created_at" | "version" | "metric_code" | "name";
  sort_order?: "asc" | "desc";
  page?: number;
  page_size?: number;
}): Promise<MetricListResponse> {
  const qs = pageQs({
    domain: params.domain,
    status: params.status,
    metric_tier: params.metric_tier,
    keyword: params.keyword,
    sort_by: params.sort_by,
    sort_order: params.sort_order,
    page: params.page ?? 1,
    page_size: params.page_size ?? 20,
  });
  return request<MetricListResponse>(`${API_BASE}/metric-definitions?${qs}`);
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
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/deprecate`,
    {
      method: "POST",
      body: JSON.stringify({ successor_code }),
    },
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

// 提交评审：DRAFT → REVIEW；change_reason 缺省"提交评审"（后端 /submit，对齐 FR-003）
export async function submitReview(metricCode: string, changeReason = "提交评审"): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(metricCode)}/submit`,
    {
      method: "POST",
      body: JSON.stringify({ change_reason: changeReason }),
    },
  );
}

// 审核通过：REVIEW → PUBLISHED/EXPERIMENTAL（后端 /approve，对齐 FR-004）
// 实现见下方统一版本（mode=standard 全量 / experimental 灰度，兼容 gray_tenant_ids/target_version）

// 审核驳回：REVIEW → DRAFT（后端 /reject，对齐 FR-005）
export async function rejectMetric(metricCode: string, reason: string): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(metricCode)}/reject`,
    {
      method: "POST",
      body: JSON.stringify({ reason }),
    },
  );
}

// 兼容旧调用：reviewMetric(approved=true) → approveMetric；false → rejectMetric
export async function reviewMetric(
  metricCode: string,
  approved: boolean,
  changeReason: string,
): Promise<MetricResponse> {
  if (approved) {
    return approveMetric(metricCode, { mode: "standard" });
  }
  return rejectMetric(metricCode, changeReason || "审核不通过，请修改后重新提交");
}

// ---- 指标可信度：健康度/对比/灰度/紧急发布/版本确认（backend /metric-definitions）----

export async function getMetricHealth(code: string): Promise<MetricHealth> {
  return request<MetricHealth>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/health`,
  );
}

// 审核通过：mode=standard 全量发布 / experimental 灰度发布（灰度可带 gray_tenant_ids）
export async function approveMetric(
  code: string,
  opts: { mode?: "standard" | "experimental"; gray_tenant_ids?: number[]; target_version?: number } = {},
): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/approve`,
    {
      method: "POST",
      body: JSON.stringify({
        mode: opts.mode ?? "standard",
        gray_tenant_ids: opts.gray_tenant_ids ?? null,
        target_version: opts.target_version ?? null,
      }),
    },
  );
}

export async function compareMetrics(
  codeA: string,
  codeB: string,
): Promise<MetricCompareResult> {
  return request<MetricCompareResult>(`${API_BASE}/metric-definitions/compare`, {
    method: "POST",
    body: JSON.stringify({ metric_codes: [codeA, codeB] }),
  });
}

export async function emergencyPublishMetric(
  code: string,
  reason: string,
  targetVersion?: number,
): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/emergency-publish`,
    {
      method: "POST",
      body: JSON.stringify({ reason, target_version: targetVersion ?? null }),
    },
  );
}

export async function promoteMetric(code: string): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/promote`,
    { method: "POST" },
  );
}

export async function rollbackMetric(code: string): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/rollback`,
    { method: "POST" },
  );
}

// 版本确认闭环：消费方确认/拒绝、Owner 延期（PENDING_VERSION 状态版本）
export async function confirmMetricVersion(code: string, version: number): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/confirm-version`,
    {
      method: "POST",
      body: JSON.stringify({ version }),
    },
  );
}

export async function rejectMetricVersion(
  code: string,
  version: number,
  reason: string,
): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/reject-version`,
    {
      method: "POST",
      body: JSON.stringify({ version, reason }),
    },
  );
}

export async function extendMetricVersion(code: string, version: number): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/extend-version`,
    {
      method: "POST",
      body: JSON.stringify({ version }),
    },
  );
}

// ---- 用户（backend /auth/users，Owner 责任链渲染）----
export async function listUsers(role?: string): Promise<UserBrief[]> {
  const qs = role ? `?role=${encodeURIComponent(role)}` : "";
  return request<UserBrief[]>(`${API_BASE}/auth/users${qs}`);
}

// ---- 冲突 ----
export async function listConflicts(params: {
  status?: string;
  page?: number;
  page_size?: number;
}): Promise<ConflictListResponse> {
  const qs = pageQs({
    status: params.status,
    page: params.page ?? 1,
    page_size: params.page_size ?? 20,
  });
  return request<ConflictListResponse>(`${API_BASE}/conflicts?${qs}`);
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

// 主动冲突检测：创建指标前的口径冲突扫描（POST /conflicts/check）
export async function checkConflict(req: ConflictCheckRequest): Promise<ConflictCheckResult> {
  return request<ConflictCheckResult>(`${API_BASE}/conflicts/check`, {
    method: "POST",
    body: JSON.stringify(req),
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
  const qs = pageQs({
    node: params.node,
    direction: params.direction ?? "downstream",
    max_hops: params.max_hops ?? 5,
    page: params.page,
    page_size: params.page_size,
  });
  return request<LineageEdgePage>(`${API_BASE}/lineage/impact?${qs}`);
}

export async function lineageEdges(params: {
  node: string;
  direction?: "upstream" | "downstream" | "both";
  page?: number;
  page_size?: number;
}): Promise<LineageEdgePage> {
  const qs = pageQs({
    node: params.node,
    direction: params.direction ?? "both",
    page: params.page,
    page_size: params.page_size,
  });
  return request<LineageEdgePage>(`${API_BASE}/lineage/edges?${qs}`);
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

// ---- 血缘采集通道（增量采集运维）----
export async function lineageChannels(): Promise<LineageChannel[]> {
  return request<LineageChannel[]>(`${API_BASE}/lineage/channels`);
}

export async function lineageChannelRuns(source: string, limit = 20): Promise<LineageIngestRun[]> {
  return request<LineageIngestRun[]>(
    `${API_BASE}/lineage/channels/${encodeURIComponent(source)}/runs?limit=${limit}`,
  );
}

export async function lineageStale(source?: string, limit = 200): Promise<StaleEdge[]> {
  const qs = source ? `?source=${encodeURIComponent(source)}&limit=${limit}` : `?limit=${limit}`;
  return request<StaleEdge[]>(`${API_BASE}/lineage/stale${qs}`);
}

export async function confirmStaleEdge(edgeId: number): Promise<StaleEdge> {
  return request<StaleEdge>(`${API_BASE}/lineage/stale/${edgeId}/confirm`, { method: "POST" });
}

export async function restoreStaleEdge(edgeId: number): Promise<StaleEdge> {
  return request<StaleEdge>(`${API_BASE}/lineage/stale/${edgeId}/restore`, { method: "POST" });
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

// ---- 语义服务（后端为 /semantics，复数）----

// 消费者仪表盘
export async function fetchDashboard(domain?: string): Promise<DashboardData> {
  const qs = domain ? `?domain=${encodeURIComponent(domain)}` : "";
  return request<DashboardData>(`${API_BASE}/semantics/dashboard${qs}`);
}

// 指标模板
export async function listTemplates(params?: {
  domain?: string;
  is_active?: boolean;
  keyword?: string;
}): Promise<MetricTemplate[]> {
  const qs = pageQs({
    domain: params?.domain,
    is_active: params?.is_active === undefined ? undefined : params.is_active ? "true" : "false",
    keyword: params?.keyword,
  });
  return request<MetricTemplate[]>(`${API_BASE}/semantics/templates?${qs}`);
}

export async function getTemplate(templateId: number): Promise<MetricTemplate> {
  return request<MetricTemplate>(`${API_BASE}/semantics/templates/${templateId}`);
}

// 消费指南：后端按 metric_code 查询（对齐 semantic.py /consumption-guide/{metric_code}）
export async function fetchConsumptionGuide(metricCode: string): Promise<ConsumptionGuideResponse> {
  return request<ConsumptionGuideResponse>(
    `${API_BASE}/semantics/consumption-guide/${encodeURIComponent(metricCode)}`,
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

// ---- 消费服务 ----
export async function consumeDryRun(req: QueryRequest): Promise<DryRunResponse> {
  return request<DryRunResponse>(`${API_BASE}/consume/query/dry-run`, {
    method: "POST",
    body: JSON.stringify(req),
    consumeAuth: true,
  });
}

export async function consumeQuery(req: QueryRequest): Promise<QueryResponse> {
  return request<QueryResponse>(`${API_BASE}/consume/query`, {
    method: "POST",
    body: JSON.stringify(req),
    consumeAuth: true,
  });
}

export async function listSnapshots(
  code: string,
  limit = 50,
): Promise<SnapshotResponse[]> {
  return request<SnapshotResponse[]>(
    `${API_BASE}/consume/metrics/${encodeURIComponent(code)}/snapshots?limit=${limit}`,
    { consumeAuth: true, consumeFallbackUser: true },
  );
}

export async function createApiClient(req: ClientCreateRequest): Promise<ClientCreatedResponse> {
  return request<ClientCreatedResponse>(`${API_BASE}/consume/api-clients`, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function listApiClients(domain?: string): Promise<ClientResponse[]> {
  const qs = domain ? `?domain=${encodeURIComponent(domain)}` : "";
  return request<ClientResponse[]>(`${API_BASE}/consume/api-clients${qs}`);
}

export async function mintClientToken(clientId: string): Promise<{ access_token: string }> {
  return request<{ access_token: string }>(
    `${API_BASE}/consume/api-clients/${encodeURIComponent(clientId)}/token`,
    { method: "POST" },
  );
}

export async function confirmVersion(versionId: number): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`${API_BASE}/consume/versions/${versionId}/confirm`, {
    method: "POST",
  });
}

export async function rejectVersion(versionId: number, reason?: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`${API_BASE}/consume/versions/${versionId}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason: reason ?? null }),
  });
}

// ---- 维度 ----
export async function listDimensions(params?: {
  domain?: string;
  status?: string;
  keyword?: string;
}): Promise<{ items: Dimension[]; total: number }> {
  const qs = pageQs({ domain: params?.domain, status: params?.status, keyword: params?.keyword });
  return request(`${API_BASE}/dimensions?${qs}`);
}

export async function createDimension(body: {
  dim_code?: string;
  name: string;
  domain: string;
  type?: string;
  description?: string | null;
  owner_id?: number;
}): Promise<Dimension> {
  return request<Dimension>(`${API_BASE}/dimensions`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function publishDimension(dimCode: string): Promise<Dimension> {
  return request<Dimension>(`${API_BASE}/dimensions/${encodeURIComponent(dimCode)}/publish`, {
    method: "POST",
  });
}

export async function deprecateDimension(dimCode: string): Promise<Dimension> {
  return request<Dimension>(`${API_BASE}/dimensions/${encodeURIComponent(dimCode)}/deprecate`, {
    method: "POST",
  });
}

export async function listDimensionMappings(sourceDimCode?: string): Promise<{ items: DimensionMapping[]; total: number }> {
  const qs = sourceDimCode ? `?source_dim_code=${encodeURIComponent(sourceDimCode)}` : "";
  return request(`${API_BASE}/dimensions/mappings${qs}`);
}

export async function createDimensionMapping(body: {
  source_dim_code: string;
  target_dim_code: string;
  mapping_type: string;
  expression?: string | null;
}): Promise<DimensionMapping> {
  return request<DimensionMapping>(`${API_BASE}/dimensions/mappings`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function listReconciliations(status?: string): Promise<{ items: Reconciliation[]; total: number }> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  return request(`${API_BASE}/dimensions/reconciliations${qs}`);
}

export async function submitReconciliation(body: {
  metric_id: number;
  dim_code?: string | null;
  expected_expr: string;
  actual_expr: string;
  diff_summary?: string | null;
}): Promise<Reconciliation> {
  return request<Reconciliation>(`${API_BASE}/dimensions/reconciliations`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function reviewReconciliation(recId: number, decision: string): Promise<Reconciliation> {
  return request<Reconciliation>(`${API_BASE}/dimensions/reconciliations/${recId}/review`, {
    method: "POST",
    body: JSON.stringify({ decision }),
  });
}

export async function listDimensionMembers(dimCode: string): Promise<{ items: DimensionMember[]; total: number }> {
  return request(`${API_BASE}/dimensions/${encodeURIComponent(dimCode)}/members`);
}

export async function createDimensionMember(body: {
  dim_code: string;
  member_code?: string;
  member_name: string;
  parent_code?: string | null;
  path?: string | null;
  attributes?: Record<string, unknown> | null;
}): Promise<DimensionMember> {
  return request<DimensionMember>(`${API_BASE}/dimensions/${encodeURIComponent(body.dim_code)}/members`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// ---- 术语表 ----
export async function listTerms(params?: {
  domain?: string;
  status?: string;
  search?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: GlossaryTerm[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    domain: params?.domain,
    status: params?.status,
    search: params?.search,
    page: params?.page ?? 1,
    page_size: params?.page_size ?? 20,
  });
  return request(`${API_BASE}/terms?${qs}`);
}

export async function createTerm(body: {
  term_code?: string;
  name: string;
  definition: string;
  domain: string;
  synonyms?: string[];
  boundary?: string | null;
  owner_id?: number;
}): Promise<GlossaryTerm> {
  return request<GlossaryTerm>(`${API_BASE}/terms`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function submitTerm(termCode: string): Promise<GlossaryTerm> {
  return request<GlossaryTerm>(`${API_BASE}/terms/${encodeURIComponent(termCode)}/submit`, {
    method: "POST",
  });
}

export async function deprecateTerm(termCode: string): Promise<GlossaryTerm> {
  return request<GlossaryTerm>(`${API_BASE}/terms/${encodeURIComponent(termCode)}/deprecate`, {
    method: "POST",
  });
}

export async function listTermConflicts(status?: string): Promise<{ items: GlossaryConflict[]; total: number }> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  return request(`${API_BASE}/terms/conflicts${qs}`);
}

export async function resolveTermConflict(conflictId: number, decision: string): Promise<GlossaryConflict> {
  return request<GlossaryConflict>(`${API_BASE}/terms/conflicts/${conflictId}/resolve`, {
    method: "POST",
    body: JSON.stringify({ decision }),
  });
}

// ---- 治理 ----
export async function createRole(body: { name: string; description?: string | null }): Promise<RoleResponse> {
  return request<RoleResponse>(`${API_BASE}/roles`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function listGrants(params?: {
  user_id?: number;
  domain?: string;
  status?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: GrantResponse[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    user_id: params?.user_id,
    domain: params?.domain,
    status: params?.status,
    page: params?.page ?? 1,
    page_size: params?.page_size ?? 20,
  });
  return request(`${API_BASE}/grants?${qs}`);
}

export async function createGrant(req: GrantCreate): Promise<GrantResponse> {
  return request<GrantResponse>(`${API_BASE}/grants`, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function revokeGrant(grantId: number, reason?: string): Promise<GrantResponse> {
  const qs = reason ? `?reason=${encodeURIComponent(reason)}` : "";
  return request<GrantResponse>(`${API_BASE}/grants/${grantId}${qs}`, { method: "DELETE" });
}

export async function batchGrant(
  items: GrantCreate[],
  operation: "grant" | "revoke" = "grant",
  dryRun = false,
): Promise<GrantBatchResult> {
  const path = dryRun ? `${API_BASE}/grants/batch/dry-run` : `${API_BASE}/grants/batch`;
  return request<GrantBatchResult>(path, {
    method: "POST",
    body: JSON.stringify({ operation, items }),
  });
}

export async function fetchMyPermissions(): Promise<PermissionSnapshot> {
  return request<PermissionSnapshot>(`${API_BASE}/me/permissions`);
}

export async function checkPermission(body: {
  user_id: number;
  action: string;
  domain?: string | null;
  metric_code?: string | null;
}): Promise<PermissionCheckResult> {
  return request<PermissionCheckResult>(`${API_BASE}/permissions/check`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function piiReviewAction(body: {
  metric_code: string;
  decision: "APPROVE" | "REJECT";
  sensitivity_level?: string;
  pii_columns?: string[] | null;
  masking_policy?: string | null;
  comment: string;
}): Promise<PiiReviewResult> {
  return request<PiiReviewResult>(`${API_BASE}/pii/review`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function classificationRescan(body: {
  source_id?: string | null;
  catalog_ids?: number[] | null;
  limit?: number;
}): Promise<unknown> {
  return request(`${API_BASE}/classification/rescan`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function requestErasure(body: {
  subject_user_id: number;
  reason?: string | null;
}): Promise<ErasureResult> {
  return request<ErasureResult>(`${API_BASE}/erasure`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// ---- 质量 ----
export async function listQualityRules(params?: {
  metric_id?: number;
  rule_type?: string;
  severity?: string;
  enabled?: boolean;
  page?: number;
  page_size?: number;
}): Promise<{ items: QualityRule[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    metric_id: params?.metric_id,
    rule_type: params?.rule_type,
    severity: params?.severity,
    enabled: params?.enabled === undefined ? undefined : params.enabled ? "true" : "false",
    page: params?.page ?? 1,
    page_size: params?.page_size ?? 20,
  });
  return request(`${API_BASE}/quality/rules?${qs}`);
}

export async function createQualityRule(req: QualityRuleCreate): Promise<QualityRule> {
  return request<QualityRule>(`${API_BASE}/quality/rules`, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function updateQualityRule(
  ruleId: number,
  body: {
    threshold?: Record<string, unknown> | null;
    rule_mode?: string | null;
    severity?: string | null;
    enabled?: boolean | null;
    notify_targets?: Record<string, unknown> | null;
  },
): Promise<QualityRule> {
  return request<QualityRule>(`${API_BASE}/quality/rules/${ruleId}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export async function deleteQualityRule(ruleId: number): Promise<{ deleted: number }> {
  return request<{ deleted: number }>(`${API_BASE}/quality/rules/${ruleId}`, {
    method: "DELETE",
  });
}

export async function listQualityEvents(params?: {
  metric_id?: number;
  status?: string;
  level?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: QualityEvent[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    metric_id: params?.metric_id,
    status: params?.status,
    level: params?.level,
    page: params?.page ?? 1,
    page_size: params?.page_size ?? 20,
  });
  return request(`${API_BASE}/quality/events?${qs}`);
}

export async function qualityEventAck(eventId: number, note = ""): Promise<QualityEvent> {
  return request<QualityEvent>(`${API_BASE}/quality/events/${eventId}/ack`, {
    method: "POST",
    body: JSON.stringify({ note }),
  });
}

export async function qualityEventResolve(eventId: number): Promise<QualityEvent> {
  return request<QualityEvent>(`${API_BASE}/quality/events/${eventId}/resolve`, { method: "POST" });
}

export async function qualityEventClose(eventId: number): Promise<QualityEvent> {
  return request<QualityEvent>(`${API_BASE}/quality/events/${eventId}/close`, { method: "POST" });
}

export async function submitQualityObservation(body: {
  metric_id: number;
  metric_code: string;
  value: number;
  obs_time: string;
  source_id?: string | null;
  dims?: Record<string, unknown> | null;
}): Promise<QualityObservation> {
  return request<QualityObservation>(`${API_BASE}/quality/observe`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function importBenchmark(body: {
  source_id: string;
  metric_code: string;
  bench_date: string;
  dims?: Record<string, unknown> | null;
  bench_value: number;
  provider: string;
  tolerance_pct?: number | null;
}): Promise<QualityBenchmark> {
  return request<QualityBenchmark>(`${API_BASE}/quality/benchmarks/import`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function listBenchmarks(params?: {
  metric_code?: string;
  source_id?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: QualityBenchmark[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    metric_code: params?.metric_code,
    source_id: params?.source_id,
    page: params?.page ?? 1,
    page_size: params?.page_size ?? 20,
  });
  return request(`${API_BASE}/quality/benchmarks?${qs}`);
}

export async function runReconciliation(body: {
  benchmark_id: number;
  metric_value: number;
  window?: string | null;
}): Promise<ReconciliationRecord> {
  return request<ReconciliationRecord>(`${API_BASE}/quality/reconciliation/run`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function listReconciliationRecords(params?: {
  status?: string;
  metric_code?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: ReconciliationRecord[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    status: params?.status,
    metric_code: params?.metric_code,
    page: params?.page ?? 1,
    page_size: params?.page_size ?? 20,
  });
  return request(`${API_BASE}/quality/reconciliation-records?${qs}`);
}

export async function confirmReconciliation(
  recordId: number,
  decision: string,
  ownerNote?: string | null,
): Promise<ReconciliationRecord> {
  return request<ReconciliationRecord>(`${API_BASE}/quality/reconciliation-records/${recordId}/confirm`, {
    method: "POST",
    body: JSON.stringify({ decision, owner_note: ownerNote ?? null }),
  });
}

// ---- 通知 ----
export async function listNotifications(status?: string): Promise<{ items: Notification[]; total: number }> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  return request(`${API_BASE}/notify/notifications${qs}`);
}

export async function listNotifyEvents(eventType?: string): Promise<{ items: NotifyEventLog[]; total: number }> {
  const qs = eventType ? `?event_type=${encodeURIComponent(eventType)}` : "";
  return request(`${API_BASE}/notify/events${qs}`);
}

export async function publishNotifyEvent(body: {
  event_type: string;
  source?: string | null;
  payload?: Record<string, unknown> | null;
  level?: string;
}): Promise<{ event_id: number; notifications: number; delivered: number }> {
  return request(`${API_BASE}/notify/events`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function listSubscriptions(): Promise<{ items: SubscriptionPref[]; total: number }> {
  return request(`${API_BASE}/notify/subscriptions`);
}

export async function upsertSubscription(body: {
  channel: string;
  event_type: string;
  enabled?: boolean;
  threshold?: number | null;
}): Promise<SubscriptionPref> {
  return request<SubscriptionPref>(`${API_BASE}/notify/subscriptions`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

// ---- 可观测 ----
export async function listFeedback(targetType?: string): Promise<{ items: Feedback[]; total: number }> {
  const qs = targetType ? `?target_type=${encodeURIComponent(targetType)}` : "";
  return request(`${API_BASE}/observability/feedback${qs}`);
}

export async function submitFeedback(body: {
  target_type: string;
  target_id?: string | null;
  rating?: number | null;
  comment?: string | null;
}): Promise<Feedback> {
  return request<Feedback>(`${API_BASE}/observability/feedback`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateFeedbackStatus(
  feedbackId: number,
  status: string,
  resolutionNote?: string | null,
): Promise<Feedback> {
  return request<Feedback>(`${API_BASE}/observability/feedback/${feedbackId}/status`, {
    method: "PATCH",
    body: JSON.stringify({ status, resolution_note: resolutionNote ?? null }),
  });
}

export async function submitNps(body: {
  score: number;
  comment?: string | null;
  target_type?: string;
  target_id?: string | null;
}): Promise<Feedback> {
  return request<Feedback>(`${API_BASE}/observability/nps`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function fetchObsMetricsQuality(): Promise<ObsMetricsQuality> {
  return request<ObsMetricsQuality>(`${API_BASE}/observability/metrics/quality`);
}

export async function fetchObsMetricsApi(): Promise<Record<string, number>> {
  return request<Record<string, number>>(`${API_BASE}/observability/metrics/api`);
}

export async function fetchObsMetricsNotifications(): Promise<ObsMetricsNotifications> {
  return request<ObsMetricsNotifications>(`${API_BASE}/observability/metrics/notifications`);
}

export async function fetchObsMetricsLineage(): Promise<{ edges: number }> {
  return request<{ edges: number }>(`${API_BASE}/observability/metrics/lineage`);
}

// ---- 推荐 ----
export async function fetchRecommendedMetrics(limit = 20): Promise<RecommendItem[]> {
  return request<{ items: RecommendItem[]; total: number }>(
    `${API_BASE}/recommend/metrics?limit=${limit}`,
  ).then((r) => r.items);
}

export async function fetchRelatedMetrics(metricId: number | string, limit = 20): Promise<RecommendItem[]> {
  return request<{ items: RecommendItem[]; total: number }>(
    `${API_BASE}/recommend/metrics/${encodeURIComponent(String(metricId))}/related?limit=${limit}`,
  ).then((r) => r.items);
}

export async function fetchRecommendedTerms(limit = 20): Promise<GlossaryTerm[]> {
  return request<{ items: GlossaryTerm[]; total: number }>(
    `${API_BASE}/recommend/terms?limit=${limit}`,
  ).then((r) => r.items.map((t) => ({ ...t, version: t.version ?? 1 })));
}

// ---- AI 助手 ----
export async function aiNl2Sql(body: {
  nl_query: string;
  metric_scope?: string[] | null;
  execute?: boolean;
}): Promise<NL2SQLResult> {
  return request<NL2SQLResult>(`${API_BASE}/ai/nl2sql`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// ---- LLM 配置 ----
export async function getLlmConfig(): Promise<LlmConfig> {
  return request<LlmConfig>(`${API_BASE}/ai/config`);
}

export async function saveLlmConfig(body: {
  provider: string;
  base_url: string;
  model: string;
  api_key?: string;
  timeout: number;
  enabled: boolean;
}): Promise<{ id: number }> {
  return request<{ id: number }>(`${API_BASE}/ai/config`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export async function testLlmConfig(body?: {
  provider?: string;
  base_url?: string;
  model?: string;
  api_key?: string;
  timeout?: number;
}): Promise<LlmConfigTestResult> {
  return request<LlmConfigTestResult>(`${API_BASE}/ai/config/test`, {
    method: "POST",
    body: JSON.stringify(body ?? {}),
  });
}

// ---- 审计 ----
export async function listAudit(params?: {
  actor_id?: number;
  entity_type?: string;
  entity_id?: string;
  trace_id?: string;
  pii_access?: boolean;
  page?: number;
  page_size?: number;
}): Promise<{ items: AuditEntry[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    actor_id: params?.actor_id,
    entity_type: params?.entity_type,
    entity_id: params?.entity_id,
    trace_id_filter: params?.trace_id,
    pii_access: params?.pii_access === undefined ? undefined : params.pii_access ? "true" : "false",
    page: params?.page ?? 1,
    page_size: params?.page_size ?? 20,
  });
  return request(`${API_BASE}/audit?${qs}`);
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

// ---- 采集器 ----
export async function listDataSources(params?: {
  domain?: string;
  source_type?: SourceType;
  keyword?: string;
  page?: number;
  page_size?: number;
}): Promise<DataSourceListResponse> {
  const qs = pageQs({
    domain: params?.domain,
    source_type: params?.source_type,
    keyword: params?.keyword,
    page: params?.page ?? 1,
    page_size: params?.page_size ?? 100,
  });
  // P1-1: 后端返回分页结构 {items, total, page, page_size}（此前 total 被丢弃导致静默截断）
  return request<DataSourceListResponse>(`${API_BASE}/data-sources?${qs}`);
}

export async function createDataSource(req: DataSourceCreateRequest): Promise<DataSource> {
  return request<DataSource>(`${API_BASE}/data-sources`, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function updateDataSource(sourceId: string, req: DataSourceUpdateRequest): Promise<DataSource> {
  return request<DataSource>(`${API_BASE}/data-sources/${encodeURIComponent(sourceId)}`, {
    method: "PUT",
    body: JSON.stringify(req),
  });
}

export async function listDataSourceTypes(): Promise<SourceTypeInfo[]> {
  return request<SourceTypeInfo[]>(`${API_BASE}/data-sources/types`);
}

export async function testDataSourceConnection(req: {
  source_type: SourceType;
  connection_config: Record<string, unknown>;
}): Promise<TestConnectionResult> {
  return request<TestConnectionResult>(`${API_BASE}/data-sources/test-connection`, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function checkDataSourceConnection(sourceId: string): Promise<TestConnectionResult> {
  return request<TestConnectionResult>(
    `${API_BASE}/data-sources/${encodeURIComponent(sourceId)}/check`,
    { method: "POST" },
  );
}

export async function getDataSource(sourceId: string): Promise<DataSource> {
  return request<DataSource>(`${API_BASE}/data-sources/${encodeURIComponent(sourceId)}`);
}

export async function collectSource(
  sourceId: string,
  mode = "FULL",
): Promise<CollectResult> {
  return request<CollectResult>(
    `${API_BASE}/data-sources/${encodeURIComponent(sourceId)}/collect`,
    {
      method: "POST",
      body: JSON.stringify({ collector_type: "information_schema", mode }),
    },
  );
}

/** 枚举实例下可采集的非系统数据库（创建数据源时选择目标库）。 */
export async function listDataSourceDatabases(req: {
  source_type: SourceType;
  connection_config: Record<string, unknown>;
}): Promise<ListDatabasesResult> {
  return request<ListDatabasesResult>(`${API_BASE}/data-sources/databases`, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

/** 异步立即采集（返回 job_id，进度走 SSE / 轮询任务状态）。 */
export async function collectSourceNow(
  sourceId: string,
  mode = "FULL",
): Promise<CollectNowResult> {
  return request<CollectNowResult>(
    `${API_BASE}/data-sources/${encodeURIComponent(sourceId)}/collect-now`,
    {
      method: "POST",
      body: JSON.stringify({ collector_type: "information_schema", mode }),
    },
  );
}

/**
 * SSE 订阅采集任务进度（fetch 流式解析，带鉴权头）。
 *
 * @returns 取消函数；流结束或出错时自动清理。
 */
export function streamCollectionJob(
  jobId: string,
  handlers: {
    onProgress?: (status: CollectionJob, progress: CollectionProgress | null) => void;
    onDone?: (status: CollectionJob) => void;
    onError?: (message: string) => void;
  },
): () => void {
  const controller = new AbortController();
  let aborted = false;
  const headers: Record<string, string> = {
    "X-Api-Key": SEMANTIC_API_KEY,
  };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  (async () => {
    try {
      const res = await fetch(
        `${API_BASE_URL}${API_BASE}/data-sources/jobs/${encodeURIComponent(jobId)}/stream`,
        { headers, signal: controller.signal },
      );
      if (!res.ok || !res.body) {
        handlers.onError?.(`SSE 连接失败 (HTTP ${res.status})`);
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let eventType = "";
      while (!aborted) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // 以空行分隔 SSE 事件
        let idx: number;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          const raw = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          let dataStr = "";
          for (const line of raw.split("\n")) {
            if (line.startsWith("event:")) eventType = line.slice(6).trim();
            else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
          }
          if (!dataStr) continue;
          const status = JSON.parse(dataStr) as CollectionJob;
          if (eventType === "done" || status.status === "COMPLETED" || status.status === "FAILED") {
            handlers.onDone?.(status);
            aborted = true;
            break;
          }
          const progress = (status.detail?.progress ?? null) as CollectionProgress | null;
          handlers.onProgress?.(status, progress);
        }
      }
      reader.releaseLock();
    } catch (err) {
      if (!aborted && !controller.signal.aborted) {
        handlers.onError?.(err instanceof Error ? err.message : "进度推送中断");
      }
    }
  })();

  return () => {
    aborted = true;
    controller.abort();
  };
}

export async function scheduleSource(
  sourceId: string,
  cron: string,
  mode = "FULL",
): Promise<ScheduleResult> {
  return request<ScheduleResult>(
    `${API_BASE}/data-sources/${encodeURIComponent(sourceId)}/schedule`,
    {
      method: "POST",
      body: JSON.stringify({ cron, mode }),
    },
  );
}

export async function getSourceHealth(sourceId: string): Promise<SourceHealth> {
  return request<SourceHealth>(
    `${API_BASE}/data-sources/${encodeURIComponent(sourceId)}/health`,
  );
}

export interface DriftLogItem {
  source_id: string;
  entity_name: string;
  change_type: string;
  before_signature: string | null;
  after_signature: string | null;
  before_schema: Record<string, unknown> | null;
  after_schema: Record<string, unknown> | null;
  diff_json: Record<string, unknown> | null;
  detected_at: string | null;
}

export async function listDriftLogs(
  sourceId: string,
  params?: { entity_name?: string; page?: number; page_size?: number },
): Promise<{ items: DriftLogItem[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    entity_name: params?.entity_name,
    page: params?.page ?? 1,
    page_size: params?.page_size ?? 10,
  });
  return request<{ items: DriftLogItem[]; total: number; page: number; page_size: number }>(
    `${API_BASE}/data-sources/${encodeURIComponent(sourceId)}/drift-logs?${qs}`,
  );
}

/** 采集任务中心：列出异步采集任务（按入队逆序分页；可按 source_id 过滤）。 */
export async function listCollectionJobs(params?: {
  limit?: number;
  offset?: number;
  source_id?: string;
}): Promise<CollectionJob[]> {
  const qs = pageQs({
    limit: params?.limit ?? 50,
    offset: params?.offset ?? 0,
    source_id: params?.source_id ?? undefined,
  });
  return request<CollectionJob[]>(`${API_BASE}/data-sources/jobs?${qs}`);
}

/** 查询单个采集任务状态。 */
export async function getCollectionJob(jobId: string): Promise<CollectionJob | null> {
  return request<CollectionJob | null>(
    `${API_BASE}/data-sources/jobs/${encodeURIComponent(jobId)}`,
  );
}

export async function getSourceWatermark(sourceId: string): Promise<Watermark> {
  return request<Watermark>(
    `${API_BASE}/data-sources/${encodeURIComponent(sourceId)}/watermark`,
  );
}

export async function listCatalogs(params?: {
  source_id?: string;
  entity_type?: string;
  sensitivity_level?: string;
  /** 库名（entity_name 前缀过滤） */
  database?: string;
  keyword?: string;
  /** active=仅活跃源 / deleted=仅已删除源 / 不传=全部 */
  source_status?: "active" | "deleted";
  page?: number;
  page_size?: number;
}): Promise<{ items: DBCatalog[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    source_id: params?.source_id,
    entity_type: params?.entity_type,
    sensitivity_level: params?.sensitivity_level,
    database: params?.database,
    keyword: params?.keyword,
    source_status: params?.source_status,
    page: params?.page ?? 1,
    page_size: params?.page_size ?? 20,
  });
  return request(`${API_BASE}/catalogs?${qs}`);
}

/** 目录去重库名列表（供库名筛选下拉，可随 source_id 联动）。 */
export async function listCatalogDatabases(sourceId?: string): Promise<string[]> {
  const qs = pageQs({ source_id: sourceId });
  const res = await request<{ items: string[] }>(`${API_BASE}/catalogs/databases?${qs}`);
  return res.items;
}

export async function registerCatalog(
  sourceId: string,
  body: {
    source_id?: string; // 可选——后端以 URL 路径为准自动填充
    entity_name: string;
    entity_type?: string;
    schema_def: Record<string, unknown>;
    etl_sql?: string | null;
    owner_id?: number | null;
  },
): Promise<DBCatalog> {
  return request<DBCatalog>(`${API_BASE}/data-sources/${encodeURIComponent(sourceId)}/catalogs`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function bulkDeprecateCatalogs(
  items: Array<{ source_id: string; entity_name: string }>,
): Promise<{ succeeded: Array<{ source_id: string; entity_name: string }>; failed: Array<Record<string, unknown>> }> {
  return request(`${API_BASE}/catalogs/bulk-deprecate`, {
    method: "POST",
    body: JSON.stringify({ items }),
  });
}

// ---- 资产地图 ----
export async function fetchAssetSummary(): Promise<AssetCatalogSummary> {
  return request<AssetCatalogSummary>(`${API_BASE}/assetmap/summary`);
}

export async function fetchAssetClassification(): Promise<AssetClassificationSummary> {
  return request<AssetClassificationSummary>(`${API_BASE}/assetmap/classification`);
}

export async function fetchAssetMetricSummary(): Promise<AssetMetricSummary> {
  return request<AssetMetricSummary>(`${API_BASE}/assetmap/metrics`);
}

export async function fetchAssetTables(params?: {
  source_id?: string;
  sensitivity?: string;
  limit?: number;
}): Promise<{ items: AssetTableItem[]; total: number }> {
  const qs = pageQs({
    source_id: params?.source_id,
    sensitivity: params?.sensitivity,
    limit: params?.limit ?? 100,
  });
  return request(`${API_BASE}/assetmap/tables?${qs}`);
}

export async function fetchAssetOrphans(): Promise<{ items: AssetTableItem[]; total: number }> {
  return request(`${API_BASE}/assetmap/orphans`);
}

export async function fetchAssetGraph(params?: {
  domain?: string;
  depth?: number;
  pii_only?: boolean;
}): Promise<{
  nodes: Array<{
    id: string;
    type: string;
    label: string;
    /** db_catalog 主键（表/视图节点下钻实体详情用） */
    entity_id?: number;
    pii?: boolean;
    domain?: string;
    owner?: string;
  }>;
  edges: Array<{ source: string; target: string; type: string }>;
}> {
  const qs = pageQs({
    domain: params?.domain,
    depth: params?.depth,
    pii_only: params?.pii_only === undefined ? undefined : params.pii_only ? "true" : "false",
  });
  return request(`${API_BASE}/assetmap/graph?${qs}`);
}

export async function fetchAssetHeatmap(dimension = "domain"): Promise<{
  dimension: string;
  buckets: Array<Record<string, unknown>>;
}> {
  return request(`${API_BASE}/assetmap/heatmap?dimension=${encodeURIComponent(dimension)}`);
}

// 二维热力矩阵：业务域 × 敏感级别（真热力图数据源，P3）
export async function fetchAssetHeatmapMatrix(): Promise<{
  cells: Array<{ domain: string; sensitivity: string; count: number; pii_count: number }>;
  columns: string[];
}> {
  return request(`${API_BASE}/assetmap/heatmap-matrix`);
}

export async function fetchAssetOwnerView(ownerId: number): Promise<AssetOwnerView> {
  return request<AssetOwnerView>(
    `${API_BASE}/assetmap/owner-view?owner_id=${ownerId}`,
  );
}

// 实体详情：返回表/字段详情（schema 摘要/敏感度/PII/Owner/血缘边数）
export async function fetchAssetEntityDetail(entityId: number): Promise<AssetEntityDetail> {
  return request<AssetEntityDetail>(`${API_BASE}/assetmap/entities/${entityId}`);
}

// ---- 产品补充（FR-18 生产化）：搜索 / 健康 / PII / 变更 / 我的资产 / 导出 ----

// 全局资产搜索
export async function fetchAssetSearch(params: {
  q: string;
  type?: string;
  limit?: number;
}): Promise<{ items: AssetSearchItem[]; total: number }> {
  const qs = pageQs({ q: params.q, type: params.type, limit: params.limit ?? 20 });
  return request(`${API_BASE}/assetmap/search?${qs}`);
}

// 全局聚合搜索（FR-18 全局搜索栏）：跨指标/维度/术语/模板/数据源/采集目录表+字段/主题域
export async function fetchGlobalSearch(
  q: string,
  limit = 5,
): Promise<GlobalSearchResponse> {
  const qs = pageQs({ q, limit });
  return request<GlobalSearchResponse>(`${API_BASE}/search?${qs}`);
}

// 资产健康视图
export async function fetchAssetHealth(): Promise<AssetHealthSummary> {
  return request<AssetHealthSummary>(`${API_BASE}/assetmap/health`);
}

// PII 合规资产视图
export async function fetchAssetPiiOverview(): Promise<AssetPiiOverview> {
  return request<AssetPiiOverview>(`${API_BASE}/assetmap/pii`);
}

// 变更追踪流
export async function fetchAssetChanges(params?: {
  days?: number;
  limit?: number;
}): Promise<AssetChanges> {
  const qs = pageQs({ days: params?.days ?? 7, limit: params?.limit ?? 50 });
  return request<AssetChanges>(`${API_BASE}/assetmap/changes?${qs}`);
}

// 我的资产（当前登录用户负责的目录与指标）
export async function fetchAssetMyAssets(limit = 50): Promise<AssetMyAssets> {
  return request<AssetMyAssets>(`${API_BASE}/assetmap/my-assets?limit=${limit}`);
}

// 资产 CSV 导出（下载文件）
export async function downloadAssetExport(params?: {
  source_id?: string;
  sensitivity?: string;
}): Promise<void> {
  const qs = pageQs({
    source_id: params?.source_id,
    sensitivity: params?.sensitivity,
  });
  const headers: Record<string, string> = {
    "X-Api-Key": SEMANTIC_API_KEY,
  };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE_URL}${API_BASE}/assetmap/export.csv${qs ? `?${qs}` : ""}`, {
    headers,
  });
  if (!res.ok) {
    throw new UnisenseApiError(`导出失败 (HTTP ${res.status})`, "HTTP_ERROR", res.status, "");
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "assetmap_export.csv";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export type { ApiError, DimensionExpr };

// ---- 主题域管理（backend /api/v1/domains/*）----

export async function listDomainTree(status?: string): Promise<SubjectDomainTreeNode[]> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  return request<SubjectDomainTreeNode[]>(`${API_BASE}/domains${qs}`);
}

export async function getDomain(code: string): Promise<SubjectDomain> {
  return request<SubjectDomain>(`${API_BASE}/domains/${encodeURIComponent(code)}`);
}

export async function createDomain(data: SubjectDomainCreateRequest): Promise<SubjectDomain> {
  return request<SubjectDomain>(`${API_BASE}/domains`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function updateDomain(code: string, data: SubjectDomainUpdateRequest): Promise<SubjectDomain> {
  return request<SubjectDomain>(`${API_BASE}/domains/${encodeURIComponent(code)}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export async function deactivateDomain(code: string): Promise<SubjectDomain> {
  return request<SubjectDomain>(`${API_BASE}/domains/${encodeURIComponent(code)}/status?action=deactivate`, {
    method: "PATCH",
  });
}

export async function activateDomain(code: string): Promise<SubjectDomain> {
  return request<SubjectDomain>(`${API_BASE}/domains/${encodeURIComponent(code)}/status?action=activate`, {
    method: "PATCH",
  });
}

export async function deleteDomain(code: string): Promise<void> {
  await request(`${API_BASE}/domains/${encodeURIComponent(code)}`, { method: "DELETE" });
}

export async function getDomainDefaults(code: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`${API_BASE}/domains/${encodeURIComponent(code)}/defaults`);
}

export async function updateDomainDefaults(code: string, defaults: Record<string, unknown>): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`${API_BASE}/domains/${encodeURIComponent(code)}/defaults`, {
    method: "PUT",
    body: JSON.stringify({ defaults_json: defaults }),
  });
}

export async function getDomainMetrics(code: string): Promise<Array<{ id: number; metric_code: string; name: string; status: string; type: string }>> {
  return request(`${API_BASE}/domains/${encodeURIComponent(code)}/metrics`);
}

// ---- 系统字典管理（backend /api/v1/dicts/*）----

export async function listDictTypes(): Promise<string[]> {
  return request<string[]>(`${API_BASE}/dicts/types`);
}

export async function listDictItems(dictType: string): Promise<SystemDictItem[]> {
  return request<SystemDictItem[]>(`${API_BASE}/dicts/${encodeURIComponent(dictType)}`);
}

export async function listAllDictItems(dictType: string): Promise<SystemDictItem[]> {
  return request<SystemDictItem[]>(`${API_BASE}/dicts/${encodeURIComponent(dictType)}/all`);
}

export async function createDictItem(dictType: string, data: DictItemCreateRequest): Promise<SystemDictItem> {
  return request<SystemDictItem>(`${API_BASE}/dicts/${encodeURIComponent(dictType)}`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function updateDictItem(dictType: string, code: string, data: DictItemUpdateRequest): Promise<SystemDictItem> {
  return request<SystemDictItem>(`${API_BASE}/dicts/${encodeURIComponent(dictType)}/${encodeURIComponent(code)}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export async function deactivateDictItem(dictType: string, code: string): Promise<SystemDictItem> {
  return request<SystemDictItem>(`${API_BASE}/dicts/${encodeURIComponent(dictType)}/${encodeURIComponent(code)}/status?action=deactivate`, {
    method: "PATCH",
  });
}

export async function activateDictItem(dictType: string, code: string): Promise<SystemDictItem> {
  return request<SystemDictItem>(`${API_BASE}/dicts/${encodeURIComponent(dictType)}/${encodeURIComponent(code)}/status?action=activate`, {
    method: "PATCH",
  });
}

export async function deleteDictItem(dictType: string, code: string): Promise<void> {
  await request(`${API_BASE}/dicts/${encodeURIComponent(dictType)}/${encodeURIComponent(code)}`, { method: "DELETE" });
}

export async function getDictItemRefCount(dictType: string, code: string): Promise<{ ref_count: number }> {
  return request(`${API_BASE}/dicts/${encodeURIComponent(dictType)}/${encodeURIComponent(code)}/ref-count`);
}

// ---- 自动推断（backend /api/v1/metric-definitions/auto-suggest）----

export async function autoSuggestMetric(data: AutoSuggestRequest): Promise<AutoSuggestResponse> {
  return request<AutoSuggestResponse>(`${API_BASE}/metric-definitions/auto-suggest`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}
