import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, InputNumber, Select, message, Tabs, Alert, Spin, Empty, Pagination } from "antd";
import { PlusOutlined, SendOutlined, ClockCircleOutlined, LinkOutlined, CheckOutlined, DeleteOutlined, UserOutlined, StarOutlined, SearchOutlined } from "@ant-design/icons";
import {
  listNotifications,
  listNotifyEvents,
  listSubscriptions,
  upsertSubscription,
  publishNotifyEvent,
  markNotificationRead,
  markAllNotificationsRead,
  deleteNotification,
  deleteAllNotifications,
  retryNotification,
  markNotificationHandled,
  UnisenseApiError,
} from "../api";
import type { Notification, NotifyEventLog, SubscriptionPref } from "../types";
import { NOTIFY_STATUS_LABEL, QUALITY_LEVEL_LABEL } from "../utils/enums";
import { formatCnTime } from "../utils/timeCn";
import { notifyNotifChanged } from "../utils/notifBus";

// 渠道 = 消息送达方式（面向业务用户，不用 webhook/sms 等英文码）
const CHANNEL_LABEL: Record<string, string> = {
  email: "邮件",
  webhook: "接口推送",
  sms: "短信",
  in_app: "站内消息",
  dingtalk: "钉钉",
  console: "控制台",
};

// 消息类型（业务术语，替代 metric.created 等内部事件码）
const EVENT_TYPE_LABEL: Record<string, string> = {
  "metric.created": "指标创建",
  "metric.submitted": "指标待审核",
  "metric.approved": "指标已通过",
  "metric.rejected": "指标已驳回",
  "metric.deprecated": "指标废弃",
  "metric.voided": "指标作废",
  "metric.rename_required": "指标需要改名",
  "metric.promoted": "指标已升级",
  "metric.rolled_back": "指标已回滚",
  "metric.emergency_published": "指标紧急发布",
  "metric.health_critical": "指标健康告警",
  "conflict_open": "口径冲突待处理",
  "conflict_ruled": "口径冲突已裁决",
  "conflict_escalated": "口径冲突已升级",
  "pii_conflict": "PII 冲突",
  "quality.anomaly": "数据质量异常告警",
  "reconciliation.alert": "对账告警",
  "benchmark.imported": "参照基准已导入",
  "grant.granted": "权限已授予",
  "grant.revoked": "权限已收回",
  "grant.expired": "权限已过期",
  "grant.expiring_soon": "权限即将到期",
  "pii.propagated": "敏感数据已扩散",
  "pii.reviewed": "敏感数据已复核",
  "classification.changed": "数据分类变更",
  "classification.done": "数据分类完成",
  "escalation.triggered": "告警升级已触发",
  "feedback.status_updated": "反馈状态更新",
  "nps.submitted": "满意度已提交",
  "audit.capacity_warning": "审计容量告警",
  // 采集/血缘断链修复（collector/lineage 双发 EventBus，TD §5.5）
  "catalog_registered": "数据目录已注册",
  "catalog_schema_drifted": "目录 Schema 漂移",
  "lineage_parsed": "血缘已解析",
  "lineage_ingested": "血缘已接入",
  // 采集定向通知（collector 经 notify_user 直发源 Owner）
  "catalog.deprecated": "数据目录已废弃",
  "collect.degraded": "采集降级",
  // 核心依赖降级 / 冲突重开
  "degradation.state_changed": "系统依赖状态变更",
  "conflict_reopened": "口径冲突已重开",
  // 账号安全/组织（users.py/organizations.py 定向通知）
  "user.created": "账号已创建",
  "user.status_changed": "账号状态变更",
  "user.password_reset": "密码已重置",
  "org.status_changed": "组织状态变更",
  // 采集异常 / PII 复核待办（定向通知）
  "collect.failed": "采集任务失败",
  "catalog.connection_failed": "数据源连接失败",
  "pii.review_pending": "PII 复核待办",
};

// 事件类型兜底：未命中的 ``域.动作`` 拆词为中文（历史数据/新类型都能显示中文）
const EVENT_SOURCE_CN: Record<string, string> = {
  metric: "指标",
  lineage: "血缘",
  quality: "数据质量",
  governance: "治理合规",
  semantic: "指标口径",
  system: "系统",
  scheduler: "定时任务",
  conflict: "口径冲突",
  grant: "权限",
  pii: "敏感数据",
  benchmark: "参照基准",
  orphan: "孤立实体",
  review: "审核",
  // 三梯队通知接入新增来源
  catalog: "数据目录",
  collect: "采集",
  user: "账号",
  org: "组织",
  degradation: "系统依赖",
};
const EVENT_ACTION_CN: Record<string, string> = {
  created: "创建",
  updated: "更新",
  published: "发布",
  submitted: "待审核",
  approved: "已通过",
  rejected: "已驳回",
  deprecated: "废弃",
  promoted: "升级",
  rolled_back: "回滚",
  emergency_published: "紧急发布",
  health_critical: "健康告警",
  detected: "检测",
  alert: "告警",
  open: "待处理",
  ruled: "已裁决",
  escalated: "升级",
  imported: "导入",
  granted: "授予",
  revoked: "收回",
  expired: "过期",
  propagated: "扩散",
  reviewed: "复核",
  changed: "变更",
  done: "完成",
  pending: "待办",
  triggered: "已触发",
  change: "变更",
  notice: "公告",
  anomaly: "异常告警",
};

function eventTypeLabel(v: string | null | undefined): string {
  if (!v) return "—";
  const known = EVENT_TYPE_LABEL[v];
  if (known) return known;
  const dot = v.indexOf(".");
  if (dot > 0) {
    const src = v.slice(0, dot);
    const act = v.slice(dot + 1);
    return `${EVENT_SOURCE_CN[src] ?? src} · ${EVENT_ACTION_CN[act] ?? act}`;
  }
  return v;
}

// 历史通知正文兜底：旧数据 body 是 JSON dump，解析后渲染成「中文标签：值」
const PAYLOAD_FIELD_LABEL_FE: Record<string, string> = {
  metric_id: "指标ID",
  metric_code: "指标编码",
  metric_name: "指标名称",
  level: "重要程度",
  rule_type: "规则类型",
  rule_mode: "规则模式",
  obs_value: "观测值",
  threshold: "阈值",
  domain: "业务域",
  user_id: "用户ID",
  operator_id: "操作人ID",
  grant_id: "授权ID",
  grant_type: "授权类型",
  expires_at: "到期时间",
  conflict_id: "冲突编号",
  note: "说明",
  reason: "原因",
  notify_targets: "通知对象",
  // 业务字段（定向通知 payload：账号/组织/采集/血缘/权限/冲突）
  username: "账号",
  org_id: "组织ID",
  org_name: "组织名称",
  status: "状态",
  source_id: "数据源ID",
  source_name: "数据源名称",
  entity_name: "实体名称",
  table_name: "表名",
  successor_code: "后继指标",
  candidate: "候选指标",
  existing: "现有指标",
  severity: "严重级别",
  window: "统计周期",
  source_table: "源表",
  target_table: "目标表",
  pii_columns: "敏感字段",
  reviewer_id: "审核人ID",
  reviewer: "审核人",
  resolver_id: "处理人",
  // 反馈类
  feedback_id: "反馈编号",
  comment: "反馈内容",
  // 指标类（健康度/废弃/审批）
  score: "健康得分",
  missing_dimensions: "缺失治理项",
  deprecated_at: "废弃时间",
  version: "版本",
  type: "指标类型",
  definition_json: "口径定义",
  // 血缘类
  table_edges: "表血缘",
  field_edges: "字段血缘",
};
const RULE_TYPE_CN_FE: Record<string, string> = {
  COMPLETENESS: "完整性",
  ACCURACY: "准确性",
  TIMELINESS: "时效性",
  CONSISTENCY: "一致性",
  UNIQUENESS: "唯一性",
  VALIDITY: "有效性",
  WAVE_DIFF: "波动差异",
  CROSS_SOURCE: "跨源校验",
};
const LEVEL_CN_FE: Record<string, string> = {
  P0: "严重",
  P1: "高",
  P2: "中",
  INFO: "提示",
  WARN: "警告",
  WARNING: "警告",
  ERROR: "错误",
  CRITICAL: "严重",
};
// 健康等级（后端 health_scorer：EXCELLENT/GOOD/WARNING/CRITICAL）
const HEALTH_LEVEL_CN_FE: Record<string, string> = {
  EXCELLENT: "优秀",
  GOOD: "良好",
  WARNING: "警告",
  CRITICAL: "严重",
};
// 指标类型（atomic/derived/composite）
const METRIC_TYPE_CN_FE: Record<string, string> = {
  atomic: "原子指标",
  derived: "派生指标",
  composite: "复合指标",
};
// 健康度缺失治理维度 → 中文（missing_dimensions 数组元素）
const DIMENSION_CN_FE: Record<string, string> = {
  sla: "SLA",
  lineage_coverage: "血缘覆盖",
  quality: "质量规则",
  activity: "活跃度",
  owner_response: "负责人响应",
};

const STATUS_CN_FE: Record<string, string> = {
  active: "启用",
  disabled: "禁用",
  suspended: "停用",
  deleted: "已删除",
};
// 反馈处理状态（feedback.status：pending/in_progress/adopted/rejected）
const FEEDBACK_STATUS_CN_FE: Record<string, string> = {
  pending: "待处理",
  in_progress: "跟进中",
  adopted: "已采纳",
  rejected: "已驳回",
};

// 剥离反馈 comment 中后端追加的状态变更历史（[时间戳] status=... note=... by=...），
// 支持历史独立成行或内嵌行尾两种形态——只保留用户提交的原始反馈内容，
// 否则通知里塞满处理日志，既不直观也难读懂。
function cleanFeedbackComment(comment: string): string {
  return comment
    .split("\n")
    .map((l) => l.replace(/\s*\[\d{4}-\d{2}-\d{2}T[\dTZ:.+\-]+\]\s*status=.*$/, "").trimEnd())
    .filter((l) => l.trim() !== "")
    .join("\n")
    .trim();
}

function humanizeFeValue(key: string, v: unknown): string {
  if (v === null || v === undefined) return "无";
  if (typeof v === "boolean") return v ? "是" : "否";
  if (key === "level" || key === "severity") return LEVEL_CN_FE[String(v)] ?? String(v);
  if (key === "rule_type") return RULE_TYPE_CN_FE[String(v)] ?? String(v);
  if (key === "grant_type") return v === "READ" ? "只读" : v === "READ_WRITE" ? "读写" : String(v);
  if (key === "status") return FEEDBACK_STATUS_CN_FE[String(v)] ?? STATUS_CN_FE[String(v)] ?? String(v);
  if (key === "type") return METRIC_TYPE_CN_FE[String(v)] ?? String(v);
  if (key === "comment" && typeof v === "string") return cleanFeedbackComment(v) || "无";
  if (key === "health_level") return HEALTH_LEVEL_CN_FE[String(v)] ?? String(v);
  if (key === "score" && typeof v === "number") return `${v} 分`;
  if (key === "table_edges" || key === "field_edges") return `${String(v)} 条`;
  if (key === "missing_dimensions" && Array.isArray(v)) {
    return v.length ? v.map((d) => DIMENSION_CN_FE[String(d)] ?? String(d)).join("、") : "无";
  }
  if ((key === "deprecated_at" || key === "expires_at") && typeof v === "string") {
    return formatCnTime(v);
  }
  if (key === "definition_json" && typeof v === "object" && v !== null) {
    const o = v as Record<string, unknown>;
    const parts: string[] = [];
    if (o.expression) parts.push(`表达式 ${String(o.expression)}`);
    if (o.sql) {
      const sql = String(o.sql);
      parts.push(sql.length > 60 ? `${sql.slice(0, 60)}…` : sql);
    }
    const src = o.source_tables ?? o.source_table;
    if (Array.isArray(src) && src.length) parts.push(`源表 ${src.join("、")}`);
    return parts.length ? parts.join(" · ") : "已定义";
  }
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function formatNotifyBody(body: string | null): string {
  if (!body) return "";
  const t = body.trim();
  if (t.startsWith("{")) {
    try {
      const obj = JSON.parse(t) as Record<string, unknown>;
      return Object.entries(obj)
        .filter(([k]) => k !== "event_type" && k !== "payload")
        .map(([k, v]) => `${PAYLOAD_FIELD_LABEL_FE[k] ?? k}：${humanizeFeValue(k, v)}`)
        .join("\n");
    } catch {
      return body;
    }
  }
  return body;
}

// 把正文解析为结构化字段（{标签, 值}），供卡片网格化展示。
// 策略：payload 字段优先（保留原始类型做枚举/时间/数组渲染，值更友好）；
// body 文本补充——自然语言整行保留展示，结构化行若字段未被 payload 覆盖则补充，
// 已知英文 key 做二次中文映射。
function parseNotifyBodyFields(
  body: string | null,
  payload: Record<string, unknown> | null,
  templateCode?: string | null,
): { label: string; value: string; target?: string | null }[] {
  const fields: { label: string; value: string; target?: string | null }[] = [];
  const seenLabels = new Set<string>();
  if (payload && Object.keys(payload).length > 0) {
    const skipKeys = new Set([
      "event_type",
      "payload",
      "source",
      "note",
      // 投递控制字段（不展示给用户）
      "recipient_user_id",
      "actor_id",
      "actor_name",
      // 处理人已由 meta 区「操作者」展示，payload 里的数字 ID 不重复展示
      "resolver_id",
      // 重要程度已由卡片头部 level Tag 展示，正文不重复
      "level",
      "severity",
    ]);
    for (const [k, v] of Object.entries(payload)) {
      const label = PAYLOAD_FIELD_LABEL_FE[k] ?? k;
      if (skipKeys.has(k)) {
        seenLabels.add(label); // 标记已处理，避免 body 重复补回
        continue;
      }
      if (v === null || v === undefined) continue;
      const value = humanizeFeValue(k, v);
      if (value === "" || value === "无") {
        seenLabels.add(label);
        continue;
      }
      fields.push({ label, value, target: fieldTarget(k, value) });
      seenLabels.add(label);
    }
  }
  // 反馈通知：comment 已在 payload 剥离历史，body 不再补充（避免重复/残留）
  if (templateCode && templateCode.startsWith("feedback.")) return fields;
  const text = formatNotifyBody(body);
  if (text) {
    for (const line of text.split("\n")) {
      const idx = line.indexOf("：");
      if (idx > 0) {
        const rawLabel = line.slice(0, idx);
        const label = PAYLOAD_FIELD_LABEL_FE[rawLabel] ?? rawLabel;
        if (seenLabels.has(label)) continue; // payload 已渲染（值更友好）
        const rawValue = line.slice(idx + 1);
        if (rawValue !== "" && rawValue !== "无") {
          fields.push({ label, value: rawValue, target: fieldTarget(label, rawValue) });
          seenLabels.add(label);
        }
      } else if (line !== "") {
        // 自然语言整行：剥离反馈历史（[时间戳] status=...）后保留
        const cleaned = cleanFeedbackComment(line);
        if (cleaned) fields.push({ label: "", value: cleaned });
      }
    }
  }
  return fields;
}

// 所属模块（业务术语）
const SOURCE_LABEL: Record<string, string> = {
  metric: "指标",
  lineage: "血缘",
  quality: "数据质量",
  governance: "治理合规",
  semantic: "指标口径",
  system: "系统",
  scheduler: "定时任务",
};

// 关联对象（把 metric#5 这样的技术引用翻译成业务可读的「指标 #5」）
const REF_TYPE_LABEL: Record<string, string> = {
  metric: "指标",
  metric_definition: "指标",
  metric_version: "指标版本",
  metric_template: "指标模板",
  quality_rule: "质量规则",
  quality_event: "数据质量告警",
  conflict: "口径冲突",
  grant: "权限授权",
  term: "业务术语",
  dimension: "维度",
  lineage_edge: "血缘关系",
  data_source: "数据源",
  db_catalog: "目录实体",
  notification: "通知",
  reconciliation: "数据对账",
  benchmark: "参照基准",
  event: "消息",
};

// 影响/待办提示：每条通知对接收者的业务含义——「对我意味着什么、需要我做什么」。
// 从产品角度让用户一眼看到通知的意图，而非只看到"发生了什么事件"。
const IMPACT_TEXT: Record<string, string> = {
  "metric.created": "新指标已创建，下一步提交审核。",
  "metric.submitted": "指标已提交审核，等待审批结果。",
  "metric.approved": "指标已通过审核，可对外发布使用。",
  "metric.rejected": "指标审核未通过，请查看原因并修改后重新提交。",
  "metric.deprecated": "指标已废弃，消费方应改用后继指标。",
  "metric.voided": "指标已作废，不再提供服务。",
  "metric.promoted": "指标已升级发布，口径版本已更新。",
  "metric.rolled_back": "指标已回滚至上一生效版本。",
  "metric.emergency_published": "指标已紧急发布，请注意补审确认。",
  "metric.health_critical": "指标健康度偏低，请及时补充缺失治理项。",
  "metric.rename_required": "指标命名不合规，请尽快修改编码。",
  conflict_open: "有新的口径冲突待处理，请及时仲裁。",
  conflict_ruled: "口径冲突已裁决，请以权威口径为准。",
  conflict_escalated: "口径冲突已升级，需上级介入裁决。",
  conflict_reopened: "口径冲突已重新打开，需再次仲裁。",
  "quality.anomaly": "数据质量异常，请及时核查处理。",
  "reconciliation.alert": "数据对账发现偏差，请确认口径一致性。",
  "grant.granted": "权限已授予，可访问对应资源。",
  "grant.revoked": "权限已收回，将无法访问对应资源。",
  "grant.expired": "授权已过期，如需继续访问请联系管理员续期。",
  "grant.expiring_soon": "授权即将到期，请提前联系管理员续期。",
  "pii.propagated": "敏感数据已扩散，请关注合规风险。",
  "pii.reviewed": "敏感数据已完成合规复核。",
  "pii.review_pending": "有 PII 复核待办，请及时处理。",
  "classification.changed": "数据分类已变更，请关注权限影响。",
  "classification.done": "数据分类已完成。",
  "escalation.triggered": "告警已升级，请优先处理。",
  "feedback.status_updated": "反馈状态已更新，请查看处理结果。",
  "nps.submitted": "新的满意度评价已提交。",
  "benchmark.imported": "参照基准已导入，可用于口径对比。",
  "audit.capacity_warning": "审计存储容量告警，请关注归档清理。",
  catalog_registered: "新数据目录已注册，可接入资产。",
  catalog_schema_drifted: "目录结构发生漂移，请核对字段映射。",
  lineage_parsed: "血缘关系已解析，可查看影响链路。",
  lineage_ingested: "血缘关系已接入，影响链路已更新。",
  "catalog.deprecated": "数据目录已废弃，相关消费请迁移。",
  "collect.degraded": "采集降级，数据时效可能受影响。",
  "collect.failed": "采集任务失败，请检查数据源连接。",
  "catalog.connection_failed": "数据源连接失败，请检查配置与网络。",
  "degradation.state_changed": "系统依赖状态变更，请关注服务可用性。",
  "user.created": "账号已创建，可登录使用平台。",
  "user.status_changed": "账号状态已变更，请关注访问权限。",
  "user.password_reset": "密码已重置，请使用新密码登录。",
  "org.status_changed": "组织状态已变更，请关注成员权限。",
};

// 待处理类事件（与后端 TODO_EVENT_TYPES 对齐）：接收者需要采取行动，而非仅被告知。
// 这类通知卡片加「待处理」标记 + 行动按钮；也可用「仅看待处理」筛选聚焦。
const NEEDS_ACTION = new Set<string>([
  "metric.submitted",
  "metric.rename_required",
  "metric.health_critical",
  "conflict_open",
  "conflict_escalated",
  "conflict_reopened",
  "pii_conflict",
  "pii.review_pending",
  "quality.anomaly",
  "reconciliation.alert",
  "escalation.triggered",
  "audit.capacity_warning",
  "catalog_schema_drifted",
  "collect.degraded",
  "collect.failed",
  "catalog.connection_failed",
  "grant.expiring_soon",
  "grant.expired",
]);

// 待处理通知的行动按钮：一句话告诉用户「去做什么」（label + 跳转目标）。
// 有 metric_code 的优先直达指标详情，其余跳对应业务页。
function actionFor(templateCode: string, payload: Record<string, unknown>): { label: string; target: string } | null {
  const code = payload.metric_code ? `/detail/${encodeURIComponent(String(payload.metric_code))}` : null;
  switch (templateCode) {
    case "metric.submitted":
      return { label: "去审批", target: code ?? "/metrics/review" };
    case "metric.rename_required":
    case "metric.health_critical":
      return { label: "去处理", target: code ?? "/detail" };
    case "conflict_open":
    case "conflict_escalated":
    case "conflict_reopened":
      return { label: "去仲裁", target: "/review" };
    case "pii_conflict":
    case "pii.review_pending":
      return { label: "去复核", target: "/governance" };
    case "quality.anomaly":
    case "reconciliation.alert":
    case "escalation.triggered":
      return { label: "去处理", target: "/quality" };
    case "audit.capacity_warning":
      return { label: "去清理", target: "/audit" };
    case "catalog_schema_drifted":
    case "collect.degraded":
    case "collect.failed":
    case "catalog.connection_failed":
      return { label: "去检查", target: "/data-sources" };
    case "grant.expiring_soon":
    case "grant.expired":
      return { label: "去续期", target: "/account" };
    default:
      return null;
  }
}

// 字段值 → 跳转目标：关联对象在通知里可直接点击直达（指标→详情、冲突→仲裁、反馈→反馈中心）。
function fieldTarget(key: string, value: string): string | null {
  if (key === "metric_code" || key === "指标编码") return `/detail/${encodeURIComponent(value)}`;
  if (key === "conflict_id" || key === "冲突编号") return "/review";
  if (key === "feedback_id" || key === "反馈编号") return "/feedback";
  if (key === "source_id" || key === "数据源ID") return "/data-sources";
  return null;
}

// 可选渠道：后端无短信实现，不提供 sms（避免订阅了永不投递的渠道）
const CHANNELS = ["email", "webhook", "in_app"];
// 订阅可选事件：与后端 EventBus 实际订阅集合对齐（backend/app/main.py _BUSINESS_EVENT_TYPES）。
// 移除幽灵事件（metric.published / governance.grant / lineage.change / system.notice），
// 保留 quality/conflict/governance/classification/escalation + metric.* 九种。
export const EVENT_TYPES = [
  "metric.created",
  "metric.submitted",
  "metric.approved",
  "metric.rejected",
  "metric.deprecated",
  "metric.promoted",
  "metric.rolled_back",
  "metric.emergency_published",
  "metric.health_critical",
  "quality.anomaly",
  "reconciliation.alert",
  "benchmark.imported",
  "conflict_open",
  "conflict_ruled",
  "conflict_escalated",
  "pii_conflict",
  "grant.granted",
  "grant.revoked",
  "grant.expired",
  "pii.reviewed",
  "pii.propagated",
  "classification.changed",
  "classification.done",
  "escalation.triggered",
  "feedback.status_updated",
  "nps.submitted",
  "audit.capacity_warning",
  // 三梯队通知接入：采集/血缘断链 + 降级/冲突重开（走 EventBus 订阅扇出）
  "catalog_registered",
  "catalog_schema_drifted",
  "lineage_parsed",
  "lineage_ingested",
  "degradation.state_changed",
  "conflict_reopened",
];

// 订阅消息类型按业务模块分组（新增订阅弹窗下拉用 OptGroup，避免 35 项平铺难找）
export const EVENT_TYPE_GROUPS: { label: string; options: { value: string; label: string }[] }[] = [
  {
    label: "指标生命周期",
    options: ["metric.created", "metric.submitted", "metric.approved", "metric.rejected", "metric.deprecated", "metric.promoted", "metric.rolled_back", "metric.emergency_published", "metric.health_critical"].map((v) => ({ value: v, label: eventTypeLabel(v) })),
  },
  {
    label: "口径冲突",
    options: ["conflict_open", "conflict_ruled", "conflict_escalated", "conflict_reopened"].map((v) => ({ value: v, label: eventTypeLabel(v) })),
  },
  {
    label: "数据质量",
    options: ["quality.anomaly", "reconciliation.alert", "audit.capacity_warning"].map((v) => ({ value: v, label: eventTypeLabel(v) })),
  },
  {
    label: "权限与安全",
    options: ["grant.granted", "grant.revoked", "grant.expired", "pii_conflict", "pii.reviewed", "pii.propagated", "classification.changed", "classification.done", "escalation.triggered"].map((v) => ({ value: v, label: eventTypeLabel(v) })),
  },
  {
    label: "采集与血缘",
    options: ["catalog_registered", "catalog_schema_drifted", "lineage_parsed", "lineage_ingested"].map((v) => ({ value: v, label: eventTypeLabel(v) })),
  },
  {
    label: "反馈与满意度",
    options: ["feedback.status_updated", "nps.submitted", "benchmark.imported"].map((v) => ({ value: v, label: eventTypeLabel(v) })),
  },
  {
    label: "系统",
    options: ["degradation.state_changed"].map((v) => ({ value: v, label: eventTypeLabel(v) })),
  },
];

// 一键推荐订阅：对绝大多数用户都有价值的核心事件（审批/冲突/质量/权限到期/反馈）。
// 幂等 upsert——已有订阅覆盖为启用，无订阅新建 in_app。
const RECOMMENDED_EVENTS = [
  "metric.submitted",
  "metric.approved",
  "metric.rejected",
  "metric.health_critical",
  "conflict_open",
  "conflict_ruled",
  "conflict_escalated",
  "quality.anomaly",
  "reconciliation.alert",
  "grant.expired",
  "grant.expiring_soon",
  "feedback.status_updated",
];

// 通知状态 → 卡片左侧状态条颜色（沿「校准仪表」设计语言：成功=数据青绿、失败=告警红、待发送=信号琥珀）
const NOTIF_STATUS_BAR: Record<string, string> = {
  SENT: "#0e7c86",
  FAILED: "#d64545",
  PENDING: "#e8862d",
};

function NotifListTab() {
  const [items, setItems] = useState<Notification[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  // 收件箱筛选：类型 / 已读状态 / 时间段 / 仅待处理（产品化，对应后端筛选参数）
  const [typeFilter, setTypeFilter] = useState<string | undefined>();
  const [readFilter, setReadFilter] = useState<string | undefined>();
  const [timeFilter, setTimeFilter] = useState<number | undefined>();
  const [todoOnly, setTodoOnly] = useState(false);
  // 对象级聚焦：按指标编码/冲突编号等业务对象过滤（Task D）
  const [objectKey, setObjectKey] = useState<string | undefined>();
  // 同类聚合开关：相同消息类型合并为一张卡片（避免刷屏）
  const [grouped, setGrouped] = useState(false);
  const navigate = useNavigate();

  async function load(p = page, ps = pageSize, tt = typeFilter, rf = readFilter, tf = timeFilter, to = todoOnly, ok = objectKey) {
    setLoading(true);
    try {
      const res = await listNotifications({
        template_code: tt,
        read_state: rf,
        days: tf,
        todo_only: to,
        object_key: ok,
        page: p,
        page_size: ps,
      });
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize, typeFilter, readFilter, timeFilter, todoOnly, objectKey]);

  // 筛选变化 → 回到第一页（page 与筛选 state 批量更新，单一 effect 只触发一次加载，避免重复请求）
  function changeType(v?: string) {
    setPage(1);
    setTypeFilter(v);
  }
  function changeRead(v?: string) {
    setPage(1);
    setReadFilter(v);
  }
  function changeTime(v?: number) {
    setPage(1);
    setTimeFilter(v);
  }
  function changeTodo(v: boolean) {
    setPage(1);
    setTodoOnly(v);
  }
  function changeGrouped(v: boolean) {
    setGrouped(v);
  }
  function changeObjectKey(v?: string) {
    setPage(1);
    setObjectKey(v || undefined);
  }

  // 重试投递失败的通知（Task A）：成功后刷新并同步全局角标
  async function handleRetry(n: Notification) {
    try {
      const updated = await retryNotification(n.id);
      message.success(updated.status === "SENT" ? "重试成功，已重新投递" : "重试失败，请查看原因");
      load();
      notifyNotifChanged();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "重试失败");
    }
  }

  // 标记待办类通知为「已处理」（Task B）：处理后不再出现在「仅待处理」
  async function handleMarkHandled(n: Notification) {
    try {
      await markNotificationHandled(n.id);
      message.success("已标记为已处理");
      load();
      notifyNotifChanged();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    }
  }

  async function handleMarkRead(n: Notification) {
    if (n.read_at) return;
    try {
      await markNotificationRead(n.id);
      // 本地即时更新，避免整页刷新闪烁：
      // - 若当前按「未读/仅待处理」过滤 → 该条不再匹配，从列表移除
      // - 否则 → 仅更新该条 read_at（卡片立即变已读样式）
      if (readFilter === "unread" || todoOnly) {
        setItems((prev) => prev.filter((x) => x.id !== n.id));
        setTotal((prev) => Math.max(0, prev - 1));
      } else {
        setItems((prev) =>
          prev.map((x) => (x.id === n.id ? { ...x, read_at: new Date().toISOString() } : x)),
        );
      }
      notifyNotifChanged();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    }
  }

  async function handleReadAll() {
    try {
      await markAllNotificationsRead();
      message.success("已全部标记为已读");
      load();
      notifyNotifChanged();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    }
  }

  async function handleDelete(n: Notification) {
    try {
      await deleteNotification(n.id);
      // 本地即时移除，避免整页刷新闪烁
      setItems((prev) => prev.filter((x) => x.id !== n.id));
      setTotal((prev) => Math.max(0, prev - 1));
      notifyNotifChanged();
      // 若当前页被删空且非第一页，回退一页（触发按 [page] 的 useEffect 重载）
      if (items.length <= 1 && page > 1) setPage((p) => p - 1);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    }
  }

  async function handleClear() {
    try {
      await deleteAllNotifications();
      message.success("已清空全部通知");
      load();
      notifyNotifChanged();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    }
  }

  // 点击通知：未读先自动标记已读（消除全局角标红点），再深链到对应业务页。
  // 跳转与标记已读并行不阻塞——mark 失败不影响跳转（best-effort）。
  function handleOpen(n: Notification) {
    if (!n.read_at) {
      markNotificationRead(n.id)
        .then(() => {
          notifyNotifChanged();
          load();
        })
        .catch(() => {});
    }
    const tpl = n.template_code ?? "";
    const payload = (n.payload ?? {}) as Record<string, unknown>;
    if (tpl.startsWith("metric.") && payload.metric_code) {
      navigate(`/detail/${String(payload.metric_code)}`);
      return;
    }
    if (tpl.startsWith("conflict")) {
      navigate("/review");
      return;
    }
    // 账号安全 / 组织状态 / 授权变更 → 个人中心（用户本人视角：我的账号 / 我的授权）
    if (tpl.startsWith("user.") || tpl.startsWith("org.") || tpl.startsWith("grant.")) {
      navigate("/account");
      return;
    }
    if (tpl.startsWith("collect.") || tpl.startsWith("catalog.")) {
      navigate("/data-sources");
      return;
    }
    // PII / 分级分类 → 权限治理（compliance_officer 经 governance:view 权限点可访问）
    if (tpl.startsWith("pii.") || tpl.startsWith("classification.")) {
      navigate("/governance");
      return;
    }
    message.info("该通知没有关联的可跳转页面");
  }

  const unreadCount = items.filter((n) => !n.read_at).length;

  // 聚合模式：相同消息类型合并为一张卡片（×N 计数），避免同类通知刷屏。
  // 仅合并当前页内同类项（分页语义内准确，不跨页误并）。
  const groups = useMemo(() => {
    const m = new Map<string, Notification[]>();
    for (const n of items) {
      const key = n.template_code ?? n.title;
      const arr = m.get(key) ?? [];
      arr.push(n);
      m.set(key, arr);
    }
    return [...m.entries()].map(([key, list]) => ({ key, list }));
  }, [items]);
  const displayGroups = grouped ? groups : items.map((n) => ({ key: String(n.id), list: [n] }));

  // 单卡片渲染：状态条 / 标题 / 重要程度 / 影响 / 字段（关联对象可点击）/ 待处理行动按钮
  function renderCard(n: Notification, extraCount?: number) {
    const statusKey = n.status ?? "";
    const payload = (n.payload ?? {}) as Record<string, unknown>;
    const levelRaw = String(payload.level ?? payload.severity ?? "");
    const levelHigh = ["ERROR", "CRITICAL", "P0", "P1"].includes(levelRaw);
    const levelWarn = ["WARN", "WARNING", "P2"].includes(levelRaw);
    const fields = parseNotifyBodyFields(n.body, n.payload, n.template_code);
    const singleLine = fields.length <= 1;
    const unread = !n.read_at;
    const handled = !!n.handled_at;
    const needsAction = NEEDS_ACTION.has(n.template_code ?? "");
    const action = actionFor(n.template_code ?? "", payload);
    const failed = n.status === "FAILED";
    const cls = [
      "notif-card",
      unread ? "notif-unread" : "",
      handled ? "notif-handled" : "",
      levelHigh ? "notif-level-error" : levelWarn ? "notif-level-warn" : "",
    ]
      .filter(Boolean)
      .join(" ");
    return (
      <div key={n.id} className={cls} onClick={() => handleOpen(n)}>
        <div className="notif-bar" style={{ background: NOTIF_STATUS_BAR[statusKey] ?? "#c4cbd6" }} />
        <div className="notif-main">
          <div className="notif-head">
            <span className="notif-title">{eventTypeLabel(n.title)}</span>
            {extraCount && extraCount > 1 && <Tag className="notif-count" color="cyan">×{extraCount}</Tag>}
            {levelRaw && LEVEL_CN_FE[levelRaw] && (
              <Tag color={levelHigh ? "error" : levelWarn ? "warning" : "default"}>{LEVEL_CN_FE[levelRaw]}</Tag>
            )}
            {n.template_code && eventTypeLabel(n.template_code) !== eventTypeLabel(n.title) && (
              <Tag className="notif-type" color="geekblue">{eventTypeLabel(n.template_code)}</Tag>
            )}
            <div className="notif-head-right">
              {n.sent_at && <span className="notif-sent-time">已送达 {formatCnTime(n.sent_at)}</span>}
              {handled && needsAction && <Tag color="success">已处理</Tag>}
              <Tag
                className="notif-status"
                color={n.status === "SENT" ? "success" : n.status === "FAILED" ? "error" : "warning"}
              >
                {NOTIFY_STATUS_LABEL[n.status] ?? n.status}
              </Tag>
            </div>
          </div>
          {IMPACT_TEXT[n.template_code ?? ""] && (
            <div className="notif-impact">
              <span className="notif-impact-label">影响</span>
              <span className="notif-impact-text">{IMPACT_TEXT[n.template_code ?? ""]}</span>
            </div>
          )}
          {failed && n.last_error && (
            <div className="notif-impact notif-fail-reason">
              <span className="notif-impact-label">失败原因</span>
              <span className="notif-impact-text">{n.last_error}</span>
            </div>
          )}
          {fields.length > 0 && (
            <div className={`notif-body${singleLine ? " notif-body-single" : ""}`}>
              {fields.map((f, i) =>
                f.label ? (
                  <div key={i} className="notif-body-field">
                    <span className="notif-body-label">{f.label}</span>
                    {f.target ? (
                      <Button
                        type="link"
                        size="small"
                        style={{ padding: 0, height: "auto", lineHeight: "inherit" }}
                        onClick={(e) => {
                          e.stopPropagation();
                          navigate(f.target as string);
                        }}
                      >
                        {f.value}
                      </Button>
                    ) : (
                      <span className="notif-body-value">{f.value}</span>
                    )}
                  </div>
                ) : (
                  <div key={i} className="notif-body-full">{f.value}</div>
                ),
              )}
            </div>
          )}
          <div className="notif-meta">
            <span className="notif-meta-item">
              <SendOutlined /> {CHANNEL_LABEL[(n.channel ?? "").toLowerCase()] ?? n.channel}
            </span>
            {n.ref_type && n.ref_id != null && (
              <span className="notif-meta-item">
                <LinkOutlined /> {REF_TYPE_LABEL[n.ref_type] ?? n.ref_type} #{n.ref_id}
              </span>
            )}
            {n.actor_name ? (
              <span className="notif-meta-item" title={`操作人 #${n.actor_id ?? ""}`}>
                <UserOutlined /> {n.actor_name}
              </span>
            ) : n.actor_id != null ? (
              <span className="notif-meta-item">
                <UserOutlined /> #{n.actor_id}
              </span>
            ) : null}
            <span className="notif-meta-item">
              <ClockCircleOutlined /> 触发于 {formatCnTime(n.created_at)}
            </span>
            <div className="notif-actions" onClick={(e) => e.stopPropagation()}>
              {needsAction && action && !handled && (
                <Button size="small" type="primary" ghost onClick={() => navigate(action.target)}>{action.label}</Button>
              )}
              {needsAction && !handled && (
                <Button size="small" type="link" onClick={() => handleMarkHandled(n)}>标记已处理</Button>
              )}
              {failed && (
                <Button size="small" type="link" onClick={() => handleRetry(n)}>重试投递</Button>
              )}
              {unread && (
                <Button size="small" type="link" onClick={() => handleMarkRead(n)}>标记已读</Button>
              )}
              <Button size="small" type="link" danger onClick={() => handleDelete(n)}>删除</Button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div>
      {/* 收件箱筛选：消息类型 / 已读状态 / 时间段 / 仅待处理 / 同类聚合 */}
      <div className="notif-filterbar">
        <Select
          size="small"
          allowClear
          placeholder="全部类型"
          value={typeFilter}
          onChange={changeType}
          style={{ minWidth: 170 }}
          options={EVENT_TYPES.map((c) => ({ value: c, label: eventTypeLabel(c) }))}
        />
        <Select
          size="small"
          allowClear
          placeholder="全部状态"
          value={readFilter}
          onChange={changeRead}
          style={{ minWidth: 120 }}
          options={[
            { value: "unread", label: "未读" },
            { value: "read", label: "已读" },
          ]}
        />
        <Select
          size="small"
          allowClear
          placeholder="全部时间"
          value={timeFilter}
          onChange={changeTime}
          style={{ minWidth: 120 }}
          options={[
            { value: 7, label: "近 7 天" },
            { value: 30, label: "近 30 天" },
            { value: 90, label: "近 90 天" },
          ]}
        />
        <Input
          size="small"
          allowClear
          className="notif-object-focus"
          placeholder="聚焦对象：指标编码/冲突编号…"
          value={objectKey}
          onChange={(e) => changeObjectKey(e.target.value)}
          style={{ width: 190 }}
          prefix={<SearchOutlined />}
        />
        <Tag.CheckableTag checked={todoOnly} onChange={changeTodo}>仅待处理</Tag.CheckableTag>
        <Tag.CheckableTag checked={grouped} onChange={changeGrouped}>同类聚合</Tag.CheckableTag>
      </div>
      {items.length > 0 && (
        <div className="notif-toolbar">
          <span className="muted">未读 {unreadCount} 条</span>
          <div>
            <Button size="small" icon={<CheckOutlined />} onClick={handleReadAll} disabled={unreadCount === 0}>全部已读</Button>
            <Button size="small" danger icon={<DeleteOutlined />} onClick={handleClear}>清空</Button>
          </div>
        </div>
      )}
      {loading ? (
        <div className="notif-loading">
          <Spin />
          <span className="muted">正在加载消息…</span>
        </div>
      ) : items.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无通知" style={{ padding: "32px 0" }} />
      ) : (
        <>
          <div className="notif-stream">
            {displayGroups.map((g) => renderCard(g.list[0], grouped ? g.list.length : undefined))}
          </div>
          <Pagination
            className="notif-pagination"
            current={page}
            pageSize={pageSize}
            total={total}
            showTotal={(t) => `共 ${t} 条`}
            showSizeChanger
            pageSizeOptions={[10, 20, 50]}
            onChange={(p, ps) => {
              setPage(p);
              setPageSize(ps);
            }}
          />
        </>
      )}
    </div>
  );
}

function SubscriptionsTab() {
  const [items, setItems] = useState<SubscriptionPref[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();

  async function load() {
    setLoading(true);
    try {
      const res = await listSubscriptions();
      setItems(res.items);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleCreate(values: Record<string, unknown>) {
    try {
      // 支持多选消息类型：对每个选中的事件类型各建一条订阅（同渠道不同事件=多行，后端幂等 upsert）
      const eventTypes = Array.isArray(values.event_type)
        ? (values.event_type as string[])
        : [String(values.event_type)];
      if (eventTypes.length === 0) {
        message.warning("请至少选择一个消息类型");
        return;
      }
      for (const et of eventTypes) {
        await upsertSubscription({
          channel: String(values.channel),
          event_type: et,
          enabled: true,
          threshold: values.threshold !== undefined && values.threshold !== null ? Number(values.threshold) : null,
        });
      }
      message.success(`已保存 ${eventTypes.length} 个订阅`);
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "保存失败");
    }
  }

  // 一键推荐订阅：批量启用推荐事件（in_app 渠道，幂等 upsert）
  const [recommending, setRecommending] = useState(false);
  async function handleRecommend() {
    setRecommending(true);
    try {
      for (const et of RECOMMENDED_EVENTS) {
        await upsertSubscription({ channel: "in_app", event_type: et, enabled: true });
      }
      message.success(`已为你订阅 ${RECOMMENDED_EVENTS.length} 项推荐消息`);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "推荐订阅失败");
    } finally {
      setRecommending(false);
    }
  }

  const columns = [
    { title: "送达方式", dataIndex: "channel", key: "channel", width: 130, render: (v: string) => <Tag>{CHANNEL_LABEL[(v ?? "").toLowerCase()] ?? v}</Tag> },
    { title: "消息类型", dataIndex: "event_type", key: "event", render: (v: string) => eventTypeLabel(v) },
    { title: "告警阈值", dataIndex: "threshold", key: "threshold", width: 100, render: (v: number | null) => v ?? <span className="muted">—</span> },
    { title: "是否启用", dataIndex: "enabled", key: "enabled", width: 90, render: (v: boolean) => <Tag color={v ? "success" : "default"}>{v ? "是" : "否"}</Tag> },
  ];

  return (
    <div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
        <Alert type="info" showIcon style={{ flex: 1, minWidth: 260 }} message="推荐订阅：覆盖指标审批 / 冲突仲裁 / 质量告警 / 权限到期等核心事件，一键开启即可收到站内通知。" />
        <div style={{ display: "flex", gap: 8 }}>
          <Button icon={<StarOutlined />} loading={recommending} onClick={handleRecommend}>一键推荐订阅</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>新增订阅</Button>
        </div>
      </div>
      <Table dataSource={items} columns={columns} rowKey={(r) => `${r.user_id}-${r.channel}-${r.event_type}`} loading={loading} pagination={false} locale={{ emptyText: "暂无订阅" }} />

      <Modal title="新增订阅" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="保存">
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="channel" label="送达方式" rules={[{ required: true }]}>
            <Select options={CHANNELS.map((c) => ({ value: c, label: CHANNEL_LABEL[c] ?? c }))} />
          </Form.Item>
          <Form.Item
            name="event_type"
            label="消息类型"
            rules={[{ required: true, message: "请至少选择一个消息类型" }]}
          >
            <Select
              mode="multiple"
              allowClear
              placeholder="按模块选择消息类型"
              optionFilterProp="label"
              options={EVENT_TYPE_GROUPS.map((g) => ({ label: g.label, options: g.options }))}
            />
          </Form.Item>
          <Form.Item name="threshold" label="告警阈值（可选）" extra="用于数据质量告警，达到该阈值时才推送。">
            <InputNumber min={1} style={{ width: 200 }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

function EventLogTab() {
  const [items, setItems] = useState<NotifyEventLog[]>([]);
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const res = await listNotifyEvents();
      setItems(res.items);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const columns = [
    { title: "编号", dataIndex: "id", key: "id", width: 70 },
    { title: "消息类型", dataIndex: "event_type", key: "event", render: (v: string) => eventTypeLabel(v) },
    { title: "所属模块", dataIndex: "source", key: "source", width: 110, render: (v: string | null) => (v ? SOURCE_LABEL[v] ?? v : <span className="muted">—</span>) },
    { title: "重要程度", dataIndex: "level", key: "level", width: 90, render: (v: string) => <Tag color={v === "ERROR" ? "error" : v === "WARN" ? "warning" : "default"}>{QUALITY_LEVEL_LABEL[v] ?? v}</Tag> },
    { title: "已推送", dataIndex: "notified", key: "notified", width: 90, render: (v: boolean) => <Tag color={v ? "success" : "default"}>{v ? "是" : "否"}</Tag> },
    { title: "触发时间", dataIndex: "created_at", key: "created", width: 170, render: (v: string | null) => (v ? <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> : <span className="muted">—</span>) },
  ];

  return <Table dataSource={items} columns={columns} rowKey="id" loading={loading} pagination={{ pageSize: 20 }} locale={{ emptyText: "暂无消息记录" }} />;
}

function PublishTab() {
  const [form] = Form.useForm();

  async function handlePublish(values: Record<string, unknown>) {
    try {
      const res = await publishNotifyEvent({
        event_type: String(values.event_type),
        source: values.source ? String(values.source) : null,
        level: String(values.level ?? "INFO"),
        payload: { note: values.note ? String(values.note) : null },
      });
      message.success(`消息已发送：生成 ${res.notifications} 条通知，成功送达 ${res.delivered} 条`);
      form.resetFields();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "发送失败");
    }
  }

  return (
    <div>
      <Alert type="info" showIcon style={{ marginBottom: 12 }} message="手动发送的消息会按您的订阅设置推送到对应渠道（邮件 / 接口推送 / 短信 / 站内消息）。" />
      <Card title="发送消息" size="small">
        <Form form={form} layout="vertical" onFinish={handlePublish}>
          <Form.Item name="event_type" label="消息类型" rules={[{ required: true }]}>
            <Select options={EVENT_TYPES.map((c) => ({ value: c, label: eventTypeLabel(c) }))} />
          </Form.Item>
          <Form.Item name="source" label="所属模块">
            <Select allowClear options={["metric", "lineage", "quality", "governance", "semantic", "system", "scheduler"].map((c) => ({ value: c, label: SOURCE_LABEL[c] ?? c }))} />
          </Form.Item>
          <Form.Item name="level" label="重要程度" initialValue="INFO">
            <Select options={["INFO", "WARN", "ERROR"].map((c) => ({ value: c, label: QUALITY_LEVEL_LABEL[c] ?? c }))} />
          </Form.Item>
          <Form.Item name="note" label="补充说明">
            <Input.TextArea rows={2} />
          </Form.Item>
          <Button type="primary" htmlType="submit">发送消息</Button>
        </Form>
      </Card>
    </div>
  );
}

export function Notifications() {
  const tabItems = [
    { key: "list", label: "我的通知", children: <NotifListTab /> },
    { key: "subs", label: "订阅设置", children: <SubscriptionsTab /> },
    { key: "events", label: "消息记录", children: <EventLogTab /> },
    { key: "publish", label: "发送消息", children: <PublishTab /> },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Collaboration / Notifications</div>
          <h2>通知中心</h2>
          <p>集中查看系统发送给您的消息、管理接收偏好，并跟踪每条消息的送达情况。</p>
        </div>
      </div>
      <Card styles={{ body: { paddingTop: 8 } }}>
        <Tabs items={tabItems} />
      </Card>
    </div>
  );
}
