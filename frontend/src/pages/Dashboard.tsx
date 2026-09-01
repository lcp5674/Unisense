import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Card, Popconfirm, Popover, Row, Col, Spin, Alert, Tag, Empty, Tooltip, message } from "antd";
import {
  AppstoreOutlined,
  PlusCircleOutlined,
  ConsoleSqlOutlined,
  GlobalOutlined,
  RobotOutlined,
  DeploymentUnitOutlined,
  ExperimentOutlined,
  WarningOutlined,
  SafetyCertificateOutlined,
  IssuesCloseOutlined,
  FieldTimeOutlined,
} from "@ant-design/icons";
import { Pie, Bar } from "@ant-design/charts";
import {
  fetchDashboard,
  fetchObsOverview,
  fetchRecommendedMetrics,
  fetchRecommendedTerms,
  listDomainTree,
} from "../api";
import type {
  AssetStat,
  DashboardData,
  ObsOverview,
  OwnerAssetStat,
  RecommendItem,
  GlossaryTerm,
  SubjectDomainTreeNode,
} from "../types";
import { METRIC_HEALTH_LEVEL_LABEL } from "../utils/enums";
import { useTracking } from "../hooks/useTracking";

const EDGE_TYPE_LABEL: Record<string, string> = {
  DERIVED_FROM: "派生自",
  CONSUMED_BY: "被消费",
  LINEAGE: "关联",
  POPULAR: "热门",
  RECENT: "最新",
};

// 推荐指标数量：默认展示 6 条（卡片区高度有限，超出滚动）；「查看更多」可展开到 20 条
const RECOMMEND_INITIAL_LIMIT = 6;
const RECOMMEND_EXPAND_LIMIT = 20;

// 生命周期五站：顺序即真实流程
const STATIONS = [
  { key: "DRAFT", name: "草稿", hotPriority: 2 },
  { key: "EXPERIMENTAL", name: "灰度", hotPriority: 3 },
  { key: "REVIEW", name: "审核中", hotPriority: 1 },
  { key: "PUBLISHED", name: "已发布", hotPriority: 4 },
  { key: "DEPRECATED", name: "已废弃", hotPriority: 5 },
] as const;

const STATUS_LABEL: Record<string, string> = {
  DRAFT: "DRAFT",
  EXPERIMENTAL: "EXPERIMENTAL",
  REVIEW: "REVIEW",
  PUBLISHED: "PUBLISHED",
  DEPRECATED: "DEPRECATED",
};

function LifecycleSignalBar({ data }: { data: DashboardData }) {
  const navigate = useNavigate();
  const total = Math.max(data.total, 1);

  // 最热站：有待审 → 审核；有草稿 → 草稿；否则按已发布等
  const hotKey = useMemo(() => {
    const order = [
      { k: "REVIEW", v: data.by_status.REVIEW ?? 0 },
      { k: "DRAFT", v: data.by_status.DRAFT ?? 0 },
      { k: "EXPERIMENTAL", v: data.by_status.EXPERIMENTAL ?? 0 },
      { k: "PUBLISHED", v: data.by_status.PUBLISHED ?? 0 },
    ];
    const hit = order.find((o) => (o.v ?? 0) > 0);
    return hit ? hit.k : "PUBLISHED";
  }, [data]);

  return (
    <div className="lifecycle-track">
      <div className="lc-line" />
      {STATIONS.map((s) => {
        const count = data.by_status[s.key] ?? 0;
        const hot = s.key === hotKey;
        const pct = Math.round((count / total) * 100);
        return (
          <Tooltip key={s.key} title={`${s.name}：${count} 个（${pct}%）`}>
            <div
              className={`lifecycle-station${hot ? " hot" : ""}`}
              onClick={() => navigate(`/catalog?status=${STATUS_LABEL[s.key]}`)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => e.key === "Enter" && navigate(`/catalog?status=${STATUS_LABEL[s.key]}`)}
            >
              <div className="lc-led">{s.name.slice(0, 1)}</div>
              <div className="lc-name">{s.name}</div>
              <div className="lc-readout">{count}</div>
              <div className="lc-sub">{pct}%</div>
            </div>
          </Tooltip>
        );
      })}
    </div>
  );
}

function GaugeCell({
  label,
  value,
  sub,
  accent,
  small,
}: {
  label: string;
  value: string | number;
  sub?: string;
  accent?: "signal" | "data" | "ok" | "warn" | "danger";
  small?: boolean;
}) {
  return (
    <div className="gauge-cell" data-accent={accent ?? "data"}>
      <div className="g-label">{label}</div>
      <div className={`g-value${small ? " small" : ""}`}>{value}</div>
      {sub && <div className="g-sub">{sub}</div>}
    </div>
  );
}

function DomainChart({
  byDomain,
  nameMap,
}: {
  byDomain: Record<string, number>;
  nameMap: Record<string, string>;
}) {
  const data = useMemo(
    () =>
      Object.entries(byDomain)
        .map(([domain, count]) => ({ type: nameMap[domain] ?? domain, value: count }))
        .sort((a, b) => b.value - a.value)
        .slice(0, 8),
    [byDomain, nameMap],
  );
  if (data.length === 0) return <Empty description="暂无域分布数据" />;
  const config = {
    data,
    angleField: "value",
    colorField: "type",
    radius: 0.9,
    innerRadius: 0.62,
    label: { text: "value", style: { fontWeight: 600, fontSize: 12 } },
    legend: { color: { position: "right", layout: "vertical", itemLabelFontSize: 12 } },
    interactions: [{ type: "element-active" }],
  };
  return <Pie {...config} height={260} />;
}

function TierBar({ byTier }: { byTier: Record<string, number> }) {
  const data = useMemo(
    () =>
      (["T1", "T2", "T3"] as const)
        .filter((t) => (byTier[t] ?? 0) > 0)
        .map((t) => ({ tier: t, count: byTier[t] ?? 0 })),
    [byTier],
  );
  if (data.length === 0) return <Empty description="暂无分级数据" />;
  const config = {
    data,
    xField: "tier",
    yField: "count",
    colorField: "tier",
    color: ["#E8862D", "#0E7C86", "#5B6472"],
    label: { text: "count", style: { fontWeight: 600 } },
    legend: { color: { position: "top" } },
  };
  return <Bar {...config} height={260} />;
}

const QUICK_ENTRIES = [
  {
    icon: <PlusCircleOutlined />,
    title: "注册指标",
    desc: "录入新指标定义，进入生命周期",
    to: "/create",
  },
  {
    icon: <ConsoleSqlOutlined />,
    title: "查询工作台",
    desc: "dry-run 校验语义，安全执行查询",
    to: "/query",
  },
  {
    icon: <GlobalOutlined />,
    title: "资产地图",
    desc: "图谱 / 敏感热力 / 责任人视图",
    to: "/assetmap",
  },
  {
    icon: <RobotOutlined />,
    title: "AI 助手",
    desc: "内测中，暂未开放使用",
    to: "/ai",
  },
  {
    icon: <DeploymentUnitOutlined />,
    title: "冲突仲裁",
    desc: "处理口径冲突与定义分歧",
    to: "/review",
  },
  {
    icon: <ExperimentOutlined />,
    title: "质量中心",
    desc: "质量规则、告警与基准对账",
    to: "/quality",
  },
];

// ---- 资产总览卡片配置（对齐后端 /semantics/dashboard assets 聚合）----
// 每种资产按自身的治理/运行状态分组，点击状态段带参数下钻对应目录页。
interface AssetStatusDef {
  value: string;
  label: string;
}
interface AssetConfig {
  key: keyof NonNullable<DashboardData["assets"]>;
  label: string;
  route: string;
  /** 下钻时写入 URL 的查询参数名（各目录页按此参数过滤） */
  statusParam: string;
  statuses: AssetStatusDef[];
}

const ASSET_CONFIGS: AssetConfig[] = [
  {
    key: "metric",
    label: "指标",
    route: "/catalog",
    statusParam: "status",
    statuses: [
      { value: "DRAFT", label: "草稿" },
      { value: "EXPERIMENTAL", label: "灰度" },
      { value: "REVIEW", label: "审核中" },
      { value: "PUBLISHED", label: "已发布" },
      { value: "DEPRECATED", label: "已废弃" },
    ],
  },
  {
    key: "table",
    label: "数据表",
    route: "/catalogs",
    statusParam: "sensitivity",
    statuses: [
      { value: "PUBLIC", label: "公开" },
      { value: "INTERNAL", label: "内部" },
      { value: "CONFIDENTIAL", label: "机密" },
      { value: "PII", label: "PII" },
      { value: "NEEDS_REVIEW", label: "待复核" },
    ],
  },
  {
    key: "source",
    label: "数据源",
    route: "/data-sources",
    statusParam: "health",
    statuses: [
      { value: "healthy", label: "健康" },
      { value: "unhealthy", label: "异常" },
      { value: "unknown", label: "未知" },
    ],
  },
  {
    key: "dimension",
    label: "维度",
    route: "/dimensions",
    statusParam: "status",
    statuses: [
      { value: "DRAFT", label: "草稿" },
      { value: "PUBLISHED", label: "已发布" },
      { value: "DEPRECATED", label: "已废弃" },
    ],
  },
  {
    key: "term",
    label: "术语",
    route: "/glossary",
    statusParam: "status",
    statuses: [
      { value: "DRAFT", label: "草稿" },
      { value: "PUBLISHED", label: "已发布" },
      { value: "DEPRECATED", label: "已废弃" },
    ],
  },
  {
    key: "template",
    label: "指标模板",
    route: "/templates",
    statusParam: "is_active",
    statuses: [
      { value: "active", label: "启用" },
      { value: "inactive", label: "停用" },
    ],
  },
  {
    key: "collection_task",
    label: "采集任务",
    route: "/collection-tasks",
    statusParam: "status",
    statuses: [
      { value: "QUEUED", label: "排队" },
      { value: "RUNNING", label: "采集中" },
      { value: "COMPLETED", label: "已完成" },
      { value: "FAILED", label: "失败" },
    ],
  },
  {
    key: "system_dict",
    label: "数据字典",
    route: "/dicts",
    statusParam: "status",
    statuses: [
      { value: "active", label: "启用" },
      { value: "inactive", label: "停用" },
    ],
  },
];

function AssetCard({
  config,
  stat,
  navigate,
}: {
  config: AssetConfig;
  stat?: AssetStat;
  navigate: (to: string) => void;
}) {
  const total = stat?.total ?? 0;
  const byStatus = stat?.by_status ?? {};
  // 采集链路故障（后端 unavailable 标记）：不伪装成「0 个任务」，明示不可用
  const unavailable = stat?.unavailable ?? false;
  return (
    <div className="asset-card">
      <button
        className="ac-head"
        type="button"
        onClick={() => navigate(config.route)}
        title={`查看全部${config.label}`}
      >
        <span className="ac-label">{config.label}</span>
        <span className="ac-total">{unavailable ? "—" : total}</span>
      </button>
      {unavailable ? (
        <div className="ac-unavailable" title={stat?.message ?? "采集服务暂不可用"}>
          <WarningOutlined /> 采集服务暂不可用
        </div>
      ) : (
        <div className="ac-statuses">
          {config.statuses.map((s) => {
            const count = byStatus[s.value] ?? 0;
            return (
              <button
                key={s.value}
                type="button"
                className={`ac-seg${count > 0 ? " has" : ""}`}
                onClick={() => navigate(`${config.route}?${config.statusParam}=${s.value}`)}
                title={`${s.label}：${count} 个（下钻 ${config.label} 目录）`}
              >
                <span className="ac-seg-name">{s.label}</span>
                <span className="ac-seg-count">{count}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ---- 治理指标体系：质量健康 / 合规复核 / 冲突风险 / 近 30 天更新 ----
function GovernanceCards({ data, navigate }: { data: DashboardData; navigate: (to: string) => void }) {
  const q = data.quality ?? { total: 0, by_severity: {}, pending: 0 };
  const c = data.compliance ?? { total: 0, reviewed: 0, pending: 0, reviewed_ratio: 0 };
  const cf = data.conflict ?? { total: 0, open: 0, escalated: 0, by_status: {} };
  const f = data.freshness ?? { total: 0, updated_30d: 0, updated_30d_ratio: 0 };
  const sevOrder = ["P0", "P1", "P2"] as const;

  return (
    <Card
      style={{ marginBottom: 20 }}
      styles={{ body: { paddingTop: 16, paddingBottom: 16 } }}
      title={
        <span style={{ fontSize: 15, fontWeight: 600 }}>
          治理指标体系
          <span className="muted" style={{ fontWeight: 400, fontSize: 12, marginLeft: 8 }}>
            质量 · 合规 · 冲突 · 新鲜度 —— 点击卡片进入对应工作台
          </span>
        </span>
      }
    >
      <div className="gov-grid">
        <button className="gov-card" data-tone="danger" type="button" onClick={() => navigate("/quality")} title="进入质量中心">
          <div className="gov-head">
            <span className="gov-label"><WarningOutlined /> 质量健康</span>
            <span className="gov-total">{q.total}</span>
          </div>
          <div className="gov-sevs">
            {sevOrder.map((s) => (
              <span key={s} className="gov-sev" data-sev={s}><b>{s}</b> {q.by_severity[s] ?? 0}</span>
            ))}
          </div>
          <div className="gov-sub">{q.pending > 0 ? `待处理 ${q.pending} 项` : "当前无待处理告警"}</div>
        </button>

        <button className="gov-card" data-tone="ok" type="button" onClick={() => navigate("/catalog")} title="进入指标目录">
          <div className="gov-head">
            <span className="gov-label"><SafetyCertificateOutlined /> 合规复核</span>
            <span className="gov-total">{Math.round(c.reviewed_ratio * 100)}%</span>
          </div>
          <div className="gov-sevs">
            <span className="gov-sev">已复核 <b>{c.reviewed}</b></span>
            <span className="gov-sev">待复核 <b>{c.pending}</b></span>
          </div>
          <div className="gov-sub">指标合规复核进度</div>
        </button>

        <button className="gov-card" data-tone="warn" type="button" onClick={() => navigate("/review")} title="进入冲突仲裁">
          <div className="gov-head">
            <span className="gov-label"><IssuesCloseOutlined /> 冲突风险</span>
            <span className="gov-total">{cf.total}</span>
          </div>
          <div className="gov-sevs">
            <span className="gov-sev">待仲裁 <b>{cf.open}</b></span>
            <span className="gov-sev">升级中 <b>{cf.escalated}</b></span>
          </div>
          <div className="gov-sub">未关闭冲突总数</div>
        </button>

        <button className="gov-card" data-tone="info" type="button" onClick={() => navigate("/catalog")} title="进入指标目录">
          <div className="gov-head">
            <span className="gov-label"><FieldTimeOutlined /> 近 30 天更新</span>
            <span className="gov-total">{f.updated_30d}</span>
          </div>
          <div className="gov-sevs">
            <span className="gov-sev">更新占比 <b>{Math.round(f.updated_30d_ratio * 100)}%</b></span>
          </div>
          <div className="gov-sub">近 30 天有更新的指标数</div>
        </button>
      </div>
    </Card>
  );
}

// ---- 指标可信度：健康度四档分布（绿/黄/红）+ 覆盖率 + 低健康 Top ----
// 数据源复用可观测中心聚合端点（/observability/overview quality.metric_health）——
// 与可观测中心同源同口径，避免在仪表盘聚合端点重复实现健康度评分逻辑。
const METRIC_HEALTH_ORDER = ["EXCELLENT", "GOOD", "WARNING", "CRITICAL"] as const;
type MetricHealth = ObsOverview["quality"]["metric_health"];

function MetricCredibilityCard({ h, navigate }: { h: MetricHealth; navigate: (to: string) => void }) {
  const byLevel = h.by_level ?? {};
  const totalScored = h.total_scored ?? 0;
  const coverage = h.coverage_pct ?? 0;
  const risks = h.top_risk ?? [];
  // 绿档可信率：EXCELLENT+GOOD 占已评分指标的比例，作为「业务可信度」的主读数
  // （覆盖率在"每日强制全量评分"机制下恒为 100%，无区分度，只作次要信息）
  const totalHealth = METRIC_HEALTH_ORDER.reduce((sum, l) => sum + (byLevel[l] ?? 0), 0);
  const credibleRate = totalHealth > 0 ? Math.round(((byLevel.EXCELLENT ?? 0) + (byLevel.GOOD ?? 0)) / totalHealth * 100) : 0;
  const credibleCount = (byLevel.EXCELLENT ?? 0) + (byLevel.GOOD ?? 0);

  return (
    <Card
      style={{ marginBottom: 20 }}
      styles={{ body: { paddingTop: 16, paddingBottom: 16 } }}
      title={
        <span style={{ fontSize: 15, fontWeight: 600 }}>
          指标可信度
          <span className="muted" style={{ fontWeight: 400, fontSize: 12, marginLeft: 8 }}>
            基于健康度五维评分（口径完整度 / 活跃度 / 质量 / Owner 响应 / 血缘覆盖）—— 点击档位下钻指标目录，点击低健康指标直达详情
          </span>
        </span>
      }
    >
      {/* 外层用 div（role=button）承载"进入可观测中心"，内部档位/低健康项用真实
          button 独立下钻——避免 button 嵌套 button 的非法 HTML 与冒泡冲突 */}
      <div
        className="gov-card gov-card-wide"
        data-tone="health"
        role="button"
        tabIndex={0}
        onClick={() => navigate("/observability")}
        onKeyDown={(e) => {
          if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            navigate("/observability");
          }
        }}
        title="进入可观测中心查看完整健康明细"
      >
        <div className="gov-head">
          <span className="gov-label">
            <SafetyCertificateOutlined /> 绿档可信率
          </span>
          <span className="gov-total">{credibleRate}%</span>
        </div>
        <div className="gov-sevs">
          {METRIC_HEALTH_ORDER.map((l) => (
            <button
              key={l}
              type="button"
              className="gov-sev gov-sev-link"
              data-sev={l}
              title={`查看${METRIC_HEALTH_LEVEL_LABEL[l] ?? l}健康度的指标`}
              onClick={(e) => {
                e.stopPropagation();
                navigate(`/catalog?health=${l}`);
              }}
            >
              {METRIC_HEALTH_LEVEL_LABEL[l] ?? l} <b>{byLevel[l] ?? 0}</b>
            </button>
          ))}
        </div>
        <div className="gov-sub">
          健康覆盖率 {coverage}% · 已评分 {totalScored} 项 · 可信档 {credibleCount} 项
        </div>
        {risks.length > 0 && (
          <div className="gov-risks">
            <div className="gov-risks-title">
              <WarningOutlined /> 低健康指标 Top {risks.length}（按评分升序）
            </div>
            {risks.map((r) => (
              <button
                key={r.metric_id}
                type="button"
                className="gov-risk-item gov-risk-link"
                disabled={!r.metric_code}
                title={r.metric_code ? `查看 ${r.metric_name ?? r.metric_code} 详情` : "该指标无编码，无法直达详情"}
                onClick={(e) => {
                  e.stopPropagation();
                  if (r.metric_code) navigate(`/detail/${r.metric_code}`);
                }}
              >
                <span className="gov-risk-name" title={r.metric_code ?? undefined}>
                  {r.metric_name ?? r.metric_code ?? `指标 #${r.metric_id}`}
                </span>
                <span className="gov-risk-score">{r.score} 分</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </Card>
  );
}

// ---- Owner 责任分布：以堆积条形图展示每位 Owner 的指标状态构成 ----
// 分段颜色与生命周期信号条语义一致：草稿灰 / 实验紫 / 审核橙 / 已发布青 / 已废弃深灰
const OWNER_STATES = [
  { key: "DRAFT", label: "草稿", cls: "ob-draft" },
  { key: "EXPERIMENTAL", label: "灰度", cls: "ob-experimental" },
  { key: "REVIEW", label: "审核中", cls: "ob-review" },
  { key: "PUBLISHED", label: "已发布", cls: "ob-published" },
  { key: "DEPRECATED", label: "已废弃", cls: "ob-deprecated" },
] as const;

// Owner 名下各资产类型构成条（跨资产责任分布，比例可视化）——替代原图标计数块
// href 为该类资产的目录页路由（点击资产段 → 跳对应页面并带 owner_id 过滤）
const OWNER_ASSETS = [
  { key: "metrics", label: "指标", cls: "oc-metric", href: "/catalog" },
  { key: "tables", label: "数据表", cls: "oc-table", href: "/catalogs" },
  { key: "sources", label: "数据源", cls: "oc-source", href: "/data-sources" },
  { key: "dimensions", label: "维度", cls: "oc-dim", href: "/dimensions" },
  { key: "terms", label: "术语", cls: "oc-term", href: "/glossary" },
  { key: "templates", label: "模板", cls: "oc-tpl", href: "/templates" },
] as const;

// 跨资产待处理分类明细（方案 A）：指标待审核（REVIEW）+ 维度草稿（DRAFT）+ 术语草稿（DRAFT）。
// 模板/数据表/数据源无审核或草稿状态概念，不纳入待处理统计。
// href 为该类资产目录页路由，status 为下钻过滤状态（跳转时拼接 status + owner_id）。
const PENDING_BREAKDOWN = [
  { key: "metric", label: "指标待审核", cls: "oc-pending-metric", href: "/catalog", status: "REVIEW" },
  { key: "dim", label: "维度草稿", cls: "oc-pending-dim", href: "/dimensions", status: "DRAFT" },
  { key: "term", label: "术语草稿", cls: "oc-pending-term", href: "/glossary", status: "DRAFT" },
] as const;

// 兼容旧版后端 by_owner 结构：新版为 { total, by_status }，旧版 metrics 是对象但
// tables/sources/dimensions/terms/templates 是纯数字。API 升级有部署窗口期（后端镜像重建前
// 前端 HMR 已先上线新代码），生产上不能假设结构已升级——统一归一化为 { total, by_status }。
function normalizeOwnerStat(v: OwnerAssetStat): AssetStat {
  if (typeof v === "object" && v !== null && "total" in v) {
    return { total: v.total ?? 0, by_status: v.by_status ?? {} };
  }
  return { total: typeof v === "number" ? v : 0, by_status: {} };
}

// Owner 卡片头部「待处理 N」徽标：跨资产汇总（指标待审核+维度草稿+术语草稿），
// 点击弹 Popover 列出分类明细，每项精确跳转对应目录并携带 status + owner_id 过滤。
// 独立组件持有弹层开关状态；onClick 需 stopPropagation 防止冒泡到卡片触发 owner 下钻。
function OwnerHotBadge({
  ownerId,
  ownerName,
  counts,
  navigate,
}: {
  ownerId: string;
  ownerName: string;
  counts: Record<(typeof PENDING_BREAKDOWN)[number]["key"], number>;
  navigate: (to: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const pending = PENDING_BREAKDOWN.reduce((sum, p) => sum + (counts[p.key] ?? 0), 0);
  const items = PENDING_BREAKDOWN.map((p) => ({ ...p, count: counts[p.key] ?? 0 })).filter(
    (i) => i.count > 0,
  );
  const content = (
    <div className="oc-hot-pop">
      <div className="oc-hot-pop-title">{ownerName} 名下待处理资产</div>
      {items.map((p) => (
        <button
          key={p.key}
          type="button"
          className={`oc-hot-pop-item ${p.cls}`}
          onClick={(e) => {
            // portal 弹层的事件沿 React 组件树（fiber）向上冒泡，若不阻断会触发
            // 卡片 <button> 的 owner 下钻（navigate /catalog?owner_id=…）覆盖本次精确跳转
            e.stopPropagation();
            setOpen(false);
            navigate(`${p.href}?status=${p.status}&owner_id=${ownerId}`);
          }}
        >
          <i className="oc-hot-pop-dot" />
          {p.label} {p.count}
        </button>
      ))}
    </div>
  );
  return (
    <Popover
      open={open}
      onOpenChange={(v) => {
        if (!v) setOpen(false);
      }}
      trigger="click"
      placement="bottomRight"
      arrow={false}
      content={content}
      rootClassName="oc-hot-popover"
    >
      <span
        className="oc-hot"
        role="button"
        tabIndex={0}
        title={`${ownerName}：${pending} 项资产待处理（指标待审核 ${counts.metric ?? 0} / 维度草稿 ${counts.dim ?? 0} / 术语草稿 ${counts.term ?? 0}），点击查看分类明细`}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.stopPropagation();
            setOpen((v) => !v);
          }
        }}
      >
        待处理 {pending}
      </span>
    </Popover>
  );
}

function OwnerDistribution({ data, navigate }: { data: DashboardData; navigate: (to: string) => void }) {
  const owners = Object.entries(data.by_owner ?? {}).sort((a, b) => b[1].total - a[1].total);
  if (owners.length === 0) return null;
  return (
    <Card
      style={{ marginBottom: 20 }}
      styles={{ body: { paddingTop: 16, paddingBottom: 16 } }}
      title={
        <span style={{ fontSize: 15, fontWeight: 600 }}>
          Owner 责任分布
          <span className="muted" style={{ fontWeight: 400, fontSize: 12, marginLeft: 8 }}>
            跨资产构成（指标/数据表/数据源/维度/术语/模板），点击资产段直达对应目录、点卡片查看其指标目录
          </span>
        </span>
      }
    >
      <div className="owner-grid">
        {owners.map(([id, o]) => {
          // 各资产先归一化（兼容旧版纯数字结构），再取状态分布——直接读 .by_status
          // 会在旧结构数字上取属性 → undefined → 运行时崩溃
          const assetStats = OWNER_ASSETS.reduce(
            (acc, a) => {
              acc[a.key] = normalizeOwnerStat(o[a.key]);
              return acc;
            },
            {} as Record<(typeof OWNER_ASSETS)[number]["key"], AssetStat>,
          );
          // 跨资产待处理：指标待审核（REVIEW）+ 维度草稿（DRAFT）+ 术语草稿（DRAFT）
          const pendingCounts = {
            metric: assetStats.metrics.by_status.REVIEW ?? 0,
            dim: assetStats.dimensions.by_status.DRAFT ?? 0,
            term: assetStats.terms.by_status.DRAFT ?? 0,
          };
          const pending = pendingCounts.metric + pendingCounts.dim + pendingCounts.term;
          const hot = pending > 0;
// 资产构成：完整渲染 6 类（指标/数据表/数据源/维度/术语/模板）——即使 count=0 也保留为窄灰段，
// 让 Owner 一眼看清全维度资产分布（0 值段也能看到，确认该责任人确实没有此类资产）
const mix = OWNER_ASSETS.map((a) => ({
            ...a,
            count: assetStats[a.key].total,
          }));
          const total = Math.max(o.total, 1);
          const initials = (o.name || "?").slice(0, 2);
          return (
            <button
              key={id}
              type="button"
              className={`owner-card${hot ? " owner-hot" : ""}`}
              onClick={() => navigate(`/catalog?owner_id=${id}`)}
              title={`${o.name}：共 ${o.total} 项资产，待处理 ${pending}。点击查看其指标目录`}
            >
              <span className="oc-head">
                <span className="oc-avatar">{initials}</span>
                <span className="oc-name">{o.name}</span>
                {hot && (
                  <OwnerHotBadge
                    ownerId={id}
                    ownerName={o.name}
                    counts={pendingCounts}
                    navigate={navigate}
                  />
                )}
                <span className="oc-total">共 {o.total} 项</span>
              </span>
              <span className="oc-bar" role="img" aria-label={`${o.name} 资产构成`}>
                {mix.map((m) => {
                  const isZero = m.count === 0;
                  // 0 值段：最小占位宽度 1.5%（让 6 类完整可见，但不抢视觉权重）
                  const w = isZero ? 1.5 : (m.count / total) * 100;
                  return (
                    <span
                      key={m.key}
className={`oc-seg ${m.cls}${isZero ? " oc-zero" : ""}`}
                    style={{ width: `${w.toFixed(2)}%` }}
                    title={isZero ? `${m.label} ${m.count}（该责任人名下暂无此类资产）` : `${m.label} ${m.count}，点击查看该责任人名下${m.label}`}
                    role="button"
                    tabIndex={0}
                    onClick={(e) => {
                      e.stopPropagation();
                      navigate(`${m.href}?owner_id=${id}`);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.stopPropagation();
                        navigate(`${m.href}?owner_id=${id}`);
                      }
                    }}
                  >
                    {isZero ? "0" : `${m.label} ${m.count}`}
                  </span>
                  );
                })}
              </span>
              <span className="oc-legend">
                {mix.map((m) => {
                  const isZero = m.count === 0;
                  return (
                    <span
                      key={m.key}
                      className={`oc-chip ${m.cls}${isZero ? " oc-chip-zero" : ""}`}
                      title={isZero ? `${m.label} ${m.count}（该责任人名下暂无此类资产）` : `${m.label} ${m.count}，点击查看该责任人名下${m.label}`}
                      role="button"
                      tabIndex={0}
                      onClick={(e) => {
                        e.stopPropagation();
                        navigate(`${m.href}?owner_id=${id}`);
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.stopPropagation();
                          navigate(`${m.href}?owner_id=${id}`);
                        }
                      }}
                    >
                      <i className="oc-chip-dot" />
                      {m.label} {m.count}
                    </span>
                  );
                })}
              </span>
              <span className="oc-life">
                {OWNER_STATES.map((s) => {
                  const count = assetStats.metrics.by_status[s.key] ?? 0;
                  if (count <= 0) return null;
                  return (
                    <span
                      key={s.key}
                      className="oc-life-item"
                      title={`${s.label} ${count}，点击查看该责任人名下${s.label}指标`}
                      role="button"
                      tabIndex={0}
                      onClick={(e) => {
                        e.stopPropagation();
                        navigate(`/catalog?status=${s.key}&owner_id=${id}`);
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.stopPropagation();
                          navigate(`/catalog?status=${s.key}&owner_id=${id}`);
                        }
                      }}
                    >
                      <i className={`oc-dot ${s.cls}`} />
                      {s.label} {count}
                    </span>
                  );
                })}
              </span>
            </button>
          );
        })}
      </div>
    </Card>
  );
}

export function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [recommended, setRecommended] = useState<RecommendItem[]>([]);
  const [terms, setTerms] = useState<GlossaryTerm[]>([]);
  // 指标可信度：复用可观测中心聚合端点（/observability/overview quality.metric_health）
  const [metricHealth, setMetricHealth] = useState<MetricHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // 业务域编码 → 中文显示名映射（拉取主题域树扁平化；失败静默回退显示编码）
  const [domainNameMap, setDomainNameMap] = useState<Record<string, string>>({});
  const { track } = useTracking();
  const navigate = useNavigate();

  // 推荐曝光上报：仅对首次进入列表的推荐项上报 recommend_view；
  // 负反馈移除后该指标不再出现在列表中，后续渲染不会补报。
  const reportedViewRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    const reported = reportedViewRef.current;
    for (const r of recommended) {
      if (!reported.has(r.metric_id)) {
        reported.add(r.metric_id);
        track("recommend_view", r.metric_id, "metric");
      }
    }
  }, [recommended, track]);

  function handleDismiss(metricId: string) {
    track("recommend_dismiss", metricId, "metric");
    setRecommended((prev) => prev.filter((r) => r.metric_id !== metricId));
  }

  // 查看更多推荐：拉取更多并去重合并（保留已有 reason/via 展示）。
  // 注意：候选集可能很小（如库中 PUBLISHED 指标本身少于 limit），去重后无新增即
  // 判定"已展示全部"，避免按钮永远显示「查看更多」却点了没反应。
  const [expandingRec, setExpandingRec] = useState(false);
  const [allRecLoaded, setAllRecLoaded] = useState(false);
  async function handleExpandRecommend() {
    if (expandingRec || allRecLoaded) return;
    setExpandingRec(true);
    try {
      const more = await fetchRecommendedMetrics(RECOMMEND_EXPAND_LIMIT);
      setRecommended((prev) => {
        const seen = new Set(prev.map((r) => r.metric_id));
        const fresh = more.filter((r) => !seen.has(r.metric_id));
        if (fresh.length === 0) {
          // 已到底：无新增候选，明确告知用户，避免"点了没反应"
          setAllRecLoaded(true);
          message.info("已展示全部推荐指标");
          return prev;
        }
        return [...prev, ...fresh];
      });
    } catch (e) {
      console.warn("[Dashboard] 展开推荐指标失败", e);
    } finally {
      setExpandingRec(false);
    }
  }

  useEffect(() => {
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const [dash, rec, recTerms, overview] = await Promise.all([
          fetchDashboard(),
          fetchRecommendedMetrics(RECOMMEND_INITIAL_LIMIT).catch((e) => {
            console.warn("[Dashboard] 推荐指标加载失败", e);
            return [];
          }),
          fetchRecommendedTerms(5).catch(() => []),
          // 指标可信度独立拉取可观测聚合，失败静默降级（卡片隐藏），不阻断仪表盘其余读数
          fetchObsOverview()
            .then((o) => o?.quality?.metric_health ?? null)
            .catch((e) => {
              console.warn("[Dashboard] 指标可信度加载失败", e);
              return null;
            }),
        ]);
        setData(dash);
        setRecommended(rec);
        setTerms(recTerms);
        setMetricHealth(overview);
        track("dashboard_view", undefined, "dashboard");
      } catch (err) {
        setError(err instanceof Error ? err.message : "加载仪表盘失败");
      } finally {
        setLoading(false);
      }
    }
    load();
  }, [track]);

  // 拉取主题域树 → code→name 映射（域编码中文化展示用）；失败静默回退英文编码
  useEffect(() => {
    let alive = true;
    listDomainTree()
      .then((nodes) => {
        if (!alive) return;
        const map: Record<string, string> = {};
        const walk = (list: SubjectDomainTreeNode[]) => {
          for (const n of list) {
            map[n.code] = n.name;
            if (n.children?.length) walk(n.children);
          }
        };
        walk(nodes);
        setDomainNameMap(map);
      })
      .catch(() => {
        // 域列表不可用时保留编码展示，不阻断仪表盘其余数据
      });
    return () => {
      alive = false;
    };
  }, []);

  if (loading) {
    return (
      <div style={{ textAlign: "center", padding: 64 }}>
        <Spin size="large" tip="正在校准仪表盘…" />
      </div>
    );
  }

  if (error) return <Alert type="error" message="加载失败" description={error} showIcon />;
  if (!data) return null;

  const reviewCount = data.by_status.REVIEW ?? 0;
  const piiRatio = Math.round(data.pii_ratio * 100);
  const domainCount = Object.keys(data.by_domain ?? {}).length;
  const assetTypeCount = Object.keys(data.assets ?? {}).length;

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Workspace / Overview</div>
          <h2>总览仪表</h2>
          <p>全资产与治理状态的实时读数——点击生命周期站点或资产卡片可下钻目录。</p>
        </div>
        <Tag color={piiRatio > 30 ? "error" : piiRatio > 10 ? "warning" : "success"} style={{ margin: 0 }}>
          PII 占比 {piiRatio}%
        </Tag>
      </div>

      {/* 签名元素：生命周期信号条 */}
      <Card
        style={{ marginBottom: 20 }}
        styles={{ body: { paddingTop: 20, paddingBottom: 12 } }}
        title={
          <span style={{ fontSize: 15, fontWeight: 600 }}>
            生命周期信号条
            <span className="muted" style={{ fontWeight: 400, fontSize: 12, marginLeft: 8 }}>
              琥珀 = 当前最需要关注的站点
            </span>
          </span>
        }
      >
        <LifecycleSignalBar data={data} />
      </Card>

      {/* 治理指标体系：质量健康 / 合规复核 / 冲突风险 / 近 30 天更新 */}
      <GovernanceCards data={data} navigate={navigate} />

      {/* 指标可信度：健康度四档分布（红黄绿）+ 覆盖率 + 低健康 Top（复用可观测中心聚合） */}
      {metricHealth ? <MetricCredibilityCard h={metricHealth} navigate={navigate} /> : null}

      {/* Owner 责任分布：按责任人查看指标规模与待审积压 */}
      <OwnerDistribution data={data} navigate={navigate} />

      {/* 资产总览：全资产计数 + 状态下钻（与生命周期信号条一致的交互） */}
      <Card
        style={{ marginBottom: 20 }}
        styles={{ body: { paddingTop: 16, paddingBottom: 16 } }}
        title={
          <span style={{ fontSize: 15, fontWeight: 600 }}>
            资产总览
            <span className="muted" style={{ fontWeight: 400, fontSize: 12, marginLeft: 8 }}>
              点击资产名进入目录，点击状态段带状态下钻
            </span>
          </span>
        }
      >
        <div className="asset-grid">
          {ASSET_CONFIGS.map((cfg) => (
            <AssetCard key={cfg.key} config={cfg} stat={data.assets?.[cfg.key]} navigate={navigate} />
          ))}
        </div>
      </Card>

      {/* KPI 读数格：与信号条去重后的汇总指标（不再重复已发布/待审核/草稿中） */}
      <div className="gauge-grid" style={{ marginBottom: 20 }}>
        <GaugeCell label="指标总数" value={data.total} accent="data" sub={`覆盖 ${domainCount} 个业务域`} />
        <GaugeCell label="覆盖业务域" value={domainCount} accent="data" sub="已接入指标的业务域数" />
        <GaugeCell
          label="PII 占比"
          value={`${piiRatio}%`}
          accent={piiRatio > 30 ? "danger" : piiRatio > 10 ? "warn" : "ok"}
          sub={`${data.pii_count} 个指标含 PII`}
          small
        />
        <GaugeCell label="资产类型" value={assetTypeCount} accent="data" sub="总览覆盖的资产种类" />
      </div>

      {/* 告警带 */}
      {(reviewCount > 0 || piiRatio > 30) && (
        <Alert
          type={reviewCount > 0 ? "warning" : "error"}
          showIcon
          style={{ marginBottom: 20 }}
          message={
            <span>
              {reviewCount > 0 && (
                <a onClick={() => navigate("/metrics/review")} style={{ marginRight: 16 }}>
                  {reviewCount} 个指标待审核 →
                </a>
              )}
              {piiRatio > 30 && "PII 指标占比超过 30%，请复核数据合规策略"}
            </span>
          }
        />
      )}

      {/* 图表区 */}
      <Row gutter={[20, 20]} style={{ marginBottom: 20 }}>
        <Col xs={24} lg={12}>
          <Card title="业务域分布" styles={{ body: { paddingTop: 8 } }}>
            <DomainChart byDomain={data.by_domain ?? {}} nameMap={domainNameMap} />
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card title="指标分级（T1/T2/T3）" styles={{ body: { paddingTop: 8 } }}>
            <TierBar byTier={data.by_tier ?? {}} />
          </Card>
        </Col>
      </Row>

      {/* 快速入口 */}
      <Card title="快捷入口" style={{ marginBottom: 20 }}>
        <Row gutter={[16, 16]}>
          {QUICK_ENTRIES.map((q) => (
            <Col xs={12} md={8} key={q.to}>
              <button className="quick-entry" onClick={() => navigate(q.to)} style={{ width: "100%" }}>
                <div className="qe-icon">{q.icon}</div>
                <div className="qe-title">{q.title}</div>
                <div className="qe-desc">{q.desc}</div>
              </button>
            </Col>
          ))}
        </Row>
      </Card>

      {/* 推荐流：协同过滤 + 血缘兜底 / 推荐术语 */}
      <Row gutter={[20, 20]}>
        <Col xs={24} lg={12}>
          <Card
            title="为你推荐指标"
            styles={{ body: { maxHeight: 320, overflow: "auto" } }}
            extra={<a onClick={() => navigate("/catalog", { state: { from: "dashboard" } })}>去目录</a>}
          >
            {recommended.length === 0 ? (
              <Empty description="暂无推荐（去指标目录逛逛，很快就有专属推荐）" />
            ) : (
              recommended.map((r) => (
                <div
                  key={r.metric_id}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    gap: 12,
                    padding: "10px 0",
                    borderBottom: "1px solid var(--line-soft)",
                  }}
                >
                  <div
                    style={{
                      flex: 1,
                      minWidth: 0,
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      gap: 12,
                      cursor: "pointer",
                    }}
                    onClick={() => {
                      track("recommend_click", r.metric_id, "metric");
                      navigate(`/detail/${r.metric_id}`, { state: { from: "dashboard" } });
                    }}
                  >
                    <span className="mono" style={{ fontWeight: 600 }}>{r.metric_id}</span>
                    <span className="muted" style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {r.reason
                        ? r.reason
                        : r.via === "collaborative_filtering"
                          ? "协同过滤"
                          : `血缘 · ${EDGE_TYPE_LABEL[r.edge_type] ?? r.edge_type}`}
                      {typeof r.score === "number" && ` · ${(r.score * 100).toFixed(0)}%`}
                    </span>
                  </div>
                  <Popconfirm
                    title="不再推荐该指标？"
                    description="我们将减少此类推荐"
                    okText="不再推荐"
                    cancelText="取消"
                    onConfirm={() => handleDismiss(r.metric_id)}
                  >
                    <Button size="small" type="text" style={{ flexShrink: 0 }}>
                      不感兴趣
                    </Button>
                  </Popconfirm>
                </div>
              ))
            )}
            {recommended.length > 0 && (
              <div style={{ textAlign: "center", paddingTop: 10 }}>
                <Button
                  type="link"
                  size="small"
                  loading={expandingRec}
                  disabled={allRecLoaded}
                  onClick={handleExpandRecommend}
                >
                  {allRecLoaded || recommended.length >= RECOMMEND_EXPAND_LIMIT
                    ? "已展示全部推荐"
                    : "查看更多推荐"}
                </Button>
              </div>
            )}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card
            title="推荐术语"
            styles={{ body: { maxHeight: 320, overflow: "auto" } }}
            extra={<a onClick={() => navigate("/glossary")}>术语表</a>}
          >
            {terms.length === 0 ? (
              <Empty description="暂无推荐术语" />
            ) : (
              terms.map((t) => (
                <div
                  key={t.term_code}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: 12,
                    alignItems: "baseline",
                    padding: "10px 0",
                    borderBottom: "1px solid var(--line-soft)",
                    cursor: "pointer",
                  }}
                  onClick={() => navigate(`/glossary?focus=${t.term_code}`)}
                >
                  <span style={{ fontWeight: 600 }}>{t.name}</span>
                  <span className="muted" style={{ fontSize: 12, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {t.definition}
                  </span>
                  <Tag>{domainNameMap[t.domain] ?? t.domain}</Tag>
                </div>
              ))
            )}
          </Card>
        </Col>
      </Row>

      <div style={{ height: 8 }} />
      <div className="muted" style={{ fontSize: 12, display: "flex", gap: 20 }}>
        <span><AppstoreOutlined /> 总览数据由后端聚合仪表接口实时返回</span>
        <span>推荐基于协同过滤与血缘关系</span>
      </div>
    </div>
  );
}
