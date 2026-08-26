/** 前端错误处理增强模块（PROD-08/PROD-09: 降级提示 + 207 逐项展示）。

职责：
1. DEPENDENCY_DEGRADED_ENGINE 错误码展示专用降级提示
2. 207 Multi-Status 响应逐项解析展示
3. 统一 API 错误处理工具函数
*/

export interface UnisenseApiError {
  code: string;
  message: string;
  trace_id?: string;
  detail?: Record<string, unknown>;
  degraded?: boolean;
  degradation_message?: string;
}

export interface MultiStatusItem {
  id: string;
  status: 'success' | 'failed';
  error?: string;
}

export interface MultiStatusResult {
  succeeded: string[];
  failed: Array<{ id: string; error: string }>;
}

/** 降级相关错误码集合 */
const DEGRADATION_ERROR_CODES: ReadonlySet<string> = new Set([
  'DEPENDENCY_DEGRADED_ENGINE',
  'DEPENDENCY_DEGRADED_GRAPH',
  'DEPENDENCY_DEGRADED_LLM',
]);

/** 降级提示文案映射 */
const DEGRADATION_MESSAGES: ReadonlyMap<string, string> = new Map([
  ['DEPENDENCY_DEGRADED_ENGINE', '查询引擎暂不可用，请稍后重试'],
  ['DEPENDENCY_DEGRADED_GRAPH', '血缘图暂不可用，指标列表仍可浏览'],
  ['DEPENDENCY_DEGRADED_LLM', 'AI 暂不可用，请手动填写'],
]);

/**
 * 判断是否为降级错误
 */
export function isDegradationError(error: UnisenseApiError): boolean {
  return DEGRADATION_ERROR_CODES.has(error.code);
}

/**
 * 处理降级引擎错误：返回专用降级提示
 */
export function handleDegradedEngine(error: UnisenseApiError): {
  isDegraded: boolean;
  message: string;
  canRetry: boolean;
} {
  if (!isDegradationError(error)) {
    return { isDegraded: false, message: error.message, canRetry: false };
  }

  const message =
    error.degradation_message ||
    DEGRADATION_MESSAGES.get(error.code) ||
    '依赖暂不可用，请稍后重试';

  return {
    isDegraded: true,
    message,
    canRetry: true,
  };
}

/**
 * 解析 207 Multi-Status 响应
 *
 * 207 响应体格式：
 * {
 *   code: "MULTI_STATUS",
 *   data: {
 *     results: [
 *       { id: "1", status: "success" },
 *       { id: "2", status: "failed", error: "原因" }
 *     ]
 *   }
 * }
 */
export function parseMultiStatus(response: {
  data?: {
    results?: Array<{
      id?: string;
      status?: string;
      error?: string;
    }>;
  };
}): MultiStatusResult {
  const results = response?.data?.results ?? [];
  const succeeded: string[] = [];
  const failed: Array<{ id: string; error: string }> = [];

  for (const item of results) {
    if (item.status === 'success') {
      succeeded.push(item.id ?? '');
    } else if (item.status === 'failed') {
      failed.push({
        id: item.id ?? '',
        error: item.error ?? '未知错误',
      });
    }
  }

  return { succeeded, failed };
}

/**
 * 格式化 207 响应为用户可读文案
 */
export function formatMultiStatusMessage(result: MultiStatusResult): string {
  const parts: string[] = [];
  if (result.succeeded.length > 0) {
    parts.push(`${result.succeeded.length} 项成功`);
  }
  if (result.failed.length > 0) {
    parts.push(`${result.failed.length} 项失败`);
  }
  return parts.join('，') || '无操作结果';
}

/**
 * 通用 API 错误处理：根据 error_code 返回用户友好提示
 */
export function getUserFriendlyMessage(error: UnisenseApiError): string {
  // 降级错误
  if (isDegradationError(error)) {
    return handleDegradedEngine(error).message;
  }

  // 常见错误码映射
  const messageMap: Record<string, string> = {
    AUTH_TOKEN_MISSING: '请先登录',
    AUTH_TOKEN_EXPIRED: '登录已过期，请重新登录',
    AUTH_TOKEN_INVALID: '登录凭证无效，请重新登录',
    AUTH_INVALID_CREDENTIALS: '用户名或密码错误',
    FORBIDDEN: '无权限执行此操作',
    RATE_LIMITED: '操作过于频繁，请稍后重试',
    VALIDATION_ERROR: '输入参数有误，请检查后重试',
    NOT_FOUND: '请求的资源不存在',
    CONFLICT: '数据冲突，请刷新后重试',
  };

  return messageMap[error.code] || error.message || '操作失败，请稍后重试';
}

export function parseMultiStatusResponse(response: any): Array<{item: string; status: 'success' | 'failed'; message?: string}> {
  if (!response?.detail?.items) return [];
  return response.detail.items.map((item: any) => ({
    item: item.id || item.name || 'unknown',
    status: item.status === 'success' ? 'success' : 'failed',
    message: item.message || item.error,
  }));
}

/**
 * 统一 API 错误文案（F-3 第十一轮）：标准格式 ``message（codeZh）``。
 * 各页面此前重复 ``${err.message}（${err.codeZh}）`` 三元模板约 170 处、
 * MeasureCatalogs 还本地复制了同款 helper——统一收敛到本函数。
 */
export function errMsg(e: unknown, fallback: string): string {
  if (e && typeof e === "object" && "message" in e) {
    const err = e as { message?: unknown; codeZh?: unknown };
    const base = typeof err.message === "string" && err.message ? err.message : fallback;
    const codeZh = typeof err.codeZh === "string" ? err.codeZh : "";
    return codeZh ? `${base}（${codeZh}）` : base;
  }
  return fallback;
}
