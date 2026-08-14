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

/** 审计动作 → 中文 */
export const AUDIT_ACTION_LABEL: Record<string, string> = {
  "metric.created": "创建指标",
  "metric.updated": "更新指标",
  "metric.submitted": "提交审核",
  "metric.approved": "审核通过",
  "metric.rejected": "审核驳回",
  "metric.published": "发布指标",
  "metric.deprecated": "废弃指标",
  "metric.emergency_publish": "紧急发布",
  "metric.rollback": "回滚指标",
  "metric.promote": "灰度发布",
  "metric.updated_definition": "更新口径",
  "metric.updated_owner": "变更责任人",
  "metric.health_check": "健康检查",
  "metric.pii_flagged": "PII 标记",
  "metric.activated": "激活指标",
  "metric.deactivated": "停用指标",
  "lineage.created": "创建血缘",
  "lineage.updated": "更新血缘",
  "lineage.deleted": "删除血缘",
  "lineage.change": "血缘变更",
  "lineage.imported": "导入血缘",
  "lineage.sync": "血缘同步",
  "conflict.detected": "冲突检测",
  "conflict.resolved": "解决冲突",
  "conflict.escalated": "升级冲突",
  "conflict.rejected": "驳回冲突",
  "grant.created": "创建授权",
  "grant.revoked": "撤销授权",
  "grant.batch": "批量授权",
  "grant.check": "权限检查",
  "term.created": "创建术语",
  "term.updated": "更新术语",
  "term.submitted": "提交术语",
  "term.approved": "审核术语",
  "term.deprecated": "废弃术语",
  "term.resolved": "解决术语冲突",
  "dimension.created": "创建维度",
  "dimension.updated": "更新维度",
  "dimension.deprecated": "废弃维度",
  "dimension.published": "发布维度",
  "dimension.mapping": "维度映射",
  "dimension.reconciliation": "维度对账",
  "member.created": "创建成员",
  "quality.rule_created": "创建质量规则",
  "quality.rule_updated": "更新质量规则",
  "quality.rule_deleted": "删除质量规则",
  "quality.alert": "质量告警",
  "quality.observation": "质量观测",
  "quality.benchmark_imported": "导入基准",
  "quality.reconciliation": "质量对账",
  "data_source.created": "创建数据源",
  "data_source.updated": "更新数据源",
  "data_source.deleted": "删除数据源",
  "data_source.collected": "采集元数据",
  "data_source.tested": "测试连接",
  "data_source.checked": "探活检查",
  "data_source.scheduled": "配置调度",
  "catalog.registered": "登记目录实体",
  "catalog.deprecated": "废弃目录实体",
  "auth.login": "登录系统",
  "auth.logout": "退出登录",
  "auth.login_failed": "登录失败",
  "audit.archive": "归档审计",
  "ai.nl2sql": "AI 问数",
  "nl2sql": "AI 问数",
  "secret.reveal": "查看密钥",
  "config.updated": "更新配置",
  "config.created": "创建配置",
  "config.deleted": "删除配置",
  "TEST_CONNECTION": "测试连接",
  "CHECK_CONNECTION": "探活检查",
  "COLLECT": "采集元数据",
  "REGISTER": "登记实体",
  "DELETE": "删除",
  "UPDATE": "更新",
  "CREATE": "创建",
  "LOGIN": "登录系统",
  "LOGOUT": "退出登录",
};

/** 审计动作 → 格式化中文显示（命中返回中文，未命中用拆词兜底） */
export function auditActionLabel(action: string | null | undefined): string {
  if (!action) return "—";
  const known = AUDIT_ACTION_LABEL[action];
  if (known) return known;
  // 兜底：域.动作 拆词
  const dot = action.indexOf(".");
  if (dot > 0) {
    const domain = entityTypeLabel(action.slice(0, dot)) || action.slice(0, dot);
    const verb = action.slice(dot + 1);
    const verbMap: Record<string, string> = {
      created: "创建", updated: "更新", deleted: "删除",
      published: "发布", deprecated: "废弃", submitted: "提交审核",
      approved: "审核通过", rejected: "驳回", activated: "激活",
      deactivated: "停用", resolved: "解决", escalated: "升级",
      imported: "导入", checked: "检查", tested: "测试",
      collected: "采集", registered: "登记", scheduled: "配置调度",
      sync: "同步", migrated: "迁移", applied: "应用",
    };
    return `${domain}${verbMap[verb] ? "·" + verbMap[verb] : "·" + verb}`;
  }
  // 纯英文动作（如 DELETE, UPDATE, COLLECT）
  const pureMap: Record<string, string> = {
    DELETE: "删除", UPDATE: "更新", CREATE: "创建",
    LOGIN: "登录系统", LOGOUT: "退出登录", COLLECT: "采集元数据",
    REGISTER: "登记实体", TEST_CONNECTION: "测试连接",
    CHECK_CONNECTION: "探活检查",
  };
  return pureMap[action] ?? action;
}

/** 格式化审计时间——ISO 转为 "YYYY-MM-DD HH:mm" */
export function formatAuditTime(v: string | null | undefined): string {
  if (!v) return "—";
  const t = v.includes("T") ? v.replace("T", " ").replace(/\.\d+/, "") : v;
  return t.length > 19 ? t.slice(0, 19) : t;
}

/** 实体 ID 加业务前缀（把 metric#123 转为「指标 #123」） */
export function entityIdWithLabel(entityType: string | null | undefined, entityId: string | null | undefined): string {
  if (!entityId) return "—";
  if (entityType) {
    const label = entityTypeLabel(entityType);
    if (label) return `${label} #${entityId.replace(/^[^#]*#?/, "")}`;
  }
  return entityId;
}
