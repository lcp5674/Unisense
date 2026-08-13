// 审计日志中文化 —— detail_json 字段名映射 + 常见枚举值翻译
// 后端 detail_json 仍以英文 key 透传，这里统一映射为中文展示，避免用户直面技术字段。

import { enumLabel, ENTITY_TYPE_LABEL } from "./enums";

/** detail_json 字段名 → 中文 */
export const AUDIT_FIELD_LABEL: Record<string, string> = {
  version: "版本",
  target_version: "目标版本",
  pii_flag: "PII 标记",
  decision: "裁决",
  canonical: "标准指标",
  owner_id: "责任人",
  source_id: "数据源",
  sensitivity_level: "敏感级",
  scanned: "扫描数",
  registered: "注册数",
  failed_count: "失败数",
  successor_code: "替代指标",
  reason: "原因",
  note: "说明",
  mode: "发布模式",
  metric_id: "指标",
  metric_code: "指标编码",
  rule_type: "规则类型",
  obs_value: "观测值",
  threshold: "阈值",
  status: "状态",
  change_reason: "变更说明",
  change_type: "变更类型",
  snapshot_version: "快照版本",
  before: "变更前",
  after: "变更后",
  field: "字段",
  fields: "字段列表",
  entity: "实体",
  entity_id: "实体 ID",
  event_type: "事件类型",
  channel: "渠道",
  enabled: "是否启用",
  severity: "严重度",
  level: "级别",
  mask_policy: "脱敏策略",
  reviewer: "复核人",
  reviewed_at: "复核时间",
  rule_id: "规则",
  condition: "条件",
  expr: "表达式",
  target: "目标",
  source: "来源",
  action: "动作",
  granted_role: "授予角色",
  revoked_role: "撤销角色",
};

/** 审计 detail 值中的常见枚举 → 中文（展示前做一次值翻译） */
const AUDIT_VALUE_LABEL: Record<string, string> = {
  // 指标状态
  DRAFT: "草稿",
  EXPERIMENTAL: "实验",
  REVIEW: "审核",
  PUBLISHED: "已发布",
  DEPRECATED: "已废弃",
  // 指标类型 / 聚合
  atomic: "原子指标",
  derived: "派生指标",
  composite: "复合指标",
  SUM: "求和",
  AVG: "平均",
  COUNT: "计数",
  COUNT_DISTINCT: "去重计数",
  LAST_VALUE: "末值",
  // 时间语义 / 新鲜度
  PERIOD: "区间累计",
  YTD: "年初至今",
  TTM: "滚动 12 月",
  REALTIME: "实时",
  HOURLY: "小时级",
  T1: "T+1",
  // 发布模式
  gray: "灰度",
  experimental: "实验",
  normal: "正常",
  // 决策 / 状态
  reasonable: "合理",
  caliber_error: "口径错误",
  APPROVED: "已批准",
  REJECTED: "已驳回",
  PENDING: "待处理",
  OPEN: "待处理",
  ACK: "已确认",
  RESOLVED: "已解决",
  CLOSED: "已关闭",
  CONFIRMED: "已确认",
  // 敏感级
  PUBLIC: "公开",
  INTERNAL: "内部",
  CONFIDENTIAL: "机密",
  PII: "PII",
  NEEDS_REVIEW: "待复核",
  // 变更类型
  CREATE: "创建",
  UPDATE: "更新",
  DELETE: "删除",
};

/** 将值转为可展示文本；对象/数组紧凑序列化，常见枚举值翻译为中文 */
export function auditValueText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") return String(value);
  if (typeof value === "object") return JSON.stringify(value);
  return AUDIT_VALUE_LABEL[String(value)] ?? String(value);
}

/** 实体类型 → 中文（缺省回退原值） */
export function entityTypeLabel(v: string | null | undefined): string {
  return enumLabel(ENTITY_TYPE_LABEL, v);
}
