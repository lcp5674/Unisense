export const mockMetrics = [
  { id: 1, code: 'REV_TOTAL', name: '总营收', status: 'PUBLISHED', domain: 'finance' },
  { id: 2, code: 'USER_DAU', name: '日活用户', status: 'DRAFT', domain: 'growth' },
];

export const mockConflicts = [
  { id: 1, type: 'NAMING', status: 'OPEN', metric_id: 1, created_at: '2026-01-01' },
];

export const mockCurrentUser = {
  id: 1, username: 'admin', display_name: '管理员', role: 'admin', domain: 'finance', org_id: 1,
};

export function createMockResponse(data: any, _status = 200) {
  return { code: 'OK', message: 'success', data, trace_id: 'test-trace' };
}

export function createMockError(code: string, message: string, _status = 400) {
  return { code, message, trace_id: 'test-trace', detail: null };
}
