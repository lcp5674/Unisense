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
  AssetHeatmapMatrix,
  AssetMetricSummary,
  AssetMetricDimensionSummary,
  AssetMyAssets,
  AssetOwnerView,
  AssetPiiAssetItem,
  AssetPiiOverview,
  AssetPiiTemplate,
  AssetSearchItem,
  AssetTableItem,
  AuditEntry,
  AutoSuggestRequest,
  AutoSuggestResponse,
  DomainSuggestionResponse,
  SqlBatchParseResult,
  SqlBatchRegisterRequest,
  SqlInferEvalData,
  SqlInferEvalRunResult,
  SqlParseRequest,
  SqlParseTablesResult,
  BatchDeleteRequest,
  BatchSourceResult,
  BatchToggleRequest,
  ClientCreateRequest,
  ClientCreatedResponse,
  ConflictCheckRequest,
  ConflictCheckResult,
  ClientResponse,
  RenameSuggestResponse,
  CollectNowResult,
  CollectResult,
  CollectionProgress,
  ConflictListResponse,
  ConflictResponse,
  ConsumptionGuideResponse,
  MetricConsumptionGuideUpdateRequest,
  CurrentUser,
  DashboardData,
  DataSource,
  DataSourceCreateRequest,
  DataSourceListResponse,
  DataSourceUpdateRequest,
  DBCatalog,
  DictBatchResult,
  DictItemCreateRequest,
  DictItemUpdateRequest,
  DictUnknownNotifyRequest,
  DictValueCheckItem,
  DictValuesVerifyResponse,
  Dimension,
  DimensionMapping,
  MeasureCatalog,
  MeasureSuggestResult,
  MetricMount,
  MetricMountInput,
  DimensionMember,
  DimensionExpr,
  DryRunResponse,
  ErasureResult,
  EvalSample,
  EvalSampleIn,
  EvalSamplePreview,
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
  LineageCoverage,
  CoverageOrphanList,
  CoverageBrokenEdgeList,
  LineageEdgeDetail,
  LineageEdgePage,
  LineageGraphData,
  LineageIngestRun,
  LineageNode,
  ManualLineageEdgeResult,
  ParseLineageResult,
  ListDatabasesResult,
  ListTablesResult,
  ArchivedMetricResponse,
  MetricBatchRegisterRequest,
  MetricBatchRegisterResult,
  BatchResult,
  BatchSubmitItem,
  MetricCreateRequest,
  MetricListResponse,
  MetricReuseStats,
  MetricLedgerStats,
  MetricDimension,
  DimensionMetricBinding,
  MetricCompareResult,
  MetricCompareMatrixResult,
  MetricHealth,
  MetricResponse,
  MetricTemplate,
  MetricUpdateRequest,
  MetricVersionResponse,
  NL2SQLResult,
  LlmConfigList,
  LlmConfigPayload,
  LlmConfigSecret,
  LlmConfigTestResult,
  LlmModelsResult,
  Notification,
  NotifyEventLog,
  NpsStats,
  ObsMetricsNotifications,
  ObsMetricsQuality,
  ObsOverview,
  PermissionCheckResult,
  PermissionSnapshot,
  PiiReviewResult,
  QualityBenchmark,
  QualityEvent,
  QualityEventItem,
  QualityRule,
  QualityRuleCreate,
  QueryRequest,
  QueryResponse,
  RecommendItem,
  Reconciliation,
  ReconciliationRecord,
  RegexCheckResult,
  RoleOption,
  RolePermissionItem,
  RoleResponse,
  UserPermissionResponse,
  ActionRegistryItem,
  OrganizationView,
  RulingRecord,
  ScheduleResult,
  SnapshotResponse,
  CollectionJob,
  CollectionRun,
  CollectionRunLogItem,
  SourceHealth,
  SourceOverview,
  SourceType,
  SourceTypeInfo,
  StaleEdge,
  SubscriptionPref,
  SubjectDomain,
  SubjectDomainCreateRequest,
  SubjectDomainTreeNode,
  SubjectDomainUpdateRequest,
  SystemDictItem,
  SensitiveRuleCategory,
  SensitiveRuleCreate,
  SensitiveRuleItem,
  SensitiveRuleTestRequest,
  SensitiveRuleTestResponse,
  SensitiveRuleUpdate,
  TermRelation,
  TermRelationView,
  TestConnectionResult,
  TrackingGroupBy,
  TrackingStatsResponse,
  UserBrief,
  UserCreateRequest,
  UserUpdateRequest,
  AdminUser,
  AdminUserListResponse,
  UserBatchStatusResult,
  UserPreferenceItem,
  UserPreferenceList,
  Watermark,
  API_BASE,
} from "./types";

// ---- 运行配置 ----
// 后端地址：开发时由 Vite dev server 代理到 :8000（见 vite.config.ts），
// 生产构建可通过 VITE_API_BASE 覆盖。X-Api-Key 用于 Semantic API 网关鉴权。
// P2（第六轮）：Dockerfile 构建 ARG 用 VITE_API_BASE_URL（compose 传入空串走 nginx
// 相对路径），源码此前只读 VITE_API_BASE → 构建参数实际失效；此处两者兼容读取。
const API_BASE_URL: string =
  ((import.meta.env.VITE_API_BASE ??
    import.meta.env.VITE_API_BASE_URL) as string | undefined)?.replace(/\/$/, "") ||
  "";
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

// P0 令牌无感续期：访问令牌（短效）过期后，用长效 refresh token 换新
const REFRESH_TOKEN_KEY = "unisense_refresh_token";
export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_TOKEN_KEY);
}
export function setRefreshToken(token: string): void {
  localStorage.setItem(REFRESH_TOKEN_KEY, token);
}
export function clearRefreshToken(): void {
  localStorage.removeItem(REFRESH_TOKEN_KEY);
}

/** 清除全部登录态（access + refresh），用于会话彻底失效（刷新失败/登出）。 */
export function clearAuthTokens(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
}

// 消费服务客户端访问令牌（role=consume 的 JWT），由 /consume/api-clients/{id}/token 签发
const CONSUME_TOKEN_KEY = "unisense_consume_token";
/** 消费令牌写入/清除事件：request() 在 401 时清除令牌、或令牌被过期自清理时，
 * 组件（如 QueryWorkspace 的「令牌已就绪」状态）需实时同步，否则 UI 显示假就绪。
 */
export const CONSUME_TOKEN_CHANGED_EVENT = "unisense:consume-token-changed";
function emitConsumeTokenChanged(): void {
  window.dispatchEvent(new Event(CONSUME_TOKEN_CHANGED_EVENT));
}
export function getConsumeToken(): string | null {
  return localStorage.getItem(CONSUME_TOKEN_KEY);
}
/** 解析消费 JWT 的 exp（毫秒时间戳）。非 JWT 或 payload 无 exp 返回 null。 */
export function getConsumeTokenExpiry(): number | null {
  const raw = getConsumeToken();
  if (!raw) return null;
  const parts = raw.split(".");
  if (parts.length !== 3) return null;
  try {
    let b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    while (b64.length % 4) b64 += "=";
    const payload = JSON.parse(atob(b64)) as { exp?: unknown };
    return typeof payload.exp === "number" ? payload.exp * 1000 : null;
  } catch {
    return null;
  }
}
/** 解析消费 JWT 的 sub（= 绑定的 client_id）。非 JWT 或 payload 无 sub 返回 null。 */
export function getConsumeTokenClientId(): string | null {
  const raw = getConsumeToken();
  if (!raw) return null;
  const parts = raw.split(".");
  if (parts.length !== 3) return null;
  try {
    let b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    while (b64.length % 4) b64 += "=";
    const payload = JSON.parse(atob(b64)) as { sub?: unknown };
    return typeof payload.sub === "string" ? payload.sub : null;
  } catch {
    return null;
  }
}
export function setConsumeToken(token: string): void {
  localStorage.setItem(CONSUME_TOKEN_KEY, token);
  emitConsumeTokenChanged();
}
export function clearConsumeToken(): void {
  localStorage.removeItem(CONSUME_TOKEN_KEY);
  emitConsumeTokenChanged();
}

// 后端 error_code → 中文可读描述（供全站错误提示展示，避免英文技术码直出给业务用户）
const ERROR_CODE_ZH: Record<string, string> = {
  AUTH_TOKEN_MISSING: "未登录或登录状态缺失",
  AUTH_TOKEN_EXPIRED: "登录已过期，请重新登录",
  AUTH_TOKEN_INVALID: "登录状态无效",
  AUTH_REFRESH_EXPIRED: "登录已过期，请重新登录",
  AUTH_REFRESH_REVOKED: "登录状态已失效，请重新登录",
  AUTH_INVALID_CREDENTIALS: "用户名或密码错误",
  AUTH_APIKEY_MISSING: "缺少访问密钥（X-Api-Key）",
  AUTH_APIKEY_INVALID: "访问密钥无效或已吊销",
  FORBIDDEN: "您无权执行该操作",
  FEATURE_DISABLED: "该功能已被平台管理员关闭，请走常规流程",
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
  METRIC_ARCHIVED: "该指标已因口径裁决作废，请查看权威指标",
  VALIDATION_ERROR: "输入校验未通过",
  INTERNAL_ERROR: "系统内部错误，请稍后重试",
  DEPENDENCY_UNPUBLISHED: "依赖指标尚未发布",
  CONFLICT: "存在冲突，需协商或裁决后继续",
  METRIC_CODE_EXISTS: "指标编码已存在，请修改编码后重试",
  PII_REVIEW_REQUIRED: "该指标含个人信息，需先完成合规复核",
  GRANULARITY_VIOLATION: "查询粒度与指标定义不符",
  ADDITIVITY_VIOLATION: "查询可加性与指标定义不符",
  API_VERSION_SUNSET: "该接口版本已停用，请升级客户端",
  COMPLIANCE_BLOCKED: "该指标含个人信息，需先完成合规复核",
  CONCURRENT_MODIFICATION: "数据已被他人修改，请刷新后重试",
  OPTIMISTIC_LOCK_CONFLICT: "数据已被他人修改，请刷新后重试",
  METRIC_PENDING_VERSION_EXISTS: "该指标存在待确认的破坏性变更，请先完成确认或等待超时后再发起新变更",
  DEPENDENCY_DEGRADED_GRAPH: "血缘图服务暂不可用，已降级为库表查询",
  DEPENDENCY_DEGRADED_LLM: "AI 服务暂不可用，请稍后再试",
  DRIFT_DETECTED: "检测到数据表结构漂移，请核查上游",
  EXPRESSION_INVALID: "计算表达式非法，请检查口径定义",
  FRESHNESS_DELAYED: "数据更新延迟，暂未就绪",
  LLM_QUOTA_EXCEEDED: "AI 服务配额已用尽，请联系管理员",
  METRIC_DEPRECATED: "该指标已废弃",
  METRIC_VERSION_SUNSET: "该指标版本已停用",
  NL_AMBIGUOUS: "提问存在歧义，请补充说明",
  NL_INTENT_UNRECOGNIZED: "无法识别提问意图，请换一种问法",
  NL_MAPPING_FAILED: "未能匹配到对应指标，请调整提问",
  QUALITY_DOWNGRADED: "数据质量已降级，请关注告警",
  FORBIDDEN_REVIEWER: "您不是被指派的评审人，无法操作该指标",
  DIMENSION_BOUND_BY_METRICS: "该维度已被指标绑定，请先解除绑定再废弃",
  DUPLICATE_MEMBER_CODE: "成员编码已存在",
  DUPLICATE_CODE: "编码已存在，请更换",
  DUPLICATE_TERM_RELATION: "该术语关系已存在",
  DOMAIN_INACTIVE: "该业务域已停用，无法使用",
  DOMAIN_NOT_FOUND: "业务域不存在",
  HAS_CHILDREN: "存在子节点，无法删除",
  HAS_REFERENCED_METRICS: "该资源被指标引用，无法删除",
  DICT_VALUE_NOT_FOUND: "配置的字典值不存在或已停用，请重新选择",
  INVALID_DECISION: "仲裁决策不合法，请重新选择",
  INVALID_RELATION_TYPE: "关系类型不合法",
  INVALID_DIM_TYPE: "维度类型不合法",
  DIM_EXISTS: "维度编码已存在",
  DEFINITION_INCOMPLETE: "指标口径尚未定义，请完善表达式/源表后提交评审",
  QUERY_TIMEOUT: "查询超时，请缩小范围后重试",
  RECONCILIATION_ALERT: "存在对账异常告警，请及时处理",
  STALE_DATA: "数据已过期，请刷新后重试",
  VERSION_CONFLICT: "口径版本冲突，请核对当前生效版本",
  CIRCULAR_DEPENDENCY: "检测到循环依赖，已拒绝",
};

export class UnisenseApiError extends Error {
  code: string;
  traceId: string;
  detail?: Record<string, unknown> | null;
  status: number;
  constructor(
    message: string,
    code: string,
    status: number,
    traceId: string,
    detail?: Record<string, unknown> | null,
  ) {
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
  /** 自定义主请求超时（毫秒）；缺省用全局 REQUEST_TIMEOUT_MS（30s）。长任务
   * （SQL 批量解析/评测运行等 LLM 场景耗时可达 60-90s）须显式放宽，否则 30s
   * 中止会让用户误以为解析失败。 */
  timeout?: number;
}

// S-3（第八轮）：主请求超时（毫秒）——此前无超时保护，慢网络/后端挂起时请求无限等待。
export const REQUEST_TIMEOUT_MS = 30000;
// S-4（第八轮）：会话中途失效全局事件——401 刷新失败清 token 后派发，App 层监听回登录页。
// 用自定义事件而非直接 location 跳转：保持 SPA 状态、不依赖路由库、App 守卫（user 为
// null 即回登录页）统一收敛，登录接口自身 401 也会触发但此时 user 已 null 无害。
export const AUTH_EXPIRED_EVENT = "unisense:auth-expired";

// P0 令牌无感续期：单飞（single-flight）刷新，多个并发 401 共享同一次 /auth/refresh，
// 避免重复刷新互相覆盖令牌。返回是否续期成功。
let _refreshPromise: Promise<boolean> | null = null;

async function refreshAccessToken(): Promise<boolean> {
  if (_refreshPromise) return _refreshPromise;
  // 先创建任务 Promise，再赋值给单飞锁，最后 await + finally 释放。
  // 关键：任务内部可能有同步完成路径（如无 refresh token 提前返回），
  // 若把 finally 放进任务体，其会先于外层赋值执行，把锁重新覆盖成已 resolve 的
  // Promise，导致单飞锁被永久占用、后续刷新全部失效。
  const task = (async (): Promise<boolean> => {
    try {
      const refreshToken = getRefreshToken();
      if (!refreshToken) {
        clearRefreshToken();
        return false;
      }
      const res = await fetch(`${API_BASE_URL}${API_BASE}/auth/refresh`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Api-Key": SEMANTIC_API_KEY,
        },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!res.ok) {
        clearAuthTokens();
        return false;
      }
      const body = (await res.json()) as {
        data?: { access_token?: string; refresh_token?: string };
      };
      const newAccess = body.data?.access_token;
      const newRefresh = body.data?.refresh_token;
      if (!newAccess || !newRefresh) {
        clearAuthTokens();
        return false;
      }
      setToken(newAccess);
      setRefreshToken(newRefresh);
      return true;
    } catch {
      clearAuthTokens();
      return false;
    }
  })();
  _refreshPromise = task;
  try {
    return await task;
  } finally {
    _refreshPromise = null;
  }
}

async function request<T>(path: string, init?: RequestOptions): Promise<T> {
  const headers: Record<string, string> = {
    "X-Api-Key": SEMANTIC_API_KEY,
    // P2-2（第八轮）：透传 X-Trace-Id——后端 TraceIdMiddleware 读取并随响应回传，
    // 使前端请求与后端日志/审计 trace_id 贯通（排查慢请求/报错无需手工对时间）。
    "X-Trace-Id": crypto.randomUUID(),
    ...(init?.headers as Record<string, string> | undefined),
  };
  // FormData（文件上传）由浏览器自动生成 multipart boundary——显式 Content-Type
  // 会破坏 boundary 导致后端 422；JSON 请求才设置默认 Content-Type。
  if (!(init?.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  if (init?.consumeAuth) {
    // 过期令牌不发出：先自动清除（触发 CONSUME_TOKEN_CHANGED_EVENT 让 UI 同步），
    // 避免「带过期 Bearer → 401 → 清除 → 组件仍显示已就绪 → 下次请求无 Bearer 报 X-Api-Key 误导」。
    let consumeToken = getConsumeToken();
    const consumeExp = getConsumeTokenExpiry();
    if (consumeToken && consumeExp !== null && consumeExp <= Date.now()) {
      clearConsumeToken();
      consumeToken = null;
    }
    let bearer: string | null = consumeToken;
    if (!bearer && init?.consumeFallbackUser) bearer = getToken();
    if (bearer) headers["Authorization"] = `Bearer ${bearer}`;
  } else {
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }

  const {
    consumeAuth: _consumeAuth,
    consumeFallbackUser: _consumeFallbackUser,
    signal: _initSignal,
    timeout: _initTimeout,
    ...restInit
  } = init ?? {};

  // S-3：主请求加超时中止（后端挂起不再无限等待）。init.signal 存在时（调用方自定义
  // 中止，如搜索竞态取消）与超时信号合并，两者任一触发即中止。init.timeout 显式
  // 覆盖全局超时（SQL 批量解析/评测运行等 LLM 长任务需放宽到 180s，避免误报失败）。
  const _timeoutMs = _initTimeout ?? REQUEST_TIMEOUT_MS;
  const timeoutSignal = AbortSignal.timeout(_timeoutMs);
  const signal = _initSignal ? AbortSignal.any([_initSignal, timeoutSignal]) : timeoutSignal;
  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}${path}`, {
      ...restInit,
      headers,
      signal,
    });
  } catch (err) {
    if (err instanceof DOMException && (err.name === "TimeoutError" || err.name === "AbortError")) {
      throw new UnisenseApiError("请求超时，请检查网络或稍后重试", "REQUEST_TIMEOUT", 0, "");
    }
    throw err;
  }

  // P0 令牌无感续期：用户访问令牌过期（401）时，用 refresh token 换新后重放一次原请求。
  // 403 是权限拒绝（非过期）、consumeAuth 走独立消费令牌，均不触发刷新。
  if (res.status === 401 && !init?.consumeAuth) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      const newToken = getToken();
      if (newToken) {
        headers["Authorization"] = `Bearer ${newToken}`;
        res = await fetch(`${API_BASE_URL}${path}`, {
          ...restInit,
          headers,
          signal: AbortSignal.timeout(_timeoutMs),
        });
      }
    }
  }

  if (res.status === 401 || res.status === 403) {
    // 鉴权失效：清 token，交由上层跳登录。
    // consumeAuth 仅 401（令牌无效/过期）清除消费令牌——403 是授权拒绝（FORBIDDEN_DOMAIN/
    // FORBIDDEN_METRIC/FORBIDDEN_PII），令牌本身有效，清除会导致后续请求无 Bearer 回落
    // X-Api-Key 报「需要消费令牌」误导（与下方用户 JWT 的 403 保留登录态同语义）。
    if (init?.consumeAuth) {
      if (res.status === 401) clearConsumeToken();
    } else if (res.status === 401) {
      // 401：刷新失败或重放仍 401 → 会话彻底失效，清空 access + refresh
      clearAuthTokens();
      // S-4：通知 App 层会话失效（user 置 null → 回登录页）。
      window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
    }
    // 403：已登录但无权限（非令牌过期），保留登录态（access + refresh），
    // 交由上层路由守卫/页面按角色处理，避免「清 access 后刷新仍 403」的割裂中间态。
  }

  // 尝试解析统一信封；非 2xx 抛出 UnisenseApiError
  let body:
    ApiEnvelope<T> | { code: string; message: string; trace_id: string; detail?: unknown } | null =
    null;
  try {
    body = (await res.json()) as ApiEnvelope<T>;
  } catch {
    if (!res.ok) {
      throw new UnisenseApiError(`请求失败 (HTTP ${res.status})`, "HTTP_ERROR", res.status, "");
    }
  }

  if (!res.ok) {
    const err =
      (body as {
        message?: string;
        code?: string;
        trace_id?: string;
        detail?: Record<string, unknown>;
      }) || {};
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

function pageQs(params: Record<string, string | number | string[] | undefined>): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) {
      // 数组 → 重复 key（?role=a&role=b），后端 list query 收集为多值。
      for (const item of v) {
        if (item !== undefined && item !== null && item !== "") qs.append(k, String(item));
      }
    } else {
      qs.set(k, String(v));
    }
  }
  return qs.toString();
}

// ---- 鉴权 ----
export async function apiLogin(username: string, password: string): Promise<string> {
  const data = await request<{
    access_token: string;
    refresh_token: string;
    token_type: string;
  }>(`${API_BASE}/auth/login`, {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  setToken(data.access_token);
  if (data.refresh_token) setRefreshToken(data.refresh_token);
  return data.access_token;
}

export async function fetchCurrentUser(): Promise<CurrentUser> {
  return request<CurrentUser>(`${API_BASE}/auth/me`);
}

/** 登出：调用后端撤销当前 access token（JWT jti 入黑名单），best-effort。 */
export async function apiLogout(): Promise<void> {
  await request<void>(`${API_BASE}/auth/logout`, { method: "POST" });
}

/** 自助修改密码（POST /users/me/password，登录用户改自己的密码）。 */
export async function changePassword(body: {
  current_password: string;
  new_password: string;
}): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`${API_BASE}/users/me/password`, {
    method: "POST",
    body: JSON.stringify(body),
  });
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

/** 删除用户偏好（软删，幂等）——如恢复侧栏折叠等 UI 状态到默认。 */
export async function deletePreference(key: string): Promise<void> {
  await request<void>(`${API_BASE}/me/preferences/${encodeURIComponent(key)}`, {
    method: "DELETE",
  });
}

// ---- 指标定义 ----
export async function listMetrics(params: {
  domain?: string;
  status?: string;
  metric_tier?: string;
  /** 指标类型过滤：atomic 原子 / derived 派生 / composite 复合（服务端精确过滤） */
  metric_type?: "atomic" | "derived" | "composite";
  keyword?: string;
  /** 责任人（Owner）ID 过滤（资产地图 Owner 视图下钻） */
  owner_id?: number;
  /** PII 过滤：true=仅 PII，false=仅非 PII（热力指标视角下钻） */
  pii_flag?: boolean;
  /** 生命周期快筛：仅返回 created_at 晚于该时间（ISO）的指标 */
  created_after?: string;
  /** 生命周期快筛：仅返回 updated_at 早于该时间（ISO）的指标 */
  updated_before?: string;
  /** 审批人（Approver）ID 过滤（审批工作台「我审过的」视图） */
  approver_id?: number;
  /** 评审历史过滤：命中 审批通过(approver_id) 或 驳回(reject_reviewer_id) 任一
   *  （「我审过的」完整视图——评审人通过+驳回的记录都可见，不丢驳回历史） */
  reviewed_by?: number;
  /** 仅查已软删（回收站）指标：true 时展示已删除草稿供恢复 */
  deleted?: boolean;
  /** 下游引用过滤：true=仅有活跃下游引用（被派生/消费方引用），false=仅无下游。
   *  （批量废弃前按引用情况收敛，语义与 downstream-check 一致） */
  has_downstream?: boolean;
  /** 批次过滤（生产就绪审查 P2）：按批量注册批次 ID 精确匹配（审核/列表按"这一批"收敛） */
  batch_id?: string;
  /** 健康度档位过滤（仪表盘/可观测中心健康度分布下钻）：EXCELLENT/GOOD/WARNING/CRITICAL */
  health_level?: "EXCELLENT" | "GOOD" | "WARNING" | "CRITICAL";
  sort_by?: "updated_at" | "created_at" | "version" | "metric_code" | "name";
  sort_order?: "asc" | "desc";
  page?: number;
  page_size?: number;
}): Promise<MetricListResponse> {
  const qs = pageQs({
    domain: params.domain,
    status: params.status,
    metric_tier: params.metric_tier,
    metric_type: params.metric_type,
    keyword: params.keyword,
    owner_id: params.owner_id,
    approver_id: params.approver_id,
    reviewed_by: params.reviewed_by,
    pii_flag: params.pii_flag === undefined ? undefined : String(params.pii_flag),
    deleted: params.deleted === undefined ? undefined : String(params.deleted),
    has_downstream: params.has_downstream === undefined ? undefined : String(params.has_downstream),
    created_after: params.created_after,
    updated_before: params.updated_before,
    batch_id: params.batch_id,
    health_level: params.health_level,
    sort_by: params.sort_by,
    sort_order: params.sort_order,
    page: params.page ?? 1,
    page_size: params.page_size ?? 20,
  });
  return request<MetricListResponse>(`${API_BASE}/metric-definitions?${qs}`);
}

// 指标运营分析：复用度 / 资产账本 / 口径一致率（backend metric_stats.py + metrics.py）
export async function fetchMetricReuseStats(): Promise<MetricReuseStats> {
  return request<MetricReuseStats>(`${API_BASE}/metric-definitions/stats/reuse`);
}

export async function fetchMetricLedger(): Promise<MetricLedgerStats> {
  return request<MetricLedgerStats>(`${API_BASE}/metric-definitions/stats/ledger`);
}

export async function fetchConsistencyStats(): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`${API_BASE}/metric-definitions/consistency/stats`);
}

export async function getMetric(code: string): Promise<MetricResponse> {
  return request<MetricResponse>(`${API_BASE}/metric-definitions/${encodeURIComponent(code)}`);
}

// ---- 通用批量导入（外部 agent / CSV，编码名称可缺省自动补全）----

export interface MetricImportResult {
  batch_id: string;
  candidates: Array<{
    metric_code: string;
    status: string;
    validation_errors?: string[];
  }>;
  row_errors?: Array<{ row: number; error: string }>;
}

export interface MetricImportCandidateInput {
  metric_code?: string;
  name?: string;
  type?: "atomic" | "derived" | "composite";
  source_table?: string;
  measure_column?: string;
  aggregation?: string;
  unit?: string;
  period?: string;
  granularity?: string;
  measure_id?: number;
  expression?: string;
  dependencies?: string[];
  raw_sql?: string;
}

/** 通用批量导入（外部 agent / 结构化数据）：domain + candidates → 逐条创建 DRAFT */
export async function batchImportMetrics(body: {
  domain: string;
  source?: "agent" | "csv" | "manual";
  candidates: MetricImportCandidateInput[];
}): Promise<MetricImportResult> {
  return request<MetricImportResult>(`${API_BASE}/metric-definitions/batch-import`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** 上传 CSV 批量导入：FormData（file + domain），逐行解析 + 逐条创建 DRAFT */
export async function importMetricsCsv(formData: FormData): Promise<MetricImportResult> {
  return request<MetricImportResult>(`${API_BASE}/metric-definitions/imports/csv`, {
    method: "POST",
    body: formData,
    // 批量导入可能含多行 + 冲突预检，放宽到 60s
    timeout: 60000,
  });
}

/** 下载指标批量导入模板（CSV / Excel xlsx，带鉴权，blob 触发浏览器下载） */
export async function downloadMetricImportTemplate(format: "csv" | "xlsx" = "csv"): Promise<void> {
  const qs = format === "xlsx" ? "?format=xlsx" : "";
  const res = await fetch(`${API_BASE_URL}${API_BASE}/metric-definitions/imports/template${qs}`, {
    headers: {
      Authorization: `Bearer ${getToken() ?? ""}`,
      "X-Api-Key": SEMANTIC_API_KEY,
      "X-Trace-Id": crypto.randomUUID(),
    },
  });
  if (!res.ok) {
    throw new UnisenseApiError("模板下载失败", "HTTP_ERROR", res.status, "");
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = format === "xlsx" ? "metric_import_template.xlsx" : "metric_import_template.csv";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// 作废指标详情（供作废引导页展示历史口径 + 跳转权威指标）
export async function fetchArchivedMetric(code: string): Promise<ArchivedMetricResponse> {
  return request<ArchivedMetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/archived`,
  );
}

export async function createMetric(req: MetricCreateRequest): Promise<MetricResponse> {
  return request<MetricResponse>(`${API_BASE}/metric-definitions`, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

// 批量注册指标：源宽表 + 多个度量列 → 批量创建 DRAFT（backend POST /metric-definitions/batch-register，对齐 FR-030）
export async function batchRegisterMetrics(
  req: MetricBatchRegisterRequest,
): Promise<MetricBatchRegisterResult> {
  return request<MetricBatchRegisterResult>(
    `${API_BASE}/metric-definitions/batch-register`,
    {
      method: "POST",
      body: JSON.stringify(req),
    },
  );
}

export async function updateMetric(
  code: string,
  req: MetricUpdateRequest,
): Promise<MetricResponse> {
  return request<MetricResponse>(`${API_BASE}/metric-definitions/${encodeURIComponent(code)}`, {
    method: "PUT",
    body: JSON.stringify(req),
  });
}

// 更新指标业务描述（治理补充 TD §12.1，不触发版本；空串清除）
export async function updateMetricDescription(
  code: string,
  description: string,
  rowVersion?: number,
): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/description`,
    {
      method: "PUT",
      body: JSON.stringify({ description, row_version: rowVersion ?? undefined }),
    },
  );
}

// 绑定/解绑指标↔业务术语（P2-11：术语治理归属写路径；空数组解绑）
// 2026-09：支持多术语全量替换（term_ids），主术语=首项写 term_id（兼容旧单值语义）
export async function bindMetricTerms(code: string, termIds: number[]): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/term`,
    {
      method: "PUT",
      body: JSON.stringify({ term_ids: termIds, term_id: termIds[0] ?? null }),
    },
  );
}

// LLM 推断指标业务描述（治理补充 TD §12.1，source=llm，不触发版本）
// force=true 强制重新生成；默认已存在 LLM 描述时后端短路返回（避免重复调 LLM）
export async function inferMetricDescription(
  code: string,
  opts?: { force?: boolean },
): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/infer-description${
      opts?.force ? "?force=true" : ""
    }`,
    { method: "POST" },
  );
}

// 注：publishMetric 死代码已删除（P2-9 发布语义统一）——发布实际走 approve
// （REVIEW→PUBLISHED），前端无独立「发布」动作；后端 /publish 端点亦无调用方。

export async function deprecateMetric(
  code: string,
  successor_code: string,
): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/deprecate`,
    {
      method: "POST",
      // 空替代指标：传 undefined 使 JSON 序列化省略该字段（后端 Optional 接受），
      // 避免空串触发「替代指标不存在:（空）」的误导性错误。
      body: JSON.stringify({ successor_code: successor_code || undefined }),
    },
  );
}

// 数据源下线指标恢复（DSD → PUBLISHED，源恢复/确认误报）
export async function recoverSourceDropped(code: string): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/recover-source-dropped`,
    { method: "POST" },
  );
}

// 确认数据源下线指标退役（DSD → DEPRECATED，须填替代指标）
export async function confirmDeprecateDropped(
  code: string,
  successor_code: string,
): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/confirm-deprecate-dropped`,
    {
      method: "POST",
      body: JSON.stringify({ successor_code: successor_code || undefined }),
    },
  );
}

// 删除指标（软删除，仅 DRAFT/DEPRECATED 状态；平台/域管理员或原 Owner）
export async function deleteMetric(code: string): Promise<{ deleted: boolean }> {
  return request<{ deleted: boolean }>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}`,
    { method: "DELETE" },
  );
}

// 恢复已软删指标（回收站恢复，仅 DRAFT 且已删状态；平台管理员或原 Owner）
export async function restoreMetric(code: string): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/restore`,
    { method: "POST" },
  );
}

// 彻底删除已软删指标（回收站硬删，物理删除不可恢复；仅平台管理员）
export async function purgeMetric(code: string): Promise<{ metric_code: string }> {
  return request<{ metric_code: string }>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/purge`,
    { method: "POST" },
  );
}

export async function listVersions(code: string): Promise<MetricVersionResponse[]> {
  return request<MetricVersionResponse[]>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/versions`,
  );
}

export async function piiReview(code: string): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/pii-review`,
    { method: "POST" },
  );
}

// 提交评审：DRAFT → REVIEW；change_reason 缺省"提交评审"（后端 /submit，对齐 FR-003）
// 评审指派（TD §13）：可传 reviewer_id/reviewer_type/reviewer_domain 指定评审用户或域评审组
export async function submitReview(
  metricCode: string,
  changeReason = "提交评审",
  reviewer?: {
    reviewer_id?: number | null;
    reviewer_type?: "user" | "domain" | null;
    reviewer_domain?: string | null;
  },
): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(metricCode)}/submit`,
    {
      method: "POST",
      body: JSON.stringify({
        change_reason: changeReason,
        reviewer_id: reviewer?.reviewer_id ?? null,
        reviewer_type: reviewer?.reviewer_type ?? null,
        reviewer_domain: reviewer?.reviewer_domain ?? null,
      }),
    },
  );
}

// ---- 批量治理（TD §13：提交/通过/打回/下线，逐条收集结果不整体失败）----

// 批量提交审核（可带评审指派）
export async function batchSubmitMetrics(
  items: BatchSubmitItem[],
): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/metric-definitions/batch-submit`, {
    method: "POST",
    body: JSON.stringify({ items }),
  });
}

// 批量审核通过（= 批量发布；后端保留灰度参数 mode/gray_tenant_ids）
export async function batchApproveMetrics(
  metricCodes: string[],
  mode: "standard" | "experimental" = "standard",
  grayTenantIds?: number[],
): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/metric-definitions/batch-approve`, {
    method: "POST",
    body: JSON.stringify({
      metric_codes: metricCodes,
      mode,
      // P1-3（第六轮）：批量通过支持灰度发布（对齐单条 MetricReview）——
      // 后端 MetricBatchApproveRequest 已接受 gray_tenant_ids，前端此前不传
      gray_tenant_ids: mode === "experimental" ? grayTenantIds ?? [] : undefined,
    }),
  });
}

// 批量审核驳回
export async function batchRejectMetrics(
  metricCodes: string[],
  reason: string,
): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/metric-definitions/batch-reject`, {
    method: "POST",
    body: JSON.stringify({ codes: metricCodes, reason }),
  });
}

// 批量下线（废弃；有下游引用时必须填替代指标，无下游引用可留空）
export async function batchDeprecateMetrics(
  items: { metric_code: string; successor_code: string | null }[],
): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/metric-definitions/batch-deprecate`, {
    method: "POST",
    body: JSON.stringify({ items }),
  });
}

// P2-1：批量重新启用已废弃指标（DEPRECATED → DRAFT，对齐维度/逻辑度量/术语 batch-reactivate）
export async function batchReactivateMetrics(
  metricCodes: string[],
): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/metric-definitions/batch-reactivate`, {
    method: "POST",
    body: JSON.stringify({ metric_codes: metricCodes }),
  });
}

// 回收站批量彻底删除已软删指标（物理删除不可恢复；仅平台管理员）
export async function batchPurgeMetrics(metricCodes: string[]): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/metric-definitions/batch-purge`, {
    method: "POST",
    body: JSON.stringify({ metric_codes: metricCodes }),
  });
}

// 批量下线下游使用审查：返回每指标的被引用情况（下线弹窗预审用）
export interface MetricDownstreamReferrer {
  node: string;
  edge_type: string;
}
export interface MetricDownstreamCheckResult {
  metric_code: string;
  referrer_count: number;
  referrers: MetricDownstreamReferrer[];
}
export async function checkMetricDownstream(
  metricCodes: string[],
): Promise<MetricDownstreamCheckResult[]> {
  return request<MetricDownstreamCheckResult[]>(
    `${API_BASE}/metric-definitions/downstream-check`,
    {
      method: "POST",
      body: JSON.stringify({ metric_codes: metricCodes }),
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
  return request<MetricHealth>(`${API_BASE}/metric-definitions/${encodeURIComponent(code)}/health`);
}

// 审核通过：mode=standard 全量发布 / experimental 灰度发布（灰度可带 gray_tenant_ids）
export async function approveMetric(
  code: string,
  opts: {
    mode?: "standard" | "experimental";
    gray_tenant_ids?: number[];
    target_version?: number;
  } = {},
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

export async function compareMetrics(codeA: string, codeB: string): Promise<MetricCompareResult> {
  return request<MetricCompareResult>(`${API_BASE}/metric-definitions/compare`, {
    method: "POST",
    body: JSON.stringify({ metric_codes: [codeA, codeB] }),
  });
}

export async function compareMetricsMatrix(codes: string[]): Promise<MetricCompareMatrixResult> {
  return request<MetricCompareMatrixResult>(`${API_BASE}/metric-definitions/compare/matrix`, {
    method: "POST",
    body: JSON.stringify({ metric_codes: codes }),
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

export async function completeEmergencyReview(code: string): Promise<MetricResponse> {
  return request<MetricResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(code)}/emergency-review`,
    { method: "POST" },
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

// ---- 用户管理（backend /api/v1/users，platform_admin 专属）----
export async function listAdminUsers(params: {
  role?: string[];
  status?: string;
  keyword?: string;
  page?: number;
  page_size?: number;
}): Promise<AdminUserListResponse> {
  const qs = pageQs({
    role: params.role,
    status: params.status,
    keyword: params.keyword,
    page: params.page ?? 1,
    page_size: params.page_size ?? 50,
  });
  return request<AdminUserListResponse>(`${API_BASE}/users?${qs}`);
}

export async function createUser(payload: UserCreateRequest): Promise<AdminUser> {
  return request<AdminUser>(`${API_BASE}/users`, { method: "POST", body: JSON.stringify(payload) });
}

export async function updateUser(userId: number, payload: UserUpdateRequest): Promise<AdminUser> {
  return request<AdminUser>(`${API_BASE}/users/${userId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function setUserStatus(
  userId: number,
  status: "active" | "disabled",
): Promise<AdminUser> {
  return request<AdminUser>(`${API_BASE}/users/${userId}/status`, {
    method: "PATCH",
    body: JSON.stringify({ status }),
  });
}

export async function batchSetUserStatus(
  userIds: number[],
  status: "active" | "disabled",
): Promise<UserBatchStatusResult> {
  return request<UserBatchStatusResult>(`${API_BASE}/users/batch-status`, {
    method: "POST",
    body: JSON.stringify({ user_ids: userIds, status }),
  });
}

export async function resetUserPassword(
  userId: number,
  newPassword: string,
): Promise<{ user_id: number; ok: boolean }> {
  return request<{ user_id: number; ok: boolean }>(
    `${API_BASE}/users/${userId}/reset-password`,
    { method: "POST", body: JSON.stringify({ new_password: newPassword }) },
  );
}

// ---- 冲突 ----
export async function listConflicts(params: {
  status?: string;
  severity?: string;
  page?: number;
  page_size?: number;
}): Promise<ConflictListResponse> {
  const qs = pageQs({
    status: params.status,
    severity: params.severity,
    page: params.page ?? 1,
    page_size: params.page_size ?? 20,
  });
  return request<ConflictListResponse>(`${API_BASE}/conflicts?${qs}`);
}

export async function arbitrateConflict(
  conflictId: string,
  decision: string,
  canonicalMetricCode: string,
  renameTargetOrCode = "",
): Promise<ConflictResponse> {
  // renameTargetOrCode 为「角色」（candidate/existing）或兼容旧调用的 metric_code：
  // 同名冲突下候选/现有 code 相同，须以角色区分，故优先按角色解析。
  const body: Record<string, string> = {
    decision,
    canonical_metric_code: canonicalMetricCode,
  };
  if (renameTargetOrCode === "candidate" || renameTargetOrCode === "existing") {
    body.rename_target = renameTargetOrCode;
  } else if (renameTargetOrCode) {
    body.rename_metric_code = renameTargetOrCode;
  }
  return request<ConflictResponse>(`${API_BASE}/conflicts/${conflictId}/arbitrate`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function escalateConflict(
  conflictId: string,
  note: string,
): Promise<ConflictResponse> {
  return request<ConflictResponse>(`${API_BASE}/conflicts/${conflictId}/escalate`, {
    method: "POST",
    body: JSON.stringify({ note }),
  });
}

export async function closeConflict(conflictId: string): Promise<ConflictResponse> {
  return request<ConflictResponse>(`${API_BASE}/conflicts/${conflictId}/close`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

// 强制关闭未决冲突（悬空处置：关联指标已删，仲裁失去对象；仅治理管理员）
export async function forceCloseConflict(conflictId: string): Promise<ConflictResponse> {
  return request<ConflictResponse>(`${API_BASE}/conflicts/${conflictId}/force-close`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

// 重新打开已关闭冲突（CLOSED → OPEN，供重新裁决）：POST /conflicts/{id}/reopen
export async function reopenConflict(conflictId: string): Promise<ConflictResponse> {
  return request<ConflictResponse>(`${API_BASE}/conflicts/${conflictId}/reopen`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

// 主动冲突检测：创建指标前的口径冲突扫描（POST /conflicts/check）
export async function checkConflict(req: ConflictCheckRequest): Promise<ConflictCheckResult> {
  return request<ConflictCheckResult>(`${API_BASE}/conflicts/check`, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

// 裁决记录（知识库）：GET /conflicts/{conflict_id}/rulings，返回历史裁决列表
export async function listConflictRulings(conflictId: string): Promise<RulingRecord[]> {
  return request<RulingRecord[]>(`${API_BASE}/conflicts/${conflictId}/rulings`);
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

// 血缘图谱（力导向图渲染数据，节点/边结构与资产地图图谱对齐）
export async function lineageGraph(params?: {
  domain?: string;
  pii_only?: boolean;
  limit?: number;
  /** 来源通道过滤：dp_csv/sqlglot/metric_definition；为空=采集目录视角图谱 */
  provenance?: string;
}): Promise<LineageGraphData> {
  const qs = pageQs({
    domain: params?.domain,
    pii_only: params?.pii_only === undefined ? undefined : String(params.pii_only),
    limit: params?.limit,
    provenance: params?.provenance,
  });
  return request<LineageGraphData>(`${API_BASE}/lineage/graph${qs ? `?${qs}` : ""}`);
}

export async function parseLineage(
  sql: string,
  dialect?: string,
  targetTable?: string,
): Promise<ParseLineageResult> {
  return request<ParseLineageResult>(`${API_BASE}/lineage/parse`, {
    method: "POST",
    body: JSON.stringify({
      sql,
      dialect: dialect ?? null,
      provenance: "sqlglot",
      target_table: targetTable?.trim() || null,
    }),
  });
}

// 血缘候选节点（影响分析/血缘查询选项框）：无 kw 预加载 top-N，带 kw 按关键词搜索指定
export async function lineageNodes(kw?: string, limit = 50): Promise<LineageNode[]> {
  const qs = pageQs({ kw: kw || undefined, limit });
  return request<LineageNode[]>(`${API_BASE}/lineage/nodes${qs ? `?${qs}` : ""}`);
}

// ---- 血缘治理开放能力（P2/P3/P4 补齐前端入口）----

export interface LineageHealthDimension {
  score: number;
  weight: number;
  detail: Record<string, unknown>;
}

export interface LineageHealthResult {
  overall_score: number;
  grade: "excellent" | "good" | "fair" | "poor";
  dimensions: Record<string, LineageHealthDimension>;
  edge_total: number;
  metric_total: number;
  table_total: number;
  evaluated_at: string;
}

export interface LineagePathEdgeItem {
  source: string;
  target: string;
  edge_type: string;
}

export interface LineagePathItem {
  nodes: string[];
  edges: LineagePathEdgeItem[];
  hops: number;
}

export interface LineagePathResult {
  source: string;
  target: string;
  has_path: boolean;
  path_count: number;
  shortest_hops: number | null;
  paths: LineagePathItem[];
  truncated: boolean;
}

export interface LineageTerminalItem {
  node: string;
  entity_exists: boolean;
  hops: number;
}

export interface LineageTerminalsResult {
  source: string;
  total: number;
  terminals: LineageTerminalItem[];
}

export interface LineageBatchStatementResult {
  index: number;
  status: "ok" | "failed" | "skipped";
  sql: string;
  added?: number;
  updated?: number;
  error?: string;
}

export interface LineageParseBatchResult {
  total_statements: number;
  succeeded: number;
  failed: number;
  total_edges: number;
  added: number;
  updated: number;
  skipped: number;
  graph_written: boolean;
  statements: LineageBatchStatementResult[];
}

export interface LineageScanResult {
  files: number;
  statements: number;
  table_edges: number;
  field_edges: number;
  ddl_edges: number;
  dry_run: boolean;
  graph_written: boolean;
}

export interface LineageJsonExportResult {
  format: string;
  producer: string;
  exported_at: string;
  edge_count: number;
  edges: Array<Record<string, unknown>>;
}

// 血缘平台综合健康度（P2 五维评分）
export async function lineageHealth(): Promise<LineageHealthResult> {
  return request<LineageHealthResult>(`${API_BASE}/lineage/health`);
}

// A→B 血缘路径查询（P3）
export async function lineagePathQuery(
  source: string,
  target: string,
  maxHops = 5,
  limit = 50,
): Promise<LineagePathResult> {
  const qs = pageQs({ source, target, max_hops: maxHops, limit });
  return request<LineagePathResult>(`${API_BASE}/lineage/path?${qs}`);
}

// 下游终止节点（断链定位，P3）
export async function lineagePathTerminals(
  node: string,
  maxHops = 5,
  limit = 100,
): Promise<LineageTerminalsResult> {
  const qs = pageQs({ node, max_hops: maxHops, limit });
  return request<LineageTerminalsResult>(`${API_BASE}/lineage/path/terminals?${qs}`);
}

// 标准血缘导出（P4：JSON 边明细 / OpenLineage 事件）
export async function lineageExport(params: {
  format?: "openlineage" | "json";
  node?: string;
  direction?: "upstream" | "downstream" | "both";
  granularity?: "L1" | "L2" | "L3" | "all";
  limit?: number;
  domain?: string;
}): Promise<LineageJsonExportResult> {
  const qs = pageQs({
    format: params.format ?? "json",
    node: params.node,
    direction: params.direction ?? "both",
    granularity: params.granularity ?? "all",
    limit: params.limit ?? 10000,
    domain: params.domain,
  });
  return request<LineageJsonExportResult>(`${API_BASE}/lineage/export?${qs}`);
}

// 批量解析多条 SQL 并幂等写入血缘（P2 企业级批量导入）
export async function lineageParseBatch(body: {
  dialect?: string;
  statements?: string[];
  text?: string;
  target_table?: string;
  source_node?: string;
}): Promise<LineageParseBatchResult> {
  return request<LineageParseBatchResult>(`${API_BASE}/lineage/parse-batch`, {
    method: "POST",
    body: JSON.stringify({ provenance: "sqlglot", ...body }),
  });
}

// 库级扫描 SQL 目录（P2 企业级血缘重建）
export async function lineageScanDirectory(body: {
  path: string;
  dialect?: string;
  dry_run?: boolean;
  target_table?: string;
}): Promise<LineageScanResult> {
  return request<LineageScanResult>(`${API_BASE}/lineage/scan`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// 级联软删某节点相关的全部血缘边（数据源删除时维护一致性）
export async function deleteLineageEdgesByNode(node: string): Promise<{ deleted: number }> {
  const qs = pageQs({ node });
  return request<{ deleted: number }>(`${API_BASE}/lineage/edges?${qs}`, { method: "DELETE" });
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

// 单条采集运行详情（含详情快照 detail：SQL 原文/方言/落点/边明细 或 批量变更边明细）
export async function lineageRunDetail(runId: number): Promise<LineageIngestRun> {
  return request<LineageIngestRun>(`${API_BASE}/lineage/runs/${runId}`);
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

// ---- 血缘覆盖率治理（backend/app/api/lineage.py 覆盖率端点）----

// 覆盖率统计：指标/表血缘完整度 + 孤儿子数 + 断链边数（治理看板）
export async function fetchLineageCoverage(): Promise<LineageCoverage> {
  return request<LineageCoverage>(`${API_BASE}/lineage/coverage`);
}

// 孤立指标清单：后端返回纯数组（CoverageOrphanItem），容错兼容 {items,total} 信封，
// 统一归一化为 CoverageOrphanList 供 UI 消费。
export async function fetchLineageOrphans(): Promise<CoverageOrphanList> {
  const data = await request<unknown>(`${API_BASE}/lineage/coverage/orphans`);
  if (Array.isArray(data)) {
    const items = data as import("./types").CoverageOrphanItem[];
    return { items, total: items.length };
  }
  const wrapped = data as { items?: import("./types").CoverageOrphanItem[]; total?: number };
  const items = wrapped.items ?? [];
  return { items, total: wrapped.total ?? items.length };
}

// 断链边明细：后端返回纯数组（CoverageBrokenEdgeItem），容错兼容 {items,total} 信封。
export async function fetchLineageBrokenEdges(limit = 50): Promise<CoverageBrokenEdgeList> {
  const data = await request<unknown>(
    `${API_BASE}/lineage/coverage/broken?limit=${encodeURIComponent(limit)}`,
  );
  if (Array.isArray(data)) {
    const items = data as import("./types").CoverageBrokenEdgeItem[];
    return { items, total: items.length };
  }
  const wrapped = data as { items?: import("./types").CoverageBrokenEdgeItem[]; total?: number };
  const items = wrapped.items ?? [];
  return { items, total: wrapped.total ?? items.length };
}

// 单条血缘边详情 + 变更历史：后端返回 {edge:{...}, history:[...]} 嵌套结构，
// 且 history 项无 before_value（含 source/target/edge_type/change_reason/created_at）。
// 此处用 any 兜底读取并归一化为扁平 LineageEdgeDetail，不改动后端。
export async function fetchLineageEdgeDetail(edgeId: number): Promise<LineageEdgeDetail> {
  const raw = (await request<unknown>(`${API_BASE}/lineage/edges/${edgeId}`)) as {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    edge?: any;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    history?: any[];
  } | null;
  // 已扁平化（契约形态）直接透传
  if (raw && !("edge" in raw) && Array.isArray((raw as { history?: unknown }).history)) {
    return raw as unknown as LineageEdgeDetail;
  }
  const edge = raw?.edge ?? raw ?? {};
  const historyRaw = Array.isArray(raw?.history) ? raw.history : [];
  const history = historyRaw.map((h) => {
    const c = h ?? {};
    return {
      id: c.id,
      change_reason: c.change_reason,
      before_value:
        c.before_value !== undefined
          ? c.before_value
          : c.source_node !== undefined && c.target_node !== undefined
            ? `${c.source_node} → ${c.target_node}`
            : undefined,
      changed_at: c.changed_at ?? c.created_at,
      created_at: c.created_at ?? c.changed_at,
      source_node: c.source_node,
      target_node: c.target_node,
      edge_type: c.edge_type,
    };
  });
  return {
    id: edge.id,
    source_node: edge.source_node,
    target_node: edge.target_node,
    edge_type: edge.edge_type,
    granularity: edge.granularity,
    confidence: edge.confidence,
    provenance: edge.provenance,
    pii_inherited: edge.pii_inherited,
    created_at: edge.created_at,
    history,
  };
}

// 手动登记一条血缘边（人工治理：自动解析覆盖不到的业务依赖）。
// source_node 为上游、target_node 为下游，均须带前缀（metric:/table:/column:/dimension:/consumer:/external:）。
export async function addManualLineageEdge(req: {
  source_node: string;
  target_node: string;
  edge_type?: string;
  note?: string;
}): Promise<ManualLineageEdgeResult> {
  return request<ManualLineageEdgeResult>(`${API_BASE}/lineage/edges/manual`, {
    method: "POST",
    body: JSON.stringify({
      source_node: req.source_node,
      target_node: req.target_node,
      edge_type: req.edge_type ?? "DERIVED_FROM",
      note: req.note?.trim() || undefined,
    }),
  });
}

// 单条血缘边软删（人工治理：误登记/断链修复的单边删除）
export async function deleteLineageEdge(edgeId: number): Promise<{
  edge_id: number;
  source_node: string;
  target_node: string;
}> {
  return request(`${API_BASE}/lineage/edges/${edgeId}`, { method: "DELETE" });
}

// 同步某指标的消费方血缘边（CONSUMED_BY）：按接入方白名单补齐消费方节点
export async function syncMetricConsumers(metricCode: string): Promise<{ registered_edges: number }> {
  return request(`${API_BASE}/lineage/consumers/${encodeURIComponent(metricCode)}/sync`, {
    method: "POST",
  });
}
export type FavoriteAssetType = "METRIC" | "TABLE" | "TERM" | "DIMENSION" | "TEMPLATE";

export interface FavoriteItem {
  asset_type: FavoriteAssetType;
  asset_id: string;
}

export async function listFavorites(): Promise<FavoriteItem[]> {
  return request<FavoriteItem[]>(`${API_BASE}/consume/me/favorites`);
}

export async function addFavorite(assetType: FavoriteAssetType, assetId: string): Promise<FavoriteResponse> {
  return request<FavoriteResponse>(`${API_BASE}/consume/me/favorites`, {
    method: "POST",
    body: JSON.stringify({ asset_type: assetType, asset_id: assetId }),
  });
}

export async function removeFavorite(assetType: FavoriteAssetType, assetId: string): Promise<FavoriteResponse> {
  return request<FavoriteResponse>(
    `${API_BASE}/consume/me/favorites/${encodeURIComponent(assetType)}/${encodeURIComponent(assetId)}`,
    {
      method: "DELETE",
    },
  );
}

export interface FavoriteDetail {
  asset_type: FavoriteAssetType;
  asset_id: string;
  name: string;
  description: string | null;
  domain: string | null;
  status: string;
  tier: string | null;
  is_pii: boolean;
  /** 收藏时间（ISO 字符串） */
  created_at: string;
  /** 资产已软删除/不存在 */
  dead: boolean;
}

/** 收藏详情聚合（一次查询，避免逐条取名的 N+1；含收藏时间与失效标记）。 */
export async function listFavoriteDetails(): Promise<FavoriteDetail[]> {
  return request<FavoriteDetail[]>(`${API_BASE}/consume/me/favorites/detail`);
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
  owner_id?: number;
  page?: number;
  page_size?: number;
}): Promise<{ items: MetricTemplate[]; total: number }> {
  const qs = pageQs({
    domain: params?.domain,
    is_active: params?.is_active === undefined ? undefined : params.is_active ? "true" : "false",
    keyword: params?.keyword,
    owner_id: params?.owner_id,
    page: params?.page,
    page_size: params?.page_size,
  });
  return request<{ items: MetricTemplate[]; total: number }>(
    `${API_BASE}/semantics/templates?${qs}`,
  );
}

// 指派/解除指标模板责任人（PATCH /semantics/templates/{id}/owner，owner_id=null 解除）
export async function updateTemplateOwner(
  templateId: number,
  ownerId: number | null,
): Promise<MetricTemplate> {
  return request<MetricTemplate>(`${API_BASE}/semantics/templates/${templateId}/owner`, {
    method: "PATCH",
    body: JSON.stringify({ owner_id: ownerId }),
  });
}

// 启用/停用指标模板（PATCH /semantics/templates/{id}/active，is_active=false 停止新实例化）
export async function setTemplateActive(
  templateId: number,
  isActive: boolean,
): Promise<MetricTemplate> {
  return request<MetricTemplate>(`${API_BASE}/semantics/templates/${templateId}/active`, {
    method: "PATCH",
    body: JSON.stringify({ is_active: isActive }),
  });
}

// 创建指标模板（POST /semantics/templates，code 缺省由后端自动生成 tpl_{domain}_{name}）
export async function createMetricTemplate(
  body: Partial<{
    code: string;
    name: string;
    domain: string;
    description: string;
    defaults_json: Record<string, unknown>;
    required_fields: string[];
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
    measure_id: number | null;
    mount: MetricMountInput | null;
    product_owner_id: number | null;
    tech_owner_id: number | null;
    dw_developer_id: number | null;
    product_owner_name: string | null;
    tech_owner_name: string | null;
    dw_developer_name: string | null;
    owner_id: number | null;
  }>,
): Promise<MetricTemplate> {
  return request<MetricTemplate>(`${API_BASE}/semantics/templates`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// 编辑指标模板（PATCH /semantics/templates/{id}，全字段可选 PATCH 语义；code 不可改）
export async function updateMetricTemplate(
  templateId: number,
  body: Partial<{
    name: string;
    domain: string;
    description: string;
    defaults_json: Record<string, unknown>;
    required_fields: string[];
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
    // OneData 预设（方案A）：逻辑度量/挂载/三方责任局部更新（传 null 清除）
    measure_id: number | null;
    mount: MetricMountInput | null;
    product_owner_id: number | null;
    tech_owner_id: number | null;
    dw_developer_id: number | null;
    product_owner_name: string | null;
    tech_owner_name: string | null;
    dw_developer_name: string | null;
    owner_id: number | null;
    is_active: boolean;
  }>,
): Promise<MetricTemplate> {
  return request<MetricTemplate>(`${API_BASE}/semantics/templates/${templateId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

// 从模板实例化创建指标（POST /semantics/templates/{id}/instantiate，后端合并模板默认口径 + 用户覆盖）
export async function instantiateTemplate(
  templateId: number,
  body: MetricCreateRequest,
): Promise<MetricResponse> {
  return request<MetricResponse>(`${API_BASE}/semantics/templates/${templateId}/instantiate`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// 消费指南：后端按 metric_code 查询（对齐 semantic.py /consumption-guide/{metric_code}）
export async function fetchConsumptionGuide(metricCode: string): Promise<ConsumptionGuideResponse> {
  return request<ConsumptionGuideResponse>(
    `${API_BASE}/semantics/consumption-guide/${encodeURIComponent(metricCode)}`,
  );
}

// 更新消费指南（人工维护，独立于指标状态机；row_version 乐观锁防并发覆盖）
export async function updateConsumptionGuide(
  metricCode: string,
  req: MetricConsumptionGuideUpdateRequest,
): Promise<ConsumptionGuideResponse> {
  return request<ConsumptionGuideResponse>(
    `${API_BASE}/semantics/consumption-guide/${encodeURIComponent(metricCode)}`,
    {
      method: "PUT",
      body: JSON.stringify(req),
    },
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

// 消费侧指标语义（只读拉取，GET /consume/metrics/{code}/semantic，返回 DryRunResponse）
export async function consumeSemantic(code: string): Promise<DryRunResponse> {
  return request<DryRunResponse>(
    `${API_BASE}/consume/metrics/${encodeURIComponent(code)}/semantic`,
    { consumeAuth: true },
  );
}

export async function listSnapshots(code: string, limit = 50): Promise<SnapshotResponse[]> {
  return request<SnapshotResponse[]>(
    `${API_BASE}/consume/metrics/${encodeURIComponent(code)}/snapshots?limit=${limit}`,
    { consumeAuth: true, consumeFallbackUser: true },
  );
}

/** 内部用户查询指标（POST /consume/metrics/{code}/query，真实执行 + 自动落快照）。 */
export async function queryMetricInternal(
  code: string,
  req: {
    dimensions?: { name: string; value: string | number | string[] }[];
    date_range: string;
  },
): Promise<QueryResponse> {
  return request<QueryResponse>(`${API_BASE}/consume/metrics/${encodeURIComponent(code)}/query`, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

/** 提数响应时效 KPI：近 N 天查询量/avg/p95/p99/错误数（管理员视角） */
export async function consumeResponseTime(days = 7): Promise<{
  days: number;
  items: Array<{
    date: string;
    count: number;
    avg_ms: number;
    p95_ms: number;
    p99_ms: number;
    max_ms: number;
    error_count: number;
  }>;
}> {
  return request(`${API_BASE}/consume/stats/response-time?days=${days}`);
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

export async function mintClientToken(clientId: string, expireMinutes?: number): Promise<{ access_token: string }> {
  return request<{ access_token: string }>(
    `${API_BASE}/consume/api-clients/${encodeURIComponent(clientId)}/token`,
    {
      method: "POST",
      body: JSON.stringify(expireMinutes != null ? { expire_minutes: expireMinutes } : {}),
    },
  );
}

// ---- 维度 ----
export async function listDimensions(params?: {
  domain?: string;
  status?: string;
  keyword?: string;
  owner_id?: number;
  reviewed_by?: number;
  deleted?: boolean;
  page?: number;
  page_size?: number;
}): Promise<{ items: Dimension[]; total: number }> {
  const qs = pageQs({
    domain: params?.domain,
    status: params?.status,
    keyword: params?.keyword,
    owner_id: params?.owner_id,
    reviewed_by: params?.reviewed_by,
    deleted: params?.deleted ? "true" : undefined,
    page: params?.page,
    page_size: params?.page_size ?? 50,
  });
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

/** 提交维度审核（DRAFT → REVIEW，发布须先审） */
export async function submitDimension(
  dimCode: string,
  body: ReviewSubmitBody,
): Promise<Dimension> {
  return request<Dimension>(
    `${API_BASE}/dimensions/${encodeURIComponent(dimCode)}/submit`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

/** 审核通过维度（REVIEW → PUBLISHED） */
export async function approveDimension(
  dimCode: string,
  body: { comment?: string | null },
): Promise<Dimension> {
  return request<Dimension>(
    `${API_BASE}/dimensions/${encodeURIComponent(dimCode)}/approve`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

/** 审核驳回维度（REVIEW → DRAFT，驳回原因必填） */
export async function rejectDimension(
  dimCode: string,
  body: { reason: string },
): Promise<Dimension> {
  return request<Dimension>(
    `${API_BASE}/dimensions/${encodeURIComponent(dimCode)}/reject`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export async function deprecateDimension(dimCode: string): Promise<Dimension> {
  return request<Dimension>(`${API_BASE}/dimensions/${encodeURIComponent(dimCode)}/deprecate`, {
    method: "POST",
  });
}

/** 重新启用已废弃维度（DEPRECATED → DRAFT，可编辑后重新走审核） */
export async function reactivateDimension(dimCode: string): Promise<Dimension> {
  return request<Dimension>(`${API_BASE}/dimensions/${encodeURIComponent(dimCode)}/reactivate`, {
    method: "POST",
  });
}

/** 软删除维度（仅 DRAFT/DEPRECATED 可删；管理员或原 Owner） */
export async function deleteDimension(dimCode: string): Promise<Dimension> {
  return request<Dimension>(`${API_BASE}/dimensions/${encodeURIComponent(dimCode)}/delete`, {
    method: "POST",
  });
}

/** 恢复已软删维度（回收站恢复） */
export async function restoreDimension(dimCode: string): Promise<Dimension> {
  return request<Dimension>(`${API_BASE}/dimensions/${encodeURIComponent(dimCode)}/restore`, {
    method: "POST",
  });
}

// ---- 维度批量治理（TD §13，后端统一 app/api/batch_common）----

/** 批量提交维度审核（DRAFT → REVIEW，可带评审指派） */
export async function batchSubmitDimensions(items: BatchSubmitItem[]): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/dimensions/batch-submit`, {
    method: "POST",
    body: JSON.stringify({ items }),
  });
}

/** 批量审核通过维度（REVIEW → PUBLISHED） */
export async function batchApproveDimensions(codes: string[]): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/dimensions/batch-approve`, {
    method: "POST",
    body: JSON.stringify({ codes }),
  });
}

/** 批量审核驳回维度（REVIEW → DRAFT，原因必填） */
export async function batchRejectDimensions(codes: string[], reason: string): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/dimensions/batch-reject`, {
    method: "POST",
    body: JSON.stringify({ codes, reason }),
  });
}

/** 批量废弃维度（PUBLISHED → DEPRECATED） */
export async function batchDeprecateDimensions(codes: string[]): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/dimensions/batch-deprecate`, {
    method: "POST",
    body: JSON.stringify({ codes }),
  });
}

/** 批量重新启用已废弃维度（DEPRECATED → DRAFT） */
export async function batchReactivateDimensions(codes: string[]): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/dimensions/batch-reactivate`, {
    method: "POST",
    body: JSON.stringify({ codes }),
  });
}

/** 批量软删除维度（仅 DRAFT/DEPRECATED 可删） */
export async function batchDeleteDimensions(codes: string[]): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/dimensions/batch-delete`, {
    method: "POST",
    body: JSON.stringify({ codes }),
  });
}

export async function listDimensionMappings(
  sourceDimCode?: string,
  page = 1,
  pageSize = 200,
): Promise<{ items: DimensionMapping[]; total: number }> {
  const params = new URLSearchParams();
  if (sourceDimCode) params.set("source_dim_code", sourceDimCode);
  params.set("page", String(page));
  params.set("page_size", String(pageSize));
  const qs = params.toString();
  return request(`${API_BASE}/dimensions/mappings${qs ? `?${qs}` : ""}`);
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

export async function listReconciliations(
  status?: string,
  page = 1,
  pageSize = 200,
): Promise<{ items: Reconciliation[]; total: number }> {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  params.set("page", String(page));
  params.set("page_size", String(pageSize));
  const qs = params.toString();
  return request(`${API_BASE}/dimensions/reconciliations${qs ? `?${qs}` : ""}`);
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

export async function reviewReconciliation(
  recId: number,
  decision: string,
): Promise<Reconciliation> {
  return request<Reconciliation>(`${API_BASE}/dimensions/reconciliations/${recId}/review`, {
    method: "POST",
    body: JSON.stringify({ decision }),
  });
}

// ---- 逻辑度量目录（OneData 原子层，/api/v1/measure-catalogs）----
export async function listMeasureCatalogs(params?: {
  domain?: string;
  status?: string;
  keyword?: string;
  owner_id?: number;
  reviewed_by?: number;
  deleted?: boolean;
  page?: number;
  page_size?: number;
}): Promise<{ items: MeasureCatalog[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    domain: params?.domain,
    status: params?.status,
    keyword: params?.keyword,
    owner_id: params?.owner_id,
    reviewed_by: params?.reviewed_by,
    deleted: params?.deleted ? "true" : undefined,
    page: params?.page,
    page_size: params?.page_size ?? 50,
  });
  return request(`${API_BASE}/measure-catalogs?${qs}`);
}

export async function autoSuggestMeasureCatalog(body: {
  name: string;
  description?: string | null;
  domain?: string | null;
  source_table?: string | null;
  measure_column?: string | null;
}): Promise<MeasureSuggestResult> {
  return request<MeasureSuggestResult>(`${API_BASE}/measure-catalogs/auto-suggest`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** 编辑逻辑度量「AI 生成同义词」（不落库，回填表单）：名称必填，描述作 LLM 上下文。 */
export async function inferMeasureSynonyms(body: {
  name: string;
  description?: string | null;
}): Promise<{ synonyms: string[] }> {
  return request<{ synonyms: string[] }>(`${API_BASE}/measure-catalogs/infer-synonyms`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function createMeasureCatalog(body: {
  measure_code?: string;
  name: string;
  domain: string;
  description?: string | null;
  measure_format: string;
  default_unit?: string | null;
  default_decimal_places?: number | null;
  source_system?: string[] | null;
  synonyms?: string[] | null;
  category?: string;
  stat_caliber?: string | null;
  owner_id?: number;
}): Promise<MeasureCatalog> {
  return request<MeasureCatalog>(`${API_BASE}/measure-catalogs`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateMeasureCatalog(
  measureCode: string,
  body: {
    row_version?: number;
    measure_code?: string;
    name?: string;
    domain?: string;
    description?: string | null;
    measure_format?: string;
    default_unit?: string | null;
    default_decimal_places?: number | null;
    source_system?: string[] | null;
    synonyms?: string[] | null;
    category?: string;
    stat_caliber?: string | null;
  },
): Promise<MeasureCatalog> {
  return request<MeasureCatalog>(
    `${API_BASE}/measure-catalogs/${encodeURIComponent(measureCode)}`,
    { method: "PUT", body: JSON.stringify(body) },
  );
}

export async function publishMeasureCatalog(measureCode: string): Promise<MeasureCatalog> {
  return request<MeasureCatalog>(
    `${API_BASE}/measure-catalogs/${encodeURIComponent(measureCode)}/publish`,
    { method: "POST" },
  );
}

/** 主数据审核提交请求体（对齐后端 ReviewSubmitRequest / 指标审核流 TD §13，逻辑度量/维度/术语共用） */
export interface ReviewSubmitBody {
  change_reason: string;
  reviewer_id?: number | null;
  reviewer_type?: "user" | "domain" | null;
  reviewer_domain?: string | null;
}

/** 提交逻辑度量审核（DRAFT → REVIEW，发布须先审） */
export async function submitMeasureCatalog(
  measureCode: string,
  body: ReviewSubmitBody,
): Promise<MeasureCatalog> {
  return request<MeasureCatalog>(
    `${API_BASE}/measure-catalogs/${encodeURIComponent(measureCode)}/submit`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

/** 审核通过逻辑度量（REVIEW → PUBLISHED） */
export async function approveMeasureCatalog(
  measureCode: string,
  body: { comment?: string | null },
): Promise<MeasureCatalog> {
  return request<MeasureCatalog>(
    `${API_BASE}/measure-catalogs/${encodeURIComponent(measureCode)}/approve`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

/** 审核驳回逻辑度量（REVIEW → DRAFT，驳回原因必填） */
export async function rejectMeasureCatalog(
  measureCode: string,
  body: { reason: string },
): Promise<MeasureCatalog> {
  return request<MeasureCatalog>(
    `${API_BASE}/measure-catalogs/${encodeURIComponent(measureCode)}/reject`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export async function deprecateMeasureCatalog(measureCode: string): Promise<MeasureCatalog> {
  return request<MeasureCatalog>(
    `${API_BASE}/measure-catalogs/${encodeURIComponent(measureCode)}/deprecate`,
    { method: "POST" },
  );
}

/** 重新启用已废弃逻辑度量（DEPRECATED → DRAFT，可编辑后重新走审核） */
export async function reactivateMeasureCatalog(measureCode: string): Promise<MeasureCatalog> {
  return request<MeasureCatalog>(
    `${API_BASE}/measure-catalogs/${encodeURIComponent(measureCode)}/reactivate`,
    { method: "POST" },
  );
}

/** 软删除逻辑度量（仅 DRAFT/DEPRECATED 可删；管理员或原 Owner） */
export async function deleteMeasureCatalog(measureCode: string): Promise<MeasureCatalog> {
  return request<MeasureCatalog>(
    `${API_BASE}/measure-catalogs/${encodeURIComponent(measureCode)}/delete`,
    { method: "POST" },
  );
}

/** 恢复已软删逻辑度量（回收站恢复） */
export async function restoreMeasureCatalog(measureCode: string): Promise<MeasureCatalog> {
  return request<MeasureCatalog>(
    `${API_BASE}/measure-catalogs/${encodeURIComponent(measureCode)}/restore`,
    { method: "POST" },
  );
}

/** 彻底删除已软删逻辑度量（回收站硬删，不可恢复；仅平台管理员） */
export async function purgeMeasureCatalog(measureCode: string): Promise<{ measure_code: string }> {
  return request<{ measure_code: string }>(
    `${API_BASE}/measure-catalogs/${encodeURIComponent(measureCode)}/purge`,
    { method: "POST" },
  );
}

// ---- 逻辑度量批量治理（TD §13，后端统一 app/api/batch_common）----

/** 批量提交逻辑度量审核（DRAFT → REVIEW，可带评审指派） */
export async function batchSubmitMeasures(items: BatchSubmitItem[]): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/measure-catalogs/batch-submit`, {
    method: "POST",
    body: JSON.stringify({ items }),
  });
}

/** 批量审核通过逻辑度量（REVIEW → PUBLISHED） */
export async function batchApproveMeasures(codes: string[]): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/measure-catalogs/batch-approve`, {
    method: "POST",
    body: JSON.stringify({ codes }),
  });
}

/** 批量审核驳回逻辑度量（REVIEW → DRAFT，原因必填） */
export async function batchRejectMeasures(codes: string[], reason: string): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/measure-catalogs/batch-reject`, {
    method: "POST",
    body: JSON.stringify({ codes, reason }),
  });
}

/** 批量废弃逻辑度量（PUBLISHED → DEPRECATED） */
export async function batchDeprecateMeasures(codes: string[]): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/measure-catalogs/batch-deprecate`, {
    method: "POST",
    body: JSON.stringify({ codes }),
  });
}

/** 批量重新启用已废弃逻辑度量（DEPRECATED → DRAFT） */
export async function batchReactivateMeasures(codes: string[]): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/measure-catalogs/batch-reactivate`, {
    method: "POST",
    body: JSON.stringify({ codes }),
  });
}

/** 批量软删除逻辑度量（仅 DRAFT/DEPRECATED 可删） */
export async function batchDeleteMeasures(codes: string[]): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/measure-catalogs/batch-delete`, {
    method: "POST",
    body: JSON.stringify({ codes }),
  });
}

// ---- 指标挂载实体（OneData 挂载层，/api/v1/metric-mounts）----
export async function listMetricMounts(params?: {
  metric_id?: number;
  domain?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: MetricMount[]; total: number }> {
  const qs = pageQs({
    metric_id: params?.metric_id,
    domain: params?.domain,
    page: params?.page,
    page_size: params?.page_size ?? 50,
  });
  return request(`${API_BASE}/metric-mounts?${qs}`);
}

export async function deleteMetricMount(mountId: number): Promise<void> {
  return request<void>(`${API_BASE}/metric-mounts/${mountId}`, { method: "DELETE" });
}

/** 新增挂载变体（详情页「追加挂载」）：派生指标可追加多个挂载实体（一指标多变体）。
 *  PUBLISHED 指标新增变体属非破坏（不影响现有消费底座），直接生效。 */
export async function createMetricMount(payload: {
  metric_id: number;
  source_table: string;
  source_column: string;
  granularity: string;
  granularity_dims?: string[] | null;
  default_period?: string | null;
  domain: string;
  business_filter?: string | null;
  product_owner_id?: number | null;
  tech_owner_id?: number | null;
  dw_developer_id?: number | null;
  product_owner_name?: string | null;
  tech_owner_name?: string | null;
  dw_developer_name?: string | null;
}): Promise<MetricMount> {
  return request<MetricMount>(`${API_BASE}/metric-mounts`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function listDimensionMembers(
  dimCode: string,
): Promise<{ items: DimensionMember[]; total: number }> {
  return request(`${API_BASE}/dimensions/${encodeURIComponent(dimCode)}/members`);
}

export async function createDimensionMember(body: {
  dim_code: string;
  member_code?: string;
  member_name: string;
  parent_code?: string | null;
  // path 由服务端按父级独占推导，前端不传
  attributes?: Record<string, unknown> | null;
}): Promise<DimensionMember> {
  return request<DimensionMember>(
    `${API_BASE}/dimensions/${encodeURIComponent(body.dim_code)}/members`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

/** 编辑维度成员（PUT /dimensions/{dim_code}/members/{member_code}） */
export async function updateDimensionMember(body: {
  dim_code: string;
  member_code: string;
  member_name?: string;
  parent_code?: string | null;
  // path 由服务端按父级独占推导，前端不传
  attributes?: Record<string, unknown> | null;
  status?: string;
}): Promise<DimensionMember> {
  return request<DimensionMember>(
    `${API_BASE}/dimensions/${encodeURIComponent(body.dim_code)}/members/${encodeURIComponent(body.member_code)}`,
    {
      method: "PUT",
      body: JSON.stringify(body),
    },
  );
}

/** 维度详情（GET /dimensions/{dim_code}） */
export async function getDimension(dimCode: string): Promise<Dimension> {
  return request<Dimension>(`${API_BASE}/dimensions/${encodeURIComponent(dimCode)}`);
}

/** 更新维度基础信息（PUT /dimensions/{dim_code}，可改 name/domain/type/description/dim_code；dim_code 仅 DRAFT 状态可改，由后端强校验） */
export async function updateDimension(
  dimCode: string,
  body: {
    name?: string;
    domain?: string;
    type?: string;
    description?: string | null;
    dim_code?: string;
  },
  rowVersion?: number,
): Promise<Dimension> {
  return request<Dimension>(`${API_BASE}/dimensions/${encodeURIComponent(dimCode)}`, {
    method: "PUT",
    // row_version 乐观锁透传（后端 409 防并发覆盖；缺省 undefined 兼容旧调用）
    body: JSON.stringify({ ...body, row_version: rowVersion ?? undefined }),
  });
}

/** 绑定指标到维度（POST /dimensions/{dim_code}/metrics，role 如 partition/filter/group） */
export async function bindMetricDimension(body: {
  metric_id: number;
  dim_code: string;
  role: string;
  default_member?: string | null;
}): Promise<MetricDimension> {
  return request<MetricDimension>(
    `${API_BASE}/dimensions/${encodeURIComponent(body.dim_code)}/metrics`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

/** 解除指标-维度绑定（DELETE /dimensions/{dim_code}/metrics/{metric_id}） */
export async function unbindMetricDimension(dimCode: string, metricId: number): Promise<void> {
  return request<void>(
    `${API_BASE}/dimensions/${encodeURIComponent(dimCode)}/metrics/${metricId}`,
    { method: "DELETE" },
  );
}

/** 查询某指标已绑定的维度（GET /dimensions/{metric_id}/metric-dimensions） */
export async function listMetricDimensions(
  metricId: number,
): Promise<{ items: MetricDimension[]; total: number }> {
  return request(`${API_BASE}/dimensions/${metricId}/metric-dimensions`);
}

/** 查询维度已绑定的指标（GET /dimensions/{dim_code}/metrics，role 为 PARTITION/SPLICE/FILTER） */
export async function listDimensionMetrics(
  dimCode: string,
): Promise<{ items: DimensionMetricBinding[]; total: number }> {
  return request(`${API_BASE}/dimensions/${encodeURIComponent(dimCode)}/metrics`);
}

/** 从数据源表列拉取去重枚举值（POST /dimensions/preview-values，维度值自动获取） */
export async function previewColumnValues(req: {
  source_id: string;
  table: string;
  column: string;
  limit?: number;
}): Promise<{ values: string[]; total: number; truncated: boolean }> {
  return request<{ values: string[]; total: number; truncated: boolean }>(
    `${API_BASE}/dimensions/preview-values`,
    { method: "POST", body: JSON.stringify(req) },
  );
}

/** 删除维度成员（DELETE /dimensions/{dim_code}/members/{member_code}，可能级联删除子树） */
export async function deleteDimensionMember(
  dimCode: string,
  memberCode: string,
): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(
    `${API_BASE}/dimensions/${encodeURIComponent(dimCode)}/members/${encodeURIComponent(memberCode)}`,
    { method: "DELETE" },
  );
}

/** 发布维度成员（POST /dimensions/{dim}/members/{code}/publish，DRAFT → PUBLISHED） */
export async function publishDimensionMember(
  dimCode: string,
  memberCode: string,
): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(
    `${API_BASE}/dimensions/${encodeURIComponent(dimCode)}/members/${encodeURIComponent(memberCode)}/publish`,
    { method: "POST" },
  );
}

/** 批量发布维度全部 DRAFT 成员（POST /dimensions/{dim}/members/batch-publish，从表导入闭环） */
export async function publishAllDimensionMembers(
  dimCode: string,
): Promise<{ published: number; skipped: number }> {
  return request<{ published: number; skipped: number }>(
    `${API_BASE}/dimensions/${encodeURIComponent(dimCode)}/members/batch-publish`,
    { method: "POST" },
  );
}

/** 废弃维度成员（POST /dimensions/{dim}/members/{code}/deprecate，→ DEPRECATED） */
export async function deprecateDimensionMember(
  dimCode: string,
  memberCode: string,
): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(
    `${API_BASE}/dimensions/${encodeURIComponent(dimCode)}/members/${encodeURIComponent(memberCode)}/deprecate`,
    { method: "POST" },
  );
}

/** 编辑维度映射（PUT /dimensions/mappings/{mapping_id}，仅 mapping_type/expression 可改） */
export async function updateDimensionMapping(
  mappingId: number,
  body: {
    mapping_type?: string;
    expression?: string | null;
  },
): Promise<DimensionMapping> {
  return request<DimensionMapping>(`${API_BASE}/dimensions/mappings/${mappingId}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

/** 删除维度映射（DELETE /dimensions/mappings/{mapping_id}） */
export async function deleteDimensionMapping(mappingId: number): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`${API_BASE}/dimensions/mappings/${mappingId}`, {
    method: "DELETE",
  });
}

// ---- 术语表 ----
export async function listTerms(params?: {
  domain?: string;
  status?: string;
  search?: string;
  owner_id?: number;
  reviewed_by?: number;
  deleted?: boolean;
  page?: number;
  page_size?: number;
}): Promise<{ items: GlossaryTerm[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    domain: params?.domain,
    status: params?.status,
    search: params?.search,
    owner_id: params?.owner_id,
    reviewed_by: params?.reviewed_by,
    deleted: params?.deleted ? "true" : undefined,
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

/** 提交术语审核（DRAFT → REVIEW，业务用户发布须先审） */
export async function submitTerm(termCode: string, body: ReviewSubmitBody): Promise<GlossaryTerm> {
  return request<GlossaryTerm>(`${API_BASE}/terms/${encodeURIComponent(termCode)}/submit`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** 直接发布术语（平台管理员直发通道，含"再次发布"；业务用户走 submit+approve 审核流） */
export async function publishTerm(termCode: string): Promise<GlossaryTerm> {
  return request<GlossaryTerm>(`${API_BASE}/terms/${encodeURIComponent(termCode)}/publish`, {
    method: "POST",
  });
}

/** 审核通过术语（REVIEW → PUBLISHED） */
export async function approveTerm(
  termCode: string,
  body: { comment?: string | null },
): Promise<GlossaryTerm> {
  return request<GlossaryTerm>(`${API_BASE}/terms/${encodeURIComponent(termCode)}/approve`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** 审核驳回术语（REVIEW → DRAFT，驳回原因必填） */
export async function rejectTerm(termCode: string, body: { reason: string }): Promise<GlossaryTerm> {
  return request<GlossaryTerm>(`${API_BASE}/terms/${encodeURIComponent(termCode)}/reject`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function deprecateTerm(termCode: string): Promise<GlossaryTerm> {
  return request<GlossaryTerm>(`${API_BASE}/terms/${encodeURIComponent(termCode)}/deprecate`, {
    method: "POST",
  });
}

/** 重新启用已废弃术语（DEPRECATED → DRAFT，可编辑后重新走审核） */
export async function reactivateTerm(termCode: string): Promise<GlossaryTerm> {
  return request<GlossaryTerm>(`${API_BASE}/terms/${encodeURIComponent(termCode)}/reactivate`, {
    method: "POST",
  });
}

/** 软删除术语（仅 DRAFT/DEPRECATED 可删；审核中/启用中禁止） */
export async function deleteTerm(termCode: string): Promise<GlossaryTerm> {
  return request<GlossaryTerm>(`${API_BASE}/terms/${encodeURIComponent(termCode)}/delete`, {
    method: "POST",
  });
}

/** 恢复已软删术语（回收站恢复） */
export async function restoreTerm(termCode: string): Promise<GlossaryTerm> {
  return request<GlossaryTerm>(`${API_BASE}/terms/${encodeURIComponent(termCode)}/restore`, {
    method: "POST",
  });
}

// 术语详情（GET /terms/{term_code}）
export async function getTerm(termCode: string): Promise<GlossaryTerm> {
  return request<GlossaryTerm>(`${API_BASE}/terms/${encodeURIComponent(termCode)}`);
}

// 更新术语（PUT /terms/{term_code}，字段缺省则不更新；term_code 可编辑编码）
export async function updateTerm(
  termCode: string,
  body: {
    term_code?: string;
    name?: string;
    definition?: string;
    domain?: string;
    synonyms?: string[];
    boundary?: string | null;
  },
  rowVersion?: number,
): Promise<GlossaryTerm> {
  return request<GlossaryTerm>(`${API_BASE}/terms/${encodeURIComponent(termCode)}`, {
    method: "PUT",
    // row_version 乐观锁透传（后端 409 防并发覆盖；缺省 undefined 兼容旧调用）
    body: JSON.stringify({ ...body, row_version: rowVersion ?? undefined }),
  });
}

// 术语 LLM 推断（POST /terms/infer，根据名称推断定义/同义词/边界，不落库）
export interface TermInferResult {
  definition: string;
  synonyms: string[];
  boundary: string | null;
  confidence: number;
}

export async function inferTermSuggestion(name: string): Promise<TermInferResult> {
  return request<TermInferResult>(`${API_BASE}/terms/infer`, {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

// ---- 术语批量治理（TD §13，后端统一 app/api/batch_common）----
// 业务用户批量发布须走 submit+approve 审核流；批量导入/种子走 admin 直发 batch-publish。

/** 批量提交术语审核（DRAFT → REVIEW，可带评审指派） */
export async function batchSubmitTerms(items: BatchSubmitItem[]): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/terms/batch-submit`, {
    method: "POST",
    body: JSON.stringify({ items }),
  });
}

/** 批量发布术语（platform_admin 直发通道，绕过单条审核） */
export async function batchPublishTerms(codes: string[]): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/terms/batch-publish`, {
    method: "POST",
    body: JSON.stringify({ codes }),
  });
}

/** 批量审核通过术语（REVIEW → PUBLISHED） */
export async function batchApproveTerms(codes: string[]): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/terms/batch-approve`, {
    method: "POST",
    body: JSON.stringify({ codes }),
  });
}

/** 批量审核驳回术语（REVIEW → DRAFT，原因必填） */
export async function batchRejectTerms(codes: string[], reason: string): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/terms/batch-reject`, {
    method: "POST",
    body: JSON.stringify({ codes, reason }),
  });
}

/** 批量废弃术语（PUBLISHED → DEPRECATED） */
export async function batchDeprecateTerms(codes: string[]): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/terms/batch-deprecate`, {
    method: "POST",
    body: JSON.stringify({ codes }),
  });
}

/** 批量重新启用已废弃术语（DEPRECATED → DRAFT） */
export async function batchReactivateTerms(codes: string[]): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/terms/batch-reactivate`, {
    method: "POST",
    body: JSON.stringify({ codes }),
  });
}

/** 批量软删除术语（仅 DRAFT/DEPRECATED 可删） */
export async function batchDeleteTerms(codes: string[]): Promise<BatchResult> {
  return request<BatchResult>(`${API_BASE}/terms/batch-delete`, {
    method: "POST",
    body: JSON.stringify({ codes }),
  });
}

// 建立术语关系（POST /terms/{term_code}/relations）
export async function createTermRelation(
  termCode: string,
  body: {
    target_term_id: number;
    relation_type: string;
  },
): Promise<TermRelation> {
  return request<TermRelation>(`${API_BASE}/terms/${encodeURIComponent(termCode)}/relations`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// 查某术语的全部关系（作为源或目标），供术语关系图谱展示（GET /terms/{code}/relations）
export async function listTermRelations(termCode: string): Promise<TermRelationView> {
  return request<TermRelationView>(`${API_BASE}/terms/${encodeURIComponent(termCode)}/relations`);
}

export async function listTermConflicts(
  status?: string,
): Promise<{ items: GlossaryConflict[]; total: number }> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  return request(`${API_BASE}/terms/conflicts${qs}`);
}

export async function resolveTermConflict(
  conflictId: number,
  decision: string,
): Promise<GlossaryConflict> {
  return request<GlossaryConflict>(`${API_BASE}/terms/conflicts/${conflictId}/resolve`, {
    method: "POST",
    body: JSON.stringify({ decision }),
  });
}

// ---- 治理 ----
export async function createRole(body: {
  name: string;
  description?: string | null;
  is_custom?: boolean;
}): Promise<RoleResponse> {
  return request<RoleResponse>(`${API_BASE}/roles`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** 删除自定义角色（backend DELETE /api/v1/roles/{role}；内置/占用角色后端拒绝）。 */
export async function deleteRole(role: string): Promise<{ role: string; deleted: boolean }> {
  return request<{ role: string; deleted: boolean }>(
    `${API_BASE}/roles/${encodeURIComponent(role)}`,
    { method: "DELETE" },
  );
}

/** 动作点注册表（backend GET /api/v1/roles/action-registry；角色管理可视化配置数据源）。 */
export async function listActionRegistry(): Promise<ActionRegistryItem[]> {
  return request<ActionRegistryItem[]>(`${API_BASE}/roles/action-registry`);
}

// ---- 角色权限点配置（RBAC 可配置化，backend /api/v1/roles/*）----

export async function listRolePermissions(): Promise<RolePermissionItem[]> {
  return request<RolePermissionItem[]>(`${API_BASE}/roles`);
}

export async function setRolePermissions(
  role: string,
  actions: string[],
): Promise<RolePermissionItem> {
  return request<RolePermissionItem>(`${API_BASE}/roles/${encodeURIComponent(role)}/permissions`, {
    method: "PUT",
    body: JSON.stringify({ actions }),
  });
}

export async function resetRolePermissions(role: string): Promise<RolePermissionItem> {
  return request<RolePermissionItem>(
    `${API_BASE}/roles/${encodeURIComponent(role)}/permissions`,
    { method: "DELETE" },
  );
}

/** 角色行下拉选项（backend GET /api/v1/roles/options；授权管理「角色」下拉 id→name）。 */
export async function listRoleOptions(): Promise<RoleOption[]> {
  return request<RoleOption[]>(`${API_BASE}/roles/options`);
}

/** 用户按钮权限点（角色继承 + 直挂并集；backend GET /api/v1/users/{id}/permissions）。 */
export async function getUserPermissions(
  userId: number,
): Promise<UserPermissionResponse> {
  return request<UserPermissionResponse>(`${API_BASE}/users/${userId}/permissions`);
}

/** 整表替换用户直挂按钮权限点（backend PUT /api/v1/users/{id}/permissions）。 */
export async function setUserPermissions(
  userId: number,
  body: { actions: string[]; reason?: string | null },
): Promise<UserPermissionResponse> {
  return request<UserPermissionResponse>(`${API_BASE}/users/${userId}/permissions`, {
    method: "PUT",
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
  source_ids?: string[] | null;
  catalog_ids?: number[] | null;
  limit?: number;
}): Promise<unknown> {
  return request(`${API_BASE}/classification/rescan`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** 误报反馈：字段/前缀写入 pii_vocab 豁免词表并重算实体降级（COMPL-3）。 */
export async function reportClassificationFalsePositive(
  catalogId: number,
  body: { column: string; scope: "field" | "prefix"; reason: string },
): Promise<{
  catalog_id: number;
  entity_name: string;
  column: string;
  scope: string;
  exempted_as: string;
  sensitivity_before: string;
  sensitivity_after: string;
  remaining_pii_columns: string[];
}> {
  return request(`${API_BASE}/catalogs/classification/${catalogId}/false-positive`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// ---- 敏感规则配置台（backend /api/v1/sensitive-rules/*）----
export async function listSensitiveRules(): Promise<SensitiveRuleItem[]> {
  return request<SensitiveRuleItem[]>(`${API_BASE}/sensitive-rules`);
}

export async function listSensitiveRuleCategories(): Promise<SensitiveRuleCategory[]> {
  return request<SensitiveRuleCategory[]>(`${API_BASE}/sensitive-rules/categories`);
}

export async function createSensitiveRule(
  body: SensitiveRuleCreate,
): Promise<SensitiveRuleItem> {
  return request<SensitiveRuleItem>(`${API_BASE}/sensitive-rules`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateSensitiveRule(
  ruleId: string,
  body: SensitiveRuleUpdate,
): Promise<SensitiveRuleItem> {
  return request<SensitiveRuleItem>(
    `${API_BASE}/sensitive-rules/${encodeURIComponent(ruleId)}`,
    { method: "PUT", body: JSON.stringify(body) },
  );
}

export async function setSensitiveRuleStatus(
  ruleId: string,
  action: "activate" | "deactivate",
): Promise<SensitiveRuleItem> {
  return request<SensitiveRuleItem>(
    `${API_BASE}/sensitive-rules/${encodeURIComponent(ruleId)}/status?action=${action}`,
    { method: "PATCH" },
  );
}

export async function batchSetSensitiveRuleStatus(
  ruleIds: string[],
  action: "activate" | "deactivate",
): Promise<{ action: string; succeeded: string[]; failed: { rule_id: string; error: string }[] }> {
  return request(`${API_BASE}/sensitive-rules/batch-status`, {
    method: "POST",
    body: JSON.stringify({ rule_ids: ruleIds, action }),
  });
}

export async function batchSetSensitiveRuleConfidence(
  ruleIds: string[],
  confidence: number,
): Promise<{
  confidence: number;
  succeeded: string[];
  failed: { rule_id: string; error: string }[];
}> {
  return request(`${API_BASE}/sensitive-rules/batch-confidence`, {
    method: "POST",
    body: JSON.stringify({ rule_ids: ruleIds, confidence }),
  });
}

export async function deleteSensitiveRule(ruleId: string): Promise<{ detail: string }> {
  return request<{ detail: string }>(
    `${API_BASE}/sensitive-rules/${encodeURIComponent(ruleId)}`,
    { method: "DELETE" },
  );
}

export async function validateSensitiveRegex(pattern: string): Promise<RegexCheckResult> {
  return request<RegexCheckResult>(`${API_BASE}/sensitive-rules/validate-regex`, {
    method: "POST",
    body: JSON.stringify({ pattern }),
  });
}

export async function testSensitiveRule(
  body: SensitiveRuleTestRequest,
): Promise<SensitiveRuleTestResponse> {
  return request<SensitiveRuleTestResponse>(`${API_BASE}/sensitive-rules/test`, {
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

// 手动触发质量检测（POST /quality/events/detect）：命中返回异常事件，未命中返回 null
export async function qualityEventDetect(body: {
  metric_id: number;
  rule_type: string;
  obs_value: number;
  rule_mode?: string | null;
}): Promise<QualityEvent | null> {
  return request<QualityEvent | null>(`${API_BASE}/quality/events/detect`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// Owner 确认已线下修复（POST /quality/events/{event_id}/repair，仅 OPEN 状态可确认）
export async function qualityEventConfirmRepair(eventId: number): Promise<QualityEvent> {
  return request<QualityEvent>(`${API_BASE}/quality/events/${eventId}/repair`, { method: "POST" });
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

// 绑定基准到目标指标（POST /quality/benchmarks/{benchmark_id}/bind，声明比对口径 / 容忍率）
export async function bindBenchmark(
  benchmarkId: number,
  body: {
    metric_code?: string | null;
    tolerance_pct?: number | null;
    dims?: Record<string, unknown> | null;
  },
): Promise<QualityBenchmark> {
  return request<QualityBenchmark>(`${API_BASE}/quality/benchmarks/${benchmarkId}/bind`, {
    method: "POST",
    body: JSON.stringify(body),
  });
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
  return request<ReconciliationRecord>(
    `${API_BASE}/quality/reconciliation-records/${recordId}/confirm`,
    {
      method: "POST",
      body: JSON.stringify({ decision, owner_note: ownerNote ?? null }),
    },
  );
}

// ---- 通知 ----
export async function listNotifications(params?: {
  status?: string;
  read_state?: string;
  template_code?: string;
  todo_only?: boolean;
  days?: number;
  object_key?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: Notification[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    status: params?.status,
    read_state: params?.read_state,
    template_code: params?.template_code,
    todo_only: params?.todo_only ? "true" : undefined,
    days: params?.days,
    object_key: params?.object_key,
    page: params?.page ?? 1,
    page_size: params?.page_size ?? 10,
  });
  return request(`${API_BASE}/notify/notifications${qs ? `?${qs}` : ""}`);
}

/** 当前用户未读通知总数（GET /notify/notifications/unread-count，Header 角标精确计数）。 */
export async function fetchUnreadCount(): Promise<number> {
  const res = await request<{ count: number }>(`${API_BASE}/notify/notifications/unread-count`);
  return res.count;
}

/** 单条标记已读（POST /notify/notifications/{id}/read），返回更新后的通知。 */
export async function markNotificationRead(id: number): Promise<Notification> {
  return request<Notification>(`${API_BASE}/notify/notifications/${id}/read`, {
    method: "POST",
  });
}

/** 当前用户全部标记已读（POST /notify/notifications/read-all）。 */
export async function markAllNotificationsRead(): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`${API_BASE}/notify/notifications/read-all`, {
    method: "POST",
  });
}

/** 删除单条通知（DELETE /notify/notifications/{id}）。 */
export async function deleteNotification(id: number): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`${API_BASE}/notify/notifications/${id}`, {
    method: "DELETE",
  });
}

/** 清空当前用户全部通知（DELETE /notify/notifications）。 */
export async function deleteAllNotifications(): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`${API_BASE}/notify/notifications`, {
    method: "DELETE",
  });
}

/** 重试投递失败的站内通知（POST /notify/notifications/{id}/retry），返回更新后的通知。 */
export async function retryNotification(id: number): Promise<Notification> {
  return request<Notification>(`${API_BASE}/notify/notifications/${id}/retry`, {
    method: "POST",
  });
}

/** 标记待办类通知为「已处理」（POST /notify/notifications/{id}/handled）。 */
export async function markNotificationHandled(id: number): Promise<Notification> {
  return request<Notification>(`${API_BASE}/notify/notifications/${id}/handled`, {
    method: "POST",
  });
}

export async function listNotifyEvents(
  eventType?: string,
): Promise<{ items: NotifyEventLog[]; total: number }> {
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

/** 可订阅事件类型清单（订阅配置下拉数据源，2026-08-28：后端 EventBus 权威来源）。 */
export async function listEventTypes(): Promise<{ items: string[] }> {
  return request(`${API_BASE}/notify/subscriptions/event-types`);
}

export async function upsertSubscription(body: {
  channel: string;
  event_type?: string | null;
  /** 资产维度订阅（按指标/源表 watch）：与 event_type 二选一 */
  asset_type?: string;
  asset_id?: string;
  enabled?: boolean;
  threshold?: number | null;
}): Promise<SubscriptionPref> {
  return request<SubscriptionPref>(`${API_BASE}/notify/subscriptions`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

// ---- 可观测 ----
export async function listFeedback(params?: {
  target_type?: string;
  /** 过滤：adopted/rejected/in_progress/pending */
  status?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: Feedback[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    target_type: params?.target_type,
    status: params?.status,
    page: params?.page ?? 1,
    page_size: params?.page_size ?? 20,
  });
  return request(`${API_BASE}/observability/feedback?${qs}`);
}

export async function submitFeedback(body: {
  target_type: string;
  target_id?: string | null;
  rating?: number | null;
  comment?: string | null;
  /** 反馈分类（bug/feature/improvement/question/praise），默认 improvement */
  category?: string;
  /** 反馈优先级（high/medium/low），默认 medium */
  priority?: string;
  /** 反馈来源页面 URL（前端自动捕获当前路由，不要求用户填写） */
  source_url?: string | null;
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

/** 质疑闭环：反馈提交人在 clarifying 状态提交澄清说明（仅本人可澄清） */
export async function clarifyFeedback(feedbackId: number, clarification: string): Promise<Feedback> {
  return request<Feedback>(`${API_BASE}/observability/feedback/${feedbackId}/clarify`, {
    method: "POST",
    body: JSON.stringify({ clarification }),
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

/** NPS 分布统计（GET /observability/nps/stats）：total/promoters/passives/detractors/score */
export async function fetchNpsStats(): Promise<NpsStats> {
  return request<NpsStats>(`${API_BASE}/observability/nps/stats`);
}

/** 最近质量事件明细（GET /observability/quality-events，运营大盘明细面板） */
export async function fetchObsQualityEvents(
  limit = 20,
): Promise<{ items: QualityEventItem[]; total: number }> {
  return request(`${API_BASE}/observability/quality-events?limit=${limit}`);
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

export async function fetchObsOverview(): Promise<ObsOverview> {
  return request<ObsOverview>(`${API_BASE}/observability/overview`);
}

// ---- 推荐 ----
export async function fetchRecommendedMetrics(limit = 20): Promise<RecommendItem[]> {
  return request<{ items: RecommendItem[]; total: number }>(
    `${API_BASE}/recommend/metrics?limit=${limit}`,
  ).then((r) => r.items);
}

export async function fetchRelatedMetrics(
  metricId: number | string,
  limit = 20,
): Promise<RecommendItem[]> {
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

// ---- LLM 配置（多实例轮询路由）----
export async function getLlmConfigs(): Promise<LlmConfigList> {
  return request<LlmConfigList>(`${API_BASE}/ai/config`);
}

export async function getLlmConfigSecret(id: number): Promise<LlmConfigSecret> {
  return request<LlmConfigSecret>(`${API_BASE}/ai/config/${id}/secret`);
}

export async function createLlmConfig(body: LlmConfigPayload): Promise<{ id: number }> {
  return request<{ id: number }>(`${API_BASE}/ai/config`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateLlmConfig(
  id: number,
  body: LlmConfigPayload,
): Promise<{ id: number }> {
  return request<{ id: number }>(`${API_BASE}/ai/config/${id}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export async function deleteLlmConfig(id: number): Promise<{ id: number }> {
  return request<{ id: number }>(`${API_BASE}/ai/config/${id}`, {
    method: "DELETE",
  });
}

export async function testLlmConfig(body?: {
  instance_id?: number;
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

export async function fetchLlmModels(body?: {
  instance_id?: number;
  base_url?: string;
  api_key?: string;
  timeout?: number;
  provider?: string;
}): Promise<LlmModelsResult> {
  return request<LlmModelsResult>(`${API_BASE}/ai/config/models`, {
    method: "POST",
    body: JSON.stringify(body ?? {}),
  });
}

// ---- 审计 ----
export async function listAudit(params?: {
  actor_id?: number;
  actor_keyword?: string;
  entity_type?: string;
  entity_id?: string;
  trace_id?: string;
  pii_access?: boolean;
  page?: number;
  page_size?: number;
}): Promise<{ items: AuditEntry[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    actor_id: params?.actor_id,
    actor_keyword: params?.actor_keyword,
    entity_type: params?.entity_type,
    entity_id: params?.entity_id,
    trace_id_filter: params?.trace_id,
    pii_access: params?.pii_access === undefined ? undefined : params.pii_access ? "true" : "false",
    page: params?.page ?? 1,
    page_size: params?.page_size ?? 20,
  });
  return request(`${API_BASE}/audit?${qs}`);
}

// 审计日志导出（CSV/JSON，合规留档；下载文件）
export async function exportAudit(params?: {
  actor_id?: number;
  actor_keyword?: string;
  entity_type?: string;
  entity_id?: string;
  trace_id?: string;
  pii_access?: boolean;
  format?: "csv" | "json";
  limit?: number;
}): Promise<void> {
  const qs = pageQs({
    actor_id: params?.actor_id,
    actor_keyword: params?.actor_keyword,
    entity_type: params?.entity_type,
    entity_id: params?.entity_id,
    trace_id_filter: params?.trace_id,
    pii_access: params?.pii_access === undefined ? undefined : params.pii_access ? "true" : "false",
    format: params?.format ?? "csv",
    limit: params?.limit ?? 5000,
  });
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE_URL}${API_BASE}/audit/export?${qs}`, { headers });
  if (!res.ok) {
    throw new UnisenseApiError(`导出失败 (HTTP ${res.status})`, "HTTP_ERROR", res.status, "");
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `audit_export_${new Date().toISOString().slice(0, 10)}.${params?.format ?? "csv"}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
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

// 埋点统计聚合（GET /tracking/stats，需 platform_admin/domain_admin 角色）
export async function fetchTrackingStats(params?: {
  event_type?: string;
  /** YYYY-MM-DD */
  start_date?: string;
  /** YYYY-MM-DD */
  end_date?: string;
  group_by?: TrackingGroupBy;
}): Promise<TrackingStatsResponse> {
  const qs = pageQs({
    event_type: params?.event_type,
    start_date: params?.start_date,
    end_date: params?.end_date,
    group_by: params?.group_by,
  });
  return request<TrackingStatsResponse>(`${API_BASE}/tracking/stats${qs ? `?${qs}` : ""}`);
}

// ---- 采集器 ----
export async function listDataSources(params?: {
  domain?: string;
  source_type?: SourceType;
  keyword?: string;
  /** 健康状态过滤（总览仪表「数据源」资产卡片下钻：healthy/unhealthy/unknown） */
  health?: string;
  /** 责任人（Owner）ID 过滤（总览仪表 Owner 责任分布下钻） */
  owner_id?: number;
  /** 源状态过滤：deleted=已软删源，其余默认活跃源（采集目录数据源筛选下拉联动） */
  source_status?: string;
  page?: number;
  page_size?: number;
}): Promise<DataSourceListResponse> {
  const qs = pageQs({
    domain: params?.domain,
    source_type: params?.source_type,
    keyword: params?.keyword,
    health_status: params?.health,
    owner_id: params?.owner_id,
    source_status: params?.source_status,
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

export async function updateDataSource(
  sourceId: string,
  req: DataSourceUpdateRequest,
): Promise<DataSource> {
  return request<DataSource>(`${API_BASE}/data-sources/${encodeURIComponent(sourceId)}`, {
    method: "PUT",
    body: JSON.stringify(req),
  });
}

export async function deleteDataSource(sourceId: string): Promise<void> {
  await request<void>(`${API_BASE}/data-sources/${encodeURIComponent(sourceId)}`, {
    method: "DELETE",
  });
}

export async function batchToggleDataSources(
  sourceIds: string[],
  enabled: boolean,
): Promise<BatchSourceResult> {
  return request<BatchSourceResult>(`${API_BASE}/data-sources/batch-toggle`, {
    method: "POST",
    body: JSON.stringify({ source_ids: sourceIds, enabled } satisfies BatchToggleRequest),
  });
}

export async function batchDeleteDataSources(sourceIds: string[]): Promise<BatchSourceResult> {
  return request<BatchSourceResult>(`${API_BASE}/data-sources/batch-delete`, {
    method: "POST",
    body: JSON.stringify({ source_ids: sourceIds } satisfies BatchDeleteRequest),
  });
}

/** 批量探活（用已存连接配置逐条 probe，207 语义） */
export async function batchTestDataSources(sourceIds: string[]): Promise<BatchSourceResult> {
  return request<BatchSourceResult>(`${API_BASE}/data-sources/batch-test`, {
    method: "POST",
    body: JSON.stringify({ source_ids: sourceIds }),
  });
}

/** 批量设置调度 cron（207 语义） */
export async function batchScheduleDataSources(
  sourceIds: string[],
  scheduleCron: string,
): Promise<BatchSourceResult> {
  return request<BatchSourceResult>(`${API_BASE}/data-sources/batch-schedule`, {
    method: "POST",
    body: JSON.stringify({ source_ids: sourceIds, schedule_cron: scheduleCron }),
  });
}

/** 数据源资产规模概览（实体/PII 分布、字段数、漂移、水位） */
export async function getSourceOverview(sourceId: string): Promise<SourceOverview> {
  return request<SourceOverview>(`${API_BASE}/data-sources/${encodeURIComponent(sourceId)}/overview`);
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

export async function collectSource(sourceId: string, mode = "FULL"): Promise<CollectResult> {
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

/** 枚举指定库下的表（按库分组，创建数据源时级联选表）。 */
export async function listDataSourceTables(req: {
  source_type: SourceType;
  connection_config: Record<string, unknown>;
  databases: string[];
}): Promise<ListTablesResult> {
  return request<ListTablesResult>(`${API_BASE}/data-sources/tables`, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

/** 异步立即采集（返回 job_id，进度走 SSE / 轮询任务状态）。
 *
 * ``filters`` 为本次临时表级过滤（仅本次采集生效，不污染数据源配置）；
 * 不传/空时 worker 回退到数据源配置的白黑名单。
 */
export async function collectSourceNow(
  sourceId: string,
  mode = "FULL",
  filters?: { include_patterns?: string[]; exclude_patterns?: string[] },
): Promise<CollectNowResult> {
  const body: Record<string, unknown> = { collector_type: "information_schema", mode };
  if (filters?.include_patterns?.length) body.include_patterns = filters.include_patterns;
  if (filters?.exclude_patterns?.length) body.exclude_patterns = filters.exclude_patterns;
  return request<CollectNowResult>(
    `${API_BASE}/data-sources/${encodeURIComponent(sourceId)}/collect-now`,
    {
      method: "POST",
      body: JSON.stringify(body),
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
  scheduleEnabled?: boolean,
): Promise<ScheduleResult> {
  const body: Record<string, unknown> = { cron };
  if (scheduleEnabled !== undefined) body.schedule_enabled = scheduleEnabled;
  return request<ScheduleResult>(
    `${API_BASE}/data-sources/${encodeURIComponent(sourceId)}/schedule`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

export async function getSourceHealth(sourceId: string): Promise<SourceHealth> {
  return request<SourceHealth>(`${API_BASE}/data-sources/${encodeURIComponent(sourceId)}/health`);
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

/** 采集任务中心：服务端分页列出异步采集任务（按入队逆序；可按 source_id 过滤）。 */
export async function listCollectionJobs(params?: {
  limit?: number;
  offset?: number;
  source_id?: string;
  /** 任务状态下钻（总览仪表「采集任务」资产卡片：QUEUED/RUNNING/COMPLETED/FAILED） */
  status?: string;
}): Promise<{ items: CollectionJob[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    limit: params?.limit ?? 50,
    offset: params?.offset ?? 0,
    source_id: params?.source_id ?? undefined,
    status: params?.status ?? undefined,
  });
  return request<{ items: CollectionJob[]; total: number; page: number; page_size: number }>(
    `${API_BASE}/data-sources/jobs?${qs}`,
  );
}

/** 查询单个采集任务状态。 */
export async function getCollectionJob(jobId: string): Promise<CollectionJob | null> {
  return request<CollectionJob | null>(
    `${API_BASE}/data-sources/jobs/${encodeURIComponent(jobId)}`,
  );
}

/** 取消异步采集任务（POST /data-sources/jobs/{job_id}/cancel；已终态幂等返回 canceled=false）。 */
export async function cancelCollectionJob(jobId: string): Promise<{ job_id: string; canceled: boolean }> {
  return request<{ job_id: string; canceled: boolean }>(
    `${API_BASE}/data-sources/jobs/${encodeURIComponent(jobId)}/cancel`,
    { method: "POST" },
  );
}

/** 采集运行历史：分页列出（采集记录页主视图，持久化历史含失败/排障明细）。 */
export async function listCollectionRuns(params?: {
  source_id?: string;
  status?: string;
  trigger?: string;
  started_after?: string;
  started_before?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: CollectionRun[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    source_id: params?.source_id ?? undefined,
    status: params?.status ?? undefined,
    trigger: params?.trigger ?? undefined,
    started_after: params?.started_after ?? undefined,
    started_before: params?.started_before ?? undefined,
    page: params?.page ?? 1,
    page_size: params?.page_size ?? 20,
  });
  return request<{ items: CollectionRun[]; total: number; page: number; page_size: number }>(
    `${API_BASE}/collection-runs?${qs}`,
  );
}

/** 采集运行历史聚合统计（服务端 SQL 聚合：总数/成功/失败/扫描/注册）。 */
export async function getCollectionRunSummary(params?: {
  source_id?: string;
  status?: string;
  trigger?: string;
  started_after?: string;
  started_before?: string;
}): Promise<{
  total: number;
  completed: number;
  failed: number;
  scanned: number;
  registered: number;
}> {
  const qs = pageQs({
    source_id: params?.source_id ?? undefined,
    status: params?.status ?? undefined,
    trigger: params?.trigger ?? undefined,
    started_after: params?.started_after ?? undefined,
    started_before: params?.started_before ?? undefined,
  });
  return request<{
    total: number;
    completed: number;
    failed: number;
    scanned: number;
    registered: number;
  }>(`${API_BASE}/collection-runs/summary?${qs}`);
}

/** 采集运行详情（含失败实体 / 漂移事件 / 降级原因明细）。 */
export async function getCollectionRunDetail(runId: number): Promise<CollectionRun> {
  return request<CollectionRun>(`${API_BASE}/collection-runs/${runId}`);
}

/** 采集运行日志分页（采集记录详情页「实时日志」）。
 *
 * 读取策略：终态（已回写 DB）读 collection_run_log 表；RUNNING 中或崩溃未收尾
 * 读 Redis 实时缓冲。返回 source（db/redis/none）与 status 供前端决定是否轮询。
 */
export async function getCollectionRunLogs(
  runId: number,
  params?: { offset?: number; limit?: number },
): Promise<{
  items: CollectionRunLogItem[];
  total: number;
  source: "db" | "redis" | "none" | string;
  status: string;
}> {
  const qs = pageQs({ offset: params?.offset ?? 0, limit: params?.limit ?? 200 });
  return request<{
    items: CollectionRunLogItem[];
    total: number;
    source: "db" | "redis" | "none" | string;
    status: string;
  }>(`${API_BASE}/collection-runs/${runId}/logs?${qs}`);
}

export async function getSourceWatermark(sourceId: string): Promise<Watermark> {
  return request<Watermark>(`${API_BASE}/data-sources/${encodeURIComponent(sourceId)}/watermark`);
}

export async function listCatalogs(params?: {
  source_id?: string;
  entity_type?: string;
  sensitivity_level?: string;
  /** 业务域过滤（经数据源继承）——热力下钻"域+敏感度"双过滤 */
  domain?: string;
  /** 库名（entity_name 前缀过滤） */
  database?: string;
  keyword?: string;
  /** active=仅活跃源 / deleted=仅已删除源 / 不传=全部 */
  source_status?: "active" | "deleted";
  /** 责任人（Owner）ID 过滤（总览仪表 Owner 责任分布下钻） */
  owner_id?: number;
  /** 仅未复核敏感资产（PII/CONFIDENTIAL 且未合规复核）——PII 合规卡下钻 */
  pending_review?: boolean;
  page?: number;
  page_size?: number;
}): Promise<{ items: DBCatalog[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    source_id: params?.source_id,
    entity_type: params?.entity_type,
    sensitivity_level: params?.sensitivity_level,
    domain: params?.domain,
    database: params?.database,
    keyword: params?.keyword,
    source_status: params?.source_status,
    owner_id: params?.owner_id,
    pending_review:
      params?.pending_review === undefined ? undefined : String(params.pending_review),
    page: params?.page ?? 1,
    page_size: params?.page_size ?? 20,
  });
  return request(`${API_BASE}/catalogs?${qs}`);
}

/** 按主键取目录实体详情（血缘图谱表节点下钻展示用）。 */
export async function getCatalogDetail(catalogId: number): Promise<DBCatalog> {
  return request<DBCatalog>(`${API_BASE}/catalogs/${catalogId}`);
}

/** 目录去重库名列表（供库名筛选下拉，可随 source_id / source_status 联动）。 */
export async function listCatalogDatabases(sourceId?: string, sourceStatus?: string): Promise<string[]> {
  const qs = pageQs({ source_id: sourceId, source_status: sourceStatus });
  const res = await request<{ items: string[] }>(`${API_BASE}/catalogs/databases?${qs}`);
  return res.items;
}

/** 单实体元数据刷新：只采集该表/实体，不触发全源扫描（生产运维入口）。 */
export async function refreshCatalogEntity(
  sourceId: string,
  entityName: string,
): Promise<{
  source_id: string;
  entity_name: string;
  sensitivity_level: string;
  drifted: boolean;
  columns: number;
}> {
  return request(`${API_BASE}/data-sources/${encodeURIComponent(sourceId)}/entities/${encodeURIComponent(entityName)}/refresh`, {
    method: "POST",
  });
}

/** 采样覆盖率：已采样表数/列数与占比（PII 识别精度可观测性）。 */
export interface SamplingCoverage {
  source_id: string | null;
  total_entities: number;
  sampled_entities: number;
  entity_coverage: number;
  total_columns: number;
  sampled_columns: number;
  column_coverage: number;
  /** 经 name+sample 双重验证的列数（精度高于纯名称推断） */
  verified_columns: number;
}

export async function fetchSamplingCoverage(
  sourceId?: string,
): Promise<SamplingCoverage> {
  const path = sourceId
    ? `${API_BASE}/data-sources/${encodeURIComponent(sourceId)}/sampling-coverage`
    : `${API_BASE}/data-sources/sampling-coverage`;
  return request<SamplingCoverage>(path);
}

/** 单表立即采样：不重跑全量采集，只补采样本并重算字段级 PII。 */
export async function sampleCatalogEntity(
  sourceId: string,
  entityName: string,
  sampleRows?: number,
): Promise<{
  source_id: string;
  entity_name: string;
  columns: number;
  sampled: number;
  sample_rows: number;
  sensitivity_level: string;
  pii_hits: number;
  new_pii_columns: string[];
  cleared_pii_columns: string[];
  /** 源端编码乱码字段（GBK→UTF-8 替换残留，需源端修复后重采） */
  mojibake_fields: string[];
}> {
  const qs = sampleRows ? `?sample_rows=${encodeURIComponent(String(sampleRows))}` : "";
  return request(
    `${API_BASE}/data-sources/${encodeURIComponent(sourceId)}/entities/${encodeURIComponent(entityName)}/sample${qs}`,
    { method: "POST" },
  );
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
): Promise<{
  succeeded: Array<{ source_id: string; entity_name: string }>;
  failed: Array<Record<string, unknown>>;
}> {
  return request(`${API_BASE}/catalogs/bulk-deprecate`, {
    method: "POST",
    body: JSON.stringify({ items }),
  });
}

// ---- 字段描述推断 + 人工编辑 ----

export interface InferDescriptionResult {
  column_name: string;
  description: string;
  source: string;
  confidence: number;
}

export interface InferBatchResult {
  inferred: InferDescriptionResult[];
  skipped: string[];
  failed: string[];
}

export interface UpdateDescriptionResult {
  catalog_id: number;
  column_name: string;
  description: string;
  source: string;
  updated_by: number | null;
  updated_at: string;
}

/** LLM 推断单字段描述 */
export async function inferColumnDescription(
  catalogId: number,
  columnName: string,
  params: { entity_name: string; column_type?: string; force?: boolean },
): Promise<InferDescriptionResult> {
  return request<InferDescriptionResult>(
    `${API_BASE}/catalogs/${catalogId}/columns/${encodeURIComponent(columnName)}/infer-description`,
    {
      method: "POST",
      body: JSON.stringify(params),
    },
  );
}

/** 批量推断缺失描述 */
export async function inferDescriptions(catalogId: number): Promise<InferBatchResult> {
  return request<InferBatchResult>(
    `${API_BASE}/catalogs/${catalogId}/infer-descriptions`,
    { method: "POST" },
  );
}

/** 人工编辑字段描述 */
export async function updateColumnDescription(
  catalogId: number,
  columnName: string,
  description: string,
): Promise<UpdateDescriptionResult> {
  return request<UpdateDescriptionResult>(
    `${API_BASE}/catalogs/${catalogId}/columns/${encodeURIComponent(columnName)}/description`,
    {
      method: "PUT",
      body: JSON.stringify({ description }),
    },
  );
}

// ---- 表级业务描述 + 描述缺失统计（TD §12.1） ----

export interface TableCoverageItem {
  catalog_id: number;
  entity_name: string;
  source_id: string;
  source_name?: string | null;
  entity_type: string;
  domain: string | null;
  sensitivity_level: string;
  table_desc: boolean;
  description?: string | null;
  description_source?: string | null;
  owner_name?: string | null;
  total_fields: number;
  covered_fields: number;
  missing_fields: number;
  missing_field_names?: string[];
  updated_at?: string | null;
}

export interface DescriptionCoverage {
  total_tables: number;
  tables_with_desc: number;
  tables_missing_desc: number;
  total_fields: number;
  fields_with_desc: number;
  fields_missing_desc: number;
  per_table: TableCoverageItem[];
}

export interface TableDescriptionResult {
  catalog_id: number;
  description: string;
  source: string;
  updated_by: number | null;
  updated_at: string | null;
}

export interface InferTableDescriptionResult {
  catalog_id: number;
  description: string;
  source: string;
  confidence: number;
}

/** 描述缺失统计（资产地图「描述缺失」tab / 采集目录概览卡）。
 *  params.source_id / params.database / params.keyword：治理面板「按数据源、库、表筛选治理」的服务端过滤。 */
export async function fetchDescriptionCoverage(params?: {
  source_id?: string;
  database?: string;
  keyword?: string;
}): Promise<DescriptionCoverage> {
  const qs = pageQs({
    source_id: params?.source_id,
    database: params?.database,
    keyword: params?.keyword,
  });
  return request<DescriptionCoverage>(
    `${API_BASE}/catalogs/description-coverage${qs ? `?${qs}` : ""}`,
  );
}

/** 人工编辑表级描述 */
export async function updateTableDescription(
  catalogId: number,
  description: string,
): Promise<TableDescriptionResult> {
  return request<TableDescriptionResult>(
    `${API_BASE}/catalogs/${catalogId}/description`,
    {
      method: "PUT",
      body: JSON.stringify({ description }),
    },
  );
}

/** LLM 推断表级描述 */
export async function inferTableDescription(
  catalogId: number,
  fields?: Array<{ name?: string; type?: string }>,
  force?: boolean,
): Promise<InferTableDescriptionResult> {
  return request<InferTableDescriptionResult>(
    `${API_BASE}/catalogs/${catalogId}/infer-table-description`,
    {
      method: "POST",
      body: JSON.stringify({ fields: fields ?? [], force: force ?? false }),
    },
  );
}

// ---- 跨表批量 LLM 推断历史（服务端持久化，跨设备/团队可见） ----

export interface BatchInferHistoryTable {
  catalog_id: number;
  entity_name: string;
}

export interface BatchInferHistoryEntry {
  id: number;
  actor_id: number | null;
  actor_name: string | null;
  tables: BatchInferHistoryTable[];
  done: number;
  failed: number;
  cancelled: number;
  added: number;
  elapsed: number;
  failed_tables: BatchInferHistoryTable[];
  created_at: string;
}

export interface BatchInferHistoryCreate {
  tables: BatchInferHistoryTable[];
  done: number;
  failed: number;
  cancelled: number;
  added: number;
  elapsed: number;
  failed_tables: BatchInferHistoryTable[];
}

/** 读取服务端批量推断历史（按时间倒序，团队可见）。 */
export async function fetchBatchInferHistory(limit = 20): Promise<BatchInferHistoryEntry[]> {
  return request<BatchInferHistoryEntry[]>(
    `${API_BASE}/catalogs/batch-infer-history?limit=${limit}`,
  );
}

/** 写入一条批量推断历史（服务端自动裁剪到近 20 条）。 */
export async function createBatchInferHistory(
  body: BatchInferHistoryCreate,
): Promise<BatchInferHistoryEntry> {
  return request<BatchInferHistoryEntry>(`${API_BASE}/catalogs/batch-infer-history`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** 清空当前用户自己的批量推断历史（团队他人记录保留）。 */
export async function clearBatchInferHistory(): Promise<{ cleared: number }> {
  return request<{ cleared: number }>(`${API_BASE}/catalogs/batch-infer-history`, {
    method: "DELETE",
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

export async function fetchAssetMetricDimensions(): Promise<AssetMetricDimensionSummary> {
  return request<AssetMetricDimensionSummary>(`${API_BASE}/assetmap/metric-dimensions`);
}

export async function fetchAssetTables(params?: {
  source_id?: string;
  sensitivity?: string;
  /** 业务域（经数据源继承过滤） */
  domain?: string;
  /** 责任人（Owner）ID 过滤 */
  owner_id?: number;
  /** Schema 完整性：complete / incomplete */
  schema_status?: "complete" | "incomplete";
  /** 库名（entity_name 前缀，如 wedw_dws） */
  database?: string;
  /** 关键字：表名或数据源模糊搜索 */
  keyword?: string;
  limit?: number;
  /** 偏移分页（与后端 total 配合做服务端分页，P2-1） */
  offset?: number;
}): Promise<{ items: AssetTableItem[]; total: number }> {
  const qs = pageQs({
    source_id: params?.source_id,
    sensitivity: params?.sensitivity,
    domain: params?.domain,
    owner_id: params?.owner_id,
    schema_status: params?.schema_status,
    database: params?.database,
    keyword: params?.keyword,
    limit: params?.limit ?? 100,
    offset: params?.offset,
  });
  return request(`${API_BASE}/assetmap/tables?${qs}`);
}

export async function fetchAssetOrphans(params?: {
  keyword?: string;
  source_id?: string;
  /** 业务域（经数据源继承过滤） */
  domain?: string;
  /** 实体类型：table / view / field 等 */
  entity_type?: string;
  sensitivity?: string;
  /** Schema 完整性：complete / incomplete */
  schema_status?: "complete" | "incomplete";
  /** 库名（entity_name 前缀，如 wedw_dws） */
  database?: string;
  limit?: number;
  /** 偏移分页（与后端 total 配合做服务端分页，P2-1） */
  offset?: number;
}): Promise<{ items: AssetTableItem[]; total: number }> {
  const qs = pageQs({
    keyword: params?.keyword,
    source_id: params?.source_id,
    domain: params?.domain,
    entity_type: params?.entity_type,
    sensitivity: params?.sensitivity,
    schema_status: params?.schema_status,
    database: params?.database,
    limit: params?.limit ?? 200,
    offset: params?.offset,
  });
  return request(`${API_BASE}/assetmap/orphans?${qs}`);
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
// assetType：catalog=目录资产 / metric=指标资产（指标视角列 = INTERNAL/PII）
export async function fetchAssetHeatmapMatrix(
  assetType: "catalog" | "metric" = "catalog",
): Promise<AssetHeatmapMatrix> {
  const qs = pageQs({ asset_type: assetType });
  return request<AssetHeatmapMatrix>(`${API_BASE}/assetmap/heatmap-matrix?${qs}`);
}

export async function fetchAssetOwnerView(ownerId: number): Promise<AssetOwnerView> {
  return request<AssetOwnerView>(`${API_BASE}/assetmap/owner-view?owner_id=${ownerId}`);
}

// 实体详情：返回表/字段详情（schema 摘要/敏感度/PII/Owner/血缘边数）
export async function fetchAssetEntityDetail(entityId: number): Promise<AssetEntityDetail> {
  return request<AssetEntityDetail>(`${API_BASE}/assetmap/entities/${entityId}`);
}

// ---- 资产工作台写能力（FR-18）：责任人设置 / 敏感度重分类 / 批量操作 ----
// 写接口 RBAC 仅限 platform_admin / domain_admin（后端把关），此处仅提供入口封装。

/** 认领/转让资产归属（ownerId=null 表示解除归属，回到孤儿池） */
export async function assignAssetOwner(
  entityId: number,
  ownerId: number | null,
): Promise<{ entity_id: number; owner_id: number | null }> {
  return request(`${API_BASE}/assetmap/entities/${entityId}/owner`, {
    method: "POST",
    body: JSON.stringify({ owner_id: ownerId }),
  });
}

/** 重分类资产敏感级（仅允许枚举值：PUBLIC/INTERNAL/CONFIDENTIAL/PII/NEEDS_REVIEW） */
export async function reclassifyAssetSensitivity(
  entityId: number,
  sensitivityLevel: string,
): Promise<{ entity_id: number; sensitivity_level: string }> {
  return request(`${API_BASE}/assetmap/entities/${entityId}/sensitivity`, {
    method: "POST",
    body: JSON.stringify({ sensitivity_level: sensitivityLevel }),
  });
}

/** 批量认领/转让归属（单次 ≤200，同事务原子提交） */
export async function batchAssignAssetOwner(
  entityIds: number[],
  ownerId: number | null,
): Promise<{ affected: number; owner_id: number | null; total: number }> {
  return request(`${API_BASE}/assetmap/batch-owner`, {
    method: "POST",
    body: JSON.stringify({ entity_ids: entityIds, owner_id: ownerId }),
  });
}

/** 批量重分类敏感级（单次 ≤200，同事务原子提交） */
export async function batchReclassifyAssetSensitivity(
  entityIds: number[],
  sensitivityLevel: string,
): Promise<{ affected: number; sensitivity_level: string; total: number }> {
  return request(`${API_BASE}/assetmap/batch-sensitivity`, {
    method: "POST",
    body: JSON.stringify({ entity_ids: entityIds, sensitivity_level: sensitivityLevel }),
  });
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
export async function fetchGlobalSearch(q: string, limit = 5): Promise<GlobalSearchResponse> {
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

// ---- PII 合规增强（A/B/C）：明细列表 / 模板 / 表级复核 / 脱敏 / 标注 / 保留期 / 导出 ----

/** PII 资产明细列表（分页 + 多维度筛选，PII 合规 Tab 可下钻） */
export async function fetchAssetPiiAssets(params?: {
  keyword?: string;
  source_id?: string;
  domain?: string;
  /** 0=无主 PII（最高优先级合规风险） */
  owner_id?: number;
  /** unreviewed / reviewed */
  review_status?: string;
  /** PII 类别（ID_CARD/PHONE/...） */
  category?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: AssetPiiAssetItem[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    keyword: params?.keyword,
    source_id: params?.source_id,
    domain: params?.domain,
    owner_id: params?.owner_id,
    review_status: params?.review_status,
    category: params?.category,
    page: params?.page ?? 1,
    page_size: params?.page_size ?? 20,
  });
  return request(`${API_BASE}/assetmap/pii-assets?${qs}`);
}

/** 行业分级模板列表（PII 合规盘点与批量升级） */
export async function fetchAssetPiiTemplates(): Promise<{ items: AssetPiiTemplate[]; total: number }> {
  return request(`${API_BASE}/assetmap/pii/templates`);
}

/** 应用行业分级模板（按字段类别升级资产敏感级，个保法/金融等） */
export async function applyAssetPiiTemplate(body: {
  template_id: string;
  catalog_ids?: number[];
  source_id?: string;
  all_pii?: boolean;
}): Promise<{
  template_id: string;
  applied: number;
  changed: number;
  items: Array<{ entity_id: number; entity_name: string; changed: boolean; applied_categories: string[] }>;
}> {
  return request(`${API_BASE}/assetmap/pii/templates/apply`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** 表级 PII 合规复核（APPROVE/REJECT；禁自审） */
export async function reviewAssetEntity(
  entityId: number,
  body: { decision: "APPROVE" | "REJECT"; comment?: string },
): Promise<{ entity_id: number; decision: string; compliance_reviewed: boolean; masking_policy: string | null }> {
  return request(`${API_BASE}/assetmap/entities/${entityId}/review`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** 设置资产脱敏策略（none/mask/hash/deny） */
export async function setAssetMaskingPolicy(
  entityId: number,
  policy: string,
): Promise<{ entity_id: number; masking_policy: string }> {
  return request(`${API_BASE}/assetmap/entities/${entityId}/masking`, {
    method: "POST",
    body: JSON.stringify({ policy }),
  });
}

/** 字段级人工标注（suppressed=True 误报非 PII；False 人工确认是 PII） */
export async function overrideAssetPiiField(
  entityId: number,
  body: { column: string; suppressed: boolean; reason?: string },
): Promise<{ catalog_id: number; column: string; suppressed: boolean; reason: string | null }> {
  return request(`${API_BASE}/assetmap/entities/${entityId}/pii-overrides`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** 撤销字段级人工标注（恢复规则引擎判定） */
export async function removeAssetPiiOverride(
  entityId: number,
  column: string,
): Promise<{ catalog_id: number; column: string; removed: boolean }> {
  return request(`${API_BASE}/assetmap/entities/${entityId}/pii-overrides/remove`, {
    method: "POST",
    body: JSON.stringify({ column }),
  });
}

/** 设置资产保留期与合法性基础（合规留存期限） */
export async function setAssetRetention(
  entityId: number,
  body: { retention_days?: number | null; legal_basis?: string | null },
): Promise<{
  entity_id: number;
  retention_days: number | null;
  legal_basis: string | null;
  retention_expires_at: string | null;
}> {
  return request(`${API_BASE}/assetmap/entities/${entityId}/retention`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

/** PII 合规盘点 CSV 导出（含字段明细/类别/复核/脱敏状态） */
export async function downloadPiiExport(params?: {
  keyword?: string;
  source_id?: string;
  domain?: string;
  owner_id?: number;
  review_status?: string;
  category?: string;
}): Promise<void> {
  const qs = pageQs({
    keyword: params?.keyword,
    source_id: params?.source_id,
    domain: params?.domain,
    owner_id: params?.owner_id,
    review_status: params?.review_status,
    category: params?.category,
  });
  const headers: Record<string, string> = {
    "X-Api-Key": SEMANTIC_API_KEY,
  };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE_URL}${API_BASE}/assetmap/pii-export.csv${qs ? `?${qs}` : ""}`, {
    headers,
  });
  if (!res.ok) {
    throw new UnisenseApiError(`导出失败 (HTTP ${res.status})`, "HTTP_ERROR", res.status, "");
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "pii_compliance_export.csv";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
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
  /** 业务域（经数据源继承过滤） */
  domain?: string;
  /** 责任人（Owner）ID 过滤 */
  owner_id?: number;
  /** Schema 完整性：complete / incomplete */
  schema_status?: "complete" | "incomplete";
  /** 关键字：表名或数据源模糊搜索 */
  keyword?: string;
}): Promise<void> {
  const qs = pageQs({
    source_id: params?.source_id,
    sensitivity: params?.sensitivity,
    domain: params?.domain,
    owner_id: params?.owner_id,
    schema_status: params?.schema_status,
    keyword: params?.keyword,
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

// ---- 组织（租户）管理（backend /api/v1/organizations/*）----

export async function listOrganizations(params?: {
  keyword?: string;
  status?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: OrganizationView[]; total: number; page: number; page_size: number }> {
  const qs = pageQs({
    keyword: params?.keyword,
    status: params?.status,
    page: params?.page ?? 1,
    page_size: params?.page_size ?? 50,
  });
  return request(`${API_BASE}/organizations?${qs}`);
}

export async function createOrganization(body: {
  name: string;
  code: string;
  domain?: string | null;
}): Promise<OrganizationView> {
  return request<OrganizationView>(`${API_BASE}/organizations`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateOrganization(
  orgId: number,
  body: { name?: string; status?: string; domain?: string | null },
): Promise<OrganizationView> {
  return request<OrganizationView>(`${API_BASE}/organizations/${orgId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export async function createDomain(data: SubjectDomainCreateRequest): Promise<SubjectDomain> {
  return request<SubjectDomain>(`${API_BASE}/domains`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function updateDomain(
  code: string,
  data: SubjectDomainUpdateRequest,
): Promise<SubjectDomain> {
  return request<SubjectDomain>(`${API_BASE}/domains/${encodeURIComponent(code)}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export async function deactivateDomain(code: string): Promise<SubjectDomain> {
  return request<SubjectDomain>(
    `${API_BASE}/domains/${encodeURIComponent(code)}/status?action=deactivate`,
    {
      method: "PATCH",
    },
  );
}

export async function activateDomain(code: string): Promise<SubjectDomain> {
  return request<SubjectDomain>(
    `${API_BASE}/domains/${encodeURIComponent(code)}/status?action=activate`,
    {
      method: "PATCH",
    },
  );
}

export async function deleteDomain(code: string): Promise<void> {
  await request(`${API_BASE}/domains/${encodeURIComponent(code)}`, { method: "DELETE" });
}

export async function getDomainDefaults(code: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(
    `${API_BASE}/domains/${encodeURIComponent(code)}/defaults`,
  );
}

export async function updateDomainDefaults(
  code: string,
  defaults: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(
    `${API_BASE}/domains/${encodeURIComponent(code)}/defaults`,
    {
      method: "PUT",
      body: JSON.stringify({ defaults_json: defaults }),
    },
  );
}

// ---- 系统字典管理（backend /api/v1/dicts/*）----

export async function listDictTypes(): Promise<string[]> {
  return request<string[]>(`${API_BASE}/dicts/types`);
}

export async function listDictItems(dictType: string): Promise<SystemDictItem[]> {
  return request<SystemDictItem[]>(`${API_BASE}/dicts/${encodeURIComponent(dictType)}`);
}

/** 批量校验字典值是否未收录（指标保存前权威检测，DB 实时判定）。 */
export async function verifyDictValues(
  values: DictValueCheckItem[],
): Promise<DictValuesVerifyResponse> {
  return request<DictValuesVerifyResponse>(`${API_BASE}/dicts/verify-values`, {
    method: "POST",
    body: JSON.stringify({ values }),
  });
}

/** 无收录权限用户保存未收录值时，通知管理员收录/打回（返回通知条数）。 */
export async function notifyUnknownDictValues(
  data: DictUnknownNotifyRequest,
): Promise<{ notified: number; unknown: number }> {
  return request<{ notified: number; unknown: number }>(`${API_BASE}/dicts/unknown/notify`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/** 管理员打回字典收录申请（通知提交人改用字典内值）。 */
export async function rejectUnknownDictValue(data: {
  notification_id: number;
  reason: string;
}): Promise<{ notification_id: number; handled: boolean }> {
  return request<{ notification_id: number; handled: boolean }>(`${API_BASE}/dicts/unknown/reject`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function listDomainMetrics(code: string): Promise<Array<Record<string, unknown>>> {
  return request<Array<Record<string, unknown>>>(`${API_BASE}/domains/${encodeURIComponent(code)}/metrics`);
}

export async function listAllDictItems(dictType: string): Promise<SystemDictItem[]> {
  return request<SystemDictItem[]>(`${API_BASE}/dicts/${encodeURIComponent(dictType)}/all`);
}

export async function createDictItem(
  dictType: string,
  data: DictItemCreateRequest,
): Promise<SystemDictItem> {
  return request<SystemDictItem>(`${API_BASE}/dicts/${encodeURIComponent(dictType)}`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function updateDictItem(
  dictType: string,
  code: string,
  data: DictItemUpdateRequest,
): Promise<SystemDictItem> {
  return request<SystemDictItem>(
    `${API_BASE}/dicts/${encodeURIComponent(dictType)}/${encodeURIComponent(code)}`,
    {
      method: "PUT",
      body: JSON.stringify(data),
    },
  );
}

export async function deactivateDictItem(dictType: string, code: string): Promise<SystemDictItem> {
  return request<SystemDictItem>(
    `${API_BASE}/dicts/${encodeURIComponent(dictType)}/${encodeURIComponent(code)}/status?action=deactivate`,
    {
      method: "PATCH",
    },
  );
}

export async function activateDictItem(dictType: string, code: string): Promise<SystemDictItem> {
  return request<SystemDictItem>(
    `${API_BASE}/dicts/${encodeURIComponent(dictType)}/${encodeURIComponent(code)}/status?action=activate`,
    {
      method: "PATCH",
    },
  );
}

export async function deleteDictItem(dictType: string, code: string): Promise<void> {
  await request(`${API_BASE}/dicts/${encodeURIComponent(dictType)}/${encodeURIComponent(code)}`, {
    method: "DELETE",
  });
}

/** 参照数据项描述 LLM 推断（新增/编辑弹窗「AI 生成描述」；LLM 只回填文本不落库）。 */
export async function inferDictDescription(
  dictType: string,
  label: string,
  dictTypeLabel?: string,
): Promise<string> {
  const data = await request<{ description: string }>(
    `${API_BASE}/dicts/infer-description`,
    {
      method: "POST",
      body: JSON.stringify({ dict_type: dictType, label, dict_type_label: dictTypeLabel }),
    },
  );
  return data.description;
}

/** 批量新增同一类型字典项（207 语义：单条失败逐项标注，不影响其余）。 */
export async function batchCreateDictItems(
  dictType: string,
  items: DictItemCreateRequest[],
): Promise<DictBatchResult> {
  return request<DictBatchResult>(`${API_BASE}/dicts/${encodeURIComponent(dictType)}/batch`, {
    method: "POST",
    body: JSON.stringify({ items }),
  });
}

/** 批量启用/停用同一类型字典项（207 语义）。 */
export async function batchToggleDictItems(
  dictType: string,
  codes: string[],
  action: "activate" | "deactivate",
): Promise<DictBatchResult> {
  return request<DictBatchResult>(
    `${API_BASE}/dicts/${encodeURIComponent(dictType)}/batch-status`,
    {
      method: "POST",
      body: JSON.stringify({ codes, action }),
    },
  );
}

/** 批量删除同一类型字典项（软删，207 语义；被引用的项不可删）。 */
export async function batchDeleteDictItems(
  dictType: string,
  codes: string[],
): Promise<DictBatchResult> {
  return request<DictBatchResult>(
    `${API_BASE}/dicts/${encodeURIComponent(dictType)}/batch-delete`,
    {
      method: "POST",
      body: JSON.stringify({ codes }),
    },
  );
}

// ---- 自动推断（backend /api/v1/metric-definitions/auto-suggest）----

export async function autoSuggestMetric(data: AutoSuggestRequest): Promise<AutoSuggestResponse> {
  return request<AutoSuggestResponse>(`${API_BASE}/metric-definitions/auto-suggest`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/** 业务域建议（FR-010 域建议增强）：输入 SQL 或源表 → 反向定位/LLM 兜底推断业务域。 */
export async function suggestDomain(data: {
  sql?: string | null;
  source_table?: string | null;
}): Promise<DomainSuggestionResponse> {
  return request<DomainSuggestionResponse>(`${API_BASE}/metric-definitions/suggest-domain`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/** SQL 源表解析（注册向导：数仓SQL口径失焦自动回填依赖表——轻量只读，不落库、不触发 LLM）。 */
export async function parseSqlTables(sql: string): Promise<SqlParseTablesResult> {
  return request<SqlParseTablesResult>(`${API_BASE}/metric-definitions/parse-tables`, {
    method: "POST",
    body: JSON.stringify({ sql }),
  });
}

/** SQL 批量解析候选（FR-010 批量注册增强，场景A/B：多语句切分 + 多度量拆分，只读+LLM 不落库）。
 * LLM 模式逐候选校验/补全可能耗时 60-90s → 显式放宽超时到 180s，避免 30s 中止误报失败。 */
export async function parseSqlBatch(data: SqlParseRequest): Promise<SqlBatchParseResult> {
  return request<SqlBatchParseResult>(`${API_BASE}/metric-definitions/parse-sql-batch`, {
    method: "POST",
    body: JSON.stringify(data),
    timeout: 180_000,
  });
}

/** SQL 智能推断评测页数据：当前成功率报告 + 历史运行趋势（确定性实时计算）。 */
export async function getSqlInferEval(): Promise<SqlInferEvalData> {
  return request<SqlInferEvalData>(`${API_BASE}/metric-definitions/sql-infer-eval`, {
    method: "GET",
  });
}

/** 运行一次 SQL 智能推断评测并记录历史（成功率趋势数据源）。
 * 评测含 LLM 校验路径时耗时较长 → 放宽超时。 */
export async function runSqlInferEval(): Promise<SqlInferEvalRunResult> {
  return request<SqlInferEvalRunResult>(
    `${API_BASE}/metric-definitions/sql-infer-eval/run`,
    { method: "POST", timeout: 180_000 },
  );
}

/** 评测自定义样本清单（含停用）。 */
export async function listEvalSamples(): Promise<{ items: EvalSample[]; total: number }> {
  return request<{ items: EvalSample[]; total: number }>(
    `${API_BASE}/metric-definitions/sql-infer-eval/samples`,
    { method: "GET" },
  );
}

/** 创建自定义评测样本。 */
export async function createEvalSample(data: EvalSampleIn): Promise<EvalSample> {
  return request<EvalSample>(
    `${API_BASE}/metric-definitions/sql-infer-eval/samples`,
    { method: "POST", body: JSON.stringify(data) },
  );
}

/** 更新自定义评测样本（内置基线只读拒绝）。 */
export async function updateEvalSample(
  sampleId: number,
  data: Partial<EvalSampleIn> & { enabled?: boolean },
): Promise<EvalSample> {
  return request<EvalSample>(
    `${API_BASE}/metric-definitions/sql-infer-eval/samples/${sampleId}`,
    { method: "PUT", body: JSON.stringify(data) },
  );
}

/** 软删自定义评测样本（内置基线只读拒绝）。 */
export async function deleteEvalSample(sampleId: number): Promise<{ sample_id: number }> {
  return request<{ sample_id: number }>(
    `${API_BASE}/metric-definitions/sql-infer-eval/samples/${sampleId}`,
    { method: "DELETE" },
  );
}

/** 评测样本即时解析预览（不落库）。 */
export async function previewEvalSample(sql: string): Promise<EvalSamplePreview> {
  return request<EvalSamplePreview>(
    `${API_BASE}/metric-definitions/sql-infer-eval/samples/preview`,
    { method: "POST", body: JSON.stringify({ sql }) },
  );
}

/** 从 SQL 解析候选批量注册指标（场景A/B，savepoint 逐条隔离，复合缺依赖跳过）。 */
export async function batchRegisterFromSql(
  req: SqlBatchRegisterRequest,
): Promise<MetricBatchRegisterResult> {
  return request<MetricBatchRegisterResult>(
    `${API_BASE}/metric-definitions/batch-register-from-sql`,
    {
      method: "POST",
      body: JSON.stringify(req),
    },
  );
}

// 仲裁改名建议：LLM 生成区分性名称候选（best-effort，LLM 不可用降级规则）
export async function suggestRenameName(
  metricCode: string,
  oppositeCode?: string | null,
): Promise<RenameSuggestResponse> {
  return request<RenameSuggestResponse>(
    `${API_BASE}/metric-definitions/${encodeURIComponent(metricCode)}/suggest-rename`,
    {
      method: "POST",
      body: JSON.stringify({ opposite_code: oppositeCode ?? undefined }),
    },
  );
}

// ---- 指标三层口径 LLM 增强（backend /api/v1/metric-definitions/refine-definition）----

export type RefineDefinitionField = "business" | "pseudo" | "dw";
export type RefineDefinitionAction = "enrich" | "generate" | "optimize";

export interface RefineDefinitionRequest {
  field: RefineDefinitionField;
  action: RefineDefinitionAction;
  current?: string;
  metric_code?: string;
  metric_name?: string;
  domain?: string;
  sql?: string;
  expression?: string;
  business_definition?: string;
  pseudo_definition?: string;
  dw_definition?: string;
}

/** 指标三层口径 LLM 增强：LLM 生成/丰富/优化业务口径、伪代码口径、数仓SQL口径，返回文本回填（不落库）。 */
export async function refineMetricDefinition(
  data: RefineDefinitionRequest,
): Promise<{ content: string; source: string }> {
  return request<{ content: string; source: string }>(
    `${API_BASE}/metric-definitions/refine-definition`,
    {
      method: "POST",
      body: JSON.stringify(data),
    },
  );
}
