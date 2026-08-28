import { useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type ReactNode } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { Table, Input, Select, Button, Space, Tag, message, Tooltip, Descriptions, Drawer, Dropdown, Modal, Checkbox, Card, Popconfirm, Radio, Upload, Alert } from "antd";
import {
  ArrowLeftOutlined,
  SearchOutlined,
  ColumnWidthOutlined,
  PlusCircleOutlined,
  FileTextOutlined,
  DownloadOutlined,
  UploadOutlined,
  HeartOutlined,
  HeartFilled,
  UserOutlined,
  DeleteOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  ExclamationCircleOutlined,
  ThunderboltOutlined,
  ColumnHeightOutlined,
  HolderOutlined,
  ReloadOutlined,
  UnorderedListOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import {
  fetchCurrentUser,
  fetchDashboard,
  listDomainTree,
  listMetrics,
  listUsers,
  listFavorites,
  addFavorite,
  removeFavorite,
  deleteMetric,
  restoreMetric,
  purgeMetric,
  batchApproveMetrics,
  batchRejectMetrics,
  batchDeprecateMetrics,
  batchReactivateMetrics,
  batchPurgeMetrics,
  batchSubmitMetrics,
  checkMetricDownstream,
  type MetricDownstreamCheckResult,
  listMeasureCatalogs,
  importMetricsCsv,
  downloadMetricImportTemplate,
  type MetricImportResult,
  UnisenseApiError,
} from "../api";
import type { MetricResponse, SubjectDomainTreeNode } from "../types";
import type { ColumnsType } from "antd/es/table";
import { useTracking } from "../hooks/useTracking";
import { usePermission } from "../hooks/usePermission";
import { usePersistentPageSize } from "../hooks/usePersistentPageSize";
import { MetricCompareModal } from "../components/MetricCompareModal";
import { CodeValue } from "../components/CodeValue";
import {
  AGGREGATION_LABEL,
  DW_LAYER_LABEL,
  FRESHNESS_LABEL,
  GRANULARITY_LABEL,
  METRIC_STATUS_COLOR,
  METRIC_STATUS_LABEL,
  METRIC_TYPE_LABEL,
  METRIC_TYPE_DESC,
  METRIC_TIER_LABEL,
  TIME_SEMANTICS_LABEL,
  UNIT_LABEL,
} from "../utils/enums";
import { formatCnTime } from "../utils/timeCn";

// 解析灰度租户输入（逗号/空格/顿号分隔的正整数列表）；非法项忽略，空返回 []（对齐
// MetricReview 的灰度租户解析）
function parseGrayTenants(raw: string): number[] {
  const tenants: number[] = [];
  for (const part of raw.split(/[,，、\s]+/)) {
    const n = Number(part);
    if (Number.isInteger(n) && n > 0) tenants.push(n);
  }
  return tenants;
}

// 批量操作动作中文名（结果提示用）
const BATCH_ACTION_LABEL: Record<string, string> = {
  submit: "提交审核",
  delete: "删除",
  approve: "通过",
  reject: "驳回",
  deprecate: "废弃",
  purge: "彻底删除",
};

// 健康度分级（backend metric_health_score：>=85 EXCELLENT / >=70 GOOD / >=55 WARNING / <55 CRITICAL）
const HEALTH_LABEL: Record<string, string> = {
  EXCELLENT: "优秀",
  GOOD: "良好",
  WARNING: "警告",
  CRITICAL: "严重",
};
const HEALTH_COLOR: Record<string, string> = {
  EXCELLENT: "green",
  GOOD: "blue",
  WARNING: "orange",
  CRITICAL: "red",
};

const TIER_OPTIONS = ["T1", "T2", "T3"].map((v) => ({ value: v, label: METRIC_TIER_LABEL[v] ?? v }));
const SORT_OPTIONS = [
  { value: "updated_at", label: "按更新时间" },
  { value: "created_at", label: "按创建时间" },
  { value: "version", label: "按版本号" },
  { value: "metric_code", label: "按编码" },
];

// 生命周期快筛预设
const LIFECYCLE_PRESETS = [
  { key: "created_7d", label: "最近7天创建", icon: <PlusCircleOutlined /> },
  { key: "stale_30d", label: "30天未更新", icon: <ClockCircleOutlined /> },
  { key: "deprecating", label: "即将废弃", icon: <ExclamationCircleOutlined /> },
];

// ---- 按用户群体差异化展示（OneData 治理：不同角色关注不同信息，避免统一列表的信息过载）----
// 7 角色聚合为 4 群体（analyst/viewer、reviewer/compliance_officer 诉求高度重合）。
type RoleGroup = "consumer" | "producer" | "governance" | "admin";
const ROLE_GROUP: Record<string, RoleGroup> = {
  analyst: "consumer",
  viewer: "consumer",
  metric_owner: "producer",
  reviewer: "governance",
  compliance_officer: "governance",
  platform_admin: "admin",
  domain_admin: "admin",
};
const GROUP_LABEL: Record<RoleGroup, string> = {
  consumer: "业务消费者",
  producer: "指标生产者",
  governance: "治理审核",
  admin: "平台管理",
};
// 列设置选项（restore 列仅回收站视图出现，不参与显隐）
const COLUMN_OPTIONS = [
  { value: "metric_code", label: "编码" },
  { value: "name", label: "名称" },
  { value: "fav", label: "收藏" },
  { value: "rowActions", label: "操作" },
  { value: "domain", label: "业务域" },
  { value: "owner", label: "责任人" },
  { value: "type", label: "类型" },
  { value: "status", label: "状态" },
  { value: "calibre", label: "口径摘要" },
  { value: "submitter", label: "提交人" },
  { value: "dw_layer", label: "分层" },
  { value: "tier", label: "分级" },
  { value: "badges", label: "治理徽章" },
  { value: "health", label: "健康" },
  { value: "version", label: "版本" },
  { value: "updated_at", label: "更新时间" },
];
// 各群体默认可见列（平台管理=全部；治理审核含提交人——审核追溯需要）
const DEFAULT_VISIBLE_COLUMNS: Record<RoleGroup, string[]> = {
  consumer: ["metric_code", "name", "fav", "domain", "owner", "calibre", "status"],
  producer: ["metric_code", "name", "status", "calibre", "owner", "type", "health", "version", "updated_at", "rowActions"],
  governance: ["metric_code", "name", "status", "submitter", "type", "version", "updated_at"],
  admin: COLUMN_OPTIONS.map((c) => c.value),
};
// 列显隐 localStorage 前缀（按群体隔离，避免跨角色污染偏好）
const VISIBLE_COLS_STORAGE_PREFIX = "unisense.catalog.visibleCols.";

// 递归展平主题域树 → code → 中文名 映射（同时记录 status 供停用域标识）
function flattenDomains(nodes: SubjectDomainTreeNode[], acc: Map<string, string>) {
  for (const n of nodes) {
    acc.set(n.code, n.name);
    if (n.children?.length) flattenDomains(n.children, acc);
  }
}
// 递归收集 code → status（active/inactive），供域下拉标识停用域
function collectDomainStatus(nodes: SubjectDomainTreeNode[], acc: Map<string, string>) {
  for (const n of nodes) {
    if (n.status) acc.set(n.code, n.status);
    if (n.children?.length) collectDomainStatus(n.children, acc);
  }
}

// 口径摘要：聚合(字段) · 粒度 · 单位
function calibreSummary(r: MetricResponse): string {
  // 派生/复合无聚合语义（aggregation=null）→ 展示「派生表达式」
  const agg = r.aggregation
    ? (AGGREGATION_LABEL[r.aggregation] ?? r.aggregation)
    : "派生表达式";
  const gran = r.granularity ? (GRANULARITY_LABEL[r.granularity] ?? r.granularity) : "—";
  const unit = UNIT_LABEL[r.unit] ?? r.unit;
  return `${agg} · ${gran} · ${unit}`;
}

// 展开行：完整口径定义 + 治理追溯（按用户群体裁剪——业务消费者聚焦口径，治理/生产/管理全量）
function ExpandContent({
  r,
  userName,
  domainName,
  measureName,
  group,
}: {
  r: MetricResponse;
  userName: (id: number | null | undefined) => string;
  domainName: (code: string) => string;
  measureName: (id: number | null | undefined) => string;
  group: RoleGroup;
}) {
  const def = r.definition_json ?? {};
  // 业务消费者：聚焦"这是什么、谁负责、怎么算的"，隐藏治理/运营追溯（备份/提交/审批/时间/分层/时效/时间语义）
  const isConsumer = group === "consumer";
  // 责任方展示：平台用户 id 可解析优先；id 为空但有 name → 外部人员名称（非平台用户直接输入）
  const ownerName = (id: number | null | undefined, name?: string | null) =>
    id != null ? userName(id) : name || "—";
  const expression = typeof def.expression === "string" ? def.expression : undefined;
  const definition = typeof def.definition === "string" ? def.definition : undefined;
  const dependencies = Array.isArray(def.dependencies) ? def.dependencies.map((d) => String(d)) : [];
  const rawSource = def.source_fields ?? def.source_columns;
  const sourceFields = Array.isArray(rawSource) ? rawSource.map((s) => String(s)) : rawSource ? [String(rawSource)] : [];
  const sourceTables = Array.isArray(def.source_tables)
    ? def.source_tables.map((s) => String(s))
    : def.source_tables
      ? [String(def.source_tables)]
      : [];
  const downstreamTables = Array.isArray(def.downstream_tables)
    ? def.downstream_tables.map((s) => String(s))
    : def.downstream_tables
      ? [String(def.downstream_tables)]
      : [];
  // 口径 SQL：兼容多种键名（etl_sql / sql / calculation_sql / query_sql / sql_template）
  const rawEtl = def.etl_sql ?? def.sql ?? def.calculation_sql ?? def.query_sql ?? def.sql_template;
  const etlSql = rawEtl == null ? "" : String(rawEtl);
  // 口径分角色（PRD 4.5 责任方对应）：系统开发伪代码口径 / 数仓开发详细口径
  const pseudoDefinition = typeof def.pseudo_definition === "string" ? def.pseudo_definition : "";
  const dwDefinition = typeof def.dw_definition === "string" ? def.dw_definition : "";

  return (
    <div style={{ padding: "4px 8px" }}>
      <Descriptions column={2} size="small" bordered style={{ marginBottom: 12 }}>
        <Descriptions.Item label="业务域">{domainName(r.domain)}</Descriptions.Item>
        <Descriptions.Item label="指标类型">
          <Tooltip title={METRIC_TYPE_DESC[r.type] ?? r.type}>
            <span style={{ cursor: "help" }}>{METRIC_TYPE_LABEL[r.type] ?? r.type}</span>
          </Tooltip>
        </Descriptions.Item>
        <Descriptions.Item label="责任人">{userName(r.owner_id)}</Descriptions.Item>
        {/* 治理/运营追溯：业务消费者聚焦口径，隐藏备份/提交/审批/时间/分层/时效/时间语义 */}
        {!isConsumer && <Descriptions.Item label="备份责任人">{userName(r.backup_owner_id)}</Descriptions.Item>}
        {/* 口径三方责任（PRD 4.5 补充）：产品需求方/技术方/数仓开发（平台用户 id 或外部人员名称） */}
        <Descriptions.Item label="产品需求方">{ownerName(r.product_owner_id, r.product_owner_name)}</Descriptions.Item>
        <Descriptions.Item label="技术方">{ownerName(r.tech_owner_id, r.tech_owner_name)}</Descriptions.Item>
        <Descriptions.Item label="数仓开发">{ownerName(r.dw_developer_id, r.dw_developer_name)}</Descriptions.Item>
        {/* OneData 原子层：逻辑度量（原子指标继承度量格式/单位/小数位；派生/复合继承自依赖，显示 "—"） */}
        <Descriptions.Item label="逻辑度量">{measureName(r.measure_id)}</Descriptions.Item>
        {!isConsumer && <Descriptions.Item label="提交人">{userName(r.submitted_by)}</Descriptions.Item>}
        {!isConsumer && <Descriptions.Item label="审批人">{userName(r.approver_id)}</Descriptions.Item>}
        {!isConsumer && (
          <Descriptions.Item label="创建时间">
            <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(r.created_at)}</span>
          </Descriptions.Item>
        )}
        {!isConsumer && <Descriptions.Item label="数据分层">{DW_LAYER_LABEL[r.dw_layer] ?? r.dw_layer}</Descriptions.Item>}
        {!isConsumer && <Descriptions.Item label="更新时效">{FRESHNESS_LABEL[r.freshness] ?? r.freshness}</Descriptions.Item>}
        {!isConsumer && <Descriptions.Item label="时间语义">{TIME_SEMANTICS_LABEL[r.time_semantics] ?? r.time_semantics}</Descriptions.Item>}
      </Descriptions>
      <p style={{ margin: "0 0 8px" }}>
        <span className="muted">业务口径：</span>
        {definition ? (
          definition
        ) : (
          <span className="muted" style={{ fontStyle: "italic" }}>未填写（可在详情页编辑补填）</span>
        )}
      </p>
      {expression && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">计算口径：</span>
          <code className="mono">{expression}</code>
        </p>
      )}
      {sourceTables.length > 0 && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">依赖表（上游）：</span>
          {sourceTables.map((t) => (
            <Tag key={t} className="mono">{t}</Tag>
          ))}
        </p>
      )}
      {downstreamTables.length > 0 && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">使用表（下游）：</span>
          {downstreamTables.map((t) => (
            <Tag key={t} className="mono">{t}</Tag>
          ))}
        </p>
      )}
      {dependencies.length > 0 && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">依赖指标：</span>
          {dependencies.map((d) => (
            <Tag key={d}>{d}</Tag>
          ))}
        </p>
      )}
      {sourceFields.length > 0 && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">来源字段：</span>
          {sourceFields.map((s) => (
            <Tag key={s}>{s}</Tag>
          ))}
        </p>
      )}
      {etlSql && (
        <div style={{ margin: "0 0 8px" }}>
          <span className="muted">技术口径（源业务库口径）：</span>
          <pre
            style={{
              background: "var(--paper)",
              padding: 8,
              borderRadius: 4,
              margin: "4px 0 0",
              fontSize: 12,
              overflow: "auto",
              maxHeight: 200,
            }}
          >
            {etlSql}
          </pre>
        </div>
      )}
      {/* 口径分角色展示：系统开发伪代码口径（自然语言/伪 SQL）+ 数仓开发详细口径（完整 SQL/建模口径） */}
      {pseudoDefinition && (
        <div style={{ margin: "0 0 8px" }}>
          <span className="muted">伪代码口径（系统开发）：</span>
          <pre
            style={{
              background: "var(--paper)",
              padding: 8,
              borderRadius: 4,
              margin: "4px 0 0",
              fontSize: 12,
              overflow: "auto",
              maxHeight: 160,
              whiteSpace: "pre-wrap",
            }}
          >
            {pseudoDefinition}
          </pre>
        </div>
      )}
      <div style={{ margin: "0 0 8px" }}>
        <span className="muted">数仓SQL口径：</span>
        {dwDefinition ? (
          <pre
            style={{
              background: "var(--paper)",
              padding: 8,
              borderRadius: 4,
              margin: "4px 0 0",
              fontSize: 12,
              overflow: "auto",
              maxHeight: 200,
            }}
          >
            {dwDefinition}
          </pre>
        ) : (
          <span className="muted" style={{ fontStyle: "italic" }}>未填写（可在详情页编辑补填）</span>
        )}
      </div>
      <details>
        <summary className="muted" style={{ cursor: "pointer" }}>完整口径 JSON</summary>
        <pre
          style={{
            background: "var(--paper)",
            padding: 8,
            borderRadius: 4,
            margin: "8px 0 0",
            fontSize: 12,
            overflow: "auto",
            maxHeight: 240,
          }}
        >
          {JSON.stringify(def, null, 2)}
        </pre>
      </details>
    </div>
  );
}

export function MetricCatalog() {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const { track } = useTracking();
  const urlKw = searchParams.get("kw") ?? "";
  const urlStatus = searchParams.get("status") ?? "";
  const urlOwnerId = searchParams.get("owner_id") ?? "";
  const urlDomain = searchParams.get("domain") ?? "";
  const urlTier = searchParams.get("tier") ?? "";
  const urlLifecycle = searchParams.get("lifecycle") ?? "";
  // P2-6（第六轮）：批次筛选——SQL/宽表批量创建的指标带 batch_id，列表页此前只有
  // 展示 Tag 无法按批次收敛（审核页已支持，列表页漏）；进 URL 可分享/刷新保持
  const urlBatchId = searchParams.get("batch_id") ?? "";
  // URL 同步筛选状态（replace 模式，不产生历史堆栈）：业务域/分级/生命周期快筛也进 URL，
  // 让"销售域+已发布+最近7天创建"这类筛选视图可分享、刷新保持（TD §3 协作体验）
  const [items, setItems] = useState<MetricResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [keyword, setKeyword] = useState(urlKw);
  // 搜索输入框的即时显示值：与过滤值 keyword 分离——输入不打断浏览/不发请求，确认（回车/搜索按钮）才触发过滤
  const [inputValue, setInputValue] = useState(urlKw);
  const [status, setStatus] = useState(urlStatus);
  const [ownerFilter, setOwnerFilter] = useState(urlOwnerId);
  const [domain, setDomain] = useState(urlDomain);
  const [tier, setTier] = useState(urlTier);
  const [sortBy, setSortBy] = useState<"updated_at" | "created_at" | "version" | "metric_code" | "name">(
    (searchParams.get("sort_by") as "updated_at" | "created_at" | "version" | "metric_code" | "name") ?? "updated_at",
  );
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">(
    searchParams.get("sort_order") === "asc" ? "asc" : "desc",
  );
  const [batchIdFilter, setBatchIdFilter] = useState(urlBatchId);
  const [domainOptions, setDomainOptions] = useState<Array<{ value: string; label: string }>>([]);
  // 主题域 code → status（active/inactive），供域下拉标识停用域
  const [domainStatusMap, setDomainStatusMap] = useState<Map<string, string>>(new Map());
  const [page, setPage] = useState(1);
  // 每页条数持久化（对齐 AssetMap/Dimensions 的 usePersistentPageSize 跨页记忆）
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.catalog.pageSize", 20);
  const [selected, setSelected] = useState<MetricResponse[]>([]);
  // 指标矩阵对比弹窗（勾选 2~6 个后点「对比所选」在当前页内展开，不再跳转对比页）
  const [compareOpen, setCompareOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  // P2-15 全量导出 loading：导出过程中禁用按钮 + 显示"正在导出全量"，防重复点击
  const [exporting, setExporting] = useState(false);
  // 列表加载失败的错误信息（区别于空结果：失败显示重试空态，空结果显示原空态引导）
  const [loadError, setLoadError] = useState<string | null>(null);
  const [userMap, setUserMap] = useState<Map<number, string>>(new Map());
  // 评审用户下拉 label：display_name（username）格式（与责任人列 userMap 区分，责任人列保持纯 display_name）
  const [userLabelMap, setUserLabelMap] = useState<Map<number, string>>(new Map());
  const [domainMap, setDomainMap] = useState<Map<string, string>>(new Map());
  // OneData 逻辑度量目录映射（id → 名称/单位）：原子指标展示继承的逻辑度量（目录名称 + 默认单位）
  const [measureMap, setMeasureMap] = useState<Map<number, { name: string; default_unit?: string | null }>>(new Map());
  const [currentUserId, setCurrentUserId] = useState<number | undefined>(undefined);
  const [currentUserRole, setCurrentUserRole] = useState<string>("");
  const [currentUserDomain, setCurrentUserDomain] = useState<string>("");
  // 当前用户群体（consumer/producer/governance/admin）：决定列默认视图与明细抽屉信息密度
  const roleGroup = ROLE_GROUP[currentUserRole] ?? "admin";
  // 删除权限：平台/域管理员，或指标创建者（原 Owner）；仅非发布状态可删（DRAFT/DEPRECATED，
  // 后端 INVALID_STATE 兜底），对齐维度/度量「草稿/废弃可由管理员或生产者处理」的决策
  const canDeleteMetric = (r: MetricResponse) =>
    (r.status === "DRAFT" || r.status === "DEPRECATED") &&
    (currentUserRole === "platform_admin" ||
      currentUserRole === "domain_admin" ||
      r.owner_id === currentUserId);

  const [myMetricsOnly, setMyMetricsOnly] = useState(false);
  // 合规官默认只看 PII 指标（listMetrics 支持 pii_flag 过滤）
  const [piiOnly, setPiiOnly] = useState(false);
  // 下游引用过滤（批量废弃前按引用收敛）：all 不过滤 / with 仅有下游 / without 仅无下游
  const [downstreamFilter, setDownstreamFilter] = useState<"all" | "with" | "without">("all");
  // 按用户群体差异化的可见列（null=角色未就绪/未初始化，渲染全部列避免闪烁）
  const [visibleCols, setVisibleCols] = useState<string[] | null>(null);
  // 角色默认筛选仅应用一次（URL 参数优先，不覆盖用户手动选择）
  const roleDefaultApplied = useRef(false);
  const [lifecycleFilter, setLifecycleFilter] = useState<string | null>(urlLifecycle || null);
  const [urlSynced, setUrlSynced] = useState(false);
  useEffect(() => {
    if (!urlSynced) {
      setUrlSynced(true);
      return; // 首帧用 URL 初始值，不覆盖
    }
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (keyword) next.set("kw", keyword); else next.delete("kw");
        if (status) next.set("status", status); else next.delete("status");
        if (domain) next.set("domain", domain); else next.delete("domain");
        if (tier) next.set("tier", tier); else next.delete("tier");
        if (lifecycleFilter) next.set("lifecycle", lifecycleFilter); else next.delete("lifecycle");
        if (batchIdFilter) next.set("batch_id", batchIdFilter); else next.delete("batch_id");
        if (sortBy !== "updated_at") next.set("sort_by", sortBy); else next.delete("sort_by");
        if (sortOrder !== "desc") next.set("sort_order", sortOrder); else next.delete("sort_order");
        return next;
      },
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keyword, status, domain, tier, lifecycleFilter, batchIdFilter, sortBy, sortOrder]);

  // URL 直达 ?lifecycle= 时（分享/刷新），按快筛 key 计算真实日期区间；
  // 与 handleLifecycle 交互共用同一日期口径（created_7d=7天前起 / stale_30d=30天前止）
  useEffect(() => {
    if (lifecycleFilter === "created_7d" && !lifecycleDate.created_after) {
      setLifecycleDate({ created_after: new Date(Date.now() - 7 * 24 * 3600 * 1000).toISOString() });
    } else if (lifecycleFilter === "stale_30d" && !lifecycleDate.updated_before) {
      setLifecycleDate({ updated_before: new Date(Date.now() - 30 * 24 * 3600 * 1000).toISOString() });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lifecycleFilter]);
  // 生命周期快筛的真实日期区间（TD §13）：created_7d=7 天前起 / stale_30d=30 天前止
  const [lifecycleDate, setLifecycleDate] = useState<{ created_after?: string; updated_before?: string }>({});
  const [favorites, setFavorites] = useState<Set<string>>(new Set());
  // 收藏操作连点防重：per-code busy 集合，请求进行中忽略再次点击（避免并发乱序致最终状态与最后点击相反）
  const [favBusy, setFavBusy] = useState<Set<string>>(new Set());
  // 只看收藏：客户端过滤当前页（后端 list 无收藏过滤参数）
  const [favoritesOnly, setFavoritesOnly] = useState(false);
  // 收藏列表加载失败标记：失败时禁用"只看收藏"并提示（避免静默空集误导为"无收藏"）
  const [favoritesError, setFavoritesError] = useState(false);
  // 回收站视图：true 时仅展示已软删草稿（提供恢复入口）
  const [deletedView, setDeletedView] = useState(false);
  const [restoring, setRestoring] = useState<string | null>(null);
  // 批量操作权限点（方案 C 按钮级管控）：提交/删除=metric:create、通过/打回=metric:approve、
  // 下线=metric:deprecate。can() 控制批量操作按钮可用性；后端接口强制仍为最终边界。
  // P6 防 fail-open：can() 在权限快照加载期（snapshot=null）返回 true（后端兜底），
  // 但按钮若在此窗口可用会误导用户——permLoading 未就绪时禁用批量按钮。
  const { can, loading: permLoading } = usePermission();
  const canBatchManage =
    !permLoading &&
    (can("metric:create") || can("metric:approve") || can("metric:deprecate"));
  // 批量操作菜单项级权限：各操作对应独立权限点，避免仅有部分权限的用户看到并点击无权限项（后端仍为最终边界）
  const canApprove = can("metric:approve");
  const canDeprecate = can("metric:deprecate");
  // 空态引导权限感知：无创建权限的用户不显示「创建/从模板创建」（点击后会被后端 403），改为引导联系管理员
  const canCreate = can("metric:create");
  // 导出权限：metric:export 仅前端生效（CSV 为客户端生成，无后端端点拦截），
  // 无权限则禁用导出按钮，防止 viewer/analyst 等角色导出含 PII 口径的指标清单（数据导出权限缺口）。
  const canExport = can("metric:export");
  // 批量操作确认弹窗：null=关闭 / submit=批量提交审核 / delete=批量删除 /
  // approve=批量通过 / reject=批量打回 / deprecate=批量废弃 / reactivate=批量恢复 / purge=回收站批量彻底删除
  const [batchAction, setBatchAction] = useState<
    "submit" | "delete" | "approve" | "reject" | "deprecate" | "reactivate" | "purge" | null
  >(null);
  const [batchBusy, setBatchBusy] = useState(false);
  // 批量操作失败明细：超 3 条时提供「查看明细」弹窗（避免 message 截断导致用户看不到全部失败）
  const [batchErrors, setBatchErrors] = useState<string[]>([]);
  const [batchErrorsOpen, setBatchErrorsOpen] = useState(false);
  // 批量操作失败项的 metric_code（供「重试失败项」一键重选；batchRetryActionRef 记住原操作类型）
  const [batchFailedCodes, setBatchFailedCodes] = useState<string[]>([]);
  const batchRetryActionRef = useRef<
    "submit" | "delete" | "approve" | "reject" | "deprecate" | "reactivate" | "purge" | null
  >(null);
  // 批量提交审核的评审指派（TD §13）
  const [batchReviewerType, setBatchReviewerType] = useState<"user" | "domain" | null>(null);
  const [batchReviewerId, setBatchReviewerId] = useState<number | null>(null);
  // 批量导入（CSV / 外部 agent）：弹窗、上传中、结果
  const [importOpen, setImportOpen] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<MetricImportResult | null>(null);
  const [importDomain, setImportDomain] = useState("");
  // 批量打回原因 / 批量废弃替代指标映射
  const [batchRejectReason, setBatchRejectReason] = useState("");
  const [batchSuccessors, setBatchSuccessors] = useState<Record<string, string>>({});
  // P1-3（第六轮）：批量通过支持灰度发布（对齐单条 MetricReview）——
  // standard=标准发布 / experimental=灰度发布（仅指定租户可见）
  const [batchApproveMode, setBatchApproveMode] = useState<"standard" | "experimental">("standard");
  const [batchGrayTenants, setBatchGrayTenants] = useState("");
  // 批量废弃替代指标选项：已发布指标（排除勾选集内编码，防替代自身/互替代）
  const [batchSuccessorOptions, setBatchSuccessorOptions] = useState<
    Array<{ value: string; label: string }>
  >([]);
  // 批量废弃替代指标选项（惰性：仅在打开批量废弃面板时加载一次已发布指标，避免挂载时多余查询）
  function loadSuccessorOptions() {
    if (batchSuccessorOptions.length) return;
    listMetrics({ page_size: 100, status: "PUBLISHED" })
      .then((r) =>
        setBatchSuccessorOptions(
          (r.items ?? []).map((m) => ({ value: m.metric_code, label: `${m.name} (${m.metric_code})` })),
        ),
      )
      .catch(() => setBatchSuccessorOptions([]));
  }
  // 批量废弃下游使用审查：打开批量废弃面板时惰性加载勾选已发布指标的被引用情况
  const [downstreamMap, setDownstreamMap] = useState<Record<string, MetricDownstreamCheckResult>>({});
  const [downstreamLoading, setDownstreamLoading] = useState(false);
  // 有下游但未填替代指标被前端拦截的行（标红提示）
  const [deprecateBlocked, setDeprecateBlocked] = useState<Set<string>>(new Set());
  function loadDownstreamCheck(codes: string[]) {
    if (!codes.length) {
      setDownstreamMap({});
      return;
    }
    setDownstreamLoading(true);
    checkMetricDownstream(codes)
      .then((rows) => {
        const map: Record<string, MetricDownstreamCheckResult> = {};
        rows.forEach((r) => {
          map[r.metric_code] = r;
        });
        setDownstreamMap(map);
      })
      .catch(() => setDownstreamMap({}))
      .finally(() => setDownstreamLoading(false));
  }
  // 预览抽屉
  const [previewMetric, setPreviewMetric] = useState<MetricResponse | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const loadSeq = useRef(0);

  // 域列表
  useEffect(() => {
    fetchDashboard()
      .then((d) => setDomainOptions(Object.keys(d.by_domain ?? {}).map((v) => ({ value: v, label: v }))))
      .catch(() => setDomainOptions([]));
  }, []);

  // 用户/域中文名映射
  useEffect(() => {
    Promise.all([
      listUsers().then((u) => {
        setUserMap(new Map(u.map((x) => [x.id, x.display_name || x.username])));
        setUserLabelMap(
          new Map(
            u.map((x) => [x.id, x.display_name ? `${x.display_name}（${x.username}）` : x.username]),
          ),
        );
      }),
      listDomainTree().then((tree) => {
        const m = new Map<string, string>();
        flattenDomains(tree, m);
        setDomainMap(m);
        const st = new Map<string, string>();
        collectDomainStatus(tree, st);
        setDomainStatusMap(st);
      }),
      fetchCurrentUser().then((u) => { setCurrentUserId(u.id); setCurrentUserRole(u.role); setCurrentUserDomain(u.domain ?? ""); }).catch(() => {}),
      listFavorites()
        .then((favs) => {
          setFavorites(
            new Set(favs.filter((f) => f.asset_type === "METRIC").map((f) => f.asset_id)),
          );
          setFavoritesError(false);
        })
        .catch(() => setFavoritesError(true)),
      // OneData 逻辑度量目录：原子指标展示继承的逻辑度量名称/单位
      listMeasureCatalogs({ page_size: 200 })
        .then((res) =>
          setMeasureMap(
            new Map((res.items ?? []).map((m) => [m.id, { name: m.name, default_unit: m.default_unit }])),
          ),
        )
        .catch(() => setMeasureMap(new Map())),
    ]).catch(() => {});
  }, []);

  // 按用户群体差异化（OneData 治理）：角色就绪后初始化默认列（localStorage 优先）+
  // 应用角色默认筛选（URL 参数优先，不覆盖用户手动选择）
  useEffect(() => {
    if (!currentUserRole) return;
    const group = ROLE_GROUP[currentUserRole] ?? "admin";
    // 1) 可见列：本地保存优先，否则用群体默认
    if (visibleCols === null) {
      let initial: string[] | null = null;
      const saved = localStorage.getItem(`${VISIBLE_COLS_STORAGE_PREFIX}${group}`);
      if (saved) {
        try {
          const parsed = JSON.parse(saved) as string[];
          if (Array.isArray(parsed) && parsed.length > 0) initial = parsed;
        } catch {
          initial = null;
        }
      }
      setVisibleCols(initial ?? DEFAULT_VISIBLE_COLUMNS[group]);
    }
    // 2) 角色默认筛选（仅一次；URL 参数优先）
    if (!roleDefaultApplied.current) {
      roleDefaultApplied.current = true;
      const hasUrlFilter = Boolean(
        urlKw || urlStatus || urlOwnerId || urlDomain || urlTier || urlLifecycle,
      );
      if (!hasUrlFilter) {
        if (currentUserRole === "reviewer") setStatus("REVIEW");
        else if (currentUserRole === "compliance_officer") setPiiOnly(true);
        else if (currentUserRole === "metric_owner") setMyMetricsOnly(true);
        else if (currentUserRole === "domain_admin" && currentUserDomain) setDomain(currentUserDomain);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentUserRole]);

  // 可见列变更持久化（按群体隔离，避免跨角色污染偏好）
  useEffect(() => {
    if (!currentUserRole || visibleCols === null) return;
    const group = ROLE_GROUP[currentUserRole] ?? "admin";
    localStorage.setItem(`${VISIBLE_COLS_STORAGE_PREFIX}${group}`, JSON.stringify(visibleCols));
  }, [visibleCols, currentUserRole]);

  // 恢复角色默认视图：清除本地列偏好并应用群体默认列
  function handleResetRoleView() {
    const group = ROLE_GROUP[currentUserRole] ?? "admin";
    localStorage.removeItem(`${VISIBLE_COLS_STORAGE_PREFIX}${group}`);
    setVisibleCols(DEFAULT_VISIBLE_COLUMNS[group]);
    message.success(`已恢复${GROUP_LABEL[group] ?? ""}默认列`);
  }

  // 一键重置全部筛选条件（搜索/条件/快捷筛选/排序），回到默认视图
  function handleResetFilters() {
    setKeyword("");
    setInputValue("");
    setStatus("");
    setDomain("");
    setTier("");
    setOwnerFilter("");
    setBatchIdFilter("");
    setLifecycleFilter(null);
    setLifecycleDate({});
    setMyMetricsOnly(false);
    setFavoritesOnly(false);
    setPiiOnly(false);
    setDownstreamFilter("all");
    setSortBy("updated_at");
    setSortOrder("desc");
    setPage(1);
  }

  const userName = useMemo(
    () => (id: number | null | undefined) => (id == null ? "—" : (userMap.get(id) ?? `#${id}`)),
    [userMap],
  );
  // OneData 逻辑度量名（原子指标展示继承的逻辑度量）
  const measureName = useMemo(
    () => (id: number | null | undefined) =>
      id == null ? "—" : (measureMap.get(id)?.name ?? `#${id}`),
    [measureMap],
  );
  const domainName = useMemo(
    () => (code: string) => (code ? (domainMap.get(code) ?? code) : "—"),
    [domainMap],
  );
  // 域筛选下拉选项也使用中文名；dashboard 域集合与 domain_tree 不一致时兜底保留原 code（避免空 label）
  // 停用域（inactive）加「（已停用）」标识，避免用户误以为该域仍活跃
  const domainFilterOptions = useMemo(
    () =>
      domainOptions.map((d) => ({
        value: d.value,
        label: domainStatusMap.get(d.value) === "inactive" ? `${domainName(d.value) || d.value}（已停用）` : domainName(d.value) || d.value,
      })),
    [domainOptions, domainName, domainStatusMap],
  );

  useEffect(() => {
    if (urlKw && urlKw !== keyword) {
      setKeyword(urlKw);
      setInputValue(urlKw);
    }
    if (urlStatus && urlStatus !== status) setStatus(urlStatus);
    if (urlOwnerId && urlOwnerId !== ownerFilter) setOwnerFilter(urlOwnerId);
    if (urlDomain && urlDomain !== domain) setDomain(urlDomain);
    if (urlTier && urlTier !== tier) setTier(urlTier);
    if (urlLifecycle && urlLifecycle !== lifecycleFilter) setLifecycleFilter(urlLifecycle);
    if (urlBatchId && urlBatchId !== batchIdFilter) setBatchIdFilter(urlBatchId);
    if (urlKw || urlStatus || urlOwnerId || urlDomain || urlTier || urlLifecycle || urlBatchId) setPage(1);
  }, [urlKw, urlStatus, urlOwnerId, urlDomain, urlTier, urlLifecycle, urlBatchId]);

  async function load(overrideKeyword?: string) {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      const res = await listMetrics({
        keyword: overrideKeyword ?? keyword,
        status,
        domain: domain || undefined,
        metric_tier: tier || undefined,
        owner_id: ownerFilter ? Number(ownerFilter) : myMetricsOnly ? currentUserId : undefined,
        pii_flag: piiOnly || undefined,
        created_after: lifecycleDate.created_after,
        updated_before: lifecycleDate.updated_before,
        batch_id: batchIdFilter || undefined,
        has_downstream: downstreamFilter === "all" ? undefined : downstreamFilter === "with",
        deleted: deletedView,
        sort_by: sortBy,
        sort_order: sortOrder,
        page,
        page_size: pageSize,
      });
      if (seq !== loadSeq.current) return;
      setItems(res.items);
      setTotal(res.total);
      setSelected([]);
      setLoadError(null);
      // 空页回退：删除/批量操作后当前页无数据且非首页时回退上一页（total>0 说明数据仍在、仅页码超界）
      if (res.items.length === 0 && page > 1 && res.total > 0) {
        setPage(page - 1);
        return;
      }
    } catch (err) {
      if (seq !== loadSeq.current) return;
      // 用特征检查而非 instanceof：UnisenseApiError 跨 mock/打包边界不可靠（测试 mock 未导出该类会抛错）
      const e = err as { message?: string; codeZh?: string };
      const text =
        e && typeof e === "object" && typeof e.codeZh === "string" && typeof e.message === "string"
          ? `${e.message}（${e.codeZh}）`
          : "加载失败";
      setLoadError(text);
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }

  // 回收站恢复草稿指标：成功后退出回收站视图并刷新
  async function handleRestore(metricCode: string) {
    setRestoring(metricCode);
    try {
      await restoreMetric(metricCode);
      message.success(`已恢复指标 ${metricCode}`);
      if (items.length <= 1) setDeletedView(false);
      else setPage(1);
      load();
    } catch (err) {
      const e = err as { message?: string; codeZh?: string };
      const text =
        e && typeof e === "object" && typeof e.codeZh === "string" && typeof e.message === "string"
          ? `${e.message}（${e.codeZh}）`
          : "恢复失败";
      message.error(text);
    } finally {
      setRestoring(null);
    }
  }

  // 回收站彻底删除已删指标（物理删除不可恢复；仅平台管理员）
  async function handlePurge(metricCode: string) {
    setRestoring(metricCode);
    try {
      await purgeMetric(metricCode);
      message.success(`已彻底删除指标 ${metricCode}`);
      if (items.length <= 1) setDeletedView(false);
      else setPage(1);
      load();
    } catch (err) {
      const e = err as { message?: string; codeZh?: string };
      const text =
        e && typeof e === "object" && typeof e.codeZh === "string" && typeof e.message === "string"
          ? `${e.message}（${e.codeZh}）`
          : "彻底删除失败";
      message.error(text);
    } finally {
      setRestoring(null);
    }
  }

  // 单条删除草稿指标（软删除，仅平台/域管理员或原 Owner；对齐批量删除语义）
  const [deleting, setDeleting] = useState<string | null>(null);
  async function handleSingleDelete(metricCode: string) {
    setDeleting(metricCode);
    try {
      await deleteMetric(metricCode);
      message.success(`已删除草稿指标 ${metricCode}（可在回收站恢复）`);
      if (items.length <= 1) setPage(1);
      load();
    } catch (err) {
      const e = err as { message?: string; codeZh?: string };
      const text =
        e && typeof e === "object" && typeof e.codeZh === "string" && typeof e.message === "string"
          ? `${e.message}（${e.codeZh}）`
          : "删除失败";
      message.error(text);
    } finally {
      setDeleting(null);
    }
  }

  useEffect(() => {
    load();
  }, [page, pageSize, status, domain, tier, sortBy, sortOrder, myMetricsOnly, piiOnly, currentUserId, ownerFilter, lifecycleDate, deletedView, batchIdFilter, downstreamFilter]);

  function handleSearch() {
    const kw = inputValue;
    setKeyword(kw);
    if (kw) {
      track("metric_search", undefined, "metric", { keyword: kw });
    }
    setPage(1);
    load(kw);
  }

  // 统一返回上一入口：优先按来源标记精确返回（总览仪表/推荐流/血缘视图等入口），
  // 无来源标记时回退浏览器历史（历史栈有上一页才回退），URL 直达则兜底总览仪表。
  // 说明：SPA 中 window.history.length 是跨站点累计的（含浏览器历史），不能作为
  // "是否有上一页"的可靠判据，故来源标记优先于 history.length 判断。
  function handleBack() {
    const from = (location.state as { from?: string } | null)?.from;
    if (from === "dashboard" || from === "recommend") {
      navigate("/dashboard");
      return;
    }
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  function handleLifecycle(key: string) {
    const now = new Date();
    if (key === "created_7d") {
      const from = new Date(now.getTime() - 7 * 24 * 3600 * 1000);
      setKeyword("");
      setInputValue("");
      setStatus("");
      setLifecycleDate({ created_after: from.toISOString() });
      setLifecycleFilter(key);
      setSortBy("created_at");
      setSortOrder("desc");
    } else if (key === "stale_30d") {
      const until = new Date(now.getTime() - 30 * 24 * 3600 * 1000);
      setKeyword("");
      setInputValue("");
      setStatus("");
      setLifecycleDate({ updated_before: until.toISOString() });
      setLifecycleFilter(key);
      setSortBy("updated_at");
      setSortOrder("asc");
    } else if (key === "deprecating") {
      setKeyword("");
      setInputValue("");
      setStatus("DEPRECATED");
      setLifecycleDate({});
      setLifecycleFilter(key);
    }
    setPage(1);
  }

  // 收藏切换（心形列）
  async function toggleFavorite(code: string) {
    if (favBusy.has(code)) return; // 连点防重：请求进行中忽略再次点击
    setFavBusy((prev) => new Set(prev).add(code));
    const fav = favorites.has(code);
    try {
      if (fav) {
        await removeFavorite("METRIC", code);
        setFavorites((prev) => {
          const next = new Set(prev);
          next.delete(code);
          return next;
        });
        message.success("已取消收藏");
      } else {
        await addFavorite("METRIC", code);
        setFavorites((prev) => new Set(prev).add(code));
        message.success("已收藏");
      }
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "收藏操作失败");
    } finally {
      setFavBusy((prev) => {
        const next = new Set(prev);
        next.delete(code);
        return next;
      });
    }
  }

  // 只看收藏：客户端过滤当前页
  const displayItems = useMemo(
    () => (favoritesOnly ? items.filter((m) => favorites.has(m.metric_code)) : items),
    [items, favoritesOnly, favorites],
  );

  // 批量操作执行：逐条提交审核 / 删除，收集成功与失败明细
  async function runBatch() {
    if (!batchAction || !selected.length) return;
    batchRetryActionRef.current = batchAction;
    setBatchBusy(true);
    let ok = 0;
    const errors: string[] = [];
    const failedCodes: string[] = [];
    // 前端拦截（有下游未填替代）时保留弹窗 + 标红，不进入 finally 的关闭/清理
    let intercepted = false;
    try {
      if (batchAction === "approve") {
        const codes = selected.filter((m) => m.status === "REVIEW").map((m) => m.metric_code);
        if (!codes.length) {
          message.warning("勾选的指标中没有待评审（REVIEW）状态");
          return;
        }
        const res = await batchApproveMetrics(
          codes,
          batchApproveMode,
          batchApproveMode === "experimental" ? parseGrayTenants(batchGrayTenants) : undefined,
        );
        ok = res.ok_count;
        res.results.filter((r) => !r.ok).forEach((r) => { errors.push(`${r.code}: ${r.message}`); failedCodes.push(r.code); });
      } else if (batchAction === "reject") {
        if (!batchRejectReason.trim() || batchRejectReason.trim().length < 4) {
          message.warning("请填写驳回原因（至少 4 字）");
          return;
        }
        const codes = selected.filter((m) => m.status === "REVIEW").map((m) => m.metric_code);
        if (!codes.length) {
          message.warning("勾选的指标中没有待评审（REVIEW）状态");
          return;
        }
        const res = await batchRejectMetrics(codes, batchRejectReason.trim());
        ok = res.ok_count;
        res.results.filter((r) => !r.ok).forEach((r) => { errors.push(`${r.code}: ${r.message}`); failedCodes.push(r.code); });
      } else if (batchAction === "deprecate") {
        const deprecatable = selected.filter((m) => m.status === "PUBLISHED");
        if (!deprecatable.length) {
          message.warning("勾选的指标中没有已发布（PUBLISHED）状态");
          return;
        }
        // 下游审查后：有下游引用但未填替代指标 → 前端拦截（标红提示，不静默跳过）
        const blocked = deprecatable.filter(
          (m) =>
            (downstreamMap[m.metric_code]?.referrer_count ?? 0) > 0 &&
            !batchSuccessors[m.metric_code],
        );
        if (blocked.length) {
          intercepted = true;
          setDeprecateBlocked(new Set(blocked.map((m) => m.metric_code)));
          message.error(
            `以下 ${blocked.length} 个指标存在下游引用，须填写替代指标后才能废弃：${blocked
              .map((m) => m.metric_code)
              .join("、")}`,
          );
          return;
        }
        // 无下游引用的行可留空（successor_code=null），有下游且已填替代的正常提交
        const items = deprecatable.map((m) => ({
          metric_code: m.metric_code,
          successor_code: batchSuccessors[m.metric_code] || null,
        }));
        const res = await batchDeprecateMetrics(items);
        ok = res.ok_count;
        res.results.filter((r) => !r.ok).forEach((r) => { errors.push(`${r.code}: ${r.message}`); failedCodes.push(r.code); });
      } else if (batchAction === "submit") {
        // 批量提交：走后端原子 /batch-submit（逐条收集结果、单条失败不整体回滚），
        // 不再 N 次 submitReview 循环（P2-9 接线）
        const targets = selected.filter((m) => m.status === "DRAFT");
        if (!targets.length) {
          message.warning("勾选的指标中没有草稿状态可操作");
          return;
        }
        // 双保险：指定评审用户但未选用户（按钮已禁用，此处兜底防程序化触发）
        if (batchReviewerType === "user" && !batchReviewerId) {
          message.warning("已选择「指定评审用户」，请先选择具体评审人");
          return;
        }
        const res = await batchSubmitMetrics(
          targets.map((m) => ({
            code: m.metric_code,
            change_reason: "批量提交审核",
            reviewer_id: batchReviewerType === "user" ? batchReviewerId : null,
            reviewer_type: batchReviewerType,
            reviewer_domain: m.domain,
          })),
        );
        ok = res.ok_count;
        res.results.filter((r) => !r.ok).forEach((r) => { errors.push(`${r.code}: ${r.message}`); failedCodes.push(r.code); });
      } else if (batchAction === "reactivate") {
        // P2-1：批量恢复已废弃指标（DEPRECATED → DRAFT，对齐维度/度量/术语批量重新启用）
        const targets = selected.filter((m) => m.status === "DEPRECATED");
        if (!targets.length) {
          message.warning("勾选的指标中没有已废弃（DEPRECATED）状态");
          return;
        }
        const res = await batchReactivateMetrics(targets.map((m) => m.metric_code));
        ok = res.ok_count;
        res.results.filter((r) => !r.ok).forEach((r) => { errors.push(`${r.code}: ${r.message}`); failedCodes.push(r.code); });
      } else if (batchAction === "purge") {
        // 回收站批量彻底删除（物理删除不可恢复；仅平台管理员，后端逐条容错）
        const targets = selected; // 回收站视图下勾选即已软删记录
        if (!targets.length) {
          message.warning("请先勾选回收站中的指标");
          return;
        }
        const res = await batchPurgeMetrics(targets.map((m) => m.metric_code));
        ok = res.ok_count;
        res.results.filter((r) => !r.ok).forEach((r) => { errors.push(`${r.code}: ${r.message}`); failedCodes.push(r.code); });
      } else {
        // delete：逐条处理（无批量删除端点；后端允许 DRAFT/DEPRECATED）
        const targets = selected.filter((m) => m.status === "DRAFT" || m.status === "DEPRECATED");
        if (!targets.length) {
          message.warning("勾选的指标中没有草稿/已废弃状态可操作");
          return;
        }
        for (const m of targets) {
          try {
            await deleteMetric(m.metric_code);
            ok += 1;
          } catch (err) {
            errors.push(
              `${m.metric_code}: ${err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "失败"}`,
            );
            failedCodes.push(m.metric_code);
          }
        }
      }
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量操作失败",
      );
    } finally {
      setBatchBusy(false);
      // 前端拦截：保留弹窗 + 标红，等待用户补填替代指标
      if (intercepted) return;
      setBatchAction(null);
      setBatchFailedCodes(failedCodes);
      if (ok) message.success(`${BATCH_ACTION_LABEL[batchAction] ?? "操作"}成功 ${ok} 个`);
      if (errors.length) {
        setBatchErrors(errors);
        if (errors.length <= 3) {
          message.error(errors.join("；"));
        } else {
          message.error(`批量操作失败 ${errors.length} 条（前 3 条：${errors.slice(0, 3).join("；")}…），点击「查看失败明细」查看全部`);
          setBatchErrorsOpen(true);
        }
      }
      setSelected([]);
      setBatchRejectReason("");
      setBatchSuccessors({});
      setDeprecateBlocked(new Set());
      setDownstreamMap({});
      setBatchReviewerType(null);
      setBatchReviewerId(null);
      load();
    }
  }

  async function exportCsv() {
    // P2-15 全量导出：此前仅导出当前页 items（名不副实），大结果集导出残缺。
    // 改为按当前筛选条件分页拉全量（复用 load() 的筛选参数，page_size=200 上限循环至 total），
    // 再生成 CSV——「导出当前筛选结果」名副其实。
    if (exporting) return; // 防重复点击
    setExporting(true);
    try {
      const all: MetricResponse[] = [];
      const pageSize = 200;
      for (let p = 1; ; p += 1) {
        const res = await listMetrics({
          keyword,
          status,
          domain: domain || undefined,
          metric_tier: tier || undefined,
          owner_id: ownerFilter ? Number(ownerFilter) : myMetricsOnly ? currentUserId : undefined,
          pii_flag: piiOnly || undefined,
          created_after: lifecycleDate.created_after,
          updated_before: lifecycleDate.updated_before,
          deleted: deletedView,
          sort_by: sortBy,
          sort_order: sortOrder,
          page: p,
          page_size: pageSize,
        });
        all.push(...res.items);
        if (all.length >= res.total || res.items.length === 0) break;
      }
      // 表头与表格中文列标题对齐（业务术语化，值列已用中文标签）
      const header = [
        "指标编码", "名称", "业务域", "责任人", "类型", "状态",
        "聚合", "粒度", "单位", "数仓层", "分级",
        "PII", "版本", "创建时间", "更新时间",
      ];
      // CSV 注入防护（OWASP）：单元格以 = / + / - / @ 开头时，Excel/WPS 会当作公式执行。
      // 指标名/域名等用户可写字段可能被注入恶意公式，导出时统一前缀单引号消毒。
      const sanitize = (v: unknown) => {
        const s = String(v ?? "");
        if (/^[=+\-@]/.test(s)) return `'${s}`;
        return s;
      };
      const rows = all.map((m) =>
        [
          m.metric_code, m.name, domainName(m.domain), userName(m.owner_id), m.type,
          METRIC_STATUS_LABEL[m.status] ?? m.status,
          m.aggregation
            ? (AGGREGATION_LABEL[m.aggregation] ?? m.aggregation)
            : "派生表达式",
          GRANULARITY_LABEL[m.granularity ?? ""] ?? (m.granularity ?? "—"),
          m.unit && UNIT_LABEL[m.unit] ? UNIT_LABEL[m.unit] : m.unit, DW_LAYER_LABEL[m.dw_layer] ?? m.dw_layer, METRIC_TIER_LABEL[m.metric_tier] ?? m.metric_tier,
          m.pii_flag ? "PII" : "", m.version, formatCnTime(m.created_at), formatCnTime(m.updated_at),
        ]
          .map((c) => `"${sanitize(c).replace(/"/g, '""')}"`)
          .join(","),
      );
      const blob = new Blob([[header.join(","), ...rows].join("\n")], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `metric-catalog-${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      URL.revokeObjectURL(url);
      message.success(`已导出 ${all.length} 条指标（全量筛选结果）`);
    } catch (err) {
      const e = err as { message?: string };
      message.error(e?.message ? `导出失败：${e.message}` : "导出失败");
    } finally {
      setExporting(false);
    }
  }

  // 列宽拖拽：零依赖实现（antd 5.x 的 Table 原生不支持 resizable 列）。
  // 用户拖拽调整后的列宽持久化到 localStorage，刷新/重开浏览器后保留；
  // 表头通过 onHeaderCell 注入相对定位，标题区渲染拖拽手柄，mousedown 后
  // 监听 document 的 mousemove 实时改列宽，mouseup 还原光标与选区。
  const COL_WIDTH_STORAGE_KEY = "unisense.metric-catalog.colWidths";
  const DEFAULT_COL_WIDTH = 160;
  const MIN_COL_WIDTH = 60;
  const MAX_COL_WIDTH = 600;
  const [columnWidths, setColumnWidths] = useState<Record<string, number>>(() => {
    try {
      const raw = localStorage.getItem(COL_WIDTH_STORAGE_KEY);
      return raw ? (JSON.parse(raw) as Record<string, number>) : {};
    } catch {
      return {};
    }
  });
  const widthsRef = useRef(columnWidths);
  widthsRef.current = columnWidths;
  const [hoveredColKey, setHoveredColKey] = useState<string | null>(null);
  useEffect(() => {
    try {
      localStorage.setItem(COL_WIDTH_STORAGE_KEY, JSON.stringify(columnWidths));
    } catch {
      // localStorage 不可用（隐私模式/被禁用）时静默降级：列宽仅当前会话生效
    }
  }, [columnWidths]);

  // ---- 整列拖拽排序（与列宽同构：localStorage 持久化 + 重置）----
  // colOrder 为 null 表示未自定义列序（用默认顺序）；非空数组为当前自定义顺序（仅含可排序列的 key）
  const COL_ORDER_STORAGE_KEY = "unisense.metric-catalog.colOrder";
  const [colOrder, setColOrder] = useState<string[] | null>(() => {
    try {
      const raw = localStorage.getItem(COL_ORDER_STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as unknown;
      return Array.isArray(parsed) && parsed.length > 0 ? (parsed as string[]) : null;
    } catch {
      return null;
    }
  });
  const orderRef = useRef(colOrder);
  orderRef.current = colOrder;
  useEffect(() => {
    try {
      localStorage.setItem(COL_ORDER_STORAGE_KEY, JSON.stringify(colOrder ?? []));
    } catch {
      // localStorage 不可用（隐私模式/被禁用）时静默降级：列序仅当前会话生效
    }
  }, [colOrder]);
  // 拖拽过程中的工作列序（mousemove 高频触发，用 ref 持有最新顺序避免 state 滞后）
  const dragWorkingOrderRef = useRef<string[] | null>(null);
  const [dragCol, setDragCol] = useState<string | null>(null);
  const [dragOverCol, setDragOverCol] = useState<string | null>(null);

  const startResize = (e: ReactMouseEvent, key: string, baseWidth: number) => {
    e.stopPropagation();
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = widthsRef.current[key] ?? baseWidth;
    let frame = 0;
    const onMouseMove = (me: MouseEvent) => {
      const delta = me.clientX - startX;
      const next = Math.max(MIN_COL_WIDTH, Math.min(MAX_COL_WIDTH, Math.round(startWidth + delta)));
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        setColumnWidths((prev) => (prev[key] === next ? prev : { ...prev, [key]: next }));
      });
    };
    const onMouseUp = () => {
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
      cancelAnimationFrame(frame);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  };

  const columns: ColumnsType<MetricResponse> = [
    {
      title: "编码",
      dataIndex: "metric_code",
      key: "metric_code",
      width: 200,
      // 长编码用 CodeValue：单行中间省略 + hover 完整值 + 复制，避免 nowrap 撑破列宽覆盖相邻列
      render: (text: string) => (
        <CodeValue
          value={text}
          maxChars={30}
          maxWidth={190}
          target={`/detail/${encodeURIComponent(text)}`}
          onNavigate={(t) => navigate(t)}
        />
      ),
    },
    { title: "名称", dataIndex: "name", key: "name", ellipsis: true },
    ...(deletedView
      ? [
          {
            title: "操作",
            key: "restore",
            width: 150,
            align: "center" as const,
            render: (_: unknown, r: MetricResponse) => (
              <Space size={4}>
                <Button
                  type="link"
                  size="small"
                  disabled={restoring === r.metric_code || !can("metric:create")}
                  onClick={(e) => {
                    e.stopPropagation();
                    handleRestore(r.metric_code);
                  }}
                >
                  恢复
                </Button>
                {currentUserRole === "platform_admin" && (
                  <span onClick={(e) => e.stopPropagation()}>
                    <Popconfirm
                      title="确认彻底删除该指标？"
                      description="物理删除不可恢复，关联版本/维度/健康度/血缘将一并清除"
                      okButtonProps={{ danger: true }}
                      onConfirm={() => handlePurge(r.metric_code)}
                    >
                      <Button
                        type="link"
                        size="small"
                        danger
                        disabled={restoring === r.metric_code}
                      >
                        彻底删除
                      </Button>
                    </Popconfirm>
                  </span>
                )}
              </Space>
            ),
          },
        ]
      : []),
    {
      title: "收藏",
      key: "fav",
      width: 56,
      align: "center",
      render: (_: unknown, r: MetricResponse) => (
        <Button
          type="text"
          size="small"
          loading={favBusy.has(r.metric_code)}
          disabled={favBusy.has(r.metric_code)}
          aria-label={favorites.has(r.metric_code) ? "取消收藏" : "收藏"}
          icon={
            favorites.has(r.metric_code) ? (
              <HeartFilled style={{ color: "#eb2f96" }} />
            ) : (
              <HeartOutlined />
            )
          }
          onClick={(e) => {
            e.stopPropagation();
            toggleFavorite(r.metric_code);
          }}
        />
      ),
    },
    ...(!deletedView
      ? [
          {
            title: "操作",
            key: "rowActions",
            width: 80,
            align: "center" as const,
            render: (_: unknown, r: MetricResponse) =>
              canDeleteMetric(r) ? (
                <Popconfirm
                  title="删除指标"
                  description={`确定删除 ${r.metric_code} 吗？软删除后可在回收站恢复。`}
                  okText="删除"
                  okButtonProps={{ danger: true }}
                  onConfirm={(e) => {
                    e?.stopPropagation();
                    handleSingleDelete(r.metric_code);
                  }}
                  onCancel={(e) => e?.stopPropagation()}
                >
                  <Button
                    type="link"
                    size="small"
                    danger
                    aria-label="删除指标"
                    loading={deleting === r.metric_code}
                    onClick={(e) => e.stopPropagation()}
                  >
                    删除
                  </Button>
                </Popconfirm>
              ) : (
                <span style={{ color: "#bbb", fontSize: 12 }}>
                  {(r.status === "DRAFT" || r.status === "DEPRECATED") &&
                  currentUserRole !== "platform_admin" &&
                  currentUserRole !== "domain_admin" &&
                  r.owner_id !== currentUserId
                    ? "仅管理员或创建者可删"
                    : r.status === "REVIEW" || r.status === "EXPERIMENTAL" || r.status === "PUBLISHED"
                      ? "需先打回/下架后在草稿态删除"
                      : ""}
                </span>
              ),
          },
        ]
      : []),
    {
      title: "业务域",
      dataIndex: "domain",
      key: "domain",
      width: 110,
      render: (v: string) => domainName(v),
    },
    {
      title: "责任人",
      key: "owner",
      width: 110,
      ellipsis: true,
      render: (_: unknown, r: MetricResponse) => userName(r.owner_id),
    },
    {
      title: "类型",
      dataIndex: "type",
      key: "type",
      width: 120,
      render: (v: string, r: MetricResponse) => (
        <span>
          {METRIC_TYPE_LABEL[v] ?? v}
          {/* 存量 atomic 未关联逻辑度量（OneData 引导，D3 决策：不自动迁移） */}
          {v === "atomic" && r.measure_id == null && (
            <Tooltip title="该原子指标未关联逻辑度量（存量旧式来源）。建议在「原子指标口径库」创建逻辑度量后编辑关联，完成 OneData 化。">
              <Tag color="gold" style={{ marginLeft: 4 }}>待治理</Tag>
            </Tooltip>
          )}
        </span>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (s: string, r: MetricResponse) =>
        s === "DATA_SOURCE_DROPPED" ? (
          <Tooltip title="该指标的数据源已下线，可在指标详情页「源已恢复」或「确认退役」处理">
            <Tag color={METRIC_STATUS_COLOR[s]}>{METRIC_STATUS_LABEL[s] ?? s}</Tag>
          </Tooltip>
        ) : s === "DRAFT" && r.reject_reason ? (
          <Tooltip title={`上次评审被驳回：${r.reject_reason}`}>
            <Tag color="orange">被驳回</Tag>
          </Tooltip>
        ) : (
          <Tag color={METRIC_STATUS_COLOR[s]}>{METRIC_STATUS_LABEL[s] ?? s}</Tag>
        ),
    },
    {
      // 治理审核视角：待审指标追溯提交人（审核闭环 Who/When/Why）
      title: "提交人",
      key: "submitter",
      width: 110,
      ellipsis: true,
      render: (_: unknown, r: MetricResponse) => userName(r.submitted_by),
    },
    {
      title: "口径摘要",
      key: "calibre",
      width: 180,
      ellipsis: true,
      render: (_: unknown, r: MetricResponse) => {
        const def = r.definition_json ?? {};
        const expr = typeof def.expression === "string" ? def.expression : undefined;
        const text = calibreSummary(r);
        return expr ? (
          <Tooltip title={`计算口径：${expr}`}>
            <span style={{ fontSize: 12 }}>{text}</span>
          </Tooltip>
        ) : (
          <span style={{ fontSize: 12 }}>{text}</span>
        );
      },
    },
    {
      title: "分层",
      dataIndex: "dw_layer",
      key: "dw_layer",
      width: 110,
      render: (v: string) => DW_LAYER_LABEL[v] ?? v,
    },
    { title: "分级", dataIndex: "metric_tier", key: "tier", width: 70, render: (v: string) => <Tag>{METRIC_TIER_LABEL[v] ?? v}</Tag> },
    {
      title: "治理徽章",
      key: "badges",
      width: 220,
      render: (_: unknown, r: MetricResponse) => (
        <Space size={4} wrap>
          {r.pii_flag && (
            <Tag color={r.compliance_reviewed ? "green" : "orange"}>{r.compliance_reviewed ? "PII 已复核" : "PII 待复核"}</Tag>
          )}
          {r.emergency_publish && <Tag color="volcano">紧急</Tag>}
          {r.pending_conflict && <Tag color="orange">冲突</Tag>}
          {r.pending_version && <Tag color="purple" icon={<ThunderboltOutlined />}>版本待确认</Tag>}
          {/* P0-C：批量注册指标带批次标识——整批可回溯（列表可识别"这一批 50 个"） */}
          {r.batch_id && (
            <Tooltip title={`批量注册批次：${r.batch_id}`} placement="top">
              <Tag color="cyan">批量</Tag>
            </Tooltip>
          )}
          {r.gray_tenant_ids && r.gray_tenant_ids.length > 0 && (
            <Tooltip
              title={`灰度租户：${r.gray_tenant_ids.join("、")}`}
              placement="top"
            >
              <Tag color="purple">灰度 {r.gray_tenant_ids.length} 租户</Tag>
            </Tooltip>
          )}
          {!r.pii_flag && !r.emergency_publish && !r.pending_conflict && !r.pending_version && !r.gray_tenant_ids && !r.batch_id && <span className="muted">—</span>}
        </Space>
      ),
    },
    {
      title: "健康",
      key: "health",
      width: 90,
      render: (_: unknown, r: MetricResponse) =>
        r.health_level ? (
          <Tooltip title={`健康分 ${r.health_score ?? 0}/100（${HEALTH_LABEL[r.health_level] ?? r.health_level}）`}>
            <Tag color={HEALTH_COLOR[r.health_level]}>{HEALTH_LABEL[r.health_level] ?? r.health_level}</Tag>
          </Tooltip>
        ) : (
          <span className="muted">未评分</span>
        ),
    },
    {
      title: "版本",
      dataIndex: "version",
      key: "version",
      width: 70,
      render: (v: number) => `v${v}`,
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      key: "updated_at",
      width: 170,
      render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span>,
    },
  ];

  // 按用户群体差异化：仅渲染可见列（restore 列仅回收站视图，始终保留；角色未就绪时渲染全部避免闪烁）
  const filteredColumns = useMemo(() => {
    const allowed = visibleCols ?? COLUMN_OPTIONS.map((c) => c.value);
    return columns.filter((col) => col.key === "restore" || allowed.includes(String(col.key)));
  }, [columns, visibleCols]);

  // 按用户自定义列序重排（colOrder 为空/未设置时保持默认；restore 等未参与排序的列置尾保持原相对顺序）
  const orderedColumns = useMemo<ColumnsType<MetricResponse>>(() => {
    if (!colOrder || colOrder.length === 0) return filteredColumns;
    const byKey = new Map(filteredColumns.map((c) => [String(c.key), c] as const));
    const ordered: ColumnsType<MetricResponse> = [];
    const seen = new Set<string>();
    for (const k of colOrder) {
      const col = byKey.get(k);
      if (col && !seen.has(k)) {
        ordered.push(col);
        seen.add(k);
      }
    }
    for (const col of filteredColumns) {
      const k = String(col.key);
      if (!seen.has(k)) {
        ordered.push(col);
        seen.add(k);
      }
    }
    return ordered;
  }, [filteredColumns, colOrder]);
  // 当前渲染列的有序 key（拖拽排序计算插入位置用；restore 操作列不参与排序）
  const orderedKeysRef = useRef<string[]>([]);
  orderedKeysRef.current = orderedColumns.map((c) => String(c.key)).filter((k) => k !== "restore");

  // 整列拖拽排序：按下拖拽柄后监听 document mousemove，用 elementFromPoint 定位鼠标所在列，
  // 把被拖列实时插入到目标列位置（live reorder 有即时预览效果），松开持久化到 localStorage。
  const startDrag = (e: ReactMouseEvent, key: string) => {
    e.stopPropagation();
    e.preventDefault();
    setDragCol(key);
    dragWorkingOrderRef.current = [...orderedKeysRef.current];
    const onMouseMove = (me: MouseEvent) => {
      const el = document.elementFromPoint(me.clientX, me.clientY);
      const overKey =
        (el?.closest?.("[data-col-key]") as HTMLElement | null)?.getAttribute("data-col-key") ?? null;
      if (overKey && overKey !== key && overKey !== "restore") {
        const list = dragWorkingOrderRef.current ?? [];
        const from = list.indexOf(key);
        const to = list.indexOf(overKey);
        if (from !== -1 && to !== -1 && from !== to) {
          const next = [...list];
          next.splice(from, 1);
          next.splice(to, 0, key);
          dragWorkingOrderRef.current = next;
          setColOrder(next);
        }
      }
      setDragOverCol(overKey);
    };
    const onMouseUp = () => {
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
      setDragCol(null);
      setDragOverCol(null);
      dragWorkingOrderRef.current = null;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
    document.body.style.cursor = "grabbing";
    document.body.style.userSelect = "none";
  };

  // 把「记忆的列宽 + 默认列宽」合并成可拖拽表头：每列标题区渲染一个拖拽手柄，
  // 通过 onHeaderCell 给 <th> 注入相对定位，手柄绝对定位在列右边缘，拖拽即改列宽。
  const resizableColumns = useMemo(
    () =>
      orderedColumns.map((col) => {
        const key = String(col.key);
        const baseWidth = (col.width as number | undefined) ?? DEFAULT_COL_WIDTH;
        const width = columnWidths[key] ?? baseWidth;
        return {
          ...col,
          width,
          onHeaderCell: () => ({
            style: { position: "relative" as const },
            "data-col-key": key,
          }),
          title: (
            <span
              style={{
                display: "block",
                position: "relative",
                paddingRight: 8,
                paddingLeft: 16,
                background: dragCol === key ? "rgba(24,144,255,0.12)" : "transparent",
                borderLeft: dragOverCol === key && dragCol ? "2px solid rgba(24,144,255,0.8)" : "2px solid transparent",
                borderRadius: 2,
              }}
              onMouseEnter={() => setHoveredColKey(key)}
              onMouseLeave={() => setHoveredColKey((k) => (k === key ? null : k))}
            >
              {/* 整列拖拽排序柄：按住可把该列移动到其他列前/后 */}
              <span
                role="button"
                aria-label="拖拽调整列顺序"
                title="按住拖动可调整列顺序"
                onMouseDown={(e) => startDrag(e, key)}
                style={{
                  position: "absolute",
                  left: 2,
                  top: 0,
                  bottom: 0,
                  display: "flex",
                  alignItems: "center",
                  cursor: dragCol === key ? "grabbing" : "grab",
                  color: dragCol === key || hoveredColKey === key ? "#1677ff" : "#c0c4cc",
                  userSelect: "none",
                  touchAction: "none",
                }}
              >
                <HolderOutlined style={{ fontSize: 11 }} />
              </span>
              {col.title as ReactNode}
              {/* 列宽拖拽柄：按住可调整该列宽度 */}
              <span
                role="separator"
                aria-orientation="vertical"
                aria-label="拖拽调整列宽"
                onMouseDown={(e) => startResize(e, key, baseWidth)}
                style={{
                  position: "absolute",
                  right: 0,
                  top: 0,
                  bottom: 0,
                  width: hoveredColKey === key ? 3 : 6,
                  transform: "translateX(50%)",
                  cursor: "col-resize",
                  userSelect: "none",
                  touchAction: "none",
                  background: hoveredColKey === key ? "rgba(24,144,255,0.7)" : "rgba(0,0,0,0.08)",
                  transition: "background 0.12s",
                }}
              />
            </span>
          ),
        };
      }) as ColumnsType<MetricResponse>,
    [orderedColumns, columnWidths, hoveredColKey, dragCol, dragOverCol],
  );

  const totalWidth = useMemo(
    () => resizableColumns.reduce((sum, c) => sum + ((c.width as number | undefined) ?? DEFAULT_COL_WIDTH), 0),
    [resizableColumns],
  );

  const hasFilter = Boolean(
    keyword || status || domain || tier || ownerFilter || myMetricsOnly || piiOnly || lifecycleFilter || favoritesOnly || downstreamFilter !== "all",
  );
  const emptyGuide = useMemo(
    () => (
      <div style={{ padding: "16px 0", textAlign: "center" }}>
        {loadError ? (
          <>
            <p className="muted">加载指标列表失败：{loadError}</p>
            <Button onClick={() => { setLoadError(null); void load(); }}>重试</Button>
          </>
        ) : (
          <>
            <p className="muted">{hasFilter ? "没有匹配的指标，试试放宽或清除筛选条件" : "目录还是空的，创建第一个指标或从模板开始"}</p>
            <Space>
          {hasFilter ? (
            <Button icon={<ColumnWidthOutlined />} aria-label="清除筛选" onClick={() => {
              setKeyword("");
      setInputValue("");
              setStatus("");
              setDomain("");
              setTier("");
              setLifecycleFilter(null);
              setLifecycleDate({});
              setMyMetricsOnly(false);
              setFavoritesOnly(false);
              setSortBy("updated_at");
              setSortOrder("desc");
              setPage(1);
            }}>
              清除筛选
            </Button>
          ) : (
            <>
              {canCreate ? (
                <Space>
                  <Button type="primary" icon={<PlusCircleOutlined />} onClick={() => navigate("/create")}>
                    创建指标
                  </Button>
                  <Button icon={<FileTextOutlined />} onClick={() => navigate("/templates")}>
                    从模板创建
                  </Button>
                </Space>
              ) : (
                <span className="muted">如需创建指标，请联系域管理员或平台管理员</span>
              )}
            </>
          )}
        </Space>
            </>
          )}
      </div>
    ),
    [hasFilter, loadError, canCreate],
  );

  return (
    <div>
      <div className="page-head">
        <div>
          <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
            返回
          </Button>
          <div className="page-kicker">指标资产 / 指标目录</div>
          <h2>指标目录</h2>
          <p>全量指标定义——按状态/域/分级/关键词检索；展开行查看口径与治理追溯。</p>
        </div>
        <Space wrap>
          {canCreate && (
            <>
              <Button type="primary" icon={<PlusCircleOutlined />} onClick={() => navigate("/create")}>
                创建指标
              </Button>
              <Button icon={<FileTextOutlined />} onClick={() => navigate("/templates")}>
                从模板创建
              </Button>
              <Tooltip title="上传 CSV 或供外部智能体调 /batch-import，批量创建 DRAFT 指标（编码/名称可缺省自动补全）">
                <Button icon={<UploadOutlined />} onClick={() => { setImportOpen(true); setImportResult(null); }}>
                  批量导入
                </Button>
              </Tooltip>
            </>
          )}
          <Tooltip title={deletedView ? "回收站数据不可导出（含已软删指标，避免误用为正式数据）" : (canExport ? "将当前筛选结果导出为 CSV" : "无导出权限（metric:export）")}>
            <Button
              icon={<DownloadOutlined />}
              onClick={exportCsv}
              loading={exporting}
              disabled={!items.length || !canExport || deletedView}
            >
              {exporting ? "导出中" : "导出"}
            </Button>
          </Tooltip>
          <Tooltip title="刷新列表（其他用户的新发布/状态变更会在此同步）">
            <Button icon={<ReloadOutlined />} onClick={() => { setLoadError(null); load(); }} loading={loading}>
              刷新
            </Button>
          </Tooltip>
          <Tooltip title={deletedView ? "返回正常指标列表" : "查看已软删的草稿指标（回收站，可恢复）"}>
            <Button
              icon={<DeleteOutlined />}
              type={deletedView ? "primary" : "default"}
              danger={deletedView}
              onClick={() => {
                // 切换回收站视图时清空勾选：避免正常列表/回收站的勾选残留
                // （软删记录 status 仍为 DRAFT，残留勾选会误触发批量删除→重复软删 404）
                setSelected([]);
                setPage(1);
                setDeletedView((v) => !v);
              }}
            >
              {deletedView ? "返回列表" : "回收站"}
            </Button>
          </Tooltip>
          <Tooltip title="勾选 2~6 个指标进行矩阵对比（每行字段、每列指标）">
            <Button
              type="primary"
              icon={<ColumnWidthOutlined />}
              disabled={selected.length < 2}
              onClick={() => {
                // 对比上限 6：勾选不限数量（可能用于批量操作），仅在点对比时校验——
                // 超 6 提示引导取消部分勾选，不清空不截断已选项，弹窗不打开
                if (selected.length > 6) {
                  message.warning(`指标对比最多支持 6 个，当前勾选 ${selected.length} 个，请取消部分勾选后再对比`);
                  return;
                }
                setCompareOpen(true);
              }}
            >
              对比所选{selected.length > 1 ? ` (${selected.length})` : ""}
            </Button>
        </Tooltip>
        <Tooltip title="恢复默认列宽与列顺序（清除本地保存的列布局偏好，下次进入按默认展示）">
          <Button
            icon={<ColumnHeightOutlined />}
            onClick={() => { setColumnWidths({}); setColOrder(null); }}
            disabled={Object.keys(columnWidths).length === 0 && !colOrder}
          >
            重置列布局
          </Button>
        </Tooltip>
        <Dropdown
          trigger={["click"]}
          popupRender={() => (
            <div
              style={{
                background: "#fff",
                borderRadius: 8,
                boxShadow: "0 3px 6px -4px rgba(0,0,0,.12), 0 6px 16px 0 rgba(0,0,0,.08)",
                padding: 12,
                width: 260,
              }}
            >
              <Space direction="vertical" size={12} style={{ width: "100%" }}>
                <span className="muted" style={{ fontSize: 12 }}>
                  列设置（{currentUserRole ? GROUP_LABEL[ROLE_GROUP[currentUserRole] ?? "admin"] : "—"}视图默认）
                </span>
                <Checkbox.Group
                  value={visibleCols ?? []}
                  onChange={(vals) => setVisibleCols(vals as string[])}
                  options={COLUMN_OPTIONS}
                  style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px 8px" }}
                />
                <Button size="small" onClick={handleResetRoleView} icon={<ReloadOutlined />}>
                  恢复角色默认
                </Button>
              </Space>
            </div>
          )}
        >
          <Button icon={<UnorderedListOutlined />}>列设置</Button>
        </Dropdown>
        <Dropdown
          menu={{
              items: deletedView
                ? [
                    {
                      // 回收站批量彻底删除（物理删除不可恢复；仅平台管理员）
                      key: "purge",
                      label: (
                        <Tooltip
                          title={
                            currentUserRole !== "platform_admin"
                              ? "仅平台管理员可彻底删除；回收站中的已软删记录将物理删除不可恢复"
                              : "物理删除勾选的已软删指标，关联版本/维度/健康度/血缘将一并清除"
                          }
                        >
                          <span>批量彻底删除（回收站）</span>
                        </Tooltip>
                      ),
                      icon: <DeleteOutlined />,
                      danger: true,
                      disabled: currentUserRole !== "platform_admin" || !selected.length,
                    },
                  ]
                : [
                {
                  key: "submit",
                  label: (
                    <Tooltip
                      title={
                        !selected.some((m) => m.status === "DRAFT")
                          ? "批量提交仅适用于勾选中的草稿（DRAFT）指标；当前勾选无草稿指标"
                          : !canCreate
                            ? "无提交审核权限（metric:create）"
                            : undefined
                      }
                    >
                      <span>批量提交审核（草稿）</span>
                    </Tooltip>
                  ),
                  icon: <CheckCircleOutlined />,
                  disabled: !selected.some((m) => m.status === "DRAFT") || !canCreate,
                },
                {
                  key: "approve",
                  label: (
                    <Tooltip
                      title={
                        !selected.some((m) => m.status === "REVIEW")
                          ? "批量通过仅适用于勾选中的评审中（REVIEW）指标；当前勾选无评审中指标"
                          : !canApprove
                            ? "无审核通过权限（metric:approve）"
                            : undefined
                      }
                    >
                      <span>批量通过（评审中）</span>
                    </Tooltip>
                  ),
                  icon: <CheckCircleOutlined />,
                  disabled: !selected.some((m) => m.status === "REVIEW") || !canApprove,
                },
                {
                  key: "reject",
                  label: (
                    <Tooltip
                      title={
                        !selected.some((m) => m.status === "REVIEW")
                          ? "批量打回仅适用于勾选中的评审中（REVIEW）指标；当前勾选无评审中指标"
                          : !canApprove
                            ? "无审核打回权限（metric:approve）"
                            : undefined
                      }
                    >
                      <span>批量驳回（评审中）</span>
                    </Tooltip>
                  ),
                  icon: <ClockCircleOutlined />,
                  disabled: !selected.some((m) => m.status === "REVIEW") || !canApprove,
                },
                {
                  key: "deprecate",
                  label: (
                    <Tooltip
                      title={
                        !selected.some((m) => m.status === "PUBLISHED")
                          ? "批量废弃仅适用于勾选中的已发布（PUBLISHED）指标；当前勾选无已发布指标"
                          : !canDeprecate
                            ? "无废弃权限（metric:deprecate）"
                            : undefined
                      }
                    >
                      <span>批量废弃（已发布）</span>
                    </Tooltip>
                  ),
                  icon: <DeleteOutlined />,
                  disabled: !selected.some((m) => m.status === "PUBLISHED") || !canDeprecate,
                },
                {
                  // P2-1：批量恢复已废弃指标（DEPRECATED → DRAFT，对齐维度/逻辑度量/术语批量重新启用）
                  key: "reactivate",
                  label: (
                    <Tooltip
                      title={
                        !selected.some((m) => m.status === "DEPRECATED")
                          ? "批量恢复仅适用于勾选中的已废弃（DEPRECATED）指标；当前勾选无已废弃指标"
                          : !canDeprecate
                            ? "无恢复权限（metric:deprecate）"
                            : undefined
                      }
                    >
                      <span>批量恢复（已废弃）</span>
                    </Tooltip>
                  ),
                  icon: <ReloadOutlined />,
                  disabled: !selected.some((m) => m.status === "DEPRECATED") || !canDeprecate,
                },
                { type: "divider" },
                {
                  key: "delete",
                  label: (
                    <Tooltip
                      title={
                        !selected.some((m) => m.status === "DRAFT" || m.status === "DEPRECATED")
                          ? "批量删除仅适用于勾选中的草稿（DRAFT）或已废弃（DEPRECATED）指标；当前勾选无可删指标"
                          : currentUserRole !== "platform_admin" &&
                              currentUserRole !== "domain_admin" &&
                              !selected.some((m) => m.owner_id === currentUserId)
                            ? "仅平台/域管理员或指标创建者（Owner）可删除；当前勾选非你创建的指标"
                            : undefined
                      }
                    >
                      <span>批量删除（草稿/已废弃）</span>
                    </Tooltip>
                  ),
                  icon: <DeleteOutlined />,
                  danger: true,
                  // 后端允许平台/域管理员或原 Owner 删除 DRAFT/DEPRECATED（service.delete_metric）；非权限禁用避免 403
                  disabled:
                    !selected.some((m) => m.status === "DRAFT" || m.status === "DEPRECATED") ||
                    (currentUserRole !== "platform_admin" &&
                      currentUserRole !== "domain_admin" &&
                      !selected.some((m) => m.owner_id === currentUserId)),
                },
              ],
              onClick: ({ key }) => {
                const act = key as
                  | "submit"
                  | "delete"
                  | "approve"
                  | "reject"
                  | "deprecate"
                  | "reactivate"
                  | "purge";
                if (act === "deprecate") {
                  loadSuccessorOptions();
                  // 打开批量废弃面板即审查勾选已发布指标的下游使用情况
                  setDeprecateBlocked(new Set());
                  loadDownstreamCheck(
                    selected.filter((m) => m.status === "PUBLISHED").map((m) => m.metric_code),
                  );
                }
                setBatchAction(act);
              },
            }}
            trigger={["click"]}
          >
            <Button
              icon={<ThunderboltOutlined />}
              disabled={
                !selected.length ||
                (deletedView ? currentUserRole !== "platform_admin" : !canBatchManage)
              }
              title={
                deletedView
                  ? currentUserRole !== "platform_admin"
                    ? "批量彻底删除仅平台管理员可用"
                    : "回收站批量彻底删除勾选的已软删指标（物理删除不可恢复）"
                  : undefined
              }
            >
              批量操作
            </Button>
          </Dropdown>
        </Space>
      </div>

      {/* 筛选面板：搜索 / 条件筛选 / 快捷筛选 分区展示，避免单行堆叠（美化重构）。
          三个区域用发丝虚线分隔，标签前置让每个控件用途一目了然；已应用筛选在面板内回显 */}
      <Card size="small" style={{ marginBottom: 16 }}>
        {/* ① 搜索行：主入口突出，输入框自适应拉宽 */}
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <Input
            placeholder="搜索指标名 / 编码 / 描述"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onPressEnter={handleSearch}
            prefix={<SearchOutlined />}
            allowClear
            style={{ flex: "1 1 260px", minWidth: 220 }}
          />
          <Button type="primary" onClick={handleSearch} icon={<SearchOutlined />}>
            搜索
          </Button>
          {/* P2-6（第六轮）：批次筛选——SQL/宽表批量创建的指标带 batch_id，列表页
              此前只有展示 Tag 无法按批次收敛；输入批次号精确过滤整批（回车触发，URL 同步） */}
          <Input
            placeholder="批次 ID（批量注册追溯）"
            value={batchIdFilter}
            onChange={(e) => {
              setBatchIdFilter(e.target.value);
              setPage(1);
            }}
            allowClear
            style={{ width: 210 }}
          />
          <Button icon={<ReloadOutlined />} disabled={!hasFilter} onClick={handleResetFilters}>
            重置筛选
          </Button>
          <span style={{ flex: 1 }} />
          <span className="muted" style={{ fontSize: 12 }}>
            {favoritesOnly ? `当前页命中 ${displayItems.length} 条（全量 ${total} 条）` : `共 ${total} 条`}
          </span>
        </div>

        {/* ② 条件筛选行：字段筛选 + 排序，标签分组 */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            flexWrap: "wrap",
            marginTop: 12,
            paddingTop: 12,
            borderTop: "1px dashed var(--line)",
          }}
        >
          <span className="muted" style={{ fontSize: 12, flex: "none" }}>业务域</span>
          <Select
            value={domain || undefined}
            onChange={(v) => { setDomain(v || ""); setPage(1); }}
            style={{ width: 140 }}
            allowClear
            placeholder="全部域"
            options={domainFilterOptions}
          />
          <span className="muted" style={{ fontSize: 12, flex: "none" }}>状态</span>
          <Select
            value={status || undefined}
            onChange={(v) => { setStatus(v || ""); setPage(1); }}
            style={{ width: 140 }}
            allowClear
            placeholder="全部状态"
            options={[
              { value: "DRAFT", label: "草稿" },
              { value: "EXPERIMENTAL", label: "灰度" },
              { value: "REVIEW", label: "审核" },
              { value: "PUBLISHED", label: "已发布" },
              { value: "DEPRECATED", label: "已废弃" },
              { value: "DATA_SOURCE_DROPPED", label: "数据源下线" },
            ]}
          />
          <span className="muted" style={{ fontSize: 12, flex: "none" }}>分级</span>
          <Select
            value={tier || undefined}
            onChange={(v) => { setTier(v || ""); setPage(1); }}
            style={{ width: 120 }}
            allowClear
            placeholder="全部分级"
            options={TIER_OPTIONS}
          />
          {/* 责任人（Owner）筛选：此前仅支持资产地图 URL 下钻（?owner_id=），无独立控件；
              补 UI 入口（复审 D4），选择责任人即按 owner_id 过滤 */}
          <span className="muted" style={{ fontSize: 12, flex: "none" }}>责任人</span>
          <Select
            showSearch
            value={ownerFilter ? Number(ownerFilter) : undefined}
            onChange={(v) => {
              setOwnerFilter(v ? String(v) : "");
              setPage(1);
            }}
            style={{ width: 170 }}
            allowClear
            placeholder="全部责任人"
            optionFilterProp="label"
            options={[...userMap.entries()].map(([id, name]) => ({
              value: id,
              label: `${name}（#${id}）`,
            }))}
          />
          {/* 下游引用过滤（批量废弃前按引用收敛）：有/无下游一键筛选，
              勾选「有下游」批量废弃时自动带替代指标，避免逐个翻详情确认 */}
          <span className="muted" style={{ fontSize: 12, flex: "none" }}>下游引用</span>
          <Select
            value={downstreamFilter === "all" ? undefined : downstreamFilter}
            onChange={(v) => { setDownstreamFilter((v as "with" | "without") || "all"); setPage(1); }}
            style={{ width: 130 }}
            allowClear
            placeholder="全部"
            options={[
              { value: "with", label: "有下游" },
              { value: "without", label: "无下游" },
            ]}
          />
          <span style={{ flex: 1 }} />
          <span className="muted" style={{ fontSize: 12, flex: "none" }}>排序</span>
          <Select
            value={sortBy}
            onChange={setSortBy}
            style={{ width: 140 }}
            options={SORT_OPTIONS}
          />
          <Button
            size="small"
            type={sortOrder === "asc" ? "primary" : "default"}
            onClick={() => setSortOrder((o) => (o === "asc" ? "desc" : "asc"))}
          >
            {sortOrder === "asc" ? "升序 ↑" : "降序 ↓"}
          </Button>
        </div>

        {/* ③ 快捷筛选行：常用视角开关 + 生命周期快筛 */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            flexWrap: "wrap",
            marginTop: 12,
            paddingTop: 12,
            borderTop: "1px dashed var(--line)",
          }}
        >
          <span className="muted" style={{ fontSize: 12, flex: "none" }}>快捷筛选</span>
          {currentUserId && (
            <Button
              type={myMetricsOnly ? "primary" : "default"}
              icon={<UserOutlined />}
              onClick={() => setMyMetricsOnly(!myMetricsOnly)}
            >
              {myMetricsOnly ? "我的指标" : "全部指标"}
            </Button>
          )}
          <Tooltip
            title={
              favoritesError
                ? "收藏列表加载失败，无法使用「只看收藏」过滤"
                : favoritesOnly
                  ? "仅过滤当前页命中的收藏，可在搜索框输入关键字缩小范围"
                  : "只看我收藏的指标（当前页内过滤）"
            }
          >
            <Button
              type={favoritesOnly ? "primary" : "default"}
              icon={favoritesOnly ? <HeartFilled style={{ color: "#eb2f96" }} /> : <HeartOutlined />}
              disabled={favoritesError}
              onClick={() => setFavoritesOnly(!favoritesOnly)}
            >
              {favoritesOnly ? "只看收藏" : "我的收藏"}
            </Button>
          </Tooltip>
          <Tooltip title="只看含 PII 的指标（合规官默认开启此视角）">
            <Button
              type={piiOnly ? "primary" : "default"}
              icon={<SafetyCertificateOutlined />}
              onClick={() => setPiiOnly(!piiOnly)}
            >
              {piiOnly ? "只看PII" : "PII指标"}
            </Button>
          </Tooltip>
          <span className="muted" style={{ fontSize: 12, flex: "none", marginLeft: 4 }}>生命周期</span>
          {LIFECYCLE_PRESETS.map((p) => (
            <Button
              key={p.key}
              size="small"
              type={lifecycleFilter === p.key ? "primary" : "default"}
              icon={p.icon}
              onClick={() => {
                if (lifecycleFilter === p.key) {
                  setLifecycleFilter(null);
                  setStatus("");
                  setLifecycleDate({});
                  setSortBy("updated_at");
                  setPage(1);
                } else {
                  handleLifecycle(p.key);
                }
              }}
            >
              {p.label}
            </Button>
          ))}
        </div>

        {/* ④ 已应用筛选回显 */}
        {hasFilter && !deletedView && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              flexWrap: "wrap",
              marginTop: 12,
              paddingTop: 10,
              borderTop: "1px dashed var(--line)",
            }}
          >
            <span className="muted" style={{ fontSize: 12, flex: "none" }}>已应用筛选：</span>
            {keyword && <Tag closable onClose={() => { setKeyword(""); setInputValue(""); setPage(1); }}>关键词：{keyword}</Tag>}
            {status && <Tag closable onClose={() => { setStatus(""); setPage(1); }}>状态：{METRIC_STATUS_LABEL[status] ?? status}</Tag>}
            {domain && <Tag closable onClose={() => { setDomain(""); setPage(1); }}>域：{domainName(domain)}</Tag>}
            {tier && <Tag closable onClose={() => { setTier(""); setPage(1); }}>分级：{METRIC_TIER_LABEL[tier] ?? tier}</Tag>}
            {lifecycleFilter && (
              <Tag closable onClose={() => { setLifecycleFilter(null); setStatus(""); setLifecycleDate({}); setSortBy("updated_at"); setPage(1); }}>
                {LIFECYCLE_PRESETS.find((p) => p.key === lifecycleFilter)?.label ?? lifecycleFilter}
              </Tag>
            )}
            {ownerFilter && <Tag closable onClose={() => { setOwnerFilter(""); setPage(1); }}>责任人下钻</Tag>}
            {batchIdFilter && <Tag closable onClose={() => { setBatchIdFilter(""); setPage(1); }}>批次：{batchIdFilter}</Tag>}
            {myMetricsOnly && <Tag closable onClose={() => { setMyMetricsOnly(false); setPage(1); }}>我的指标</Tag>}
            {piiOnly && <Tag closable onClose={() => { setPiiOnly(false); setPage(1); }}>只看 PII</Tag>}
            {downstreamFilter !== "all" && (
              <Tag closable onClose={() => { setDownstreamFilter("all"); setPage(1); }}>
                下游引用：{downstreamFilter === "with" ? "有下游" : "无下游"}
              </Tag>
            )}
            {favoritesOnly && <Tag closable onClose={() => { setFavoritesOnly(false); }}>只看收藏</Tag>}
            {/* 按用户群体差异化：当前角色视图只读提示（静默生效，避免用户困惑列为何变化）+ 一键恢复默认 */}
            {currentUserRole && visibleCols !== null && (
              <Tag color="blue">
                {GROUP_LABEL[ROLE_GROUP[currentUserRole] ?? "admin"] ?? ""}视图
                <a style={{ marginLeft: 6, fontSize: 12 }} onClick={handleResetRoleView}>
                  恢复默认
                </a>
              </Tag>
            )}
          </div>
        )}
      </Card>

      <Table
        dataSource={displayItems}
        columns={resizableColumns}
        rowKey="metric_code"
        loading={loading}
        rowSelection={{
          selectedRowKeys: selected.map((s) => s.metric_code),
          // 勾选不限数量：批量操作（提交审核/通过/打回/下线/删除）可能一次选很多；
          // 「最多 6 个」是对比专属限制，只在点击「对比所选」时校验（见按钮 onClick）
          onChange: (_, rows) => {
            setSelected(rows);
          },
        }}
        expandable={{
          expandedRowRender: (r) => <ExpandContent r={r} userName={userName} domainName={domainName} measureName={measureName} group={roleGroup} />,
        }}
        scroll={{ x: totalWidth }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          pageSizeOptions: [10, 20, 50, 100],
          onChange: (p, ps) => { setPage(p); onShowSizeChange(p, ps); },
          showTotal: (t) => `共 ${t} 条`,
        }}
        onRow={(record) => ({
          onClick: () => {
            setPreviewMetric(record);
            setPreviewOpen(true);
          },
          style: { cursor: "pointer" },
        })}
        locale={{ emptyText: emptyGuide }}
      />

      <Drawer
        title={previewMetric ? `${previewMetric.metric_code} · ${previewMetric.name}` : "指标预览"}
        open={previewOpen}
        onClose={() => setPreviewOpen(false)}
        width={600}
        extra={
          previewMetric ? (
            <Button type="primary" onClick={() => { setPreviewOpen(false); navigate(`/detail/${previewMetric.metric_code}`); }}>
              查看完整详情 →</Button>
          ) : null
        }
      >
        {previewMetric && (
          <ExpandContent r={previewMetric} userName={userName} domainName={domainName} measureName={measureName} group={roleGroup} />
        )}
      </Drawer>

      <Modal
        title={
          batchAction === "submit"
            ? "批量提交审核"
            : batchAction === "approve"
              ? "批量通过"
              : batchAction === "reject"
                ? "批量驳回"
                : batchAction === "deprecate"
                  ? "批量废弃"
                  : batchAction === "reactivate"
                    ? "批量恢复已废弃指标"
                    : batchAction === "purge"
                      ? "批量彻底删除（回收站）"
                      : "批量删除草稿/已废弃"
        }
        open={batchAction !== null}
        confirmLoading={batchBusy}
        onOk={runBatch}
        onCancel={() => setBatchAction(null)}
        okText={
          batchAction === "submit"
            ? "提交"
            : batchAction === "approve"
              ? "通过"
              : batchAction === "reject"
                ? "驳回"
                : batchAction === "deprecate"
                  ? "废弃"
                  : batchAction === "reactivate"
                    ? "恢复"
                    : batchAction === "purge"
                      ? "彻底删除"
                      : "删除"
        }
        okButtonProps={{
          danger: batchAction === "delete" || batchAction === "deprecate" || batchAction === "purge",
          // 指定评审用户但未选用户时禁止提交（后端校验 user 类型须有 reviewer_id，前置拦截提升体验）
          disabled:
            batchAction === "submit" && batchReviewerType === "user" && !batchReviewerId,
        }}
      >
        {batchAction === "submit" && (
          <div>
            <p>
              将勾选的 <b>{selected.filter((m) => m.status === "DRAFT").length}</b> 个草稿指标提交审核
              （DRAFT → REVIEW）。非草稿状态的勾选项将被跳过。
            </p>
            <p style={{ marginTop: 12, marginBottom: 4 }}>
              评审指派（可选）：指定评审用户或域评审组，审批页仅被指派者可评审
            </p>
            <Space wrap>
              <Select
                style={{ width: 160 }}
                placeholder="不指派（域管理员兜底）"
                allowClear
                value={batchReviewerType ?? undefined}
                onChange={(v) => setBatchReviewerType(v ?? null)}
                options={[
                  { value: "user", label: "指定评审用户" },
                  { value: "domain", label: "域评审组" },
                ]}
              />
              {batchReviewerType === "user" && (
                <Select
                  style={{ width: 220 }}
                  placeholder="选择评审用户"
                  showSearch
                  optionFilterProp="label"
                  value={batchReviewerId ?? undefined}
                  onChange={(v) => setBatchReviewerId(v ?? null)}
                  options={[...userLabelMap.entries()].map(([id, label]) => ({
                    value: id,
                    label,
                  }))}
                />
              )}
              {batchReviewerType === "domain" && (
                <span className="muted" style={{ fontSize: 12 }}>
                  将按各指标自身域组建评审组（该域 domain_admin/reviewer 可评审）
                </span>
              )}
            </Space>
          </div>
        )}
        {batchAction === "approve" && (
          <div>
            <p>
              将勾选的 <b>{selected.filter((m) => m.status === "REVIEW").length}</b> 个评审中指标通过并发布
              （REVIEW → PUBLISHED）。评审人指派校验由后端逐条执行，未通过项显示原因。
            </p>
            {/* P1-3（第六轮）：批量通过支持灰度发布——对齐单条 MetricReview 的
                标准发布/灰度发布（仅指定租户）选择，后端 MetricBatchApproveRequest
                已接受 mode/gray_tenant_ids，前端此前不传 */}
            <div style={{ marginTop: 12 }}>
              <Radio.Group
                value={batchApproveMode}
                onChange={(e) => setBatchApproveMode(e.target.value as "standard" | "experimental")}
              >
                <Radio value="standard">标准发布（全部租户）</Radio>
                <Radio value="experimental">灰度发布（仅指定租户）</Radio>
              </Radio.Group>
            </div>
            {batchApproveMode === "experimental" && (
              <Input
                style={{ marginTop: 8 }}
                placeholder="灰度租户 ID（逗号/空格分隔，如 1001, 1002）"
                value={batchGrayTenants}
                onChange={(e) => setBatchGrayTenants(e.target.value)}
              />
            )}
          </div>
        )}
        {batchAction === "reject" && (
          <div>
            <p>
              将勾选的 <b>{selected.filter((m) => m.status === "REVIEW").length}</b> 个评审中指标驳回至草稿
              （REVIEW → DRAFT）。
            </p>
            <Input.TextArea
              rows={2}
              placeholder="驳回原因（必填，至少 4 字）"
              value={batchRejectReason}
              onChange={(e) => setBatchRejectReason(e.target.value)}
            />
          </div>
        )}
        {batchAction === "deprecate" && (
          <div>
            <p>
              将勾选的 <b>{selected.filter((m) => m.status === "PUBLISHED").length}</b> 个已发布指标废弃
              （PUBLISHED → DEPRECATED）。已先审查每个指标的下游使用情况：
              <b style={{ color: "#fa8c16" }}>有下游引用须填替代指标</b>，
              <b style={{ color: "#52c41a" }}>无下游引用可安全废弃（替代指标选填）</b>。
            </p>
            {selected
              .filter((m) => m.status === "PUBLISHED")
              .map((m) => {
                const info = downstreamMap[m.metric_code];
                const hasDownstream = (info?.referrer_count ?? 0) > 0;
                const blocked = deprecateBlocked.has(m.metric_code);
                return (
                  <div
                    key={m.metric_code}
                    style={{
                      marginBottom: 8,
                      padding: 8,
                      border: `1px solid ${blocked ? "#ff4d4f" : "#f0f0f0"}`,
                      borderRadius: 6,
                      background: blocked ? "#fff2f0" : undefined,
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
                      <span className="mono" style={{ fontSize: 12 }}>
                        {m.metric_code}
                      </span>
                      {downstreamLoading ? (
                        <span style={{ fontSize: 12, color: "#999" }}>下游使用审查中…</span>
                      ) : info === undefined ? (
                        <span style={{ fontSize: 12, color: "#999" }}>未获取到下游信息</span>
                      ) : hasDownstream ? (
                        <Tooltip
                          title={
                            "下游引用：" +
                            (info.referrers
                              .map((r) =>
                                r.edge_type === "DERIVED_FROM"
                                  ? `派生指标 ${r.node.replace("metric:", "")}`
                                  : r.edge_type === "BASED_ON"
                                    ? `基础原子引用 ${r.node.replace("metric:", "")}`
                                    : `消费方 ${r.node.replace("consumer:", "")}`,
                              )
                              .join("；") || "引用明细不可见")
                          }
                        >
                          <Tag color="orange">⚠ 被 {info.referrer_count} 处下游引用</Tag>
                        </Tooltip>
                      ) : (
                        <Tag color="green">✓ 无下游引用，可安全废弃</Tag>
                      )}
                      {blocked && (
                        <span style={{ color: "#ff4d4f", fontSize: 12 }}>须填写替代指标</span>
                      )}
                    </div>
                    <Select
                      allowClear
                      showSearch
                      optionFilterProp="label"
                      style={{ width: 280, marginTop: 6 }}
                      placeholder={hasDownstream ? "有下游引用，须选择替代指标" : "无下游引用，替代指标选填"}
                      value={batchSuccessors[m.metric_code] || undefined}
                      onChange={(v) =>
                        setBatchSuccessors((prev) => ({ ...prev, [m.metric_code]: v ?? "" }))
                      }
                      options={batchSuccessorOptions.filter(
                        (o) => !selected.some((s) => s.metric_code === o.value),
                      )}
                      notFoundContent="无已发布指标可作替代"
                    />
                  </div>
                );
              })}
          </div>
        )}
        {batchAction === "reactivate" && (
          <p>
            将勾选的 <b>{selected.filter((m) => m.status === "DEPRECATED").length}</b> 个已废弃指标恢复为草稿
            （DEPRECATED → DRAFT）。恢复后请重新提交审核方可发布（不绕过审核流）。
          </p>
        )}
        {batchAction === "delete" && (
          <p>
            将删除勾选的 <b>{selected.filter((m) => m.status === "DRAFT" || m.status === "DEPRECATED").length}</b> 个草稿/已废弃指标
            （软删除，仅平台/域管理员或指标创建者可执行）。如需找回，可在右上角「回收站」中恢复。
          </p>
        )}
        {batchAction === "purge" && (
          <p>
            将<b>物理彻底删除</b>勾选的 <b>{selected.length}</b> 个回收站指标（<b>不可恢复</b>），
            关联版本 / 维度 / 健康度 / 血缘将一并清除。仅平台管理员可执行。
          </p>
        )}
      </Modal>

      {/* 批量操作失败明细弹窗：完整展示所有失败项，避免 message 截断；可一键重试失败项 */}
      <Modal
        title="批量操作失败明细"
        open={batchErrorsOpen}
        onCancel={() => setBatchErrorsOpen(false)}
        footer={
          <>
            {batchFailedCodes.length > 0 && (
              <Button
                onClick={() => {
                  // 把失败项重新选入 selected，并恢复原操作类型（重新打开确认弹窗）
                  setSelected(items.filter((m) => batchFailedCodes.includes(m.metric_code)));
                  setBatchErrorsOpen(false);
                  setBatchAction(batchRetryActionRef.current);
                }}
              >
                重试失败项
              </Button>
            )}
            <Button onClick={() => setBatchErrorsOpen(false)}>关闭</Button>
          </>
        }
      >
        <ul style={{ maxHeight: 320, overflow: "auto", paddingLeft: 18 }}>
          {batchErrors.map((e, i) => (
            <li key={i} className="mono" style={{ marginBottom: 6, fontSize: 12 }}>
              {e}
            </li>
          ))}
        </ul>
      </Modal>
      {/* 批量导入（CSV / 外部 agent）弹窗：上传 CSV 批量创建 DRAFT，逐行容错 + 逐条结果回显 */}
      <Modal
        title="批量导入指标"
        open={importOpen}
        onCancel={() => setImportOpen(false)}
        footer={<Button onClick={() => setImportOpen(false)}>关闭</Button>}
        width={720}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="批量录入存量指标"
          description="上传 CSV 批量创建 DRAFT 指标（编码/名称可缺省，系统自动按域/源表/度量列补全）。外部智能体也可直接调用 POST /api/v1/metric-definitions/batch-import 接口对接（字段说明见 API 文档）。"
        />
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
          <Button icon={<DownloadOutlined />} onClick={() => downloadMetricImportTemplate().catch(() => message.error("模板下载失败"))}>
            下载 CSV 模板
          </Button>
          <Select
            style={{ width: 220 }}
            placeholder="选择目标域"
            value={importDomain || undefined}
            onChange={(v) => setImportDomain(v)}
            options={domainOptions}
            showSearch
            optionFilterProp="label"
          />
        </div>
        <Upload
          accept=".csv"
          showUploadList={false}
          beforeUpload={(file) => {
            if (!importDomain) {
              message.warning("请先选择目标域");
              return Upload.LIST_IGNORE;
            }
            const fd = new FormData();
            fd.append("file", file);
            fd.append("domain", importDomain);
            setImporting(true);
            setImportResult(null);
            importMetricsCsv(fd)
              .then((r) => {
                setImportResult(r);
                const ok = r.candidates.filter((c) => c.status === "DRAFT").length;
                const fail = r.candidates.length - ok;
                if (r.row_errors?.length) {
                  message.warning(`导入完成：成功 ${ok} 条，失败 ${fail} 条，解析错误 ${r.row_errors.length} 行`);
                } else if (fail > 0) {
                  message.warning(`导入完成：成功 ${ok} 条，失败 ${fail} 条（详见下方明细）`);
                } else {
                  message.success(`导入完成：成功 ${ok} 条`);
                }
              })
              .catch((e) => {
                message.error(e instanceof UnisenseApiError ? e.message : "批量导入失败");
              })
              .finally(() => setImporting(false));
            return Upload.LIST_IGNORE;
          }}
        >
          <Button icon={<UploadOutlined />} loading={importing} disabled={!importDomain}>
            选择 CSV 文件上传
          </Button>
        </Upload>
        {importResult && (
          <>
            <div style={{ borderTop: "1px dashed var(--line)", margin: "14px 0 10px" }} />
            <div className="muted" style={{ marginBottom: 8, fontSize: 12 }}>
              批次 {importResult.batch_id}：成功{" "}
              {importResult.candidates.filter((c) => c.status === "DRAFT").length} 条，失败{" "}
              {importResult.candidates.filter((c) => c.status !== "DRAFT").length} 条
              {importResult.row_errors?.length ? `，解析错误 ${importResult.row_errors.length} 行` : ""}
            </div>
            {importResult.row_errors && importResult.row_errors.length > 0 && (
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 8 }}
                message="以下行解析失败（未创建）"
                description={
                  <ul style={{ maxHeight: 120, overflow: "auto", paddingLeft: 18, margin: 0 }}>
                    {importResult.row_errors.map((r) => (
                      <li key={r.row} className="mono" style={{ fontSize: 12 }}>
                        第 {r.row} 行：{r.error}
                      </li>
                    ))}
                  </ul>
                }
              />
            )}
            <Table
              size="small"
              rowKey="metric_code"
              dataSource={importResult.candidates}
              pagination={false}
              columns={[
                { title: "指标编码", dataIndex: "metric_code", ellipsis: true },
                {
                  title: "结果",
                  dataIndex: "status",
                  render: (s: string, r: { validation_errors?: string[] }) =>
                    s === "DRAFT" ? (
                      <Tag color="green">已创建（草稿）</Tag>
                    ) : (
                      <Tooltip title={r.validation_errors?.join("；")}>
                        <Tag color="red">{s}</Tag>
                      </Tooltip>
                    ),
                },
              ]}
            />
          </>
        )}
      </Modal>
      <MetricCompareModal
        open={compareOpen}
        codes={selected.map((s) => s.metric_code)}
        onClose={() => setCompareOpen(false)}
      />
    </div>
  );
}