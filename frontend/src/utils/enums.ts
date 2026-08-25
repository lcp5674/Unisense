// 共享枚举中文映射 —— 统一各页面「枚举英文直出 → 中文 label」
// 约定：value 保持英文（对接后端），label 为中文展示；未知值回退显示原值。

/** 查表并兜底：value 不在映射中时原样返回。
 *  大小写无关：后端枚举常为大写（TABLE/VIEW/FIELD），前端映射键为小写，
 *  先精确匹配，再尝试小写/大写归一，避免标签回退裸露英文（P2-18）。 */
export function enumLabel(map: Record<string, string>, value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "";
  return map[value] ?? map[value.toLowerCase()] ?? map[value.toUpperCase()] ?? value;
}

// ---- 指标元数据 ----

export const METRIC_TYPE_LABEL: Record<string, string> = {
  atomic: "原子指标",
  derived: "派生指标",
  composite: "复合指标",
};

export const METRIC_TIER_LABEL: Record<string, string> = {
  T1: "T1（核心）",
  T2: "T2（重要）",
  T3: "T3（一般）",
};

export const AGGREGATION_LABEL: Record<string, string> = {
  SUM: "求和",
  AVG: "平均",
  COUNT: "计数",
  COUNT_DISTINCT: "去重计数",
  LAST_VALUE: "末值",
  MAX: "最大值",
  MIN: "最小值",
};

export const TIME_SEMANTICS_LABEL: Record<string, string> = {
  PERIOD: "区间累计",
  YTD: "年初至今",
  TTM: "滚动 12 月",
  AVG: "期间平均",
  MOM: "环比",
  YOY: "同比",
};

// 对齐后端 models/metric.py freshness_type（REALTIME/T0/T1/HOURLY，无 T2/T3）
export const FRESHNESS_LABEL: Record<string, string> = {
  REALTIME: "实时",
  T0: "准实时(T0)",
  T1: "T+1",
  HOURLY: "小时级",
};

export const DW_LAYER_LABEL: Record<string, string> = {
  ODS: "ODS 贴源层",
  DWD: "DWD 明细层",
  DWS: "DWS 汇总层",
  ADS: "ADS 应用层",
  DM: "DM 集市层",
};

export const SERVING_MODE_LABEL: Record<string, string> = {
  BATCH_ONLY: "仅批处理",
  REALTIME_ONLY: "仅实时",
  BATCH_REALTIME_DUAL: "批实双跑",
};

export const ADDITIVITY_LABEL: Record<string, string> = {
  ADDITIVE: "全可加",
  SEMI_ADDITIVE: "半可加",
  NON_ADDITIVE: "不可加",
};

export const METRIC_STATUS_LABEL: Record<string, string> = {
  DRAFT: "草稿",
  EXPERIMENTAL: "实验",
  REVIEW: "审核中",
  PUBLISHED: "已发布",
  DEPRECATED: "已废弃",
  DATA_SOURCE_DROPPED: "数据源下线",
};

/** 指标状态 → Ant Tag 颜色（P2-14 收敛：MetricCatalog/MetricDetail 共用） */
export const METRIC_STATUS_COLOR: Record<string, string> = {
  DRAFT: "default",
  EXPERIMENTAL: "processing",
  REVIEW: "warning",
  PUBLISHED: "success",
  DEPRECATED: "error",
};

/** 指标版本变更类型（MetricVersion.change_type：CREATE/UPDATE/BREAKING；PUBLISH/DEPRECATE 兜底防御） */
export const CHANGE_TYPE_LABEL: Record<string, string> = {
  CREATE: "创建",
  UPDATE: "变更",
  BREAKING: "破坏性变更",
  PUBLISH: "发布",
  DEPRECATE: "废弃",
};

// ---- 指标关联/推荐边类型（详情页「看过此指标的人还看了」等）----
export const METRIC_RELATION_EDGE_LABEL: Record<string, string> = {
  DERIVED_FROM: "派生自",
  CONSUMED_BY: "被消费",
  LINEAGE: "关联",
  POPULAR: "热门",
  RECENT: "最新",
};

// ---- 血缘图边类型（LineageImpact 血缘影响面）----
export const LINEAGE_EDGE_TYPE_LABEL: Record<string, string> = {
  DERIVED_FROM: "派生自",
  LINEAGE_UP: "上游依赖",
  LINEAGE_DOWN: "下游影响",
  CONSUMED_BY: "被消费",
  EXTERNAL_BREAK: "外部断链",
  METRIC_DERIVES: "指标派生",
  METRIC_DEPENDS_ON: "指标依赖",
  TABLE_TO_FIELD: "表到字段",
  FIELD_TO_TABLE: "字段到表",
  SQL_PARSE: "SQL 解析",
  USES_DIMENSION: "使用维度",
  READS_COLUMN: "读取字段",
};

// ---- 查询消费 ----

export const DATE_RANGE_LABEL: Record<string, string> = {
  today: "今天",
  last_7d: "近 7 天",
  last_30d: "近 30 天",
  last_90d: "近 90 天",
  ytd: "年初至今",
  last_365d: "近 365 天",
};

export const GRANULARITY_LABEL: Record<string, string> = {
  hour: "小时",
  daily: "日",
  day: "日",
  weekly: "周",
  week: "周",
  monthly: "月",
  month: "月",
  quarterly: "季",
  quarter: "季",
  year: "年",
};

/** 单位中文映射（跨模块统一：指标详情/目录/资产地图均复用，避免一处中文一处英文 code） */
export const UNIT_LABEL: Record<string, string> = {
  CNY: "元",
  cnt: "次",
  PERSON: "人",
  PERCENT: "%",
  MINUTE: "分钟",
  HOUR: "小时",
  DAY: "天",
  MONTH: "月",
  YEAR: "年",
};

// ---- 数据源 ----

export const SOURCE_HEALTH_LABEL: Record<string, string> = {
  healthy: "健康",
  degraded: "降级",
  unhealthy: "不健康",
  unknown: "未知",
};

export const COLLECTION_MODE_LABEL: Record<string, string> = {
  FULL: "全量采集",
  INCREMENTAL: "增量采集",
};

// 核心依赖健康状态（dependency_health.status：HEALTHY/DEGRADED/UNAVAILABLE）
export const DEP_STATUS_LABEL: Record<string, string> = {
  HEALTHY: "健康",
  DEGRADED: "降级",
  UNAVAILABLE: "不可用",
};

// 熔断器状态（dependency_health.circuit_state：CLOSED/OPEN/HALF_OPEN）
export const CIRCUIT_STATE_LABEL: Record<string, string> = {
  CLOSED: "熔断闭合",
  OPEN: "熔断开启",
  HALF_OPEN: "半开恢复",
};

// 采集运行状态（collection_run.status：RUNNING/COMPLETED/FAILED）
export const COLLECTION_RUN_STATUS_LABEL: Record<string, string> = {
  RUNNING: "运行中",
  COMPLETED: "已完成",
  FAILED: "失败",
};

// 指标健康度分级（metric_health_score.level：EXCELLENT/GOOD/WARNING/CRITICAL）
export const METRIC_HEALTH_LEVEL_LABEL: Record<string, string> = {
  EXCELLENT: "优秀",
  GOOD: "良好",
  WARNING: "警告",
  CRITICAL: "严重",
};

// ---- 质量 ----

// 消息/事件「重要程度」（人工发送消息等 INFO/WARN/ERROR）
export const QUALITY_LEVEL_LABEL: Record<string, string> = {
  ERROR: "错误",
  WARN: "警告",
  INFO: "提示",
};

// 质量事件严重级（backend models/quality.py QualitySeverity：P0/P1/P2）
export const QUALITY_SEVERITY_LABEL: Record<string, string> = {
  P0: "P0 紧急",
  P1: "P1 严重",
  P2: "P2 一般",
};

export const RULE_TYPE_LABEL: Record<string, string> = {
  COMPLETENESS: "完整性",
  ACCURACY: "准确性",
  TIMELINESS: "及时性",
  CONSISTENCY: "一致性",
  UNIQUENESS: "唯一性",
  VALIDITY: "有效性",
  WAVE_DIFF: "波动差异",
  CROSS_SOURCE: "跨源核验",
};

export const RULE_MODE_LABEL: Record<string, string> = {
  static: "静态阈值",
  dynamic_baseline: "动态基线",
  yoy_woy: "同比/环比",
  cross_source: "跨源比对",
};

export const QUALITY_EVENT_STATUS_LABEL: Record<string, string> = {
  OPEN: "待处理",
  ACK: "已确认",
  RESOLVED: "已解决",
  CLOSED: "已关闭",
};

// ---- 质量事件影响风险（对齐 QualitySeverity：P0/P1/P2）----

/** 严重级 → 业务影响与风险说明（运营一眼看懂"这事有多严重、影响什么"） */
export const QUALITY_SEVERITY_IMPACT: Record<string, string> = {
  P0: "核心指标异常，直接影响对外报表或高层决策，须立即响应",
  P1: "重要指标出现偏差，可能影响业务分析结论，须尽快处理",
  P2: "一般指标轻微偏差，影响有限，按常规流程处理",
};

/** 规则类型 → 数据质量风险描述（解释"这条规则在防什么"） */
export const QUALITY_RULE_RISK: Record<string, string> = {
  COMPLETENESS: "数据缺失/空值，指标值可能被低估",
  ACCURACY: "数据不准确，口径或取值有误",
  TIMELINESS: "数据时效延迟，影响实时性",
  CONSISTENCY: "多源口径不一致，同指标不同结果",
  UNIQUENESS: "存在重复数据，计数被放大",
  VALIDITY: "数据不合规/非法值，超出业务允许范围",
  WAVE_DIFF: "波动超历史基线，可能为业务突变或采集异常",
  CROSS_SOURCE: "跨源结果不一致，需定位异常来源",
};

/** 异常模式（repair_suggestion.pattern）→ 中文描述 */
export const QUALITY_PATTERN_LABEL: Record<string, string> = {
  static_threshold_breach: "静态阈值越界",
  dynamic_baseline_deviation: "动态基线偏离",
  period_over_period_delta: "同环比波动",
  cross_source_spread: "跨源差异",
  threshold_breach: "阈值越界",
};

export const RECONCILIATION_STATUS_LABEL: Record<string, string> = {
  // 质量对账（models/quality.py：OK/WARN/ALERT/CONFIRMED）
  OK: "正常",
  ALERT: "偏差告警",
  WARN: "需关注",
  CONFIRMED: "已确认",
  // 维度对账（models/dimension.py：PENDING/APPROVED/REJECTED）
  PENDING: "待复核",
  APPROVED: "已通过",
  REJECTED: "已驳回",
};

// ---- 通知 / 健康 ----

// 对齐后端 NotifyStatus（PENDING/SENT/FAILED）；已读是 read_at 字段，非状态值
export const NOTIFY_STATUS_LABEL: Record<string, string> = {
  PENDING: "待发送",
  SENT: "已发送",
  FAILED: "发送失败",
};

export const HEALTH_LEVEL_LABEL: Record<string, string> = {
  EXCELLENT: "优秀",
  GOOD: "良好",
  WARNING: "警告",
  CRITICAL: "危急",
};

// ---- 实体类型（审计 / 资产） ----

export const ENTITY_TYPE_LABEL: Record<string, string> = {
  table: "表",
  view: "视图",
  field: "字段",
  metric: "指标",
  term: "术语",
  dimension: "维度",
  data_source: "数据源",
  db_catalog: "目录实体",
  metric_definition: "指标定义",
  metric_template: "指标模板",
  metric_version: "指标版本",
  conflict: "口径冲突",
  lineage_edge: "血缘边",
  lineage: "数据血缘",
  grant: "授权",
  grants: "授权",
  // 审计实体类型补全（对齐后端 audit_i18n.ENTITY_LABELS，避免兜底裸露英文）
  metric_description: "指标描述",
  metric_term: "指标术语",
  metric_dimension: "指标维度",
  column_description: "字段",
  api_client: "消费接入方",
  sensitive_rule: "敏感规则",
  dict_item: "参照数据项",
  glossary_conflict: "术语冲突",
  term_relation: "术语关联",
  event_log: "通知事件",
  quickbi_report: "QuickBI 报表",
  nl_query: "AI 问数",
  external_benchmark: "外部基准",
  dimension_mapping: "维度映射",
  dimension_member: "维度成员",
  reconciliation_record: "对账记录",
  quality_event: "质量事件",
  quality_observation: "质量观测",
  erasure_request: "被遗忘权申请",
  ai_config: "AI 配置",
  asset_pii: "资产 PII",
  organization: "组织",
  quality_rule: "质量规则",
  notification: "通知",
  // 审计日志常用
  user: "用户",
  auth: "认证",
  config: "系统配置",
  audit: "审计归档",
  classification: "敏感分类",
  pii: "敏感数据",
  benchmark: "参照基准",
  reconciliation: "数据对账",
  member: "维度成员",
  catalog: "目录实体",
  rule: "质量规则",
  schedule: "采集调度",
  feedback: "用户反馈",
  nps: "满意度评价",
  preference: "用户偏好",
  secret: "凭据密钥",
  template: "指标模板",
  role: "角色",
  erasure: "数据擦除",
  llm_config: "LLM 配置",
  llm: "AI 模型",
  subject_domain: "主题域",
  system_dict: "参照数据",
  scope: "作用域",
  session: "会话",
  tracking: "行为追踪",
  governance: "治理合规",
  audit_log: "审计日志",
  data_quality: "数据质量",
  data_governance: "数据治理",
  data_security: "数据安全",
  data_privacy: "数据隐私",
  data_compliance: "数据合规",
  data_catalog: "数据目录",
  data_lineage: "数据血缘",
  data_masking: "数据脱敏",
  data_classification: "数据分类",
  data_erasure: "数据擦除",
  data_export: "数据导出",
  data_import: "数据导入",
  data_backup: "数据备份",
  data_archive: "数据归档",
  data_migration: "数据迁移",
  data_sync: "数据同步",
  data_transform: "数据转换",
  data_validation: "数据验证",
  data_anomaly: "数据异常",
  data_profiling: "数据剖析",
  data_sampling: "数据采样",
  data_quality_rule: "数据质量规则",
  data_quality_check: "数据质量检查",
  data_quality_report: "数据质量报告",
  data_governance_policy: "数据治理策略",
  data_governance_role: "数据治理角色",
  data_governance_rule: "数据治理规则",
  data_governance_process: "数据治理流程",
  data_security_policy: "数据安全策略",
  data_security_role: "数据安全角色",
  data_security_rule: "数据安全规则",
  data_security_process: "数据安全流程",
};

// ---- 冲突类型（conflict/models.py ConflictType，snake_case 值域）----

export const CONFLICT_TYPE_LABEL: Record<string, string> = {
  same_name_diff_def: "同名不同义",
  same_def_diff_name: "同义不同名",
  grain_unit: "粒度/单位冲突",
  cross_domain_same_def: "跨域同口径异源",
  version_conflict: "口径版本冲突",
  pii: "PII 冲突",
};

// ---- 冲突严重度 ----

export const CONFLICT_SEVERITY_LABEL: Record<string, string> = {
  high: "高",
  medium: "中",
  low: "低",
};

// ---- 裁决决策（conflict/schemas.py RulingDecision：choose_canonical / merge / keep_diff）----

export const RULING_DECISION_LABEL: Record<string, string> = {
  choose_canonical: "选为权威",
  merge: "合并口径",
  keep_diff: "保留差异",
  // 前端决策归一化前的兼容值（service 层 ACCEPT/REJECT 语义映射），按含义归类
  choose_existing: "选现有为权威",
  choose_candidate: "选候选为权威",
};

// ---- 埋点（对齐 frontend track() 上报的事件类型全集）----

export const TRACKING_EVENT_LABEL: Record<string, string> = {
  // 通用
  page_view: "页面浏览",
  view: "页面浏览",
  button_click: "按钮点击",
  search: "搜索",
  export: "导出",
  // 总览 / 待办 / 收藏
  dashboard_view: "总览访问",
  todo_center_view: "待办中心访问",
  favorites_view: "收藏列表访问",
  favorite_add: "添加收藏",
  favorite_remove: "取消收藏",
  // 指标
  metric_view: "指标查看",
  metric_search: "指标搜索",
  metric_detail_view: "指标详情查看",
  metric_create: "注册指标",
  metric_submit: "提交评审",
  metric_approve: "审核通过",
  metric_reject: "审核驳回",
  template_instantiate: "模板实例化",
  // 消费
  consume_query: "消费查询",
  consume_dry_run: "消费校验",
  consume_semantic: "语义解析查询",
  consumption_guide_view: "消费指南查看",
  // 血缘
  lineage_graph_view: "血缘图查看",
  lineage_table_detail: "血缘表详情",
  lineage_query: "血缘查询",
  lineage_preview: "血缘预览",
  lineage_parse: "SQL 血缘解析",
  lineage_channel_runs: "血缘通道运行",
  lineage_stale_confirm: "血缘失效确认",
  lineage_stale_restore: "血缘失效恢复",
  // 治理
  review_arbitrate: "冲突仲裁",
  review_escalate: "冲突升级",
  review_reopen: "冲突重新打开",
  // AI
  ai_nl2sql: "AI 问数",
};

/** 埋点 target_type 取值 → 中文业务标签 */
export const TRACKING_TARGET_LABEL: Record<string, string> = {
  dashboard: "仪表盘",
  page: "页面",
  metric: "指标",
  table: "表",
  todo: "待办",
  favorite: "收藏",
  source: "数据源",
  conflict: "口径冲突",
  node: "血缘节点",
  sql: "SQL 解析",
  edge: "血缘边",
  ai: "AI 助手",
  template: "指标模板",
};
