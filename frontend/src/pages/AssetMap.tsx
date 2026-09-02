import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Alert,
  Avatar,
  Button,
  Card,
  Descriptions,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Col,
  Radio,
  Segmented,
  Select,
  Space,
  Spin,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  ApartmentOutlined,
  CheckOutlined,
  CloseOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  EyeOutlined,
  FileTextOutlined,
  GlobalOutlined,
  HeartOutlined,
  HeatMapOutlined,
  SafetyOutlined,
  SearchOutlined,
  SettingOutlined,
  TableOutlined,
  ThunderboltOutlined,
  UserOutlined,
  ArrowLeftOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { Bar, Pie } from "@ant-design/charts";
import {
  UnisenseApiError,
  applyAssetPiiTemplate,
  assignAssetOwner,
  batchAssignAssetOwner,
  batchReclassifyAssetSensitivity,
  bulkDeprecateCatalogs,
  downloadAssetExport,
  downloadPiiExport,
  fetchAssetChanges,
  fetchAssetEntityDetail,
  fetchAssetGraph,
  fetchAssetHealth,
  fetchAssetHeatmapMatrix,
  fetchAssetMetricDimensions,
  fetchAssetMetricSummary,
  fetchAssetMyAssets,
  fetchAssetOrphans,
  fetchAssetOwnerView,
  fetchAssetPiiAssets,
  fetchAssetPiiOverview,
  fetchAssetPiiTemplates,
  fetchAssetSearch,
  reportClassificationFalsePositive,
  fetchAssetSummary,
  fetchAssetTables,
  fetchCurrentUser,
  getMetric,
  inferColumnDescription,
  inferDescriptions,
  inferMetricDescription,
  inferTableDescription,
  listCatalogs,
  listCatalogDatabases,
  listDataSources,
  listDomainTree,
  listMetrics,
  listSnapshots,
  listUsers,
  overrideAssetPiiField,
  queryMetricInternal,
  reclassifyAssetSensitivity,
  removeAssetPiiOverride,
  reviewAssetEntity,
  setAssetMaskingPolicy,
  setAssetRetention,
  listDictItems,
  updateColumnDescription,
  updateMetricDescription,
  updateTableDescription,
} from "../api";
import { usePermission } from "../hooks/usePermission";
import type {
  AssetCatalogSummary,
  AssetChangeItem,
  AssetChangeMetric,
  AssetChanges,
  AssetDriftItem,
  AssetEntityDetail,
  AssetHealthSummary,
  AssetHeatmapMatrix,
  AssetMetricSummary,
  AssetMetricDimensionSummary,
  AssetMyAssets,
  AssetOwnerView,
  AssetPiiAssetItem,
  AssetPiiField,
  AssetPiiOverview,
  AssetPiiTemplate,
  AssetSearchItem,
  AssetTableItem,
  MetricResponse,
  SchemaColumn,
  SnapshotResponse,
  SubjectDomainTreeNode,
} from "../types";
import { useTracking } from "../hooks/useTracking";
import { PAGE_SIZE_OPTIONS, usePersistentPageSize } from "../hooks/usePersistentPageSize";
import { SchemaTable } from "../components/SchemaTable";
import { ENTITY_TYPE_LABEL, LINEAGE_EDGE_TYPE_LABEL, METRIC_RELATION_EDGE_LABEL, SOURCE_HEALTH_LABEL, UNIT_LABEL } from "../utils/enums";
import { formatCnTime } from "../utils/timeCn";
import { useUserNames } from "../utils/userNames";
import { AssetGraph } from "../components/assetmap/AssetGraph";
import type { AssetGraphNode, AssetGraphEdge } from "../components/assetmap/AssetGraph";
import { DescriptionCoveragePanel } from "../components/DescriptionCoveragePanel";
import { DrillDownDrawer } from "../components/assetmap/DrillDownDrawer";
import { ResizableDrawer } from "../components/ResizableDrawer";

const SENSITIVITY_LABEL: Record<string, string> = {
  PUBLIC: "公开",
  INTERNAL: "内部",
  CONFIDENTIAL: "机密",
  PII: "PII",
  NEEDS_REVIEW: "待复核",
  UNKNOWN: "未知",
};

const SENSITIVITY_COLOR: Record<string, string> = {
  PUBLIC: "default",
  INTERNAL: "blue",
  CONFIDENTIAL: "orange",
  PII: "red",
  NEEDS_REVIEW: "gold",
  UNKNOWN: "default",
};

// PII 字段类别中文标签（PII 合规增强：字段级明细/类别分布展示）
const PII_CATEGORY_LABEL: Record<string, string> = {
  ID_CARD: "身份证",
  PHONE: "手机/电话",
  EMAIL: "邮箱",
  NAME: "姓名",
  ADDRESS: "地址",
  BANK_CARD: "银行卡",
  DOCUMENT: "证件号",
  PASSPORT: "护照",
  GPS: "定位",
  HEALTH: "健康医疗",
  BIOMETRIC: "生物特征",
  FINANCIAL: "金融敏感",
  MANUAL: "人工确认",
};

const PII_CATEGORY_COLOR: Record<string, string> = {
  ID_CARD: "red",
  PHONE: "volcano",
  EMAIL: "orange",
  NAME: "gold",
  ADDRESS: "lime",
  BANK_CARD: "magenta",
  DOCUMENT: "purple",
  PASSPORT: "geekblue",
  GPS: "cyan",
  HEALTH: "green",
  BIOMETRIC: "blue",
  FINANCIAL: "magenta",
  MANUAL: "default",
};

const MASKING_POLICY_LABEL: Record<string, string> = {
  none: "不脱敏",
  mask: "打码",
  hash: "哈希",
  deny: "拒绝",
};

const REVIEW_STATUS_OPTIONS = [
  { value: "unreviewed", label: "仅待复核" },
  { value: "reviewed", label: "仅已复核" },
];

// 合规法律依据：优先从字典接口拉取（legal_basis，2026-08-28 字典化——受控词表
// 调整时前端无需发版）；拉取失败/空时回退以下硬编码兜底。
const FALLBACK_LEGAL_BASIS_OPTIONS = [
  { value: "user_consent", label: "用户同意" },
  { value: "contract", label: "合同必需" },
  { value: "law", label: "法定职责" },
  { value: "legitimate_interest", label: "正当利益" },
  { value: "public_interest", label: "公共利益" },
];

/** 业务域树扁平化：把 SubjectDomainTreeNode 递归展开为 {label, value} 选择项。 */
function flattenDomainTree(
  nodes: SubjectDomainTreeNode[],
): Array<{ label: string; value: string }> {
  const out: Array<{ label: string; value: string }> = [];
  const walk = (list: SubjectDomainTreeNode[]) => {
    for (const n of list) {
      out.push({ label: n.name || n.code, value: n.code });
      if (n.children?.length) walk(n.children);
    }
  };
  walk(nodes);
  return out;
}

/** 单实体废弃：复用批量废弃接口传单项（资产地图「单实体下线」治理入口）。
 *  成功返回 true；失败抛错由调用方 message 展示。 */
async function deprecateEntity(
  sourceId: string,
  entityName: string,
): Promise<boolean> {
  const res = await bulkDeprecateCatalogs([{ source_id: sourceId, entity_name: entityName }]);
  if (res.failed && res.failed.length > 0) {
    throw new Error((res.failed[0] as Record<string, unknown>).reason as string || "废弃失败");
  }
  return true;
}

// 图谱来源视角选项（方案A：资产地图聚焦「资产盘点」，血缘深挖引导去血缘视图）
// 资产视角=采集目录表+指标（指标为中心 depth 收敛）；血缘视角（完整表级血缘）已在血缘视图承接，
// 不再在此重复提供通道切换，避免与血缘视图同源图造成"重复"困惑。
const GRAPH_SOURCE_OPTIONS = [{ value: "asset", label: "资产视角（采集目录）" }];

// ---- 指标体系维度标签映射（概览「指标体系」区块）----
const METRIC_TYPE_LABEL: Record<string, string> = {
  atomic: "原子指标",
  derived: "派生指标",
  composite: "复合指标",
};
const DW_LAYER_LABEL: Record<string, string> = {
  ODS: "ODS",
  DWD: "DWD",
  DWS: "DWS",
  ADS: "ADS",
  DM: "DM",
};
const METRIC_TIER_LABEL: Record<string, string> = {
  T1: "T1",
  T2: "T2",
  T3: "T3",
};
const AGG_LABEL: Record<string, string> = {
  SUM: "求和",
  AVG: "均值",
  COUNT: "计数",
  COUNT_DISTINCT: "去重计数",
  LAST_VALUE: "末值",
  MAX: "最大值",
  MIN: "最小值",
  MEDIAN: "中位数",
  PERCENTILE: "分位",
};
const TIME_SEM_LABEL: Record<string, string> = {
  PERIOD: "周期",
  YTD: "年初至今",
  TTM: "滚动12月",
  AVG: "平均",
  MOM: "环比",
  YOY: "同比",
};
const STATUS_LABEL: Record<string, string> = {
  DRAFT: "草稿",
  REVIEW: "评审中",
  PUBLISHED: "已发布",
  EXPERIMENTAL: "灰度",
  DEPRECATED: "已废弃",
  DATA_SOURCE_DROPPED: "源下线",
};
const GRANULARITY_LABEL: Record<string, string> = {
  second: "秒",
  minute: "分钟",
  hour: "小时",
  day: "日",
  week: "周",
  month: "月",
  quarter: "季度",
  year: "年",
  // 兼容大写别名（存量数据可能大写）
  SECOND: "秒",
  MINUTE: "分钟",
  HOUR: "小时",
  DAY: "日",
  WEEK: "周",
  MONTH: "月",
  QUARTER: "季度",
  YEAR: "年",
};
const CURRENCY_LABEL: Record<string, string> = {
  CNY: "人民币",
  USD: "美元",
  EUR: "欧元",
  HKD: "港币",
  JPY: "日元",
};
const FRESHNESS_LABEL: Record<string, string> = {
  REALTIME: "实时",
  T0: "T+0",
  T1: "T+1",
  HOURLY: "小时级",
};
const SERVING_MODE_LABEL: Record<string, string> = {
  BATCH_ONLY: "仅批处理",
  REALTIME_ONLY: "仅实时",
  BATCH_REALTIME_DUAL: "批实时双轨",
};
const ADDITIVITY_LABEL: Record<string, string> = {
  ADDITIVE: "可加",
  SEMI_ADDITIVE: "半可加",
  NON_ADDITIVE: "不可加",
};
// 指标体系 13 类维度配置（key 对应 metric_dimensions 响应字段）
const METRIC_DIMENSION_GROUPS: Array<{
  key: keyof AssetMetricDimensionSummary;
  label: string;
  map: Record<string, string>;
}> = [
  { key: "by_type", label: "指标类型", map: METRIC_TYPE_LABEL },
  { key: "by_granularity", label: "粒度", map: GRANULARITY_LABEL },
  { key: "by_dw_layer", label: "数仓分层", map: DW_LAYER_LABEL },
  { key: "by_metric_tier", label: "指标分级", map: METRIC_TIER_LABEL },
  { key: "by_unit", label: "单位", map: UNIT_LABEL },
  { key: "by_currency", label: "币种", map: CURRENCY_LABEL },
  { key: "by_aggregation", label: "聚合方式", map: AGG_LABEL },
  { key: "by_time_semantics", label: "时间语义", map: TIME_SEM_LABEL },
  { key: "by_freshness", label: "新鲜度", map: FRESHNESS_LABEL },
  { key: "by_serving_mode", label: "服务模式", map: SERVING_MODE_LABEL },
  { key: "by_additivity", label: "可加性", map: ADDITIVITY_LABEL },
  { key: "by_status", label: "指标状态", map: STATUS_LABEL },
  { key: "by_domain", label: "业务域", map: {} },
];

// 维度值中文映射（未知值原样显示）
function dimLabel(map: Record<string, string>, key: string): string {
  if (key === "__null__") return "未设置";
  return map[key] ?? key;
}

// ---- 资产健康（HealthTab）：评分分档 + 9 项体检 ----
const HEALTH_LEVEL_META: Record<string, { label: string; color: string }> = {
  excellent: { label: "优", color: "green" },
  good: { label: "良", color: "blue" },
  fair: { label: "中", color: "orange" },
  poor: { label: "差", color: "red" },
};
const HEALTH_CHECK_LABEL: Record<string, string> = {
  unhealthy_sources: "不健康数据源",
  schema_incomplete: "Schema 不完整",
  orphan_assets: "孤儿资产",
  stale_assets: "陈旧资产",
  tables_missing_desc: "表描述缺失",
  fields_missing_desc: "字段描述缺失",
  pii_unreviewed: "PII 未复核",
  metrics_without_snapshot: "无快照指标",
  deprecated_without_successor: "废弃未替换指标",
};
// 变更类型（ChangesTab）
const CHANGE_TYPE_META: Record<string, { label: string; color: string }> = {
  created: { label: "新增", color: "green" },
  updated: { label: "更新", color: "blue" },
  deprecated: { label: "废弃", color: "red" },
};

// 用户角色中文映射（对齐 Layout/UserManagement 惯例）
const ROLE_LABEL: Record<string, string> = {
  platform_admin: "平台管理员",
  domain_admin: "域管理员",
  metric_owner: "指标负责人",
  reviewer: "评审员",
  compliance_officer: "合规官",
  analyst: "分析师",
  viewer: "只读用户",
};

// 敏感度渲染：历史后端 to_dict 曾剥离该字段，值可能为 null/undefined，此处做防御；
// 值含 "PII" 一律标红（无论是否精确匹配枚举）。
function sensitivityTag(s: string | null | undefined) {
  if (!s) return <Tag>未知</Tag>;
  const color = s.includes("PII") ? "red" : SENSITIVITY_COLOR[s];
  return <Tag color={color}>{SENSITIVITY_LABEL[s] ?? s}</Tag>;
}

function renderSchemaSummary(summary: SchemaColumn[] | string | null | undefined) {
  if (summary == null || summary === "") return <span className="muted">-</span>;
  if (typeof summary === "string") return <span>{summary}</span>;
  if (Array.isArray(summary)) return <SchemaTable columns={summary} editable={false} />;
  return <span className="muted">-</span>;
}

/**
 * 把实体详情的血缘边明细（source/target 为 table:/metric:/field: 前缀节点 id）转为
 * AssetGraph 可直接消费的图数据。节点按前缀推断类型（默认表），label 去前缀展示。
 */
function buildEntityLineageGraph(
  edges: NonNullable<AssetEntityDetail["lineage_edges"]>,
): { nodes: AssetGraphNode[]; edges: AssetGraphEdge[] } {
  const nodeMap = new Map<string, AssetGraphNode>();
  const graphEdges: AssetGraphEdge[] = [];
  for (const e of edges) {
    for (const id of [e.source, e.target]) {
      if (!nodeMap.has(id)) {
        const type = id.startsWith("metric:")
          ? "metric"
          : id.startsWith("field:")
            ? "field"
            : id.startsWith("dimension:")
              ? "dimension"
              : id.startsWith("consumer:")
                ? "consumer"
                : "table";
        nodeMap.set(id, { id, type, label: id.replace(/^[a-z]+:/, "") });
      }
    }
    graphEdges.push({ source: e.source, target: e.target, type: e.edge_type || "lineage" });
  }
  return { nodes: [...nodeMap.values()], edges: graphEdges };
}

/**
 * 实体血缘关系卡片：默认展示血缘图谱（G6 图，直观呈现表→指标流向），
 * 可切换「边明细列表」查看文字表格（源/目标/类型/粒度）。供各详情抽屉统一复用，
 * 替代早先仅表格的「血缘边明细」，让血缘关系一目了然。
 */
function EntityLineageCard({ edges }: { edges: NonNullable<AssetEntityDetail["lineage_edges"]> }) {
  const g = useMemo(() => buildEntityLineageGraph(edges), [edges]);
  return (
    <Card size="small" title={`血缘关系（${edges.length} 条边）`} style={{ marginTop: 16 }}>
      <Tabs
        items={[
          {
            key: "graph",
            label: "血缘图谱",
            children:
              g.nodes.length > 0 ? (
                <AssetGraph
                  nodes={g.nodes}
                  edges={g.edges}
                  height={360}
                  showFields
                  fullscreenable={false}
                />
              ) : (
                <Empty description="无血缘图数据" />
              ),
          },
          {
            key: "list",
            label: "边明细列表",
            children: (
              <Table
                dataSource={edges}
                rowKey={(e, i) => `${e.source}-${e.target}-${i}`}
                size="small"
                pagination={false}
                columns={[
                  { title: "源", dataIndex: "source", ellipsis: true },
                  { title: "目标", dataIndex: "target", ellipsis: true },
                  { title: "类型", dataIndex: "edge_type", width: 120, render: (v: string) => LINEAGE_EDGE_TYPE_LABEL[v] ?? v ?? "-" },
                  { title: "粒度", dataIndex: "granularity", width: 80 },
                ]}
              />
            ),
          },
        ]}
      />
    </Card>
  );
}

const DESCRIPTION_SOURCE_TAG: Record<string, { label: string; color: string }> = {
  manual: { label: "人工编辑", color: "blue" },
  llm: { label: "LLM 推断", color: "purple" },
  schema: { label: "采集原始", color: "default" },
};

function descriptionSourceTag(source?: string | null) {
  if (!source) return null;
  const cfg = DESCRIPTION_SOURCE_TAG[source];
  if (!cfg) return <Tag>{source}</Tag>;
  return <Tag color={cfg.color}>{cfg.label}</Tag>;
}

/**
 * LLM 推断 in-flight 去重：进行中的推断完成后才移除，拦截「退出再进」/重复点击的重复调用。
 * 该 Map 挂在模块级（跨组件实例共享），后端另有 Redis/进程内幂等兜底（409）。
 */
const inferInflight = new Map<string, Promise<unknown>>();

/** 若 key 对应的推断已在途中则返回 null（拦截）；否则执行并登记，完成时清理。 */
function runInflight<T>(key: string, task: () => Promise<T>): Promise<T> | null {
  if (inferInflight.has(key)) return null;
  const p = task().finally(() => inferInflight.delete(key));
  inferInflight.set(key, p);
  return p;
}

// 指标口径明细：definition_json 结构化解构（SQL/表达式 + 度量 + 维度 + 源表 + 周期）
function DefinitionsDetail({ def }: { def: Record<string, unknown> }) {
  const sql = typeof def?.sql === "string" ? def.sql : undefined;
  const expression = typeof def?.expression === "string" ? def.expression : undefined;
  const period = typeof def?.period === "string" ? def.period : undefined;
  const measureColumn = typeof def?.measure_column === "string" ? def.measure_column : undefined;
  const measures = Array.isArray(def?.measures)
    ? (def.measures as Array<{ name?: string; aggregation?: string }>)
    : [];
  const dimensions = Array.isArray(def?.dimensions) ? (def.dimensions as string[]) : [];
  const sourceTables = Array.isArray(def?.source_tables) ? (def.source_tables as string[]) : [];
  const sourceTable = typeof def?.source_table === "string" ? def.source_table : undefined;
  const primaryMeasure = measures.length
    ? `${measures[0].aggregation ?? ""}(${measures[0].name ?? ""})`.trim()
    : null;

  return (
    <>
      {sql ? (
        <pre
          style={{
            background: "#f6f8fa",
            padding: 10,
            borderRadius: 6,
            fontSize: 12,
            overflowX: "auto",
            marginBottom: 8,
          }}
        >
          {sql}
        </pre>
      ) : expression ? (
        <Descriptions column={1} size="small" bordered style={{ marginBottom: 8 }}>
          <Descriptions.Item label="表达式">{expression}</Descriptions.Item>
        </Descriptions>
      ) : null}
      <Descriptions column={2} size="small" bordered>
        {primaryMeasure ? (
          <Descriptions.Item label="主度量">{primaryMeasure}</Descriptions.Item>
        ) : null}
        {measureColumn ? (
          <Descriptions.Item label="度量列">{measureColumn}</Descriptions.Item>
        ) : null}
        <Descriptions.Item label="统计周期">{period ?? "—"}</Descriptions.Item>
        <Descriptions.Item label="维度">
          {dimensions.length ? dimensions.join("，") : "—"}
        </Descriptions.Item>
        <Descriptions.Item label="源表">
          {sourceTables.length ? sourceTables.join("，") : sourceTable ?? "—"}
        </Descriptions.Item>
        {measures.length > 1 ? (
          <Descriptions.Item label="全量度量">
            {measures
              .map((m) => `${m.aggregation ?? ""}(${m.name ?? ""})`.trim())
              .filter(Boolean)
              .join("；")}
          </Descriptions.Item>
        ) : null}
      </Descriptions>
    </>
  );
}

type DrillRow = Record<string, unknown>;

// 下钻明细列（目录 / 指标 / 孤儿三种口径）
// 生产化补充（FR-18）：源名称、业务域、责任人中文名、表描述、更新时间——
// 点击行可继续下钻到实体详情抽屉（onRow 由调用方接入）。
const CATALOG_COLUMNS: ColumnsType<DrillRow> = [
  {
    title: "数据源",
    dataIndex: "source_id",
    width: 150,
    ellipsis: true,
    render: (v, r) => {
      const name = (r as { source_name?: string | null }).source_name;
      return name && name !== v ? `${name}` : (v as string);
    },
  },
  {
    title: "实体",
    dataIndex: "entity_name",
    ellipsis: true,
    // 链接样式提示可点击（实际点击走 onRow → 实体详情抽屉）
    render: (v) => <a style={{ cursor: "pointer" }}>{v as string}</a>,
  },
  {
    title: "类型",
    dataIndex: "entity_type",
    width: 90,
    render: (v) => ENTITY_TYPE_LABEL[v as string] ?? v,
  },
  { title: "业务域", dataIndex: "domain", width: 110, render: (v) => v ?? <span className="muted">-</span> },
  {
    title: "敏感度",
    dataIndex: "sensitivity_level",
    width: 110,
    render: (s) => sensitivityTag(s as string | null | undefined),
  },
  {
    title: "责任人",
    dataIndex: "owner_id",
    width: 110,
    ellipsis: true,
    render: (v, r) => {
      const name = (r as { owner_name?: string | null }).owner_name;
      return v == null ? <Tag>无</Tag> : name || `#${v as number}`;
    },
  },
  {
    title: "表描述",
    dataIndex: "description",
    ellipsis: true,
    render: (v) =>
      v ? (
        <span style={{ color: "rgba(0,0,0,0.65)" }}>{v as string}</span>
      ) : (
        <span className="muted">-</span>
      ),
  },
  {
    title: "更新时间",
    dataIndex: "updated_at",
    width: 150,
    render: (v) => (v ? formatCnTime(v as string) : <span className="muted">-</span>),
  },
];

const METRIC_COLUMNS: ColumnsType<DrillRow> = [
  {
    title: "编码",
    dataIndex: "metric_code",
    ellipsis: true,
    render: (v) => <span className="mono">{v as string}</span>,
  },
  { title: "名称", dataIndex: "name", ellipsis: true },
  { title: "域", dataIndex: "domain", width: 110 },
  { title: "状态", dataIndex: "status", width: 100, render: (v: string) => STATUS_LABEL[v] ?? v ?? "-" },
  {
    title: "PII",
    dataIndex: "pii_flag",
    width: 70,
    render: (v) => (v ? <Tag color="red">PII</Tag> : null),
  },
];

const ORPHAN_COLUMNS: ColumnsType<DrillRow> = [
  { title: "数据源", dataIndex: "source_id", width: 130 },
  { title: "实体", dataIndex: "entity_name", ellipsis: true },
  {
    title: "类型",
    dataIndex: "entity_type",
    width: 90,
    render: (v) => ENTITY_TYPE_LABEL[v as string] ?? v,
  },
  {
    title: "敏感度",
    dataIndex: "sensitivity_level",
    width: 110,
    render: (s) => sensitivityTag(s as string | null | undefined),
  },
];

// ─── 变更追踪：行点击 → 本页侧边栏详情抽屉 ─────────────────────────────
// 目录行 → 实体详情；指标行 → 指标详情（口径/快照）；漂移行 → before/after diff。

/** 漂移 diff 渲染：diff_json 含 before_schema/after_schema 时左右对照展示，否则平铺键值。 */
function renderDriftDiff(diff: Record<string, unknown>) {
  const before = diff.before_schema ?? diff.before ?? null;
  const after = diff.after_schema ?? diff.after ?? null;
  const fields = Object.entries(diff).filter(
    ([k]) => !["before_schema", "after_schema", "before", "after"].includes(k),
  );
  return (
    <div>
      {fields.length > 0 ? (
        <Descriptions column={1} size="small" bordered style={{ marginBottom: 12 }}>
          {fields.map(([k, v]) => (
            <Descriptions.Item key={k} label={k}>
              <span className="mono" style={{ fontSize: 12 }}>
                {typeof v === "object" && v != null ? JSON.stringify(v) : String(v ?? "")}
              </span>
            </Descriptions.Item>
          ))}
        </Descriptions>
      ) : null}
      {before || after ? (
        <Row gutter={12}>
          <Col span={12}>
            <Card size="small" type="inner" title="变更前" style={{ marginBottom: 12 }}>
              {before ? (
                <pre
                  className="mono"
                  style={{
                    fontSize: 11,
                    maxHeight: 340,
                    overflow: "auto",
                    margin: 0,
                    whiteSpace: "pre-wrap",
                  }}
                >
                  {JSON.stringify(before, null, 2)}
                </pre>
              ) : (
                <span className="muted">（首次创建）</span>
              )}
            </Card>
          </Col>
          <Col span={12}>
            <Card size="small" type="inner" title="变更后" style={{ marginBottom: 12 }}>
              {after ? (
                <pre
                  className="mono"
                  style={{
                    fontSize: 11,
                    maxHeight: 340,
                    overflow: "auto",
                    margin: 0,
                    whiteSpace: "pre-wrap",
                  }}
                >
                  {JSON.stringify(after, null, 2)}
                </pre>
              ) : (
                <span className="muted">（已删除）</span>
              )}
            </Card>
          </Col>
        </Row>
      ) : null}
    </div>
  );
}

/** 变更追踪-目录行详情抽屉：按 catalog id 拉实体详情（字段清单/血缘/关联指标/源健康）。 */
function ChangeCatalogDetailDrawer({
  item,
  onClose,
}: {
  item: AssetChangeItem | null;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const [detail, setDetail] = useState<AssetEntityDetail | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!item) {
      setDetail(null);
      return;
    }
    setLoading(true);
    setDetail(null);
    fetchAssetEntityDetail(item.id)
      .then(setDetail)
      .catch((err) => message.error(err instanceof Error ? err.message : "加载实体详情失败"))
      .finally(() => setLoading(false));
  }, [item]);

  return (
    <ResizableDrawer
      title={item ? `变更目录详情：${item.entity_name}` : "变更目录详情"}
      open={item != null}
      onClose={onClose}
      storageKey="unisense.drawer.changes.catalog.width"
      defaultWidth={860}
      minWidth={600}
      footer={
        item && detail ? (
          <Space>
            <Button
              type="primary"
              icon={<SearchOutlined />}
              onClick={() =>
                navigate(
                  `/catalogs?kw=${encodeURIComponent(item.entity_name)}&focus=${encodeURIComponent(item.entity_name)}&from=变更追踪`,
                )
              }
            >
              在采集目录中查看
            </Button>
          </Space>
        ) : null
      }
    >
      {loading ? (
        <Spin tip="加载实体详情…" />
      ) : detail ? (
        <>
          <Descriptions column={2} bordered size="small">
            <Descriptions.Item label="实体名称">{detail.entity_name}</Descriptions.Item>
            <Descriptions.Item label="实体类型">{detail.entity_type}</Descriptions.Item>
            <Descriptions.Item label="数据源">
              {detail.source_name ? `${detail.source_name}（${detail.source_id}）` : detail.source_id}
            </Descriptions.Item>
            <Descriptions.Item label="业务域">
              {detail.domain ?? <span className="muted">-</span>}
            </Descriptions.Item>
            <Descriptions.Item label="敏感度">
              {sensitivityTag(detail.sensitivity_level)}
              {Boolean(detail.pii_flag || (detail.sensitivity_level ?? "").includes("PII")) && (
                <Tag color="red" style={{ marginLeft: 8 }}>
                  含 PII
                </Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="责任人">
              {detail.owner_id != null
                ? detail.owner_name || `#${detail.owner_id}`
                : <Tag>无</Tag>}
            </Descriptions.Item>
            <Descriptions.Item label="字段数">
              {detail.column_count ?? <span className="muted">-</span>}
            </Descriptions.Item>
            <Descriptions.Item label="表级描述">
              {detail.description ? (
                <span>{detail.description}</span>
              ) : (
                <span className="muted" style={{ fontStyle: "italic" }}>
                  暂无表级描述
                </span>
              )}
              {detail.description_source ? descriptionSourceTag(detail.description_source) : null}
            </Descriptions.Item>
            <Descriptions.Item label="变更类型">
              {item && CHANGE_TYPE_META[item.change_type] ? (
                <Tag color={CHANGE_TYPE_META[item.change_type].color}>
                  {CHANGE_TYPE_META[item.change_type].label}
                </Tag>
              ) : (
                item?.change_type ?? "-"
              )}
            </Descriptions.Item>
            <Descriptions.Item label="变更时间">
              {item?.updated_at ? formatCnTime(item.updated_at) : "-"}
            </Descriptions.Item>
            <Descriptions.Item label="Schema 状态">
              {detail.schema_incomplete ? (
                <Tag color="orange">不完整</Tag>
              ) : (
                <Tag color="green">完整</Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="源健康">
              {detail.source_health ? (
                <Tag
                  color={
                    detail.source_health.health_status === "healthy"
                      ? "green"
                      : detail.source_health.health_status === "unhealthy"
                        ? "red"
                        : "default"
                  }
                >
                  {SOURCE_HEALTH_LABEL[detail.source_health.health_status] ??
                    detail.source_health.health_status}
                </Tag>
              ) : (
                <span className="muted">未知</span>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="新鲜度">
              <div className="muted" style={{ fontSize: 12 }}>
                <div>创建：{detail.created_at ? formatCnTime(detail.created_at) : "-"}</div>
                <div>更新：{detail.updated_at ? formatCnTime(detail.updated_at) : "-"}</div>
              </div>
            </Descriptions.Item>
          </Descriptions>

          {detail.schema_summary ? (
            <Card size="small" title="字段清单" style={{ marginTop: 16 }}>
              {renderSchemaSummary(detail.schema_summary)}
            </Card>
          ) : null}

          {detail.lineage_edges && detail.lineage_edges.length > 0 && (
            <EntityLineageCard edges={detail.lineage_edges} />
          )}

          {detail.related_metrics && detail.related_metrics.length > 0 && (
            <Card size="small" title="关联指标" style={{ marginTop: 16 }}>
              <Table
                dataSource={detail.related_metrics}
                rowKey={(r, i) => `${r.metric_node}-${i}`}
                size="small"
                pagination={false}
                columns={[
                  { title: "指标节点", dataIndex: "metric_node", ellipsis: true },
                  { title: "关系", dataIndex: "edge_type", width: 140, render: (v: string) => METRIC_RELATION_EDGE_LABEL[v] ?? v ?? "-" },
                ]}
              />
            </Card>
          )}
        </>
      ) : item ? (
        <Alert type="warning" message="未获取到该实体的详情（可能已删除或未采集）" />
      ) : null}
    </ResizableDrawer>
  );
}

/** 变更追踪-指标行详情抽屉：按编码拉指标详情（业务描述/口径明细/数值快照）。 */
function ChangeMetricDetailDrawer({
  item,
  onClose,
}: {
  item: AssetChangeMetric | null;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const { snapshot: perm } = usePermission();
  const isAdmin = perm?.roles?.includes("platform_admin") ?? false;
  const [metric, setMetric] = useState<MetricResponse | null>(null);
  const [snapshots, setSnapshots] = useState<SnapshotResponse[]>([]);
  const [snapshotForbidden, setSnapshotForbidden] = useState(false);
  const [snapshotBlockedReason, setSnapshotBlockedReason] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!item) {
      setMetric(null);
      setSnapshots([]);
      return;
    }
    setLoading(true);
    setMetric(null);
    setSnapshots([]);
    setSnapshotForbidden(false);
    setSnapshotBlockedReason(null);
    getMetric(item.metric_code)
      .then((m) => {
        setMetric(m);
        // 不可消费指标（未发布/废弃且非管理员）不发注定 403 的快照请求
        const blocked =
          m.status === "DRAFT" || m.status === "REVIEW"
            ? `指标当前为「${m.status === "DRAFT" ? "草稿" : "审核中"}」状态，未发布，暂无消费快照`
            : m.status === "DEPRECATED" && !isAdmin
              ? "指标已废弃（DEPRECATED），仅平台管理员可审计回溯历史快照"
              : null;
        if (blocked) {
          setSnapshotBlockedReason(blocked);
          setSnapshotForbidden(true);
          return;
        }
        return listSnapshots(item.metric_code, 50)
          .then((snaps) => {
            setSnapshots(snaps);
            setSnapshotForbidden(false);
            setSnapshotBlockedReason(null);
            return snaps;
          })
          .catch((err: unknown) => {
            if (err instanceof UnisenseApiError && err.code === "FORBIDDEN") {
              setSnapshotForbidden(true);
              setSnapshotBlockedReason(null);
            }
            return [] as SnapshotResponse[];
          });
      })
      .catch((err) => message.error(err instanceof Error ? err.message : "加载指标详情失败"))
      .finally(() => setLoading(false));
  }, [item, isAdmin]);

  const snapshotColumns: ColumnsType<SnapshotResponse> = [
    { title: "周期", dataIndex: "date_range", key: "date_range", width: 150 },
    {
      title: "维度",
      dataIndex: "dims",
      key: "dims",
      width: 160,
      render: (v: Record<string, unknown>) =>
        v && typeof v === "object" && Object.keys(v).length ? JSON.stringify(v) : "—",
    },
    {
      title: "数值",
      dataIndex: "value_json",
      key: "value_json",
      render: (v: Record<string, unknown>) =>
        v && typeof v === "object" && Object.keys(v).length ? JSON.stringify(v) : "—",
    },
    {
      title: "质量",
      dataIndex: "quality_flag",
      key: "quality_flag",
      width: 80,
      render: (v: string | null) => v ?? "—",
    },
    {
      title: "生成时间",
      dataIndex: "generated_at",
      key: "generated_at",
      width: 170,
      render: (v: string) => formatCnTime(v),
    },
  ];

  return (
    <ResizableDrawer
      title={item ? `变更指标详情：${item.name}（${item.metric_code}）` : "变更指标详情"}
      open={item != null}
      onClose={onClose}
      storageKey="unisense.drawer.changes.metric.width"
      defaultWidth={880}
      minWidth={600}
      footer={
        metric ? (
          <Button
            type="primary"
            onClick={() =>
              navigate(`/detail/${encodeURIComponent(metric.metric_code)}`, {
                state: { from: "assetmap-changes" },
              })
            }
          >
            前往指标详情 →
          </Button>
        ) : null
      }
    >
      {loading ? (
        <Spin tip="加载指标详情…" />
      ) : metric ? (
        <>
          <Card size="small" title="业务描述" style={{ marginBottom: 16 }}>
            {metric.description ? (
              <div style={{ whiteSpace: "pre-wrap" }}>{metric.description}</div>
            ) : (
              <span className="muted">暂无描述</span>
            )}
            {metric.description_source ? descriptionSourceTag(metric.description_source) : null}
          </Card>
          <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
            <Descriptions.Item label="指标名称">{metric.name}</Descriptions.Item>
            <Descriptions.Item label="指标编码">{metric.metric_code}</Descriptions.Item>
            <Descriptions.Item label="所属域">{metric.domain}</Descriptions.Item>
            <Descriptions.Item label="类型">{METRIC_TYPE_LABEL[metric.type ?? ""] ?? metric.type ?? "-"}</Descriptions.Item>
            <Descriptions.Item label="粒度">{GRANULARITY_LABEL[metric.granularity ?? ""] ?? metric.granularity ?? "-"}</Descriptions.Item>
            <Descriptions.Item label="单位">{UNIT_LABEL[metric.unit ?? ""] ?? metric.unit ?? "-"}</Descriptions.Item>
            <Descriptions.Item label="聚合方式">{AGG_LABEL[metric.aggregation ?? ""] ?? metric.aggregation ?? "-"}</Descriptions.Item>
            <Descriptions.Item label="时间语义">{TIME_SEM_LABEL[metric.time_semantics ?? ""] ?? metric.time_semantics ?? "-"}</Descriptions.Item>
            <Descriptions.Item label="新鲜度">{FRESHNESS_LABEL[metric.freshness ?? ""] ?? metric.freshness ?? "-"}</Descriptions.Item>
            <Descriptions.Item label="数仓层">{DW_LAYER_LABEL[metric.dw_layer ?? ""] ?? metric.dw_layer ?? "-"}</Descriptions.Item>
            <Descriptions.Item label="指标分级">{METRIC_TIER_LABEL[metric.metric_tier ?? ""] ?? metric.metric_tier ?? "-"}</Descriptions.Item>
            <Descriptions.Item label="状态">{STATUS_LABEL[metric.status ?? ""] ?? metric.status ?? "-"}</Descriptions.Item>
            <Descriptions.Item label="可加性">{ADDITIVITY_LABEL[metric.additivity ?? ""] ?? metric.additivity ?? "-"}</Descriptions.Item>
            <Descriptions.Item label="PII">
              {metric.pii_flag ? <Tag color="red">PII</Tag> : "否"}
            </Descriptions.Item>
            <Descriptions.Item label="版本">{metric.version}</Descriptions.Item>
            <Descriptions.Item label="变更类型">
              {item && CHANGE_TYPE_META[item.change_type] ? (
                <Tag color={CHANGE_TYPE_META[item.change_type].color}>
                  {CHANGE_TYPE_META[item.change_type].label}
                </Tag>
              ) : (
                item?.change_type ?? "-"
              )}
            </Descriptions.Item>
            <Descriptions.Item label="变更时间">
              {item?.updated_at ? formatCnTime(item.updated_at) : "-"}
            </Descriptions.Item>
          </Descriptions>
          <Card size="small" title="口径明细" style={{ marginBottom: 16 }}>
            <DefinitionsDetail def={metric.definition_json} />
          </Card>
          <Card size="small" title="数值快照">
            {snapshotForbidden ? (
              <Alert
                type="warning"
                showIcon
                message={snapshotBlockedReason ? "该指标消费快照当前不可查看" : "无权限查看该指标消费快照"}
                description={
                  snapshotBlockedReason ??
                  "当前账号未获得该指标所属域的数据读取授权，如需查看请联系域管理员配置授权（grants）或指标白名单。"
                }
              />
            ) : snapshots.length === 0 ? (
              <Empty description="暂无查询快照" />
            ) : (
              <Table
                size="small"
                rowKey="id"
                dataSource={snapshots}
                columns={snapshotColumns}
                pagination={false}
              />
            )}
          </Card>
        </>
      ) : item ? (
        <Alert type="warning" message="未获取到该指标的详情" />
      ) : null}
    </ResizableDrawer>
  );
}

/** 变更追踪-漂移行详情抽屉：展示 diff_json 的 before/after 对照。 */
function ChangeDriftDetailDrawer({
  item,
  onClose,
}: {
  item: AssetDriftItem | null;
  onClose: () => void;
}) {
  const isDrop = item ? /drop|remove|delete/i.test(item.change_type) : false;
  return (
    <ResizableDrawer
      title={item ? `Schema 漂移详情：${item.entity_name}` : "Schema 漂移详情"}
      open={item != null}
      onClose={onClose}
      storageKey="unisense.drawer.changes.drift.width"
      defaultWidth={760}
      minWidth={560}
    >
      {item ? (
        <>
          <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
            <Descriptions.Item label="实体">{item.entity_name}</Descriptions.Item>
            <Descriptions.Item label="数据源">{item.source_id}</Descriptions.Item>
            <Descriptions.Item label="变更类型">
              <Tag color={isDrop ? "red" : "orange"}>{CHANGE_TYPE_META[item.change_type]?.label ?? item.change_type}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="时间">{formatCnTime(item.created_at)}</Descriptions.Item>
          </Descriptions>
          {item.diff_json ? (
            <Card size="small" title="变更内容（before → after）">
              {renderDriftDiff(item.diff_json)}
            </Card>
          ) : (
            <Alert type="info" message="该漂移记录无 diff 明细" />
          )}
        </>
      ) : null}
    </ResizableDrawer>
  );
}

function OverviewTab() {
  const [summary, setSummary] = useState<AssetCatalogSummary | null>(null);
  const [metricSummary, setMetricSummary] = useState<AssetMetricSummary | null>(null);
  const [metricDims, setMetricDims] = useState<AssetMetricDimensionSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // 明细下钻抽屉状态（指标点击值 → 明细表）
  const [drillOpen, setDrillOpen] = useState(false);
  const [drillLoading, setDrillLoading] = useState(false);
  const [drillTitle, setDrillTitle] = useState("");
  const [drillColumns, setDrillColumns] = useState<ColumnsType<DrillRow>>([]);
  const [drillRows, setDrillRows] = useState<DrillRow[]>([]);
  // 明细后端真实总数（undefined=未知，DrillDownDrawer 回退已加载行数；避免 100 截断误导）
  const [drillTotal, setDrillTotal] = useState<number | undefined>(undefined);
  // 下钻行点击 → 实体详情抽屉（FR-18：明细可继续下钻到单表详情）
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detail, setDetail] = useState<AssetEntityDetail | null>(null);
  // 误报反馈（COMPL-3）：PII 命中字段 → 豁免词表 + 重算降级
  const [fpTarget, setFpTarget] = useState<{ column: string; category: string } | null>(null);
  const [fpScope, setFpScope] = useState<"field" | "prefix">("field");
  const [fpReason, setFpReason] = useState("");
  const [fpSubmitting, setFpSubmitting] = useState(false);
  const canReportFp = usePermission().can("classification:rescan");

  async function submitFalsePositive() {
    if (!detail || !fpTarget) return;
    const reason = fpReason.trim();
    if (!reason) {
      message.warning("请填写误报原因（留痕）");
      return;
    }
    setFpSubmitting(true);
    try {
      const res = await reportClassificationFalsePositive(detail.id, {
        column: fpTarget.column,
        scope: fpScope,
        reason,
      });
      message.success(
        `已豁免 ${res.exempted_as}（${res.sensitivity_before} → ${res.sensitivity_after}）`,
      );
      setFpTarget(null);
      setFpReason("");
      setFpScope("field");
      // 刷新详情：豁免后 PII 命中减少/敏感级降级
      setDetail(await fetchAssetEntityDetail(detail.id));
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "误报反馈失败",
      );
    } finally {
      setFpSubmitting(false);
    }
  }

  useEffect(() => {
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const [s, m, d] = await Promise.all([
          fetchAssetSummary(),
          fetchAssetMetricSummary(),
          fetchAssetMetricDimensions(),
        ]);
        setSummary(s);
        setMetricSummary(m);
        setMetricDims(d);
      } catch (err) {
        setError(err instanceof Error ? err.message : "加载资产概览失败");
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  async function openDrill(
    title: string,
    columns: ColumnsType<DrillRow>,
    loader: () => Promise<DrillRow[]>,
    total?: number,
  ) {
    setDrillTitle(title);
    setDrillColumns(columns);
    setDrillOpen(true);
    setDrillLoading(true);
    setDrillRows([]);
    setDrillTotal(total);
    try {
      setDrillRows(await loader());
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载明细失败");
    } finally {
      setDrillLoading(false);
    }
  }

  function drillCatalogs(params?: {
    entity_type?: string;
    sensitivity_level?: string;
    source_id?: string;
    database?: string;
    pending_review?: boolean;
  }) {
    const title = params?.pending_review
      ? "待复核敏感资产明细"
      : params?.entity_type
        ? `实体类型：${ENTITY_TYPE_LABEL[params.entity_type] ?? params.entity_type}`
        : params?.sensitivity_level
          ? `敏感度：${SENSITIVITY_LABEL[params.sensitivity_level] ?? params.sensitivity_level}`
          : params?.source_id
            ? `数据源：${summary?.by_source?.find((s) => s.source_id === params.source_id)?.source_name ?? params.source_id}`
            : params?.database
              ? `库：${params.database}`
              : "目录资产明细";
    return openDrill(title, CATALOG_COLUMNS, async () => {
      const r = await listCatalogs({ ...params, page_size: 200 });
      return r.items as unknown as DrillRow[];
    });
  }

  function drillMetrics(status?: string) {
    // 无 status = 「指标总数」下钻：与总览统计（metric_summary by_domain）同口径，
    // 排除 DRAFT/DEPRECATED，否则明细多出草稿/已废弃导致总数与明细不一致。
    return openDrill(status ? "已发布指标明细" : "活跃指标明细", METRIC_COLUMNS, async () => {
      const r = await listMetrics({
        ...(status ? { status } : { exclude_statuses: "DRAFT,DEPRECATED" }),
        page_size: 200,
      });
      setDrillTotal(r.total);
      return r.items as unknown as DrillRow[];
    });
  }

  function drillOrphans() {
    return openDrill("孤儿资产明细", ORPHAN_COLUMNS, async () => {
      const r = await fetchAssetOrphans();
      return r.items as unknown as DrillRow[];
    });
  }

  // 下钻明细行点击 → 打开单表实体详情抽屉（目录行 id 可用时）
  function openCatalogDetail(row: DrillRow) {
    const id = row?.id as number | undefined;
    if (id == null) {
      message.warning("该实体缺少详情标识（id），暂无法查看详情");
      return;
    }
    // 关闭下钻明细抽屉：从「明细列表」下钻到「单表详情」时列表让位，
    // 避免两个 Drawer 同时挂 body Portal 导致详情被明细覆盖。
    setDrillOpen(false);
    setDetailOpen(true);
    setDetailLoading(true);
    setDetail(null);
    fetchAssetEntityDetail(id)
      .then((d) => setDetail(d))
      .catch((err) => message.error(err instanceof Error ? err.message : "加载实体详情失败"))
      .finally(() => setDetailLoading(false));
  }

  // 可点击值渲染：把 Statistic 的 value 包成可点击链接
  function clickableValue(onClick: () => void) {
    return (node: ReactNode) => (
      <a
        href="#"
        onClick={(e) => {
          e.preventDefault();
          onClick();
        }}
        style={{ cursor: "pointer" }}
      >
        {node}
      </a>
    );
  }

  // 指标体系：打开某维度分布明细（维度 → 数量两列）
  function openDimensionDrill(title: string, data: Record<string, number> | undefined) {
    const rows: DrillRow[] = Object.entries(data ?? {}).map(([k, v]) => ({
      key: k,
      value: v,
    }));
    const columns: ColumnsType<DrillRow> = [
      { title: "维度值", dataIndex: "key" },
      { title: "指标数", dataIndex: "value" },
    ];
    openDrill(title, columns, async () => rows);
  }

  if (loading) return <Spin />;
  if (error) return <Alert type="error" message={error} />;
  if (!summary || !metricSummary) return <Empty description="暂无资产数据" />;

  const totalMetrics = metricSummary.by_domain
    ? Object.values(metricSummary.by_domain).reduce((a, b) => a + b, 0)
    : 0;
  const sensData = Object.entries(summary.by_sensitivity ?? {}).map(([k, v]) => ({
    type: SENSITIVITY_LABEL[k] ?? k,
    key: k,
    value: v,
  }));

  return (
    <div>
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={12} md={6}>
          <Statistic
            title="目录资产总数"
            value={summary.total}
            valueRender={clickableValue(() => drillCatalogs())}
          />
        </Col>
        <Col xs={12} md={6}>
          <Statistic
            title="指标总数"
            value={totalMetrics}
            valueRender={clickableValue(() => drillMetrics())}
          />
        </Col>
        <Col xs={12} md={6}>
          <Statistic
            title="孤儿资产"
            value={summary.orphan_assets}
            valueRender={clickableValue(() => drillOrphans())}
            valueStyle={{ color: summary.orphan_assets > 0 ? "#d64545" : undefined }}
          />
        </Col>
        <Col xs={12} md={6}>
          <Statistic
            title="已发布指标"
            value={metricSummary.by_status?.PUBLISHED ?? 0}
            valueRender={clickableValue(() => drillMetrics("PUBLISHED"))}
            valueStyle={{ color: "#2e9e5b" }}
          />
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <Card title="目录实体类型" size="small">
            <Row gutter={[8, 8]}>
              {Object.entries(summary.by_entity_type ?? {}).map(([k, v]) => (
                <Col span={8} key={k}>
                  <Statistic
                    title={ENTITY_TYPE_LABEL[k] ?? k}
                    value={v}
                    valueRender={clickableValue(() => drillCatalogs({ entity_type: k }))}
                  />
                </Col>
              ))}
            </Row>
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card title="敏感度分类分布" size="small" styles={{ body: { paddingTop: 8 } }}>
            {sensData.length === 0 ? (
              <Empty description="暂无分类数据" />
            ) : (
              <Pie
                data={sensData}
                angleField="value"
                colorField="type"
                radius={0.85}
                innerRadius={0.6}
                height={220}
                label={{ text: "value", style: { fontWeight: 600 } }}
                onReady={(plot) => {
                  // 扇区点击 → 下钻该敏感度的目录明细
                  plot.on("element:click", (evt: { data?: { data?: { key?: string } } }) => {
                    const key = evt?.data?.data?.key;
                    if (key) drillCatalogs({ sensitivity_level: key });
                  });
                }}
              />
            )}
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={12}>
          <Card
            title="按数据源分布"
            size="small"
            extra={
              <span className="muted" style={{ fontSize: 12 }}>
                点击柱体查看该数据源资产明细
              </span>
            }
          >
            {!summary.by_source || summary.by_source.length === 0 ? (
              <Empty description="暂无数据源分布数据" />
            ) : (
              <Bar
                data={summary.by_source.map((s) => ({ key: s.source_name, value: s.count, source_id: s.source_id }))}
                xField="key"
                yField="value"
                height={220}
                xAxis={{ label: { autoRotate: true } }}
                onReady={(plot) => {
                  plot.on("element:click", (evt: { data?: { data?: { source_id?: string } } }) => {
                    const sid = evt?.data?.data?.source_id;
                    if (sid) drillCatalogs({ source_id: sid });
                  });
                }}
              />
            )}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card
            title="按库分布"
            size="small"
            extra={
              <span className="muted" style={{ fontSize: 12 }}>
                点击柱体查看该库资产明细
              </span>
            }
          >
            {!summary.by_database || summary.by_database.length === 0 ? (
              <Empty description="暂无库分布数据" />
            ) : (
              <Bar
                data={summary.by_database.map((d) => ({ key: d.database, value: d.count }))}
                xField="key"
                yField="value"
                height={220}
                xAxis={{ label: { autoRotate: true } }}
                onReady={(plot) => {
                  plot.on("element:click", (evt: { data?: { data?: { key?: string } } }) => {
                    const db = evt?.data?.data?.key;
                    if (db) drillCatalogs({ database: db });
                  });
                }}
              />
            )}
          </Card>
        </Col>
      </Row>

      <Card
        size="small"
        title="目录资产 PII 合规"
        style={{ marginTop: 16 }}
        extra={
          <span className="muted" style={{ fontSize: 12 }}>
            敏感资产 = PII / CONFIDENTIAL 分类
          </span>
        }
      >
        <Row gutter={[16, 16]}>
          <Col xs={24} sm={8}>
            <Statistic
              title="合规率"
              value={summary?.pii_compliance?.compliance_rate ?? 0}
              precision={1}
              suffix="%"
              valueStyle={{
                color: (summary?.pii_compliance?.compliance_rate ?? 0) >= 80 ? "#2e9e5b" : "#d64545",
              }}
            />
          </Col>
          <Col xs={24} sm={16}>
            <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
              已复核 {summary?.pii_compliance?.reviewed ?? 0} /{" "}
              {summary?.pii_compliance?.sensitive_total ?? 0} 个敏感资产
              （PII {summary?.pii_compliance?.by_sensitivity?.PII ?? 0} / CONFIDENTIAL{" "}
              {summary?.pii_compliance?.by_sensitivity?.CONFIDENTIAL ?? 0}）
            </div>
            <Space size={[6, 6]} wrap>
              <Tag
                color="red"
                style={{ cursor: "pointer", margin: 0 }}
                onClick={() => drillCatalogs({ pending_review: true })}
              >
                待复核 {summary?.pii_compliance?.pending ?? 0} 项（点击查看明细）
              </Tag>
            </Space>
          </Col>
        </Row>
      </Card>

      <Card
        title="指标体系"
        size="small"
        style={{ marginTop: 16 }}
        extra={
          <span className="muted" style={{ fontSize: 12 }}>
            共 {metricDims?.total ?? 0} 个指标
          </span>
        }
      >
        <Row gutter={[12, 12]}>
          {METRIC_DIMENSION_GROUPS.map((g) => {
            const data = ((metricDims ?? {})[g.key] ?? {}) as Record<string, number>;
            const entries = Object.entries(data);
            if (entries.length === 0) return null;
            return (
              <Col xs={24} sm={12} lg={6} key={g.key}>
                <Card size="small" title={g.label} styles={{ body: { padding: "8px 12px" } }}>
                  <Space size={[6, 6]} wrap>
                    {entries.map(([k, v]) => (
                      <Tag
                        key={k}
                        color="blue"
                        style={{ cursor: "pointer", margin: 0 }}
                        onClick={() => openDimensionDrill(`${g.label}分布明细`, data)}
                      >
                        {dimLabel(g.map, k)} {v}
                      </Tag>
                    ))}
                  </Space>
                </Card>
              </Col>
            );
          })}
          <Col xs={24} sm={12} lg={6}>
            <Card size="small" title="指标 PII 合规" styles={{ body: { padding: "8px 12px" } }}>
              {metricDims?.pii_compliance?.review_rate == null ? (
                <>
                  <Statistic title="合规率" value="—" suffix="" />
                  <div className="muted" style={{ fontSize: 12 }}>
                    暂无有效 PII 指标（已废弃不参与统计）
                  </div>
                </>
              ) : (
                <>
                  <Statistic
                    title="合规率"
                    value={Math.round(metricDims.pii_compliance.review_rate * 100)}
                    suffix="%"
                    valueStyle={{
                      color: metricDims.pii_compliance.review_rate >= 0.8 ? "#2e9e5b" : "#d64545",
                    }}
                  />
                  <div className="muted" style={{ fontSize: 12 }}>
                    已复核 {metricDims.pii_compliance.pii_reviewed} /{" "}
                    {metricDims.pii_compliance.pii_total} 个 PII 指标
                  </div>
                </>
              )}
            </Card>
          </Col>
        </Row>
      </Card>

      <DrillDownDrawer
        open={drillOpen}
        title={drillTitle}
        columns={drillColumns}
        rows={drillRows}
        loading={drillLoading}
        total={drillTotal}
        onClose={() => setDrillOpen(false)}
        onRow={(row) => ({
          onClick: () => openCatalogDetail(row),
        })}
      />
      <ResizableDrawer
        title={detail ? `实体详情：${detail.entity_name}` : "实体详情"}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        storageKey="unisense.drawer.entity.width"
        defaultWidth={860}
        minWidth={600}
        zIndex={1050}
      >
        {detailLoading ? (
          <Spin tip="加载实体详情…" />
        ) : detail ? (
          <>
            <Descriptions column={2} bordered size="small">
              <Descriptions.Item label="实体名称">{detail.entity_name}</Descriptions.Item>
              <Descriptions.Item label="实体类型">{detail.entity_type}</Descriptions.Item>
              <Descriptions.Item label="数据源">
                {detail.source_name ? `${detail.source_name}（${detail.source_id}）` : detail.source_id}
              </Descriptions.Item>
              <Descriptions.Item label="业务域">
                {detail.domain ?? <span className="muted">-</span>}
              </Descriptions.Item>
              <Descriptions.Item label="敏感度">
                {sensitivityTag(detail.sensitivity_level)}
                {Boolean(detail.pii_flag || (detail.sensitivity_level ?? "").includes("PII")) && (
                  <Tag color="red" style={{ marginLeft: 8 }}>
                    含 PII
                  </Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="责任人">
                {detail.owner_id != null
                  ? detail.owner_name || `#${detail.owner_id}`
                  : <Tag>无</Tag>}
              </Descriptions.Item>
              <Descriptions.Item label="字段数">
                {detail.column_count ?? <span className="muted">-</span>}
              </Descriptions.Item>
              <Descriptions.Item label="表级描述">
                {detail.description ? (
                  <span>{detail.description}</span>
                ) : (
                  <span className="muted" style={{ fontStyle: "italic" }}>
                    暂无表级描述
                  </span>
                )}
                {detail.description_source ? descriptionSourceTag(detail.description_source) : null}
              </Descriptions.Item>
              <Descriptions.Item label="Schema 状态">
                {detail.schema_incomplete ? (
                  <Tag color="orange">不完整</Tag>
                ) : (
                  <Tag color="green">完整</Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="源健康">
                {detail.source_health ? (
                  <Tag
                    color={
                      detail.source_health.health_status === "healthy"
                        ? "green"
                        : detail.source_health.health_status === "unhealthy"
                          ? "red"
                          : "default"
                    }
                  >
                    {SOURCE_HEALTH_LABEL[detail.source_health.health_status] ??
                      detail.source_health.health_status}
                  </Tag>
                ) : (
                  <span className="muted">未知</span>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="新鲜度">
                <div className="muted" style={{ fontSize: 12 }}>
                  <div>创建：{detail.created_at ? formatCnTime(detail.created_at) : "-"}</div>
                  <div>更新：{detail.updated_at ? formatCnTime(detail.updated_at) : "-"}</div>
                </div>
              </Descriptions.Item>
            </Descriptions>
            {detail.schema_summary != null && (
              <Card
                size="small"
                title={
                  <Space size={8}>
                    <span>字段清单</span>
                    {(detail.pii_field_count ?? 0) > 0 && (
                      <Tag color="red">含 {detail.pii_field_count} 个 PII 字段</Tag>
                    )}
                  </Space>
                }
                style={{ marginTop: 16 }}
              >
                {(detail.pii_fields ?? []).filter((f) => !f.suppressed).length > 0 && (
                  <div style={{ marginBottom: 8 }}>
                    <span className="muted" style={{ marginRight: 8, fontSize: 12 }}>
                      PII 命中：
                    </span>
                    {(detail.pii_fields ?? [])
                      .filter((f) => !f.suppressed)
                      .map((f) => (
                        <span key={f.column} style={{ display: "inline-block", marginBottom: 4 }}>
                          <Tag
                            color={PII_CATEGORY_COLOR[f.category] ?? "red"}
                            style={{ marginRight: 2 }}
                          >
                            {f.column}（{PII_CATEGORY_LABEL[f.category] ?? f.category}）
                          </Tag>
                          {canReportFp && (
                            <Button
                              type="text"
                              size="small"
                              danger
                              icon={<StopOutlined />}
                              style={{ marginRight: 8, height: 20, fontSize: 12 }}
                              onClick={() => {
                                setFpTarget({ column: f.column, category: f.category });
                                setFpScope("field");
                                setFpReason("");
                              }}
                            >
                              误报
                            </Button>
                          )}
                        </span>
                      ))}
                  </div>
                )}
                {renderSchemaSummary(detail.schema_summary)}
              </Card>
            )}
            {detail.lineage_edges && detail.lineage_edges.length > 0 && (
              <EntityLineageCard edges={detail.lineage_edges} />
            )}
          </>
        ) : null}
        {/* 误报反馈（COMPL-3）：PII 命中字段 → 豁免词表 + 重算降级 */}
        <Modal
          title="PII 误报反馈"
          open={!!fpTarget}
          onCancel={() => setFpTarget(null)}
          onOk={submitFalsePositive}
          confirmLoading={fpSubmitting}
          okText="确认豁免"
          cancelText="取消"
          width={520}
          destroyOnClose
        >
          <div style={{ padding: "8px 0 4px" }}>
            <Space direction="vertical" style={{ width: "100%" }}>
              <Alert
                type="warning"
                showIcon
                message={`字段「${fpTarget?.column ?? ""}」被识别为 ${
                  PII_CATEGORY_LABEL[fpTarget?.category ?? ""] ?? "PII"
                }，确认误报后将写入豁免词表并重算该实体敏感级。`}
              />
              <div>
                <span style={{ marginRight: 12 }}>豁免粒度：</span>
                <Radio.Group
                  value={fpScope}
                  onChange={(e) => setFpScope(e.target.value)}
                  options={[
                    { value: "field", label: "精确字段名（仅此字段）" },
                    { value: "prefix", label: "字段前缀（同前缀全豁免）" },
                  ]}
                />
              </div>
              <Input.TextArea
                placeholder="误报原因（必填，审计留痕）"
                rows={3}
                value={fpReason}
                onChange={(e) => setFpReason(e.target.value)}
              />
            </Space>
          </div>
        </Modal>
      </ResizableDrawer>
    </div>
  );
}

function GraphTab() {
  const navigate = useNavigate();
  // 按钮级权限点：无对应权限点时隐藏按钮（后端强制兜底）
  const canInferCatalog = usePermission().can("catalog:infer-description");
  const canInferMetric = usePermission().can("metric:infer-description");
  const canDeprecate = usePermission().can("catalog:deprecate");
  const canEditDesc = usePermission().can("catalog:edit-description");
  const canEditMetric = usePermission().can("metric:edit");
  const canQuery = usePermission().can("query:execute");
  const permSnapshot = usePermission().snapshot;
  const isAdmin = permSnapshot?.roles?.includes("platform_admin") ?? false;
  const [graphData, setGraphData] = useState<{
    nodes: AssetGraphNode[];
    edges: AssetGraphEdge[];
  } | null>(null);
  const [domain, setDomain] = useState<string | undefined>(undefined);
  const [piiOnly, setPiiOnly] = useState(false);
  // 图谱来源视角：asset=采集目录资产视角（指标为中心 depth 收敛）；血缘通道=完整表级血缘
  const [graphSource, setGraphSource] = useState<string>("asset");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 实体详情抽屉（table/field 节点下钻）
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detail, setDetail] = useState<AssetEntityDetail | null>(null);
  // 单实体废弃（资产地图「手动下线」治理入口）
  const [deprecating, setDeprecating] = useState(false);
  // 表级描述编辑态（治理补全，TD §12.1）
  const [tableDescEditing, setTableDescEditing] = useState(false);
  const [tableDescDraft, setTableDescDraft] = useState("");
  const [tableDescSaving, setTableDescSaving] = useState(false);
  const [tableInferring, setTableInferring] = useState(false);
  // 字段信息抽屉（field 节点无 entity_id 时的兜底展示 + 所属表入口）
  const [fieldNode, setFieldNode] = useState<AssetGraphNode | null>(null);
  const [fieldTableNode, setFieldTableNode] = useState<AssetGraphNode | null>(null);
  // 指标详情抽屉（metric 节点下钻：明细 + 补充描述，TD §12.1）
  const [metricOpen, setMetricOpen] = useState(false);
  const [metricLoading, setMetricLoading] = useState(false);
  const [metricData, setMetricData] = useState<MetricResponse | null>(null);
  const [metricSnapshots, setMetricSnapshots] = useState<SnapshotResponse[]>([]);
  const [metricSnapshotBlocked, setMetricSnapshotBlocked] = useState<string | null>(null);
  const [metricQuerying, setMetricQuerying] = useState(false);
  const [metricQueryRows, setMetricQueryRows] = useState<Record<string, unknown>[] | null>(null);
  const [metricQueryMeta, setMetricQueryMeta] = useState<{ engine: string; total: number } | null>(
    null,
  );
  const [metricDescEditing, setMetricDescEditing] = useState(false);
  const [metricDescDraft, setMetricDescDraft] = useState("");
  const [metricDescSaving, setMetricDescSaving] = useState(false);
  const [metricInferring, setMetricInferring] = useState(false);
  const [inferElapsed, setInferElapsed] = useState(0);

  // 请求序号令牌（P1 竞态修复）：domain/PII 开关快速切换时旧响应不覆盖新图谱
  const graphSeqRef = useRef(0);

  async function loadGraph() {
    const seq = ++graphSeqRef.current;
    setLoading(true);
    setError(null);
    try {
      if (graphSource === "asset") {
        // 资产视角：depth=2 从指标出发 2 层收敛，避免 500+ 节点挤成一团（depth 越大展开越多）
        const data = await fetchAssetGraph({ domain, depth: 2, pii_only: piiOnly });
        if (seq !== graphSeqRef.current) return; // 旧响应丢弃
        setGraphData(data);
      } else {
        // 血缘视角分支：方案A 已收敛为「资产视角」默认，通道切换/完整血缘移至血缘视图（/lineage）。
        // 此分支作为防御保留（历史 URL 可能带 graphSource 参数），非资产视角时提示跳转血缘视图。
        if (seq !== graphSeqRef.current) return;
        message.info("完整血缘图谱已移至「血缘与影响」视图");
        navigate("/lineage");
        return;
      }
    } catch (err) {
      if (seq !== graphSeqRef.current) return; // 旧请求失败不弹错
      setError(err instanceof Error ? err.message : "加载图谱数据失败");
    } finally {
      if (seq === graphSeqRef.current) setLoading(false);
    }
  }

  useEffect(() => {
    loadGraph();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [domain, piiOnly, graphSource]);

  // AI 推断计时：LLM 生成耗时数秒，展示秒数避免用户误以为卡死
  useEffect(() => {
    if (!metricInferring) return;
    setInferElapsed(0);
    const timer = window.setInterval(() => setInferElapsed((s) => s + 1), 1000);
    return () => window.clearInterval(timer);
  }, [metricInferring]);

  async function openDetail(entityId: number) {
    setDetailOpen(true);
    setDetailLoading(true);
    setDetail(null);
    setTableDescEditing(false);
    try {
      setDetail(await fetchAssetEntityDetail(entityId));
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载实体详情失败");
    } finally {
      setDetailLoading(false);
    }
  }

  async function refreshDetail() {
    if (!detail) return;
    setDetail(await fetchAssetEntityDetail(detail.id));
  }

  async function handleFieldEdit(col: SchemaColumn, newDesc: string) {
    if (!detail) return;
    await updateColumnDescription(detail.id, col.name, newDesc);
    message.success(`字段「${col.name}」描述已保存`);
    await refreshDetail();
  }

  function handleFieldInfer(col: SchemaColumn) {
    if (!detail) return;
    const existing = (detail.schema_summary as SchemaColumn[] | undefined)?.find(
      (c) => c.name === col.name,
    );
    const doInfer = (force: boolean) => {
      const p = runInflight(`column:${detail.id}:${col.name}`, async () => {
        await inferColumnDescription(detail.id, col.name, {
          entity_name: detail.entity_name,
          column_type: col.type,
          force,
        });
        message.success(`字段「${col.name}」描述已生成`);
        await refreshDetail();
      });
      if (p === null) message.info("该字段的 LLM 推断正在进行中，请稍候");
    };
    // 去重防线：已有 LLM 推断描述时不直接重复调 LLM，先确认是否重新生成
    if (existing?.description && existing.description_source === "llm") {
      Modal.confirm({
        title: "重新生成字段描述？",
        content: `字段「${col.name}」已存在 LLM 推断描述，重新生成将覆盖当前内容，且需要数秒等待。`,
        okText: "确认重新生成",
        cancelText: "取消",
        onOk: () => doInfer(true),
      });
      return;
    }
    doInfer(false);
  }

  async function handleBatchInfer() {
    if (!detail) return;
    const res = await inferDescriptions(detail.id);
    message.success(
      `批量推断完成：成功 ${res.inferred.length}，跳过 ${res.skipped.length}，失败 ${res.failed.length}`,
    );
    await refreshDetail();
  }

  async function handleTableDescSave() {
    if (!detail || !tableDescDraft.trim()) return;
    setTableDescSaving(true);
    try {
      await updateTableDescription(detail.id, tableDescDraft.trim());
      message.success("表级描述已保存");
      setTableDescEditing(false);
      await refreshDetail();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存表描述失败");
    } finally {
      setTableDescSaving(false);
    }
  }

  function handleTableDescInfer() {
    if (!detail) return;
    const doInfer = (force: boolean) => {
      const p = runInflight(`table:${detail.id}`, async () => {
        setTableInferring(true);
        try {
          const fields = Array.isArray(detail.schema_summary)
            ? detail.schema_summary.map((c) => ({ name: c.name, type: c.type }))
            : [];
          await inferTableDescription(detail.id, fields, force);
          message.success("表级描述已生成");
          await refreshDetail();
        } catch (err) {
          message.error(err instanceof Error ? err.message : "推断表描述失败");
        } finally {
          setTableInferring(false);
        }
      });
      if (p === null) message.info("该表的 LLM 推断正在进行中，请稍候");
    };
    // 去重防线：已有 LLM 推断描述时不直接重复调 LLM，先确认是否重新生成
    if (detail.description && detail.description_source === "llm") {
      Modal.confirm({
        title: "重新生成表级描述？",
        content: "该表已存在 LLM 推断描述，重新生成将覆盖当前内容，且需要数秒等待。",
        okText: "确认重新生成",
        cancelText: "取消",
        onOk: () => doInfer(true),
      });
      return;
    }
    doInfer(false);
  }

  async function openMetric(code: string) {
    setMetricOpen(true);
    setMetricLoading(true);
    setMetricData(null);
    setMetricDescEditing(false);
    setMetricQueryRows(null);
    setMetricQueryMeta(null);
    setMetricSnapshotBlocked(null);
    try {
      const m = await getMetric(code);
      setMetricData(m);
      // 不可消费指标（未发布/废弃且非管理员）不发注定 403 的快照请求
      const blocked =
        m.status === "DRAFT" || m.status === "REVIEW"
          ? `指标当前为「${m.status === "DRAFT" ? "草稿" : "审核中"}」状态，未发布，暂无消费快照`
          : m.status === "DEPRECATED" && !isAdmin
            ? "指标已废弃（DEPRECATED），仅平台管理员可审计回溯历史快照"
            : null;
      if (blocked) {
        setMetricSnapshotBlocked(blocked);
        setMetricSnapshots([]);
        return;
      }
      const snaps = await listSnapshots(code, 50).catch(() => [] as SnapshotResponse[]);
      setMetricSnapshots(snaps);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载指标详情失败");
    } finally {
      setMetricLoading(false);
    }
  }

  async function handleQueryLatest() {
    if (!metricData) return;
    const code = metricData.metric_code;
    setMetricQuerying(true);
    try {
      // 真实执行指标口径（OLAP 优先 / MySQL 降级），后端自动落 WORM 快照
      const res = await queryMetricInternal(code, { dimensions: [], date_range: "" });
      const rows = Array.isArray(res.data?.rows)
        ? (res.data.rows as Record<string, unknown>[])
        : null;
      setMetricQueryRows(rows);
      setMetricQueryMeta(
        res.data?.engine
          ? { engine: String(res.data.engine), total: Number(res.data?.total ?? 0) }
          : null,
      );
      // 刷新快照列表（本次查询已自动落库）
      const snaps = await listSnapshots(code, 50).catch(() => []);
      setMetricSnapshots(snaps);
      message.success(
        `查询完成：${res.data?.total ?? 0} 行 · 引擎 ${res.data?.engine ?? "unknown"}`,
      );
    } catch (err) {
      message.error(err instanceof Error ? err.message : "查询指标失败");
    } finally {
      setMetricQuerying(false);
    }
  }

  async function handleMetricDescSave() {
    if (!metricData || !metricDescDraft.trim()) return;    setMetricDescSaving(true);
    try {
      await updateMetricDescription(metricData.metric_code, metricDescDraft.trim());
      message.success("指标描述已保存");
      setMetricDescEditing(false);
      setMetricData(await getMetric(metricData.metric_code));
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存指标描述失败");
    } finally {
      setMetricDescSaving(false);
    }
  }

  async function doMetricDescInfer(force: boolean) {
    if (!metricData) return;
    setMetricInferring(true);
    setInferElapsed(0);
    try {
      const updated = await inferMetricDescription(metricData.metric_code, { force });
      message.success(force ? "指标描述已重新生成" : "指标描述已通过 AI 推断生成");
      setMetricData(updated);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "AI 推断指标描述失败");
    } finally {
      setMetricInferring(false);
      setInferElapsed(0);
    }
  }

  function handleMetricDescInfer() {
    if (!metricData) return;
    // 去重防线：已有 LLM 推断描述时不直接重复调 LLM，先确认是否重新生成（耗时数秒）
    if (metricData.description_source === "llm" && metricData.description) {
      Modal.confirm({
        title: "重新生成指标描述？",
        content: "该指标已存在 LLM 推断描述，重新生成将覆盖当前内容，且需要数秒等待。",
        okText: "确认重新生成",
        cancelText: "取消",
        onOk: () => doMetricDescInfer(true),
      });
      return;
    }
    void doMetricDescInfer(false);
  }

  async function handleTableNodeClick(node: AssetGraphNode) {
    if (node.entity_id != null) {
      openDetail(node.entity_id);
      return;
    }
    // Neo4j 图谱路径表节点可能不带 entity_id：按表名回查采集目录，命中则打开详情
    const entityName = node.id.startsWith("table:")
      ? node.id.slice("table:".length)
      : node.label;
    try {
      const res = await listCatalogs({ keyword: entityName, page_size: 20 });
      const hit = res.items.find((it) => it.entity_name === entityName && it.id != null);
      if (hit?.id != null) {
        openDetail(hit.id);
        return;
      }
    } catch {
      // 查询失败落空态，引导前往采集目录
    }
    Modal.confirm({
      title: "未找到该表详情",
      content: `「${entityName}」未在元数据目录中找到（可能尚未采集或数据源已删除）。是否前往采集目录查看？`,
      okText: "前往采集目录",
      cancelText: "取消",
      onOk: () =>
        navigate(
          `/catalogs?kw=${encodeURIComponent(entityName)}&focus=${encodeURIComponent(entityName)}&from=资产地图`,
        ),
    });
  }

  function handleNodeClick(node: AssetGraphNode) {
    if (node.type === "metric") {
      // 本页打开指标详情抽屉（明细 + 补充描述），用户可再决定是否跳转指标详情
      openMetric(node.label);
      return;
    }
    if (node.type === "table") {
      // 表/视图：优先 entity_id 直达详情；缺失时回查目录，再不行引导去采集目录
      void handleTableNodeClick(node);
      return;
    }
    if (node.entity_id != null) {
      openDetail(node.entity_id);
      return;
    }
    if (node.type === "field") {
      // field:{table}.{col} → 推导所属表节点，提供表详情入口（无 entity_id 时展示字段信息）
      const tableId = `table:${node.id.slice("field:".length).split(".").slice(0, -1).join(".")}`;
      const tableNode = graphData?.nodes.find((n) => n.id === tableId) ?? null;
      setFieldNode(node);
      setFieldTableNode(tableNode);
      return;
    }
    message.info(`节点「${node.label}」暂不支持查看详情`);
  }

  if (loading && !graphData) return <Spin tip="加载图谱数据…" />;
  if (error) return <Alert type="error" message={error} />;
  if (!graphData) return <Empty description="暂无图谱数据" />;

  const domainOptions = [...new Set(graphData.nodes.map((n) => n.domain).filter(Boolean))].map(
    (d) => ({
      label: d,
      value: d,
    }),
  );

  const detailHasPii =
    Boolean(detail?.pii_flag) || (detail?.sensitivity_level ?? "").includes("PII");

  const snapshotColumns: ColumnsType<SnapshotResponse> = [
    { title: "周期", dataIndex: "date_range", key: "date_range", width: 150 },
    {
      title: "维度",
      dataIndex: "dims",
      key: "dims",
      width: 160,
      render: (v: Record<string, unknown>) =>
        v && typeof v === "object" && Object.keys(v).length ? JSON.stringify(v) : "—",
    },
    {
      title: "数值",
      dataIndex: "value_json",
      key: "value_json",
      render: (v: Record<string, unknown>) =>
        v && typeof v === "object" && Object.keys(v).length ? JSON.stringify(v) : "—",
    },
    {
      title: "质量",
      dataIndex: "quality_flag",
      key: "quality_flag",
      width: 80,
      render: (v: string | null) => v ?? "—",
    },
    {
      title: "生成时间",
      dataIndex: "generated_at",
      key: "generated_at",
      width: 170,
      render: (v: string) => formatCnTime(v),
    },
  ];

  return (
    <div>
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }} align="middle">
        <Col>
          <span className="muted">来源：</span>
          <Select showSearch
            style={{ width: 210 }}
            value={graphSource}
            onChange={setGraphSource}
            options={GRAPH_SOURCE_OPTIONS}
          />
        </Col>
        <Col>
          <Tooltip title="要查看完整血缘（含 DP/SQL/指标通道、手动登记、覆盖率治理、数仓分层泳道），请前往「血缘与影响」视图">
            <Button type="link" size="small" onClick={() => navigate("/lineage")}>
              完整血缘图谱 →
            </Button>
          </Tooltip>
        </Col>
        <Col>
          <span className="muted">域筛选：</span>
          <Select showSearch
            allowClear
            placeholder="全部域"
            style={{ width: 200 }}
            value={domain}
            onChange={setDomain}
            options={domainOptions}
          />
        </Col>
        <Col>
          <span className="muted">仅 PII：</span>
          <Switch checked={piiOnly} onChange={setPiiOnly} />
        </Col>
        <Col>
          <Statistic title="节点数" value={graphData.nodes.length} valueStyle={{ fontSize: 22 }} />
        </Col>
        <Col>
          <Statistic title="边数" value={graphData.edges.length} valueStyle={{ fontSize: 22 }} />
        </Col>
      </Row>

      <Card title="资产地图" size="small">
        <AssetGraph
          nodes={graphData.nodes}
          edges={graphData.edges}
          height={620}
          onNodeClick={handleNodeClick}
          showFields={false}
          layout="auto"
          lanes
        />
      </Card>

      {/* 指标详情抽屉：明细 + 补充描述（TD §12.1） */}
      <ResizableDrawer
        title={
          metricData
            ? `指标详情：${metricData.name}（${metricData.metric_code}）`
            : "指标详情"
        }
        open={metricOpen}
        onClose={() => setMetricOpen(false)}
        storageKey="unisense.drawer.metric.width"
        defaultWidth={880}
        minWidth={600}
      >
        {metricLoading ? (
          <Spin tip="加载指标详情…" />
        ) : metricData ? (
          <>
            <Card size="small" title="业务描述" style={{ marginBottom: 16 }}>
              {metricDescEditing ? (
                <>
                  <Input.TextArea
                    rows={3}
                    value={metricDescDraft}
                    onChange={(e) => setMetricDescDraft(e.target.value)}
                    placeholder="补充指标的业务含义、使用场景、注意事项…"
                  />
                  <Space style={{ marginTop: 8 }}>
                    <Button
                      type="primary"
                      size="small"
                      loading={metricDescSaving}
                      onClick={handleMetricDescSave}
                    >
                      保存
                    </Button>
                    <Button
                      size="small"
                      onClick={() => {
                        setMetricDescEditing(false);
                        setMetricDescDraft(metricData.description ?? "");
                      }}
                    >
                      取消
                    </Button>
                  </Space>
                </>
              ) : (
                <>
                  <div style={{ whiteSpace: "pre-wrap" }}>
                    {metricData.description || (
                      <span className="muted">暂无描述，点击下方按钮补充</span>
                    )}
                  </div>
                  <Space size={8} style={{ marginTop: 8 }}>
                    {canEditMetric && (
                      <Button
                        size="small"
                        icon={<EditOutlined />}
                        onClick={() => {
                          setMetricDescEditing(true);
                          setMetricDescDraft(metricData.description ?? "");
                        }}
                      >
                        补充描述
                      </Button>
                    )}
                    {canInferMetric && (
                      <Button
                        size="small"
                        icon={<ThunderboltOutlined />}
                        loading={metricInferring}
                        onClick={handleMetricDescInfer}
                      >
                        {metricInferring
                          ? `AI 推断中… ${inferElapsed}s`
                          : metricData.description_source === "llm"
                            ? "重新生成"
                            : "AI 推断"}
                      </Button>
                    )}
                    {descriptionSourceTag(metricData.description_source)}
                    {metricData.description_updated_at ? (
                      <span className="muted" style={{ fontSize: 12 }}>
                        更新于 {formatCnTime(metricData.description_updated_at)}
                      </span>
                    ) : null}
                  </Space>
                </>
              )}
            </Card>

            <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
              <Descriptions.Item label="指标名称">{metricData.name}</Descriptions.Item>
              <Descriptions.Item label="指标编码">{metricData.metric_code}</Descriptions.Item>
              <Descriptions.Item label="所属域">{metricData.domain}</Descriptions.Item>
              <Descriptions.Item label="类型">{METRIC_TYPE_LABEL[metricData.type ?? ""] ?? metricData.type ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="粒度">{GRANULARITY_LABEL[metricData.granularity ?? ""] ?? metricData.granularity ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="单位">{UNIT_LABEL[metricData.unit ?? ""] ?? metricData.unit ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="聚合方式">{AGG_LABEL[metricData.aggregation ?? ""] ?? metricData.aggregation ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="时间语义">{TIME_SEM_LABEL[metricData.time_semantics ?? ""] ?? metricData.time_semantics ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="新鲜度">{FRESHNESS_LABEL[metricData.freshness ?? ""] ?? metricData.freshness ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="数仓层">{DW_LAYER_LABEL[metricData.dw_layer ?? ""] ?? metricData.dw_layer ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="指标分级">{METRIC_TIER_LABEL[metricData.metric_tier ?? ""] ?? metricData.metric_tier ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="状态">{STATUS_LABEL[metricData.status ?? ""] ?? metricData.status ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="可加性">{ADDITIVITY_LABEL[metricData.additivity ?? ""] ?? metricData.additivity ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="PII">
                {metricData.pii_flag ? <Tag color="red">PII</Tag> : "否"}
              </Descriptions.Item>
              <Descriptions.Item label="版本">{metricData.version}</Descriptions.Item>
              <Descriptions.Item label="Owner ID">{metricData.owner_id}</Descriptions.Item>
            </Descriptions>

            <Card size="small" title="口径明细" style={{ marginBottom: 16 }}>
              <DefinitionsDetail def={metricData.definition_json} />
            </Card>

            <Card
              size="small"
              title="数值快照"
              extra={
                <Space>
                  <Button
                    type="primary"
                    size="small"
                    loading={metricQuerying}
                    disabled={!canQuery}
                    onClick={handleQueryLatest}
                  >
                    查询最新数据
                  </Button>
                  <Button
                    type="link"
                    size="small"
                    onClick={() => navigate(`/detail/${encodeURIComponent(metricData.metric_code)}`)}
                  >
                    前往指标详情 →
                  </Button>
                </Space>
              }
            >
              {metricQueryRows !== null ? (
                <Card
                  size="small"
                  type="inner"
                  title={`本次查询结果（${metricQueryRows.length} 行 · 引擎 ${metricQueryMeta?.engine ?? "unknown"}）`}
                  style={{ marginBottom: 16 }}
                >
                  {metricQueryRows.length === 0 ? (
                    <Empty description="查询无数据（该口径在所选范围无匹配行）" />
                  ) : (
                    <Table
                      size="small"
                      rowKey={(_, i) => String(i ?? 0)}
                      dataSource={metricQueryRows}
                      columns={Object.keys(metricQueryRows[0] ?? {}).map((key) => ({
                        title: key,
                        dataIndex: key,
                        key,
                        ellipsis: true,
                      }))}
                      pagination={{ pageSize: 5, showSizeChanger: false }}
                    />
                  )}
                </Card>
              ) : null}
              {metricSnapshotBlocked ? (
                <Alert
                  type="warning"
                  showIcon
                  message="该指标消费快照当前不可查看"
                  description={`${metricSnapshotBlocked}。指标发布（PUBLISHED）后即可在消费侧查看历史快照。`}
                />
              ) : metricSnapshots.length === 0 ? (
                <Empty description="暂无查询快照（点击「查询最新数据」即刻生成）" />
              ) : (
                <Table
                  size="small"
                  rowKey="id"
                  dataSource={metricSnapshots}
                  columns={snapshotColumns}
                  pagination={false}
                />
              )}
            </Card>
          </>
        ) : null}
      </ResizableDrawer>

       <ResizableDrawer
         title={detail ? `实体详情：${detail.entity_name}` : "实体详情"}
         open={detailOpen}
         onClose={() => setDetailOpen(false)}
         storageKey="unisense.drawer.entity.width"
         defaultWidth={860}
         minWidth={600}
       >
        {detailLoading ? (
          <Spin tip="加载实体详情…" />
        ) : detail ? (
          <>
            <Descriptions column={2} bordered size="small">
              <Descriptions.Item label="实体名称">{detail.entity_name}</Descriptions.Item>
              <Descriptions.Item label="实体类型">{detail.entity_type}</Descriptions.Item>
              <Descriptions.Item label="数据源">
                {detail.source_name ? `${detail.source_name}（${detail.source_id}）` : detail.source_id}
              </Descriptions.Item>
              <Descriptions.Item label="业务域">
                {detail.domain ?? <span className="muted">-</span>}
              </Descriptions.Item>
              <Descriptions.Item label="敏感度">
                {sensitivityTag(detail.sensitivity_level)}
                {detailHasPii && (
                  <Tag color="red" style={{ marginLeft: 8 }}>
                    含 PII
                  </Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="责任人">
                {detail.owner_id != null
                  ? detail.owner_name || `#${detail.owner_id}`
                  : <Tag>无</Tag>}
              </Descriptions.Item>
              <Descriptions.Item label="字段数">
                {detail.column_count ?? <span className="muted">-</span>}
              </Descriptions.Item>
              <Descriptions.Item label="Schema 状态">
                {detail.schema_incomplete ? (
                  <Tag color="orange">不完整</Tag>
                ) : (
                  <Tag color="green">完整</Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="表级描述">
                {tableDescEditing ? (
                  <Space.Compact style={{ width: "100%" }}>
                    <Input.TextArea
                      value={tableDescDraft}
                      onChange={(e) => setTableDescDraft(e.target.value)}
                      autoSize={{ minRows: 1, maxRows: 3 }}
                      disabled={tableDescSaving}
                      style={{ flex: 1 }}
                    />
                    <Button
                      type="primary"
                      icon={<CheckOutlined />}
                      loading={tableDescSaving}
                      onClick={handleTableDescSave}
                    />
                    <Button
                      icon={<CloseOutlined />}
                      disabled={tableDescSaving}
                      onClick={() => setTableDescEditing(false)}
                    />
                  </Space.Compact>
                ) : (
                  <Space direction="vertical" style={{ width: "100%" }}>
                    <Space size={4} wrap>
                      {detail.description ? (
                        <span>{detail.description}</span>
                      ) : (
                        <span className="muted" style={{ fontStyle: "italic" }}>
                          暂无表级描述
                        </span>
                      )}
                      {descriptionSourceTag(detail.description_source)}
                    </Space>
                    <Space>
                      {canEditDesc && (
                        <Tooltip title="编辑表级描述">
                          <Button
                            size="small"
                            icon={<EditOutlined />}
                            onClick={() => {
                              setTableDescDraft(detail.description ?? "");
                              setTableDescEditing(true);
                            }}
                          >
                            编辑
                          </Button>
                        </Tooltip>
                      )}
                      {canInferCatalog && (
                        <Tooltip title="LLM 推断表级描述">
                          <Button
                            size="small"
                            icon={<ThunderboltOutlined />}
                            loading={tableInferring}
                            onClick={handleTableDescInfer}
                          >
                            推断
                          </Button>
                        </Tooltip>
                      )}
                    </Space>
                  </Space>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="字段描述">
                <SchemaTable
                  columns={Array.isArray(detail.schema_summary) ? detail.schema_summary : []}
                  editable={canEditDesc}
                  inferable
                  canInfer={canInferCatalog}
                  onEdit={handleFieldEdit}
                  onInfer={handleFieldInfer}
                  onBatchInfer={handleBatchInfer}
                  sampleRows={detail.sample_rows ?? []}
                />
              </Descriptions.Item>
              <Descriptions.Item label="源健康">
                {detail.source_health ? (
                  <Tag
                    color={
                      detail.source_health.health_status === "healthy"
                        ? "green"
                        : detail.source_health.health_status === "unhealthy"
                          ? "red"
                          : "default"
                    }
                  >
                    {SOURCE_HEALTH_LABEL[detail.source_health.health_status] ??
                      detail.source_health.health_status}
                  </Tag>
                ) : (
                  <span className="muted">未知</span>
                )}
              </Descriptions.Item>
            </Descriptions>
            {detail.entity_type === "TABLE" && canDeprecate && (
              <Space style={{ marginTop: 12 }} wrap>
                <Popconfirm
                  title={`废弃资产「${detail.entity_name}」？`}
                  description="废弃后该资产不再作为可消费目录，血缘边随之失效；可在采集目录重新采集恢复。"
                  okText="确认废弃"
                  okButtonProps={{ danger: true }}
                  onConfirm={async () => {
                    setDeprecating(true);
                    try {
                      await deprecateEntity(detail.source_id, detail.entity_name);
                      message.success(`已废弃 ${detail.entity_name}`);
                      setDetailOpen(false);
                      setDetail(null);
                      loadGraph();
                    } catch (err) {
                      message.error(err instanceof Error ? err.message : "废弃失败");
                    } finally {
                      setDeprecating(false);
                    }
                  }}
                >
                  <Button danger icon={<DeleteOutlined />} loading={deprecating}>
                    废弃此资产
                  </Button>
                </Popconfirm>
              </Space>
            )}
            {(detail.lineage_edges?.length ?? 0) > 0 && (
              <EntityLineageCard edges={detail.lineage_edges!} />
            )}
          </>
        ) : null}
      </ResizableDrawer>

      <ResizableDrawer
        title="字段信息"
        open={fieldNode != null}
        onClose={() => setFieldNode(null)}
        storageKey="unisense.drawer.field.width"
        defaultWidth={520}
        minWidth={460}
      >
        {fieldNode && (
          <>
            <Descriptions column={2} bordered size="small">
              <Descriptions.Item label="字段名">{fieldNode.label}</Descriptions.Item>
              <Descriptions.Item label="类型">字段</Descriptions.Item>
              <Descriptions.Item label="所属表">
                {fieldTableNode?.label ?? (
                  <span className="muted">不在当前视图，可用「资产搜索」查找</span>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="业务域">
                {fieldNode.domain ?? <span className="muted">-</span>}
              </Descriptions.Item>
              <Descriptions.Item label="PII">
                {fieldNode.pii ? <Tag color="red">含 PII</Tag> : <Tag>否</Tag>}
              </Descriptions.Item>
            </Descriptions>
            {fieldTableNode?.entity_id != null && (
              <Button
                type="primary"
                style={{ marginTop: 16 }}
                onClick={() => {
                  setFieldNode(null);
                  openDetail(fieldTableNode.entity_id as number);
                }}
              >
                查看所属表详情
              </Button>
            )}
          </>
        )}
      </ResizableDrawer>
    </div>
  );
}

// 敏感级 → 对应可视化色值（PII 红、机密橙、内部蓝、公开绿、待复核紫）
const SENSITIVITY_CHART_COLOR: Record<string, string> = {
  PII: "#f5222d",
  CONFIDENTIAL: "#fa8c16",
  NEEDS_REVIEW: "#722ed1",
  INTERNAL: "#1677ff",
  PUBLIC: "#52c41a",
  UNKNOWN: "#bfbfbf",
};

function HeatmapTab() {
  // 双视角：catalog=目录资产（按敏感级分布）/ metric=指标资产（按 PII/内部分布）
  const [assetType, setAssetType] = useState<"catalog" | "metric">("catalog");
  const [matrix, setMatrix] = useState<AssetHeatmapMatrix | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 业务域 code → 中文名映射（后端仅返回 code，展示需中文）
  const [domainNames, setDomainNames] = useState<Record<string, string>>({});
  // 条形段下钻抽屉（域 × 敏感级 → 双过滤明细）
  const [drillOpen, setDrillOpen] = useState(false);
  const [drillLoading, setDrillLoading] = useState(false);
  const [drillTitle, setDrillTitle] = useState("");
  const [drillRows, setDrillRows] = useState<DrillRow[]>([]);
  const [drillTotal, setDrillTotal] = useState<number | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    // 加载域中文名映射（失败不阻塞热力分布，仅回退显示 code）
    listDomainTree()
      .then((tree) => {
        const map: Record<string, string> = {};
        const walk = (nodes: SubjectDomainTreeNode[]) => {
          for (const n of nodes) {
            map[n.code] = n.name;
            if (n.children?.length) walk(n.children);
          }
        };
        walk(tree);
        if (!cancelled) setDomainNames(map);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  // 域 code → 中文名（无映射时回退显示 code）
  const domainLabel = (code: string) => domainNames[code] ?? code;

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const m = await fetchAssetHeatmapMatrix(assetType);
        if (!cancelled) setMatrix(m);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "加载热力分布失败");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    setMatrix(null);
    load();
    return () => {
      cancelled = true;
    };
  }, [assetType]);

  // 条形段下钻：目录视角=域+敏感度双过滤；指标视角=域+PII 过滤
  async function openBarDrill(sensKey: string, domain: string) {
    const sensLabel =
      assetType === "metric"
        ? sensKey === "PII"
          ? "PII"
          : "内部"
        : (SENSITIVITY_LABEL[sensKey] ?? sensKey);
    setDrillTitle(`${domainLabel(domain)} · ${sensLabel} 资产明细`);
    setDrillOpen(true);
    setDrillLoading(true);
    setDrillRows([]);
    setDrillTotal(undefined);
    try {
      if (assetType === "metric") {
        const r = await listMetrics({ domain, pii_flag: sensKey === "PII", page_size: 200 });
        setDrillRows(r.items as unknown as DrillRow[]);
        setDrillTotal(r.total);
      } else {
        const r = await listCatalogs({ sensitivity_level: sensKey, domain, page_size: 200 });
        setDrillRows(r.items as unknown as DrillRow[]);
        setDrillTotal(r.total);
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载明细失败");
    } finally {
      setDrillLoading(false);
    }
  }

  if (loading) return <Spin />;
  if (error) return <Alert type="error" message={error} />;
  if (!matrix) return <Empty description="暂无分布数据" />;

  const isMetric = assetType === "metric";
  // 按域汇总：每域一个总计数
  const domainTotals: Record<string, number> = {};
  // 展平数据为堆积条形图所需格式：每行 = {domain, sensitivity, count, piiCount}
  const barData = matrix.cells
    .filter((c) => c.count > 0)
    .map((c) => {
      domainTotals[c.domain] = (domainTotals[c.domain] || 0) + c.count;
      return {
        domain: c.domain,
        sensitivity: isMetric
          ? c.sensitivity === "PII"
            ? "PII"
            : "内部"
          : (SENSITIVITY_LABEL[c.sensitivity] ?? c.sensitivity),
        sensKey: c.sensitivity,
        count: c.count,
        piiCount: c.pii_count,
      };
    });
  // 按域排序（总量从高到低）
  const domainOrder = [...new Set(barData.map((d) => d.domain))].sort(
    (a, b) => (domainTotals[b] ?? 0) - (domainTotals[a] ?? 0),
  );
  const totalCount = Object.values(domainTotals).reduce((a, b) => a + b, 0);
  const piiTotal = barData.filter((d) => d.sensKey === "PII").reduce((a, c) => a + c.count, 0);
  // 动态高度：每域 40px + 上下 padding
  const chartHeight = Math.max(320, domainOrder.length * 40 + 80);

  // 敏感级 → 颜色映射（指标视角只用 PII + 内部）
  const colorMap: Record<string, string> = {};
  if (isMetric) {
    colorMap["PII"] = "#f5222d";
    colorMap["内部"] = "#1677ff";
  } else {
    for (const k of Object.keys(SENSITIVITY_CHART_COLOR)) {
      colorMap[SENSITIVITY_LABEL[k] ?? k] = SENSITIVITY_CHART_COLOR[k];
    }
  }

  return (
    <div>
      <Card
        title={
          isMetric
            ? "指标资产分布（业务域 × PII/内部）"
            : "目录资产分布（业务域 × 敏感级别）"
        }
        size="small"
        extra={
          <Space wrap>
            <Segmented
              size="small"
              value={assetType}
              onChange={(v) => setAssetType(v as "catalog" | "metric")}
              options={[
                { label: "目录资产", value: "catalog" },
                { label: "指标资产", value: "metric" },
              ]}
            />
            <span className="muted">
              共 {totalCount} 项 · PII {piiTotal} 项 · 点击色段查看明细
            </span>
          </Space>
        }
      >
        {barData.length === 0 ? (
          <Empty description="暂无分布数据" />
        ) : (
          <Bar
            data={barData}
            yField="domain"
            xField="count"
            colorField="sensitivity"
            seriesField="sensitivity"
            isStack={true}
            height={chartHeight}
            sort={{ y: domainOrder }}
            color={({ sensitivity }: { sensitivity: string }) => colorMap[sensitivity] ?? "#bfbfbf"}
            label={{
              position: "inside",
              formatter: (d: { count: number }) => (d.count > 0 ? String(d.count) : ""),
              style: { fontSize: 11, fill: "#fff" },
            }}
            legend={{
              color: { title: "敏感级别" },
            }}
            axis={{
              y: {
                title: "业务域",
                label: {
                  formatter: (v: string) => {
                    const name = domainLabel(v);
                    return name.length > 12 ? `${name.slice(0, 12)}…` : name;
                  },
                },
              },
              x: {
                title: "资产数量",
              },
            }}
            tooltip={{
              title: (d: { domain: string; sensitivity: string }) =>
                `${domainLabel(d.domain)} · ${d.sensitivity}`,
              items: [
                (d: { count: number }) => ({ name: "资产数", value: d.count }),
                (d: { piiCount: number; sensKey: string }) =>
                  d.sensKey === "PII" ? { name: "含 PII", value: d.piiCount } : null,
              ],
            }}
            interactions={[{ type: "element-active" }] as any}
            onReady={(plot) => {
              // G2Plot Bar 的 element:click 事件中 evt.data 即原始数据行；
              // 兼容个别版本嵌套 { data: { ... } } 的结构
              plot.on(
                "element:click",
                (evt: {
                  data?: { sensKey?: string; domain?: string; data?: { sensKey?: string; domain?: string } };
                }) => {
                  const row = evt?.data?.data ?? evt?.data;
                  const key = row?.sensKey;
                  const domain = row?.domain;
                  if (key && domain) openBarDrill(key, domain);
                },
              );
            }}
          />
        )}
      </Card>
      <DrillDownDrawer
        open={drillOpen}
        title={drillTitle}
        columns={isMetric ? METRIC_COLUMNS : CATALOG_COLUMNS}
        rows={drillRows}
        loading={drillLoading}
        total={drillTotal}
        onClose={() => setDrillOpen(false)}
      />
    </div>
  );
}

function OwnerTab() {
  const [ownerId, setOwnerId] = useState<number | undefined>(undefined);
  const [ownerIds, setOwnerIds] = useState<number[]>([]);
  // 责任人真实姓名映射：useUserNames 按已知 id 跨组织精确解析（display_name 优先，回退 username）。
  // 与 /auth/users（本组织列表）不同，跨组织 owner 也能解析出真实中文名，不再退化为「未知用户」。
  const ownerNames = useUserNames(ownerIds);
  const ownerOptions = useMemo(
    () => ownerIds.map((id) => ({ label: ownerNames[id]?.display_name || ownerNames[id]?.username || "未知用户", value: id })),
    [ownerIds, ownerNames],
  );
  const [view, setView] = useState<AssetOwnerView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 指标统计值下钻抽屉（点击数字 → 该口径的指标明细）
  const [drillOpen, setDrillOpen] = useState(false);
  const [drillLoading, setDrillLoading] = useState(false);
  const [drillTitle, setDrillTitle] = useState("");
  const [drillRows, setDrillRows] = useState<DrillRow[]>([]);
  const [drillTotal, setDrillTotal] = useState<number | undefined>(undefined);
  // 目录资产行点击 → 实体详情抽屉
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detail, setDetail] = useState<AssetEntityDetail | null>(null);
  // 单实体废弃（资产地图「手动下线」治理入口）
  const [deprecating, setDeprecating] = useState(false);
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.owner.catalogs.pageSize", 10);
  const canDeprecate = usePermission().can("catalog:deprecate");

  useEffect(() => {
    fetchAssetGraph({ depth: 1 })
      .then((g) => {
        const owners = [...new Set(g.nodes.map((n) => n.owner).filter(Boolean))].map((o) =>
          Number(o),
        );
        if (owners.length > 0) {
          setOwnerIds(owners);
          setOwnerId((prev) => prev ?? owners[0]);
        } else {
          // 图谱暂无责任人信息时，不伪造责任人（此前回退「责任人 #1」会指向不存在
          // 的用户，选中后 fetchAssetOwnerView(1) 必然报错/空白）——保持空选项，
          // 展示「从图谱提取责任人…」占位，待资产接入 owner 后可下拉选择。
          setOwnerIds([]);
          setOwnerId(undefined);
        }
      })
      .catch(() => {});
    // 责任人真实姓名：由 useUserNames(ownerIds) 跨组织精确解析，此处无需再拉本组织列表
  }, []);

  useEffect(() => {
    if (ownerId === undefined) return;
    setLoading(true);
    setError(null);
    fetchAssetOwnerView(ownerId)
      .then(setView)
      .catch((err) => setError(err instanceof Error ? err.message : "加载责任人视图失败"))
      .finally(() => setLoading(false));
  }, [ownerId]);

  // 按口径下钻该责任人的指标明细（status / domain / PII 组合过滤）
  async function drillMetrics(opts?: { status?: string; domain?: string; piiFlag?: boolean }) {
    const parts = [
      opts?.domain ? `域：${opts.domain}` : null,
      opts?.status ? `状态：${opts.status}` : null,
      opts?.piiFlag ? "PII" : null,
    ]
      .filter(Boolean)
      .join(" · ");
    const ownerLabel =
      ownerId != null ? (view?.owner_name ?? (ownerNames[ownerId]?.display_name || ownerNames[ownerId]?.username) ?? "未知用户") : "责任人";
    setDrillTitle(`${ownerLabel} 指标明细${parts ? `（${parts}）` : ""}`);
    setDrillOpen(true);
    setDrillLoading(true);
    setDrillRows([]);
    setDrillTotal(undefined);
    try {
      const r = await listMetrics({
        owner_id: ownerId,
        ...(opts?.status ? { status: opts.status } : {}),
        ...(opts?.domain ? { domain: opts.domain } : {}),
        ...(opts?.piiFlag !== undefined ? { pii_flag: opts.piiFlag } : {}),
        page_size: 200,
      });
      setDrillRows(r.items as unknown as DrillRow[]);
      setDrillTotal(r.total);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载指标明细失败");
    } finally {
      setDrillLoading(false);
    }
  }

  async function openDetail(entityId: number) {
    setDetailOpen(true);
    setDetailLoading(true);
    setDetail(null);
    try {
      setDetail(await fetchAssetEntityDetail(entityId));
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载实体详情失败");
    } finally {
      setDetailLoading(false);
    }
  }

  // 可点击值渲染：把 Statistic 的 value 包成可点击链接（对齐 OverviewTab 模式）
  function clickableValue(onClick: () => void) {
    return (node: ReactNode) => (
      <a
        href="#"
        onClick={(e) => {
          e.preventDefault();
          onClick();
        }}
        style={{ cursor: "pointer" }}
      >
        {node}
      </a>
    );
  }

  const ownerName =
    ownerId != null ? (view?.owner_name ?? (ownerNames[ownerId]?.display_name || ownerNames[ownerId]?.username) ?? "未知用户") : "未指定";
  const total = view?.metrics.total ?? 0;
  const published = view?.metrics.published ?? 0;
  const draft = view?.metrics.draft ?? 0;
  const publishedPct = total > 0 ? Math.round((published / total) * 100) : 0;
  const catalogCount = view?.catalogs.items?.length ?? 0;

  // 分布图数据（域=横向条形 / 类型=环形）
  const domainBarData = Object.entries(view?.metrics.by_domain ?? {}).map(([k, v]) => ({
    domain: k,
    count: v,
  }));
  const typePieData = Object.entries(view?.metrics.by_type ?? {}).map(([k, v]) => ({
    type: METRIC_TYPE_LABEL[k] ?? k,
    value: v,
  }));

  return (
    <div>
      <Card
        title="责任人视图"
        size="small"
        extra={
          ownerOptions.length > 0 ? (
            <Select showSearch
              style={{ width: 180 }}
              value={ownerId}
              onChange={setOwnerId}
              options={ownerOptions}
            />
          ) : (
            <span className="muted">从图谱提取责任人…</span>
          )
        }
      >
        {loading ? (
          <Spin />
        ) : error ? (
          <Alert type="error" message={error} />
        ) : !view ? (
          <Empty description="请选择责任人" />
        ) : (
          <>
            {/* 责任人档案卡：头像 + 姓名 + 角色/域 + 概览 */}
            <Card size="small" style={{ marginBottom: 16, background: "var(--bg-elevated, #fafafa)" }}>
              <Space align="center" size={16} wrap>
                <Avatar size={48} style={{ background: "#1976d2" }}>
                  {(ownerName ?? "?").charAt(0).toUpperCase()}
                </Avatar>
                <div>
                  <div style={{ fontSize: 18, fontWeight: 600 }}>{ownerName}</div>
                  <Space size={8} wrap>
                    {view.role && <Tag color="blue">{ROLE_LABEL[view.role] ?? view.role}</Tag>}
                    {view.domain && <Tag>{view.domain}</Tag>}
                    <span className="muted" style={{ fontSize: 12 }}>
                      责任人 ID：{view.owner_id}
                    </span>
                  </Space>
                </div>
                <Divider type="vertical" />
                <Statistic title="总指标" value={total} />
                <Statistic title="总目录资产" value={catalogCount} />
                <Statistic title="快照覆盖" value={view.metrics.snapshot_covered} suffix={`/ ${total}`} />
              </Space>
            </Card>

            {/* 4 指标大卡 */}
            <Row gutter={[16, 16]}>
              <Col span={6}>
                <Card size="small">
                  <Statistic
                    title="指标总数"
                    value={total}
                    valueRender={clickableValue(() => drillMetrics())}
                  />
                </Card>
              </Col>
              <Col span={6}>
                <Card size="small">
                  <Statistic
                    title="已发布"
                    value={published}
                    valueStyle={{ color: "#2e9e5b" }}
                    valueRender={clickableValue(() => drillMetrics({ status: "PUBLISHED" }))}
                  />
                  <Progress percent={publishedPct} size="small" strokeColor="#2e9e5b" />
                </Card>
              </Col>
              <Col span={6}>
                <Card size="small">
                  <Statistic
                    title="草稿"
                    value={draft}
                    valueRender={clickableValue(() => drillMetrics({ status: "DRAFT" }))}
                  />
                  <Progress
                    percent={total > 0 ? Math.round((draft / total) * 100) : 0}
                    size="small"
                    strokeColor="#fa8c16"
                  />
                </Card>
              </Col>
              <Col span={6}>
                <Card size="small">
                  <Statistic
                    title="PII 指标"
                    value={view.metrics.pii_count}
                    valueStyle={{ color: "#d64545" }}
                    valueRender={clickableValue(() => drillMetrics({ piiFlag: true }))}
                  />
                </Card>
              </Col>
            </Row>

            {/* 3 维分布：域条形图 / 类型环形图 / 分级 Tag */}
            <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
              <Col span={8}>
                <Card size="small" title="域分布">
                  {domainBarData.length === 0 ? (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无分布数据" />
                  ) : (
                    <Bar
                      data={domainBarData}
                      xField="count"
                      yField="domain"
                      height={180}
                      color="#1976d2"
                      label={{ position: "right", formatter: (d: { count: number }) => String(d.count) }}
                      interactions={[{ type: "element-active" }] as any}
                      onReady={(plot) => {
                        plot.on(
                          "element:click",
                          (evt: { data?: { data?: { domain?: string } } }) => {
                            const k = evt?.data?.data?.domain;
                            if (k) drillMetrics({ domain: k });
                          },
                        );
                      }}
                    />
                  )}
                </Card>
              </Col>
              <Col span={8}>
                <Card size="small" title="类型分布">
                  {typePieData.length === 0 ? (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无分布数据" />
                  ) : (
                    <Pie
                      data={typePieData}
                      angleField="value"
                      colorField="type"
                      height={180}
                      radius={0.8}
                      label={{ text: "type", position: "outside" }}
                      legend={false}
                    />
                  )}
                </Card>
              </Col>
              <Col span={8}>
                <Card size="small" title="指标分级">
                  {Object.keys(view.metrics.by_metric_tier ?? {}).length === 0 ? (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无分布数据" />
                  ) : (
                    <Space direction="vertical" style={{ width: "100%" }}>
                      {Object.entries(view.metrics.by_metric_tier).map(([k, v]) => (
                        <div key={k} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <Tag color="blue" style={{ minWidth: 40, textAlign: "center" }}>{k}</Tag>
                          <Progress percent={total > 0 ? Math.round((v / total) * 100) : 0} size="small" />
                          <span style={{ minWidth: 24, textAlign: "right" }}>{v}</span>
                        </div>
                      ))}
                    </Space>
                  )}
                </Card>
              </Col>
            </Row>

            {/* 待办区：可钻取 */}
            <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
              <Col span={12}>
                <Card size="small" styles={{ body: { padding: 12 } }}>
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                    }}
                  >
                    <span>PII 未复核</span>
                    <a
                      href="#"
                      onClick={(e) => {
                        e.preventDefault();
                        drillMetrics({ piiFlag: true });
                      }}
                    >
                      {view.metrics.todo?.pii_unreviewed ?? 0} 项
                    </a>
                  </div>
                </Card>
              </Col>
              <Col span={12}>
                <Card size="small" styles={{ body: { padding: 12 } }}>
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                    }}
                  >
                    <span>废弃未替换</span>
                    <a
                      href="#"
                      onClick={(e) => {
                        e.preventDefault();
                        drillMetrics({ status: "DEPRECATED" });
                      }}
                    >
                      {view.metrics.todo?.deprecated_without_successor ?? 0} 项
                    </a>
                  </div>
                </Card>
              </Col>
            </Row>

            {/* 目录资产明细表：分页 + 行点击进详情 */}
            <Card
              title={`目录资产明细（${catalogCount}）`}
              size="small"
              style={{ marginTop: 16 }}
            >
              {catalogCount === 0 ? (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无目录资产" />
              ) : (
                <Table
                  dataSource={view.catalogs.items ?? []}
                  rowKey={(r) => `c-${r.id}`}
                  size="small"
                  pagination={{ pageSize, showSizeChanger: true, onShowSizeChange }}
                  onRow={(r) => ({ onClick: () => openDetail(r.id), style: { cursor: "pointer" } })}
                  columns={[
                    {
                      title: "实体",
                      dataIndex: "entity_name",
                      key: "name",
                      ellipsis: true,
                      render: (v: string) => <a onClick={(e) => e.stopPropagation()}>{v}</a>,
                    },
                    {
                      title: "类型",
                      dataIndex: "entity_type",
                      key: "type",
                      width: 90,
                      render: (v: string) => ENTITY_TYPE_LABEL[v] ?? v,
                    },
                    {
                      title: "敏感度",
                      dataIndex: "sensitivity_level",
                      key: "sensitivity",
                      width: 110,
                      render: (s: string | null) => sensitivityTag(s),
                    },
                    {
                      title: "数据源",
                      key: "source",
                      width: 150,
                      ellipsis: true,
                      render: (_, r) =>
                        r.source_name ? `${r.source_name}（${r.source_id}）` : r.source_id,
                    },
                    {
                      title: "更新时间",
                      dataIndex: "updated_at",
                      key: "updated",
                      width: 170,
                      render: (v: string | null) => (v ? formatCnTime(v) : "-"),
                    },
                  ]}
                />
              )}
            </Card>
          </>
        )}
      </Card>
      <DrillDownDrawer
        open={drillOpen}
        title={drillTitle}
        columns={METRIC_COLUMNS}
        rows={drillRows}
        loading={drillLoading}
        total={drillTotal}
        onClose={() => setDrillOpen(false)}
      />
      {/* 实体详情抽屉：目录资产行点击下钻 */}
      <ResizableDrawer
        title={detail ? `实体详情：${detail.entity_name}` : "实体详情"}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        storageKey="unisense.drawer.owner.entity.width"
        defaultWidth={760}
        minWidth={560}
      >
        {detailLoading ? (
          <Spin tip="加载实体详情…" />
        ) : detail ? (
          <>
            <Descriptions column={2} bordered size="small" style={{ marginBottom: 12 }}>
              <Descriptions.Item label="实体名称">{detail.entity_name}</Descriptions.Item>
              <Descriptions.Item label="实体类型">{detail.entity_type}</Descriptions.Item>
              <Descriptions.Item label="数据源">
                {detail.source_name ? `${detail.source_name}（${detail.source_id}）` : detail.source_id}
              </Descriptions.Item>
              <Descriptions.Item label="敏感度">{sensitivityTag(detail.sensitivity_level)}</Descriptions.Item>
              <Descriptions.Item label="责任人">
                {detail.owner_name ?? (detail.owner_id ? `#${detail.owner_id}` : <Tag>无</Tag>)}
              </Descriptions.Item>
              <Descriptions.Item label="字段数">
                {Array.isArray(detail.schema_summary) ? detail.schema_summary.length : "—"}
              </Descriptions.Item>
            </Descriptions>
            {detail.entity_type === "TABLE" && canDeprecate && (
              <Space style={{ marginTop: 12, marginBottom: 8 }} wrap>
                <Popconfirm
                  title={`废弃资产「${detail.entity_name}」？`}
                  description="废弃后该资产不再作为可消费目录，血缘边随之失效；可在采集目录重新采集恢复。"
                  okText="确认废弃"
                  okButtonProps={{ danger: true }}
                  onConfirm={async () => {
                    setDeprecating(true);
                    try {
                      await deprecateEntity(detail.source_id, detail.entity_name);
                      message.success(`已废弃 ${detail.entity_name}`);
                      setDetailOpen(false);
                      setDetail(null);
                      if (ownerId) {
                        fetchAssetOwnerView(ownerId).then(setView).catch(() => {});
                      }
                    } catch (err) {
                      message.error(err instanceof Error ? err.message : "废弃失败");
                    } finally {
                      setDeprecating(false);
                    }
                  }}
                >
                  <Button danger icon={<DeleteOutlined />} loading={deprecating}>
                    废弃此资产
                  </Button>
                </Popconfirm>
              </Space>
            )}
            {Array.isArray(detail.schema_summary) && detail.schema_summary.length > 0 && (
              <Card size="small" title="字段信息">
                <SchemaTable
                  columns={detail.schema_summary}
                  editable={false}
                  sampleRows={detail.sample_rows ?? []}
                />
              </Card>
            )}
          </>
        ) : null}
      </ResizableDrawer>
    </div>
  );
}

/** 实体类型中文标签（兼容 DB 大写枚举 TABLE/VIEW/FIELD 与枚举表小写键）。 */
function entityTypeLabel(v: string | null | undefined) {
  if (!v) return <span className="muted">-</span>;
  return ENTITY_TYPE_LABEL[v] ?? ENTITY_TYPE_LABEL[String(v).toLowerCase()] ?? v;
}

// 孤儿资产 Tab：无责任人资产的消化工作台（认领闭环 + 多维度过滤 + 合规统计）。
function OrphansTab() {
  const [items, setItems] = useState<AssetTableItem[]>([]);
  // 服务端分页（P2-1：后端返回真实 total + offset，前端按页请求，不再一次拉 200 静默截断）
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // ---- 多维度筛选（关键字/数据源/库/业务域/实体类型/敏感度/Schema 状态）----
  const [keyword, setKeyword] = useState("");
  const [keywordDraft, setKeywordDraft] = useState("");
  const [sourceId, setSourceId] = useState<string | undefined>(undefined);
  const [database, setDatabase] = useState<string | undefined>(undefined);
  const [domain, setDomain] = useState<string | undefined>(undefined);
  const [entityType, setEntityType] = useState<string | undefined>(undefined);
  const [sensitivity, setSensitivity] = useState<string | undefined>(undefined);
  const [schemaStatus, setSchemaStatus] = useState<
    "complete" | "incomplete" | undefined
  >(undefined);
  // 筛选候选（数据源 / 库 / 业务域 / 责任人）
  const [sourceOptions, setSourceOptions] = useState<Array<{ label: string; value: string }>>([]);
  const [databaseOptions, setDatabaseOptions] = useState<Array<{ label: string; value: string }>>([]);
  const [domainOptions, setDomainOptions] = useState<Array<{ label: string; value: string }>>([]);
  const [ownerOptions, setOwnerOptions] = useState<Array<{ label: string; value: number }>>([]);
  // 合规统计（不受筛选影响，认领后刷新）——孤儿总数 / PII 孤儿 / 机密级孤儿
  const [stats, setStats] = useState({ total: 0, pii: 0, confidential: 0 });
  // 认领 / 转交：currentUserId 来自 /auth/me（认领 = 指定当前用户为责任人）
  const [currentUserId, setCurrentUserId] = useState<number | undefined>(undefined);
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [claiming, setClaiming] = useState(false);
  const [transferOpen, setTransferOpen] = useState(false);
  const [transferIds, setTransferIds] = useState<number[]>([]);
  const [transferSaving, setTransferSaving] = useState(false);
  const [transferForm] = Form.useForm();
  // 实体详情抽屉（认领前查看 schema/PII/血缘）
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detail, setDetail] = useState<AssetEntityDetail | null>(null);
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.orphans.pageSize", 20);
  const canEdit = usePermission().can("assetmap:edit");
  const navigate = useNavigate();

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const r = await fetchAssetOrphans({
        sensitivity,
        source_id: sourceId,
        database,
        domain,
        entity_type: entityType,
        schema_status: schemaStatus,
        keyword: keyword || undefined,
        limit: pageSize,
        offset: (page - 1) * pageSize,
      });
      setItems(r.items);
      setTotal(r.total);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载孤儿资产失败");
    } finally {
      setLoading(false);
    }
  }

  async function loadStats() {
    try {
      // 真实总数（后端 total，不再拉大样本算 length，避免 >N 孤儿截断统计）
      const [allR, piiR, confR] = await Promise.all([
        fetchAssetOrphans({ limit: 1 }),
        fetchAssetPiiOverview(),
        fetchAssetOrphans({ limit: 1, sensitivity: "CONFIDENTIAL" }),
      ]);
      setStats({
        total: allR.total,
        // PII 孤儿来自 /assetmap/pii 概览的 unowned_pii（无主 PII 资产，真实计数）
        pii: Number(piiR.unowned_pii ?? 0),
        // 机密级孤儿：孤儿列表按 CONFIDENTIAL 过滤的真实 total
        confidential: confR.total,
      });
    } catch {
      // 统计加载失败不阻断列表展示
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sensitivity, sourceId, database, domain, entityType, schemaStatus, keyword, page]);

  useEffect(() => {
    loadStats();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 当前登录用户：认领 = 一键把孤儿资产挂到当前用户名下
  useEffect(() => {
    fetchCurrentUser()
      .then((u) => setCurrentUserId(u.id))
      .catch(() => {});
  }, []);

  useEffect(() => {
    listDataSources({ page_size: 200 })
      .then((res) =>
        setSourceOptions(
          res.items.map((s) => ({
            label: s.name ? `${s.name}（${s.source_id}）` : s.source_id,
            value: s.source_id,
          })),
        ),
      )
      .catch(() => {});
  }, []);

  // 库名筛选候选（entity_name 前缀；随所选数据源联动收窄，对齐采集目录 description-coverage）
  useEffect(() => {
    listCatalogDatabases(sourceId)
      .then((dbs) => setDatabaseOptions(dbs.map((d) => ({ label: d, value: d }))))
      .catch(() => setDatabaseOptions([]));
  }, [sourceId]);

  useEffect(() => {
    listDomainTree()
      .then((tree) => setDomainOptions(flattenDomainTree(tree)))
      .catch(() => {});
  }, []);

  useEffect(() => {
    listUsers()
      .then((users) =>
        setOwnerOptions(
          users
            .filter((u) => u.status === "active")
            .map((u) => ({ label: `${u.display_name || u.username}`, value: u.id })),
        ),
      )
      .catch(() => {});
  }, []);

  const activeFilterCount = [
    keyword,
    sourceId,
    database,
    domain,
    entityType,
    sensitivity,
    schemaStatus,
  ].filter((v) => v !== undefined && v !== "").length;

  function resetFilters() {
    setKeyword("");
    setKeywordDraft("");
    setSourceId(undefined);
    setDatabase(undefined);
    setDomain(undefined);
    setEntityType(undefined);
    setSensitivity(undefined);
    setSchemaStatus(undefined);
    setPage(1);
  }

  // 认领：将选中孤儿资产归属给当前登录用户（一键消化孤儿债），成功后从池中移除
  async function claimIds(ids: number[]) {
    if (currentUserId == null) {
      message.warning("无法获取当前用户信息，请刷新后重试或使用「转交」指定责任人");
      return;
    }
    setClaiming(true);
    try {
      await (ids.length > 1
        ? batchAssignAssetOwner(ids, currentUserId)
        : assignAssetOwner(ids[0], currentUserId));
      message.success(`已认领 ${ids.length} 项孤儿资产`);
      setSelectedRowKeys([]);
      await Promise.all([load(), loadStats()]);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "认领失败");
    } finally {
      setClaiming(false);
    }
  }

  function openTransfer(ids: number[]) {
    setTransferIds(ids);
    transferForm.resetFields();
    setTransferOpen(true);
  }

  async function handleTransferSubmit() {
    const values = transferForm.getFieldsValue();
    const ownerId = values.owner_id as number | undefined;
    if (ownerId == null) {
      message.warning("请选择责任人");
      return;
    }
    setTransferSaving(true);
    try {
      await (transferIds.length > 1
        ? batchAssignAssetOwner(transferIds, ownerId)
        : assignAssetOwner(transferIds[0], ownerId));
      message.success(`已转交 ${transferIds.length} 项资产给责任人`);
      setTransferOpen(false);
      transferForm.resetFields();
      setSelectedRowKeys([]);
      await Promise.all([load(), loadStats()]);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "转交失败");
    } finally {
      setTransferSaving(false);
    }
  }

  async function openDetail(item: AssetTableItem) {
    if (item.id == null) {
      message.warning("该实体缺少详情标识（id），暂无法查看详情");
      return;
    }
    setDetailOpen(true);
    setDetailLoading(true);
    setDetail(null);
    try {
      setDetail(await fetchAssetEntityDetail(item.id));
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载实体详情失败");
    } finally {
      setDetailLoading(false);
    }
  }

  function selectedEntityIds(): number[] {
    return items
      .filter((r) => selectedRowKeys.includes(`${r.source_id}-${r.entity_name}`))
      .map((r) => r.id)
      .filter((v): v is number => v != null);
  }

  const entityTypeOptions = Object.keys(ENTITY_TYPE_LABEL)
    .filter((k) => ["table", "view", "field"].includes(k))
    .map((k) => ({ value: k, label: ENTITY_TYPE_LABEL[k] }));

  const columns: ColumnsType<AssetTableItem> = [
    {
      title: "数据源",
      dataIndex: "source_id",
      key: "source_id",
      width: 170,
      ellipsis: true,
      render: (v: string, r: AssetTableItem) =>
        r.source_name && r.source_name !== v ? `${r.source_name}（${v}）` : v,
    },
    { title: "实体", dataIndex: "entity_name", key: "entity_name", ellipsis: true },
    {
      title: "类型",
      dataIndex: "entity_type",
      key: "entity_type",
      width: 90,
      render: (v: string) => entityTypeLabel(v),
    },
    {
      title: "业务域",
      dataIndex: "domain",
      key: "domain",
      width: 110,
      ellipsis: true,
      render: (v: string | null | undefined) => v ?? <span className="muted">-</span>,
    },
    {
      title: "敏感度",
      dataIndex: "sensitivity_level",
      key: "sensitivity",
      width: 110,
      render: (s: string | null | undefined) => sensitivityTag(s),
    },
    {
      title: "Schema",
      dataIndex: "schema_incomplete",
      key: "schema",
      width: 100,
      render: (v: boolean) =>
        v ? <Tag color="orange">不完整</Tag> : <Tag color="green">完整</Tag>,
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      key: "updated_at",
      width: 150,
      render: (v: string | null | undefined) =>
        v ? formatCnTime(v) : <span className="muted">-</span>,
    },
    {
      title: "操作",
      key: "action",
      width: 190,
      render: (_: unknown, record: AssetTableItem) => (
        <Space size={0} wrap>
          <Button
            type="link"
            size="small"
            icon={<EyeOutlined />}
            disabled={record.id == null}
            onClick={() => openDetail(record)}
          >
            详情
          </Button>
          {canEdit && (
            <Popconfirm
              title={`认领「${record.entity_name}」？`}
              description="认领后你将作为该资产的责任人，从孤儿池移除。"
              okText="确认认领"
              okButtonProps={{ type: "primary" }}
              onConfirm={() => {
                if (record.id != null) void claimIds([record.id]);
              }}
            >
              <Button
                type="link"
                size="small"
                icon={<CheckOutlined />}
                loading={claiming}
                disabled={currentUserId == null}
              >
                认领
              </Button>
            </Popconfirm>
          )}
          {canEdit && (
            <Button
              type="link"
              size="small"
              icon={<SettingOutlined />}
              disabled={record.id == null}
              onClick={() => {
                if (record.id != null) openTransfer([record.id]);
              }}
            >
              转交
            </Button>
          )}
        </Space>
      ),
    },
  ];

  return (
    <Card
      title="孤儿资产（无责任人）"
      size="small"
      extra={
        <Space wrap>
          {canEdit && (
            <Popconfirm
              title={`认领选中的 ${selectedRowKeys.length} 项孤儿资产？`}
              description="认领后这些资产将归属当前登录用户。"
              okText="确认认领"
              onConfirm={() => {
                const ids = selectedEntityIds();
                if (ids.length === 0) {
                  message.warning("所选资产缺少详情标识（id），无法批量认领");
                  return;
                }
                void claimIds(ids);
              }}
            >
              <Button
                icon={<CheckOutlined />}
                disabled={selectedRowKeys.length === 0 || currentUserId == null}
                loading={claiming}
              >
                批量认领（给我）
              </Button>
            </Popconfirm>
          )}
          {canEdit && (
            <Button
              icon={<SettingOutlined />}
              disabled={selectedRowKeys.length === 0}
              onClick={() => {
                const ids = selectedEntityIds();
                if (ids.length === 0) {
                  message.warning("所选资产缺少详情标识（id），无法批量转交");
                  return;
                }
                openTransfer(ids);
              }}
            >
              批量转交
            </Button>
          )}
        </Space>
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="本页治理范围：资产责任人（Owner）——认领 / 转交无责任人的资产。"
        description="字段级描述缺失治理请前往「描述缺失」页签，或到「采集目录 → 描述缺失治理」统一治理。"
        action={
          <Button type="link" size="small" onClick={() => navigate("/catalogs?from=资产地图")}>
            前往采集目录
          </Button>
        }
      />
      <Space size={28} wrap style={{ marginBottom: 12 }}>
        <Statistic
          title="孤儿资产"
          value={stats.total}
          valueStyle={{ fontSize: 20, color: stats.total > 0 ? "#d64545" : "#3f8600" }}
        />
        <Statistic
          title="PII 孤儿"
          value={stats.pii}
          valueStyle={{ fontSize: 20, color: stats.pii > 0 ? "#d64545" : "#3f8600" }}
        />
        <Statistic
          title="机密级孤儿"
          value={stats.confidential}
          valueStyle={{ fontSize: 20, color: stats.confidential > 0 ? "#d46b08" : "#3f8600" }}
        />
        <span className="muted" style={{ fontSize: 12, maxWidth: 260 }}>
          PII / 机密级孤儿属合规优先级，建议尽快认领或转交业务责任人。
        </span>
      </Space>
      <Row gutter={[8, 8]} style={{ marginBottom: 12 }} align="middle">
        <Col>
          <Input.Search
            allowClear
            placeholder="搜索实体名 / 数据源"
            style={{ width: 210 }}
            value={keywordDraft}
            onChange={(e) => {
              const v = e.target.value;
              setKeywordDraft(v);
              if (v === "") setKeyword("");
            }}
            onSearch={(v) => setKeyword(v.trim())}
          />
        </Col>
        <Col>
          <Select
            allowClear
            showSearch
            placeholder="全部数据源"
            style={{ width: 190 }}
            value={sourceId}
            onChange={setSourceId}
            options={sourceOptions}
            optionFilterProp="label"
          />
        </Col>
        <Col>
          <Select
            allowClear
            showSearch
            placeholder="全部库"
            style={{ width: 150 }}
            value={database}
            onChange={(v) => {
              setDatabase(v);
              setPage(1);
            }}
            options={databaseOptions}
            optionFilterProp="label"
            notFoundContent="无库（请先选择数据源）"
          />
        </Col>
        <Col>
          <Select
            allowClear
            showSearch
            placeholder="全部业务域"
            style={{ width: 150 }}
            value={domain}
            onChange={setDomain}
            options={domainOptions}
            optionFilterProp="label"
          />
        </Col>
        <Col>
          <Select showSearch
            allowClear
            placeholder="全部类型"
            style={{ width: 110 }}
            value={entityType}
            onChange={setEntityType}
            options={entityTypeOptions}
          />
        </Col>
        <Col>
          <Select showSearch
            allowClear
            placeholder="全部敏感度"
            style={{ width: 130 }}
            value={sensitivity}
            onChange={setSensitivity}
            options={Object.keys(SENSITIVITY_LABEL).map((k) => ({
              value: k,
              label: SENSITIVITY_LABEL[k],
            }))}
          />
        </Col>
        <Col>
          <Select showSearch
            allowClear
            placeholder="Schema 状态"
            style={{ width: 140 }}
            value={schemaStatus}
            onChange={setSchemaStatus}
            options={[
              { value: "complete", label: "Schema 完整" },
              { value: "incomplete", label: "Schema 不完整" },
            ]}
          />
        </Col>
        {activeFilterCount > 0 && (
          <>
            <Col>
              <Tag color="blue">
                已筛选 {activeFilterCount} 项 · 共 {items.length} 个孤儿
              </Tag>
            </Col>
            <Col>
              <Button size="small" onClick={resetFilters}>
                重置筛选
              </Button>
            </Col>
          </>
        )}
      </Row>
      {loading ? (
        <Spin />
      ) : error ? (
        <Alert type="error" message={error} />
      ) : items.length === 0 ? (
        <Empty description="没有孤儿资产，全部已指定责任人" />
      ) : (
        <Table
          dataSource={items}
          rowKey={(r) => `${r.source_id}-${r.entity_name}`}
          size="small"
          pagination={{
            current: page,
            total,
            pageSize,
            showSizeChanger: true,
            pageSizeOptions: [...PAGE_SIZE_OPTIONS],
            onChange: (p) => setPage(p),
            onShowSizeChange,
          }}
          rowSelection={{
            selectedRowKeys,
            onChange: (keys) => setSelectedRowKeys(keys),
          }}
          columns={columns}
        />
      )}
      <ResizableDrawer
        title={detail ? `实体详情：${detail.entity_name}` : "实体详情"}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        storageKey="unisense.drawer.entity.width"
        defaultWidth={860}
        minWidth={600}
        destroyOnClose={false}
      >
        {detailLoading ? (
          <Spin tip="加载实体详情…" />
        ) : detail ? (
          <>
            <Descriptions column={2} bordered size="small">
              <Descriptions.Item label="实体名称">{detail.entity_name}</Descriptions.Item>
              <Descriptions.Item label="实体类型">{entityTypeLabel(detail.entity_type)}</Descriptions.Item>
              <Descriptions.Item label="数据源">
                {detail.source_name ? `${detail.source_name}（${detail.source_id}）` : detail.source_id}
              </Descriptions.Item>
              <Descriptions.Item label="业务域">
                {detail.domain ?? <span className="muted">-</span>}
              </Descriptions.Item>
              <Descriptions.Item label="敏感度">{sensitivityTag(detail.sensitivity_level)}</Descriptions.Item>
              <Descriptions.Item label="责任人">
                <Tag>无</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="字段数">
                {detail.column_count ?? <span className="muted">-</span>}
              </Descriptions.Item>
              <Descriptions.Item label="Schema 状态">
                {detail.schema_incomplete ? (
                  <Tag color="orange">不完整</Tag>
                ) : (
                  <Tag color="green">完整</Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="表级描述" span={2}>
                {detail.description ? (
                  <span>{detail.description}</span>
                ) : (
                  <span className="muted" style={{ fontStyle: "italic" }}>
                    暂无表级描述
                  </span>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="关联血缘">
                <Button
                  type="link"
                  size="small"
                  disabled={(detail.lineage_count ?? 0) <= 0}
                  onClick={() =>
                    message.info(`实体「${detail.entity_name}」关联血缘 ${detail.lineage_count} 条`)
                  }
                >
                  关联血缘 {detail.lineage_count} 条
                </Button>
              </Descriptions.Item>
              <Descriptions.Item label="新鲜度">
                <div className="muted" style={{ fontSize: 12 }}>
                  <div>创建：{detail.created_at ? formatCnTime(detail.created_at) : "-"}</div>
                  <div>更新：{detail.updated_at ? formatCnTime(detail.updated_at) : "-"}</div>
                </div>
              </Descriptions.Item>
            </Descriptions>
            {canEdit && (
              <Space style={{ marginTop: 12 }} wrap>
                <Button
                  type="primary"
                  icon={<CheckOutlined />}
                  loading={claiming}
                  disabled={currentUserId == null}
                  onClick={() => claimIds([detail.id])}
                >
                  认领此资产
                </Button>
                <Button
                  icon={<SettingOutlined />}
                  onClick={() => openTransfer([detail.id])}
                >
                  转交
                </Button>
              </Space>
            )}
          </>
        ) : null}
      </ResizableDrawer>
      <Modal
        title={transferIds.length > 1 ? `转交（${transferIds.length} 项资产）` : "转交资产归属"}
        open={transferOpen}
        onCancel={() => {
          setTransferOpen(false);
          transferForm.resetFields();
        }}
        onOk={handleTransferSubmit}
        okText="确认转交"
        confirmLoading={transferSaving}
      >
        <Form form={transferForm} layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item
            name="owner_id"
            label="指定责任人"
            extra="转交后资产归属该责任人，并从孤儿池移除。"
          >
            <Select
              allowClear
              showSearch
              placeholder="选择责任人"
              options={ownerOptions}
              optionFilterProp="label"
            />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}

function TablesTab() {
  const [items, setItems] = useState<AssetTableItem[]>([]);
  // 服务端分页（P2-1：后端返回真实 total + offset，前端按页请求，不再一次拉 200 静默截断）
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  // ---- 多维度筛选（数据表目录：关键字/数据源/库/业务域/敏感度/责任人/Schema 状态）----
  const [keyword, setKeyword] = useState<string>("");
  const [keywordDraft, setKeywordDraft] = useState<string>("");
  const [sourceId, setSourceId] = useState<string | undefined>(undefined);
  const [database, setDatabase] = useState<string | undefined>(undefined);
  const [domain, setDomain] = useState<string | undefined>(undefined);
  const [sensitivity, setSensitivity] = useState<string | undefined>(undefined);
  const [ownerId, setOwnerId] = useState<number | undefined>(undefined);
  const [schemaStatus, setSchemaStatus] = useState<
    "complete" | "incomplete" | undefined
  >(undefined);
  // 筛选选项候选（数据源 / 库 / 业务域）
  const [sourceOptions, setSourceOptions] = useState<Array<{ label: string; value: string }>>([]);
  const [databaseOptions, setDatabaseOptions] = useState<Array<{ label: string; value: string }>>([]);
  const [domainOptions, setDomainOptions] = useState<Array<{ label: string; value: string }>>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detail, setDetail] = useState<AssetEntityDetail | null>(null);
  // 单实体废弃（资产地图「手动下线」治理入口）
  const [deprecating, setDeprecating] = useState(false);
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.tables.pageSize", 20);
  // 批量行选择（责任人设置 / 敏感度重分类共用）
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  // 责任人下拉候选（后端 /auth/users，Owner 可为 null=解除归属）
  const [ownerOptions, setOwnerOptions] = useState<Array<{ label: string; value: number }>>([]);
  // 治理设置 Modal：single=单条（行/详情抽屉入口）/ batch=批量（表格勾选）
  const [govOpen, setGovOpen] = useState(false);
  const [govEntityIds, setGovEntityIds] = useState<number[]>([]);
  const [govSaving, setGovSaving] = useState(false);
  const [govOnSaved, setGovOnSaved] = useState<(() => void) | null>(null);
  const [govForm] = Form.useForm();
  const canDeprecate = usePermission().can("catalog:deprecate");
  const canEdit = usePermission().can("assetmap:edit");
  const canExport = usePermission().can("assetmap:export");
  const navigate = useNavigate();

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const r = await fetchAssetTables({
        sensitivity,
        source_id: sourceId,
        database,
        domain,
        owner_id: ownerId,
        schema_status: schemaStatus,
        keyword: keyword || undefined,
        limit: pageSize,
        offset: (page - 1) * pageSize,
      });
      setItems(r.items);
      setTotal(r.total);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载数据表失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sensitivity, sourceId, database, domain, ownerId, schemaStatus, keyword, page]);

  // 数据源筛选候选（含名称，仅活跃源）
  useEffect(() => {
    listDataSources({ page_size: 200 })
      .then((res) =>
        setSourceOptions(
          res.items.map((s) => ({
            label: s.name ? `${s.name}（${s.source_id}）` : s.source_id,
            value: s.source_id,
          })),
        ),
      )
      .catch(() => {});
  }, []);

  // 库名筛选候选（entity_name 前缀；随所选数据源联动收窄，对齐采集目录 description-coverage）
  useEffect(() => {
    listCatalogDatabases(sourceId)
      .then((dbs) => setDatabaseOptions(dbs.map((d) => ({ label: d, value: d }))))
      .catch(() => setDatabaseOptions([]));
  }, [sourceId]);

  // 业务域筛选候选（域树扁平化）
  useEffect(() => {
    listDomainTree()
      .then((tree) => setDomainOptions(flattenDomainTree(tree)))
      .catch(() => {});
  }, []);

  // 责任人候选：加载失败不阻塞治理入口（仅下拉无选项）
  useEffect(() => {
    listUsers()
      .then((users) =>
        setOwnerOptions(
          users
            .filter((u) => u.status === "active")
            .map((u) => ({ label: `${u.display_name || u.username}`, value: u.id })),
        ),
      )
      .catch(() => {});
  }, []);

  // 激活的筛选条件数（含关键字；用于「重置」入口与计数展示）
  const activeFilterCount = [
    keyword,
    sourceId,
    database,
    domain,
    sensitivity,
    ownerId,
    schemaStatus,
  ].filter((v) => v !== undefined && v !== "").length;

  function resetFilters() {
    setKeyword("");
    setKeywordDraft("");
    setSourceId(undefined);
    setDatabase(undefined);
    setDomain(undefined);
    setSensitivity(undefined);
    setOwnerId(undefined);
    setSchemaStatus(undefined);
    setPage(1);
  }

  // 打开治理设置 Modal（single 传单个 entity_id；batch 传勾选 id 列表）
  // prefill：单实体时带入当前责任人/敏感度，让用户直观看到要调整的现状
  // （批量多实体无统一现状，不预填——保持「留空不修改」语义）
  function openGov(
    entityIds: number[],
    onSaved?: () => void,
    prefill?: { owner_id?: number | null; sensitivity_level?: string | null },
  ) {
    setGovEntityIds(entityIds);
    setGovOnSaved(() => onSaved ?? null);
    setGovOpen(true);
    if (entityIds.length === 1 && prefill) {
      govForm.setFieldsValue({
        owner_id: prefill.owner_id ?? undefined,
        sensitivity_level: prefill.sensitivity_level ?? undefined,
      });
    } else {
      govForm.resetFields();
    }
  }

  // 提交：只发送用户实际填写的字段；owner 选「解除归属」哨兵值 → null
  async function handleGovSubmit() {
    const values = govForm.getFieldsValue();
    const ids = govEntityIds;
    const sens = values.sensitivity_level as string | undefined;
    const ownerRaw = values.owner_id;
    const hasOwner = ownerRaw !== undefined && ownerRaw !== null;
    const ownerId = ownerRaw === "__none__" ? null : (ownerRaw as number);
    if (!sens && !hasOwner) {
      message.warning("请选择要设置的责任人或敏感度");
      return;
    }
    setGovSaving(true);
    try {
      // 同一实体多字段写必须串行：敏感度/责任人共享 row_version 乐观锁，
      // 并行提交会竞态——后提交者 WHERE row_version=旧值 匹配 0 行 → 409。
      // 先改敏感度、再改责任人，第二次读取天然基于第一次提交后的最新版本。
      if (sens) {
        await (ids.length > 1
          ? batchReclassifyAssetSensitivity(ids, sens)
          : reclassifyAssetSensitivity(ids[0], sens));
      }
      if (hasOwner) {
        await (ids.length > 1
          ? batchAssignAssetOwner(ids, ownerId)
          : assignAssetOwner(ids[0], ownerId));
      }
      message.success(`已更新 ${ids.length} 项资产`);
      setGovOpen(false);
      govForm.resetFields();
      setSelectedRowKeys([]);
      await load();
      govOnSaved?.();
    } catch (err) {
      const conflict =
        err instanceof UnisenseApiError && err.code === "OPTIMISTIC_LOCK_CONFLICT";
      message.error(
        conflict
          ? "数据已被其他操作修改，已为你刷新最新数据，请重试"
          : err instanceof Error
            ? err.message
            : "保存失败",
      );
      if (conflict) {
        // 乐观锁冲突：刷新最新数据，让用户基于最新版本重试，避免停留在过期状态
        await load();
      }
    } finally {
      setGovSaving(false);
    }
  }

  async function openDetail(item: AssetTableItem) {
    if (item.id == null) {
      message.warning("该实体缺少详情标识（id），暂无法查看详情");
      return;
    }
    setDetailOpen(true);
    setDetailLoading(true);
    setDetail(null);
    try {
      setDetail(await fetchAssetEntityDetail(item.id));
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载实体详情失败");
    } finally {
      setDetailLoading(false);
    }
  }

  // 治理保存后刷新当前详情抽屉内容（责任人/敏感度即时生效）
  async function refreshDetail() {
    if (!detail) return;
    try {
      setDetail(await fetchAssetEntityDetail(detail.id));
    } catch {
      // 静默失败：抽屉内容保持旧值，下次打开自动刷新
    }
  }

  // 表格勾选（rowKey 为复合键）→ 映射为实体 id 列表
  function selectedEntityIds(): number[] {
    const ids = items
      .filter((r) => selectedRowKeys.includes(`${r.source_id}-${r.entity_name}`))
      .map((r) => r.id)
      .filter((v): v is number => v != null);
    return ids;
  }

  const lineageCount = detail?.lineage_count ?? 0;
  const hasPii = Boolean(detail?.pii_flag) || (detail?.sensitivity_level ?? "").includes("PII");

  async function handleExport() {
    try {
      await downloadAssetExport({
        sensitivity,
        source_id: sourceId,
        domain,
        owner_id: ownerId,
        schema_status: schemaStatus,
        keyword: keyword || undefined,
      });
      message.success("资产清单已导出");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "导出失败");
    }
  }

  return (
    <Card
      title="数据表目录"
      size="small"
      extra={
        <Space wrap>
          {canExport && (
            <Button icon={<DownloadOutlined />} onClick={handleExport}>
              导出 CSV
            </Button>
          )}
          {canEdit && (
            <Button
              icon={<SettingOutlined />}
              disabled={selectedRowKeys.length === 0}
              onClick={() => {
                const ids = selectedEntityIds();
                if (ids.length === 0) {
                  message.warning("所选资产缺少详情标识（id），无法批量设置");
                  return;
                }
                openGov(ids);
              }}
            >
              批量设置
            </Button>
          )}
        </Space>
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="本页治理范围：资产级信息（责任人 / 敏感度 / 废弃 / 表描述）。"
        description="字段级描述缺失治理请前往「描述缺失」页签，或到「采集目录 → 描述缺失治理」统一治理。"
        action={
          <Button type="link" size="small" onClick={() => navigate("/catalogs?from=资产地图")}>
            前往采集目录
          </Button>
        }
      />
      <Row gutter={[8, 8]} style={{ marginBottom: 12 }} align="middle">
        <Col>
          <Input.Search
            allowClear
            placeholder="搜索表名 / 数据源"
            style={{ width: 220 }}
            value={keywordDraft}
            onChange={(e) => {
              const v = e.target.value;
              setKeywordDraft(v);
              // 点清除（allowClear）后同时清空已应用关键字
              if (v === "") setKeyword("");
            }}
            onSearch={(v) => setKeyword(v.trim())}
          />
        </Col>
        <Col>
          <Select
            allowClear
            showSearch
            placeholder="全部数据源"
            style={{ width: 200 }}
            value={sourceId}
            onChange={setSourceId}
            options={sourceOptions}
            optionFilterProp="label"
          />
        </Col>
        <Col>
          <Select
            allowClear
            showSearch
            placeholder="全部库"
            style={{ width: 160 }}
            value={database}
            onChange={(v) => {
              setDatabase(v);
              setPage(1);
            }}
            options={databaseOptions}
            optionFilterProp="label"
            notFoundContent="无库（请先选择数据源）"
          />
        </Col>
        <Col>
          <Select
            allowClear
            showSearch
            placeholder="全部业务域"
            style={{ width: 160 }}
            value={domain}
            onChange={setDomain}
            options={domainOptions}
            optionFilterProp="label"
          />
        </Col>
        <Col>
          <Select showSearch
            allowClear
            placeholder="全部敏感度"
            style={{ width: 140 }}
            value={sensitivity}
            onChange={setSensitivity}
            options={Object.keys(SENSITIVITY_LABEL).map((k) => ({
              value: k,
              label: SENSITIVITY_LABEL[k],
            }))}
          />
        </Col>
        <Col>
          <Select
            allowClear
            showSearch
            placeholder="全部责任人"
            style={{ width: 190 }}
            value={ownerId}
            onChange={setOwnerId}
            options={[
              { label: "无责任人（未分配）", value: 0 },
              ...ownerOptions,
            ]}
            optionFilterProp="label"
          />
        </Col>
        <Col>
          <Select showSearch
            allowClear
            placeholder="Schema 状态"
            style={{ width: 150 }}
            value={schemaStatus}
            onChange={setSchemaStatus}
            options={[
              { value: "complete", label: "Schema 完整" },
              { value: "incomplete", label: "Schema 不完整" },
            ]}
          />
        </Col>
        {activeFilterCount > 0 && (
          <>
            <Col>
              <Tag color="blue">已筛选 {activeFilterCount} 项 · 共 {total} 表</Tag>
            </Col>
            <Col>
              <Button size="small" onClick={resetFilters}>
                重置筛选
              </Button>
            </Col>
          </>
        )}
      </Row>
      {loading ? (
        <Spin />
      ) : error ? (
        <Alert type="error" message={error} />
      ) : (
        <Table
          dataSource={items}
          rowKey={(r) => `${r.source_id}-${r.entity_name}`}
          size="small"
          pagination={{
            current: page,
            total,
            pageSize,
            showSizeChanger: true,
            pageSizeOptions: [...PAGE_SIZE_OPTIONS],
            onChange: (p) => setPage(p),
            onShowSizeChange,
          }}
          rowSelection={{
            selectedRowKeys,
            onChange: (keys) => setSelectedRowKeys(keys),
          }}
          columns={[
            {
              title: "数据源",
              dataIndex: "source_id",
              key: "source_id",
              width: 170,
              ellipsis: true,
              render: (v: string, r: AssetTableItem) =>
                r.source_name && r.source_name !== v ? `${r.source_name}（${v}）` : v,
            },
            { title: "实体", dataIndex: "entity_name", key: "entity_name", ellipsis: true },
            {
              title: "类型",
              dataIndex: "entity_type",
              key: "entity_type",
              width: 90,
              render: (v: string) => ENTITY_TYPE_LABEL[v] ?? v,
            },
            {
              title: "业务域",
              dataIndex: "domain",
              key: "domain",
              width: 110,
              ellipsis: true,
              render: (v: string | null | undefined) =>
                v ?? <span className="muted">-</span>,
            },
            {
              title: "敏感度",
              dataIndex: "sensitivity_level",
              key: "sensitivity",
              width: 110,
              render: (s: string | null | undefined) => sensitivityTag(s),
            },
            {
              title: "责任人",
              dataIndex: "owner_id",
              key: "owner",
              width: 110,
              ellipsis: true,
              render: (v: number | null, r: AssetTableItem) =>
                v == null ? <Tag>无</Tag> : r.owner_name || `#${v}`,
            },
            {
              title: "Schema",
              dataIndex: "schema_incomplete",
              key: "schema",
              width: 100,
              render: (v: boolean) =>
                v ? (
                  <Tag color="orange">不完整</Tag>
                ) : (
                  <Tag color="green">完整</Tag>
                ),
            },
            {
              title: "操作",
              key: "action",
              width: 140,
              render: (_: unknown, record: AssetTableItem) => (
                <Space size={0}>
                  <Button
                    type="link"
                    size="small"
                    icon={<EyeOutlined />}
                    disabled={record.id == null}
                    onClick={() => openDetail(record)}
                  >
                    详情
                  </Button>
                  {canEdit && (
                    <Button
                      type="link"
                      size="small"
                      icon={<SettingOutlined />}
                      disabled={record.id == null}
                      onClick={() => {
                        if (record.id != null)
                          openGov([record.id], undefined, {
                            owner_id: record.owner_id,
                            sensitivity_level: record.sensitivity_level,
                          });
                      }}
                    >
                      设置
                    </Button>
                  )}
                </Space>
              ),
            },
          ]}
        />
      )}
      <ResizableDrawer
        title={detail ? `实体详情：${detail.entity_name}` : "实体详情"}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        storageKey="unisense.drawer.entity.width"
        defaultWidth={860}
        minWidth={600}
        destroyOnClose={false}
      >
        {detailLoading ? (
          <Spin tip="加载实体详情…" />
        ) : detail ? (
          <>
            <Descriptions column={2} bordered size="small">
              <Descriptions.Item label="实体名称">
                {detail.entity_name}
                {canEdit && (
                  <Button
                    type="link"
                    size="small"
                    icon={<SettingOutlined />}
                    style={{ paddingLeft: 8 }}
                    onClick={() =>
                      openGov([detail.id], refreshDetail, {
                        owner_id: detail.owner_id,
                        sensitivity_level: detail.sensitivity_level,
                      })
                    }
                  >
                    设置
                  </Button>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="实体类型">{detail.entity_type}</Descriptions.Item>
              <Descriptions.Item label="数据源">
                {detail.source_name ? `${detail.source_name}（${detail.source_id}）` : detail.source_id}
              </Descriptions.Item>
              <Descriptions.Item label="业务域">
                {detail.domain ?? <span className="muted">-</span>}
              </Descriptions.Item>
              <Descriptions.Item label="敏感度">
                {sensitivityTag(detail.sensitivity_level)}
                {hasPii && (
                  <Tag color="red" style={{ marginLeft: 8 }}>
                    含 PII
                  </Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="责任人">
                {detail.owner_id != null
                  ? detail.owner_name || `#${detail.owner_id}`
                  : <Tag>无</Tag>}
              </Descriptions.Item>
              <Descriptions.Item label="字段数">
                {detail.column_count ?? <span className="muted">-</span>}
              </Descriptions.Item>
              <Descriptions.Item label="表级描述">
                <Space direction="vertical" size={4}>
                  {detail.description ? (
                    <span>{detail.description}</span>
                  ) : (
                    <span className="muted" style={{ fontStyle: "italic" }}>
                      暂无表级描述
                    </span>
                  )}
                  <Space size={4} wrap>
                    {descriptionSourceTag(detail.description_source)}
                    {detail.description_updated_at ? (
                      <span className="muted" style={{ fontSize: 12 }}>
                        更新于 {formatCnTime(detail.description_updated_at)}
                      </span>
                    ) : null}
                  </Space>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="Schema 状态">
                {detail.schema_incomplete ? (
                  <Tag color="orange">不完整</Tag>
                ) : (
                  <Tag color="green">完整</Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="内容指纹">
                {detail.content_signature ? (
                  <Tooltip title={detail.content_signature}>
                    <span style={{ fontFamily: "monospace", cursor: "help" }}>
                      {detail.content_signature.slice(0, 16)}…
                    </span>
                  </Tooltip>
                ) : (
                  <span className="muted">-</span>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="Schema 摘要">
                {renderSchemaSummary(detail.schema_summary)}
              </Descriptions.Item>
              <Descriptions.Item label="关联血缘">
                <Button
                  type="link"
                  size="small"
                  disabled={lineageCount <= 0}
                  onClick={() =>
                    message.info(`实体「${detail.entity_name}」关联血缘 ${lineageCount} 条`)
                  }
                >
                  关联血缘 {lineageCount} 条
                </Button>
              </Descriptions.Item>
              <Descriptions.Item label="源健康">
                {detail.source_health ? (
                  <Tag
                    color={
                      detail.source_health.health_status === "healthy"
                        ? "green"
                        : detail.source_health.health_status === "unhealthy"
                          ? "red"
                          : "default"
                    }
                  >
                    {SOURCE_HEALTH_LABEL[detail.source_health.health_status] ??
                      detail.source_health.health_status}
                  </Tag>
                ) : (
                  <span className="muted">未知</span>
                )}
                {detail.source_health?.last_health_check ? (
                  <span className="muted" style={{ marginLeft: 8 }}>
                    检查于 {formatCnTime(detail.source_health.last_health_check)}
                  </span>
                ) : null}
              </Descriptions.Item>
              <Descriptions.Item label="新鲜度">
                <div className="muted" style={{ fontSize: 12 }}>
                  <div>
                    创建：{detail.created_at ? formatCnTime(detail.created_at) : "-"}
                  </div>
                  <div>
                    更新：{detail.updated_at ? formatCnTime(detail.updated_at) : "-"}
                  </div>
                </div>
              </Descriptions.Item>
            </Descriptions>
            {detail.entity_type === "TABLE" && canDeprecate && (
              <Space style={{ marginTop: 12 }} wrap>
                <Popconfirm
                  title={`废弃资产「${detail.entity_name}」？`}
                  description="废弃后该资产不再作为可消费目录，血缘边随之失效；可在采集目录重新采集恢复。"
                  okText="确认废弃"
                  okButtonProps={{ danger: true }}
                  onConfirm={async () => {
                    setDeprecating(true);
                    try {
                      await deprecateEntity(detail.source_id, detail.entity_name);
                      message.success(`已废弃 ${detail.entity_name}`);
                      setDetailOpen(false);
                      setDetail(null);
                      load();
                    } catch (err) {
                      message.error(err instanceof Error ? err.message : "废弃失败");
                    } finally {
                      setDeprecating(false);
                    }
                  }}
                >
                  <Button danger icon={<DeleteOutlined />} loading={deprecating}>
                    废弃此资产
                  </Button>
                </Popconfirm>
              </Space>
            )}
            {(detail.lineage_edges?.length ?? 0) > 0 && (
              <EntityLineageCard edges={detail.lineage_edges!} />
            )}
            {(detail.related_metrics?.length ?? 0) > 0 && (
              <Card title="关联指标" size="small" style={{ marginTop: 16 }}>
                <Table
                  dataSource={detail.related_metrics}
                  rowKey={(e, i) => `${e.metric_node}-${i}`}
                  size="small"
                  pagination={false}
                  columns={[
                    {
                      title: "指标",
                      dataIndex: "metric_node",
                      key: "metric",
                      ellipsis: true,
                      render: (v: string) => (
                        <span className="mono" style={{ fontSize: 12 }}>
                          {v}
                        </span>
                      ),
                    },
                    { title: "关系", dataIndex: "edge_type", key: "edge", width: 140, render: (v: string) => METRIC_RELATION_EDGE_LABEL[v] ?? v ?? "-" },
                  ]}
                />
              </Card>
            )}
          </>
        ) : null}
      </ResizableDrawer>
      <Modal
        title={
          govEntityIds.length > 1
            ? `批量设置（${govEntityIds.length} 项资产）`
            : "设置资产治理信息"
        }
        open={govOpen}
        onCancel={() => {
          setGovOpen(false);
          govForm.resetFields();
        }}
        onOk={handleGovSubmit}
        okText="保存"
        confirmLoading={govSaving}
      >
        <Form form={govForm} layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item
            name="owner_id"
            label="责任人"
            extra="单条已带入当前责任人；留空表示不修改；选择「解除归属」将清空责任人"
          >
            <Select showSearch
              allowClear
              placeholder="选择责任人"
              options={[
                { label: "（解除归属）", value: "__none__" },
                ...ownerOptions,
              ]}
            />
          </Form.Item>
          <Form.Item name="sensitivity_level" label="敏感度" extra="留空表示不修改">
            <Select showSearch
              placeholder="选择敏感级别"
              options={Object.keys(SENSITIVITY_LABEL).map((k) => ({
                value: k,
                label: SENSITIVITY_LABEL[k],
              }))}
            />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}

// ----------------------------------------------------------------
// 产品补充（FR-18 生产化）：全局搜索 / 资产健康 / PII 合规 / 变更追踪 / 我的资产
// ----------------------------------------------------------------

function SearchTab() {
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const [type, setType] = useState<string | undefined>(undefined);
  const [items, setItems] = useState<AssetSearchItem[]>([]);
  const [searched, setSearched] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.search.pageSize", 20);

  async function doSearch() {
    if (!q.trim()) {
      message.warning("请输入搜索关键词");
      return;
    }
    setLoading(true);
    setError(null);
    setSearched(true);
    try {
      const r = await fetchAssetSearch({ q: q.trim(), type, limit: 50 });
      setItems(r.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : "搜索失败");
      setItems([]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <Card title="资产全局搜索" size="small">
      <Space.Compact style={{ width: "100%", maxWidth: 720 }}>
        <Input
          placeholder="输入表名 / 字段名 / 指标编码 / 指标名称"
          value={q}
          prefix={<SearchOutlined />}
          allowClear
          onChange={(e) => setQ(e.target.value)}
          onPressEnter={doSearch}
        />
        <Select showSearch
          allowClear
          placeholder="全部类型"
          style={{ width: 160 }}
          value={type}
          onChange={setType}
          options={[
            { value: "table", label: "表 / 视图" },
            { value: "field", label: "字段" },
            { value: "metric", label: "指标" },
          ]}
        />
        <Button type="primary" icon={<SearchOutlined />} onClick={doSearch} loading={loading}>
          搜索
        </Button>
      </Space.Compact>
      <div style={{ marginTop: 16 }}>
        {loading ? (
          <Spin />
        ) : error ? (
          <Alert type="error" message={error} />
        ) : !searched ? (
          <Empty description="输入关键词搜索资产（表/字段/指标）" />
        ) : items.length === 0 ? (
          <Empty description="未找到匹配资产" />
        ) : (
          <Table
            dataSource={items}
            rowKey={(r) => `${r.type}-${r.id}-${r.name}`}
            size="small"
            pagination={{
              pageSize,
              showSizeChanger: true,
              pageSizeOptions: [...PAGE_SIZE_OPTIONS],
              onShowSizeChange,
            }}
            onRow={(r) => ({
              onClick: () => {
                if (r.type === "metric") navigate(`/detail/${encodeURIComponent(r.name)}`);
              },
              style: r.type === "metric" ? { cursor: "pointer" } : undefined,
            })}
            columns={[
              {
                title: "类型",
                dataIndex: "type",
                key: "type",
                width: 90,
                render: (t: string) => {
                  const meta =
                    t === "metric"
                      ? { label: "指标", color: "purple" }
                      : t === "field"
                        ? { label: "字段", color: "cyan" }
                        : { label: "目录", color: "blue" };
                  return <Tag color={meta.color}>{meta.label}</Tag>;
                },
              },
              {
                title: "名称",
                dataIndex: "name",
                key: "name",
                ellipsis: true,
                render: (v: string, r: AssetSearchItem) => (
                  <div>
                    {r.type === "metric" ? (
                      <a
                        className="mono"
                        onClick={(e) => {
                          e.stopPropagation();
                          navigate(`/detail/${encodeURIComponent(v)}`);
                        }}
                      >
                        {v}
                      </a>
                    ) : (
                      <span className="mono">{v}</span>
                    )}
                    {r.description ? (
                      <div className="muted" style={{ fontSize: 12 }} title={r.description}>
                        {r.description}
                      </div>
                    ) : null}
                  </div>
                ),
              },
              {
                title: "实体类型",
                dataIndex: "entity_type",
                key: "entity_type",
                width: 100,
                render: (v: string) => ENTITY_TYPE_LABEL[v] ?? v,
              },
              {
                title: "敏感度",
                dataIndex: "sensitivity_level",
                key: "sensitivity",
                width: 110,
                render: (s: string | null) => sensitivityTag(s),
              },
              {
                title: "数据源",
                key: "source",
                width: 140,
                ellipsis: true,
                render: (_, r) =>
                  r.source_name ? `${r.source_name}（${r.source_id}）` : r.source_id ?? "-",
              },
              {
                title: "责任人",
                key: "owner",
                width: 110,
                ellipsis: true,
                render: (_, r) =>
                  r.owner_name ?? (r.owner_id != null ? `#${r.owner_id}` : <Tag>无</Tag>),
              },
              {
                title: "口径",
                key: "metric_info",
                width: 220,
                render: (_, r) =>
                  r.type === "metric" ? (
                    <Space size={[4, 4]} wrap>
                      <Tag>{METRIC_TYPE_LABEL[r.metric_type ?? ""] ?? r.metric_type ?? "-"}</Tag>
                      <Tag>粒度 {r.granularity ?? "-"}</Tag>
                      <Tag>单位 {UNIT_LABEL[r.unit ?? ""] ?? r.unit ?? "-"}</Tag>
                      <Tag>{AGG_LABEL[r.aggregation ?? ""] ?? r.aggregation ?? "-"}</Tag>
                    </Space>
                  ) : r.column_count != null ? (
                    <span className="muted">{r.column_count} 字段</span>
                  ) : (
                    "-"
                  ),
              },
              {
                title: "更新时间",
                dataIndex: "updated_at",
                key: "updated",
                width: 170,
                render: (v: string | null | undefined) => (v ? formatCnTime(v) : "-"),
              },
            ]}
          />
        )}
      </div>
    </Card>
  );
}

function HealthTab() {
  const [data, setData] = useState<AssetHealthSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchAssetHealth()
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : "加载资产健康失败"))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <Spin />;
  if (error) return <Alert type="error" message={error} />;
  if (!data) return <Empty />;

  const unhealthyCount = data.unhealthy_sources.length;
  const incompleteCount = data.schema_incomplete.length;
  const levelMeta = HEALTH_LEVEL_META[data.level] ?? { label: data.level, color: "default" };
  const scoreColor =
    data.score >= 75 ? "#3f8600" : data.score >= 60 ? "#d46b08" : "#cf1322";
  const checkRows = (data.checks ?? []).map((c) => ({
    key: c.key,
    name: HEALTH_CHECK_LABEL[c.key] ?? c.key,
    count: c.count,
    field_total: c.field_total,
    healthy: c.count === 0,
  }));

  return (
    <div>
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Card size="small" style={{ textAlign: "center" }}>
            <Statistic title="健康评分" value={data.score} valueStyle={{ color: scoreColor }} />
            <Tag color={levelMeta.color} style={{ marginTop: 4 }}>
              健康等级：{levelMeta.label}
            </Tag>
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic
              title="不健康数据源"
              value={unhealthyCount}
              valueStyle={{ color: unhealthyCount > 0 ? "#cf1322" : "#3f8600" }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic
              title="Schema 不完整"
              value={incompleteCount}
              valueStyle={{ color: incompleteCount > 0 ? "#d46b08" : "#3f8600" }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic
              title="孤儿资产"
              value={data.orphan_assets}
              valueStyle={{ color: data.orphan_assets > 0 ? "#d46b08" : "#3f8600" }}
            />
          </Card>
        </Col>
      </Row>

      <Card title="体检项（9 项）" size="small" style={{ marginBottom: 16 }}>
        <Table
          dataSource={checkRows}
          rowKey="key"
          size="small"
          pagination={false}
          columns={[
            { title: "体检项", dataIndex: "name", key: "name" },
            {
              title: "数量",
              dataIndex: "count",
              key: "count",
              width: 120,
              render: (v: number, r) =>
                r.key === "fields_missing_desc" && r.field_total ? `${v} / ${r.field_total}` : v,
            },
            {
              title: "状态",
              key: "status",
              width: 120,
              render: (_, r) =>
                r.healthy ? <Tag color="green">正常</Tag> : <Tag color="orange">需关注</Tag>,
            },
          ]}
        />
      </Card>

      <Row gutter={16}>
        <Col span={8}>
          <Card title="不健康数据源" size="small">
            {data.unhealthy_sources.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="全部健康" />
            ) : (
              <Table
                dataSource={data.unhealthy_sources}
                rowKey={(r) => r.source_id}
                size="small"
                pagination={false}
                columns={[
                  { title: "源 ID", dataIndex: "source_id", key: "source_id" },
                  { title: "名称", dataIndex: "name", key: "name", ellipsis: true },
                  {
                    title: "状态",
                    dataIndex: "health_status",
                    key: "status",
                    width: 100,
                    render: (v: string) => <Tag color="red">{SOURCE_HEALTH_LABEL[v] ?? v}</Tag>,
                  },
                ]}
              />
            )}
          </Card>
        </Col>
        <Col span={8}>
          <Card title="Schema 不完整" size="small">
            {data.schema_incomplete.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无" />
            ) : (
              <Table
                dataSource={data.schema_incomplete}
                rowKey={(r) => r.id}
                size="small"
                pagination={false}
                columns={[
                  { title: "实体", dataIndex: "entity_name", key: "name", ellipsis: true },
                  { title: "源", dataIndex: "source_id", key: "source", width: 120 },
                ]}
              />
            )}
          </Card>
        </Col>
        <Col span={8}>
          <Card title={`${data.stale_days} 天未更新资产`} size="small">
            {data.stale_assets.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无陈旧资产" />
            ) : (
              <Table
                dataSource={data.stale_assets}
                rowKey={(r) => r.id}
                size="small"
                pagination={false}
                columns={[
                  { title: "实体", dataIndex: "entity_name", key: "name", ellipsis: true },
                  {
                    title: "更新时间",
                    dataIndex: "updated_at",
                    key: "updated",
                    width: 170,
                    render: (v: string) => formatCnTime(v),
                  },
                ]}
              />
            )}
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col span={8}>
          <Card title="PII 未复核指标" size="small">
            {data.pii_unreviewed?.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无" />
            ) : (
              <Table
                dataSource={data.pii_unreviewed ?? []}
                rowKey={(r) => r.metric_code}
                size="small"
                pagination={false}
                columns={[
                  { title: "编码", dataIndex: "metric_code", key: "code", ellipsis: true },
                  { title: "名称", dataIndex: "name", key: "name", ellipsis: true },
                ]}
              />
            )}
          </Card>
        </Col>
        <Col span={8}>
          <Card title="无快照指标" size="small">
            {data.metrics_without_snapshot?.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无" />
            ) : (
              <Table
                dataSource={data.metrics_without_snapshot ?? []}
                rowKey={(r) => r.metric_code}
                size="small"
                pagination={false}
                columns={[
                  { title: "编码", dataIndex: "metric_code", key: "code", ellipsis: true },
                  { title: "名称", dataIndex: "name", key: "name", ellipsis: true },
                ]}
              />
            )}
          </Card>
        </Col>
        <Col span={8}>
          <Card title="废弃未替换指标" size="small">
            {data.deprecated_without_successor?.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无" />
            ) : (
              <Table
                dataSource={data.deprecated_without_successor ?? []}
                rowKey={(r) => r.metric_code}
                size="small"
                pagination={false}
                columns={[
                  { title: "编码", dataIndex: "metric_code", key: "code", ellipsis: true },
                  { title: "名称", dataIndex: "name", key: "name", ellipsis: true },
                ]}
              />
            )}
          </Card>
        </Col>
      </Row>
    </div>
  );
}

function PiiTab() {
  const { can } = usePermission();
  // 请求序号令牌（P1 竞态修复）：筛选/翻页快速切换时旧响应不覆盖新结果
  const listSeqRef = useRef(0);
  const [overview, setOverview] = useState<AssetPiiOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [listLoading, setListLoading] = useState(false);
  const [items, setItems] = useState<AssetPiiAssetItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [error, setError] = useState<string | null>(null);
  // 筛选状态（仿 OrphansTab）
  const [keyword, setKeyword] = useState("");
  const [keywordDraft, setKeywordDraft] = useState("");
  const [sourceId, setSourceId] = useState<string | undefined>();
  const [domain, setDomain] = useState<string | undefined>();
  const [ownerId, setOwnerId] = useState<number | undefined>();
  const [reviewStatus, setReviewStatus] = useState<string | undefined>();
  const [category, setCategory] = useState<string | undefined>();
  // 数据源/域/责任人选项
  const [sourceOptions, setSourceOptions] = useState<{ value: string; label: string }[]>([]);
  const [domainOptions, setDomainOptions] = useState<{ value: string; label: string }[]>([]);
  const [userOptions, setUserOptions] = useState<{ value: number; label: string }[]>([]);
  // 详情抽屉 + 模板应用
  const [detailItem, setDetailItem] = useState<AssetPiiAssetItem | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [templateOpen, setTemplateOpen] = useState(false);

  const activeFilterCount = [
    keyword,
    sourceId,
    domain,
    ownerId,
    reviewStatus,
    category,
  ].filter((v) => v !== undefined && v !== "").length;

  const loadOverview = () => {
    fetchAssetPiiOverview()
      .then(setOverview)
      .catch(() => undefined);
  };

  const loadList = useCallback(
    (
      p: number,
      ps: number,
      filters?: {
        keyword?: string;
        sourceId?: string;
        domain?: string;
        ownerId?: number;
        reviewStatus?: string;
        category?: string;
      },
    ) => {
      const seq = ++listSeqRef.current;
      setListLoading(true);
      fetchAssetPiiAssets({
        keyword: filters?.keyword ?? keyword,
        source_id: filters?.sourceId ?? sourceId,
        domain: filters?.domain ?? domain,
        owner_id: filters?.ownerId ?? ownerId,
        review_status: filters?.reviewStatus ?? reviewStatus,
        category: filters?.category ?? category,
        page: p,
        page_size: ps,
      })
        .then((res) => {
          if (seq !== listSeqRef.current) return; // 旧响应丢弃
          setItems(res.items);
          setTotal(res.total);
        })
        .catch((err) => {
          if (seq !== listSeqRef.current) return;
          setError(err instanceof Error ? err.message : "加载 PII 资产失败");
        })
        .finally(() => {
          if (seq === listSeqRef.current) setListLoading(false);
        });
    },
    [keyword, sourceId, domain, ownerId, reviewStatus, category],
  );

  useEffect(() => {
    loadOverview();
    fetchAssetPiiAssets({ page: 1, page_size: 10 })
      .then((res) => {
        setItems(res.items);
        setTotal(res.total);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "加载 PII 资产失败"))
      .finally(() => setLoading(false));
    // 筛选选项
    listDataSources({ page_size: 200 }).then((res) =>
      setSourceOptions((res.items ?? []).map((s) => ({ value: s.source_id, label: s.name ?? s.source_id }))),
    );
    listDomainTree().then((tree) => {
      const flat: { value: string; label: string }[] = [];
      const walk = (nodes: SubjectDomainTreeNode[]) => {
        nodes.forEach((n) => {
          flat.push({ value: n.code, label: n.name });
          if (n.children?.length) walk(n.children);
        });
      };
      walk(tree);
      setDomainOptions(flat);
    });
    listUsers().then((users) =>
      setUserOptions((users ?? []).map((u) => ({ value: u.id, label: u.display_name || u.username }))),
    );
  }, []);

  const resetFilters = () => {
    setKeyword("");
    setKeywordDraft("");
    setSourceId(undefined);
    setDomain(undefined);
    setOwnerId(undefined);
    setReviewStatus(undefined);
    setCategory(undefined);
    setPage(1);
    loadList(1, pageSize, { keyword: "", sourceId: undefined });
  };

  const applySearch = (kw: string) => {
    setKeyword(kw);
    setPage(1);
    loadList(1, pageSize, { keyword: kw, sourceId });
  };

  const openDetail = (item: AssetPiiAssetItem) => {
    setDetailItem(item);
    setDetailOpen(true);
  };

  const catRows = Object.entries(overview?.by_category ?? {}).map(([k, v]) => ({
    key: k,
    count: v,
  }));
  const catTotal = catRows.reduce((sum, r) => sum + r.count, 0);

  if (loading) return <Spin />;
  if (error && !overview && items.length === 0)
    return <Alert type="error" message={error} />;

  const canGovern = can("pii:review") || can("classification:rescan");

  return (
    <div>
      {/* 风险卡：合规风险一眼可见 */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={4}>
          <Card size="small">
            <Statistic title="PII 目录数" value={overview?.pii_catalog_count ?? 0} valueStyle={{ color: "#cf1322" }} />
          </Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Statistic title="PII 指标数" value={overview?.pii_metric_count ?? 0} valueStyle={{ color: "#d46b08" }} />
          </Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Statistic title="无主 PII" value={overview?.unowned_pii ?? 0} valueStyle={{ color: "#cf1322" }} />
          </Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Statistic title="待复核 PII" value={overview?.unreviewed_pii ?? 0} valueStyle={{ color: "#d46b08" }} />
          </Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Statistic title="已复核目录" value={overview?.reviewed_pii ?? 0} valueStyle={{ color: "#389e0d" }} />
          </Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Statistic
              title="字段命中"
              value={catTotal}
              suffix="列"
              valueStyle={{ color: "#096dd9" }}
            />
          </Card>
        </Col>
      </Row>

      {/* 操作区：模板应用 + 盘点导出 */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={12}>
          <Card title="PII 类别分布" size="small" extra={canGovern && (
            <Space>
              <Button
                size="small"
                icon={<ThunderboltOutlined />}
                onClick={() => setTemplateOpen(true)}
                disabled={!can("classification:rescan")}
              >
                应用分级模板
              </Button>
              <Button
                size="small"
                icon={<DownloadOutlined />}
                onClick={() => downloadPiiExport().catch(() => message.error("导出失败"))}
              >
                盘点导出
              </Button>
            </Space>
          )}>
            {catRows.length === 0 ? (
              <Empty description="暂无字段级 PII 命中明细（采集后自动生成）" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {catRows.map((r) => (
                  <Tag
                    key={r.key}
                    color={PII_CATEGORY_COLOR[r.key] ?? "default"}
                    style={{ padding: "2px 10px", fontSize: 13 }}
                  >
                    {PII_CATEGORY_LABEL[r.key] ?? r.key} · {r.count}
                  </Tag>
                ))}
              </div>
            )}
          </Card>
        </Col>
        <Col span={12}>
          <Card title="按业务域分布" size="small">
            {Object.keys(overview?.by_domain ?? {}).length === 0 ? (
              <Empty description="暂无 PII 指标域分布" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <Table
                dataSource={Object.entries(overview?.by_domain ?? {}).map(([k, v]) => ({ key: k, count: v }))}
                rowKey="key"
                size="small"
                pagination={false}
                columns={[
                  { title: "域", dataIndex: "key", key: "key" },
                  { title: "数量", dataIndex: "count", key: "count", align: "right" },
                ]}
              />
            )}
          </Card>
        </Col>
      </Row>

      {/* 筛选栏 + PII 资产明细列表 */}
      <Card
        title={`PII 资产明细（${total}）`}
        size="small"
        extra={
          <Space>
            {activeFilterCount > 0 && (
              <Tag color="blue">{activeFilterCount} 个筛选</Tag>
            )}
            {activeFilterCount > 0 && (
              <Button size="small" type="link" onClick={resetFilters}>
                重置
              </Button>
            )}
          </Space>
        }
      >
        <Row gutter={[8, 8]} align="middle" style={{ marginBottom: 12 }}>
          <Col>
            <Input.Search
              allowClear
              style={{ width: 210 }}
              placeholder="搜索实体名 / 数据源"
              value={keywordDraft}
              onChange={(e) => {
                setKeywordDraft(e.target.value);
                if (!e.target.value) {
                  setKeyword("");
                  setPage(1);
                  loadList(1, pageSize, { keyword: "", sourceId });
                }
              }}
              onSearch={(v) => applySearch(v.trim())}
            />
          </Col>
          <Col>
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="数据源"
              style={{ width: 170 }}
              value={sourceId}
              onChange={(v) => {
                setSourceId(v);
                setPage(1);
                loadList(1, pageSize, { sourceId: v });
              }}
              options={sourceOptions}            />
          </Col>
          <Col>
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="业务域"
              style={{ width: 140 }}
              value={domain}
              onChange={(v) => {
                setDomain(v);
                setPage(1);
                loadList(1, pageSize, { domain: v });
              }}
              options={domainOptions}
            />
          </Col>
          <Col>
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="责任人"
              style={{ width: 150 }}
              value={ownerId}
              onChange={(v) => {
                setOwnerId(v);
                setPage(1);
                loadList(1, pageSize, { ownerId: v });
              }}
              options={userOptions}
            />
          </Col>
          <Col>
            <Select showSearch
              allowClear
              placeholder="复核状态"
              style={{ width: 130 }}
              value={reviewStatus}
              onChange={(v) => {
                setReviewStatus(v);
                setPage(1);
                loadList(1, pageSize, { reviewStatus: v });
              }}
              options={REVIEW_STATUS_OPTIONS}
            />
          </Col>
          <Col>
            <Select showSearch
              allowClear
              placeholder="PII 类别"
              style={{ width: 150 }}
              value={category}
              onChange={(v) => {
                setCategory(v);
                setPage(1);
                loadList(1, pageSize, { category: v });
              }}
              options={Object.keys(PII_CATEGORY_LABEL).map((k) => ({
                value: k,
                label: PII_CATEGORY_LABEL[k],
              }))}
            />
          </Col>
        </Row>
        <Table
          dataSource={items}
          rowKey="id"
          size="small"
          loading={listLoading}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: [10, 20, 50],
            onChange: (p, ps) => {
              setPage(p);
              setPageSize(ps);
              loadList(p, ps);
            },
          }}
          onRow={(r) => ({ onClick: () => openDetail(r), style: { cursor: "pointer" } })}
          columns={[
            {
              title: "实体",
              dataIndex: "entity_name",
              key: "name",
              ellipsis: true,
              render: (v: string, r) => (
                <Space>
                  <SafetyOutlined style={{ color: "#cf1322" }} />
                  <span>{v}</span>
                  {r.entity_type === "TABLE" && <Tag color="blue">表</Tag>}
                  {r.entity_type === "VIEW" && <Tag color="cyan">视图</Tag>}
                </Space>
              ),
            },
            {
              title: "数据源",
              key: "source",
              width: 150,
              ellipsis: true,
              render: (_, r) => (r.source_name ? `${r.source_name}（${r.source_id}）` : r.source_id),
            },
            {
              title: "业务域",
              dataIndex: "domain",
              key: "domain",
              width: 100,
              render: (v: string | null) => v ?? <Tag>未归属</Tag>,
            },
            {
              title: "责任人",
              key: "owner",
              width: 110,
              render: (_, r) =>
                r.owner_name ?? (r.owner_id != null ? `#${r.owner_id}` : <Tag color="red">无主</Tag>),
            },
            {
              title: "命中字段",
              dataIndex: "pii_field_count",
              key: "fields",
              width: 90,
              align: "right",
              render: (v: number) => <b style={{ color: "#cf1322" }}>{v}</b>,
            },
            {
              title: "类别",
              key: "categories",
              width: 180,
              render: (_, r) =>
                (r.categories ?? []).slice(0, 3).map((c) => (
                  <Tag key={c} color={PII_CATEGORY_COLOR[c] ?? "default"} style={{ marginRight: 4 }}>
                    {PII_CATEGORY_LABEL[c] ?? c}
                  </Tag>
                )),
            },
            {
              title: "合规复核",
              dataIndex: "compliance_reviewed",
              key: "review",
              width: 110,
              render: (v: boolean) =>
                v ? (
                  <Tag color="green" icon={<CheckOutlined />}>已复核</Tag>
                ) : (
                  <Tag color="gold" icon={<CloseOutlined />}>待复核</Tag>
                ),
            },
            {
              title: "脱敏",
              dataIndex: "masking_policy",
              key: "masking",
              width: 90,
              render: (v: string | null) =>
                v && v !== "none" ? (
                  <Tag color="volcano">{MASKING_POLICY_LABEL[v] ?? v}</Tag>
                ) : (
                  <Tag>未设</Tag>
                ),
            },
          ]}
        />
      </Card>

      <PiiDetailDrawer
        open={detailOpen}
        item={detailItem}
        onClose={() => setDetailOpen(false)}
        onChanged={() => {
          loadOverview();
          loadList(page, pageSize);
        }}
      />
      <PiiTemplateModal
        open={templateOpen}
        onClose={() => setTemplateOpen(false)}
        onApplied={() => {
          loadOverview();
          loadList(page, pageSize);
        }}
      />
    </div>
  );
}

// PII 行业分级模板应用弹窗（个保法敏感个人信息 / 金融行业 / 标准）
function PiiTemplateModal({
  open,
  onClose,
  onApplied,
}: {
  open: boolean;
  onClose: () => void;
  onApplied: () => void;
}) {
  const [templates, setTemplates] = useState<AssetPiiTemplate[]>([]);
  const [templateId, setTemplateId] = useState<string>("");
  const [scope, setScope] = useState<"all_pii" | "source" | "ids">("all_pii");
  const [sourceId, setSourceId] = useState<string | undefined>();
  const [catalogIds, setCatalogIds] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (open) {
      fetchAssetPiiTemplates()
        .then((res) => {
          setTemplates(res.items);
          if (res.items.length) setTemplateId(res.items[0].id);
        })
        .catch(() => message.error("加载模板失败"));
    }
  }, [open]);

  const apply = async () => {
    if (!templateId) {
      message.warning("请选择模板");
      return;
    }
    setSubmitting(true);
    try {
      const body: {
        template_id: string;
        all_pii?: boolean;
        source_id?: string;
        catalog_ids?: number[];
      } = { template_id: templateId, all_pii: scope === "all_pii" };
      if (scope === "source") body.source_id = sourceId;
      if (scope === "ids")
        body.catalog_ids = catalogIds
          .split(",")
          .map((s) => Number(s.trim()))
          .filter((n) => Number.isFinite(n) && n > 0);
      const res = await applyAssetPiiTemplate(body);
      message.success(`模板应用完成：处理 ${res.applied} 项，升级 ${res.changed} 项`);
      onApplied();
      onClose();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "应用模板失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title="应用行业分级模板"
      open={open}
      onCancel={onClose}
      onOk={apply}
      confirmLoading={submitting}
      okText="应用"
      width={520}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="模板按字段类别升级资产敏感级"
        description="例：个保法敏感个人信息模板将含生物识别/健康医疗/金融账户/行踪轨迹字段的资产升级为 PII。"
      />
      <Form layout="vertical">
        <Form.Item label="分级模板" required>
          <Select showSearch value={templateId} onChange={setTemplateId} options={templates.map((t) => ({ value: t.id, label: t.name }))} />
        </Form.Item>
        {templates.find((t) => t.id === templateId) && (
          <Form.Item label="模板说明">
            <div style={{ color: "#888", fontSize: 12 }}>
              {templates.find((t) => t.id === templateId)?.description}
            </div>
          </Form.Item>
        )}
        <Form.Item label="作用范围" required>
          <Select showSearch
            value={scope}
            onChange={setScope}
            options={[
              { value: "all_pii", label: "全部 PII 资产" },
              { value: "source", label: "指定数据源" },
              { value: "ids", label: "指定资产 ID（逗号分隔）" },
            ]}
          />
        </Form.Item>
        {scope === "source" && (
          <Form.Item label="数据源">
            <Input placeholder="source_id" value={sourceId} onChange={(e) => setSourceId(e.target.value)} />
          </Form.Item>
        )}
        {scope === "ids" && (
          <Form.Item label="资产 ID">
            <Input placeholder="如 1,2,3" value={catalogIds} onChange={(e) => setCatalogIds(e.target.value)} />
          </Form.Item>
        )}
      </Form>
    </Modal>
  );
}

// PII 资产详情抽屉：字段级明细 + 合规复核/脱敏/标注/保留期操作闭环
function PiiDetailDrawer({
  open,
  item,
  onClose,
  onChanged,
}: {
  open: boolean;
  item: AssetPiiAssetItem | null;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { can } = usePermission();
  const [detail, setDetail] = useState<AssetEntityDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [masking, setMasking] = useState<string>("none");
  const [retentionDays, setRetentionDays] = useState<number | null>(null);
  const [legalBasis, setLegalBasis] = useState<string | undefined>();
  // 合规法律依据选项（字典驱动，回退硬编码）
  const [legalBasisOptions, setLegalBasisOptions] =
    useState<Array<{ value: string; label: string }>>(FALLBACK_LEGAL_BASIS_OPTIONS);
  // 字段标注弹窗
  const [overrideField, setOverrideField] = useState<AssetPiiField | null>(null);
  const [overrideSuppressed, setOverrideSuppressed] = useState(true);
  const [overrideReason, setOverrideReason] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    listDictItems("legal_basis")
      .then((items) => {
        if (items.length > 0) {
          setLegalBasisOptions(items.map((i) => ({ value: i.code, label: i.label })));
        }
      })
      .catch(() => {
        /* 字典拉取失败保留硬编码兜底 */
      });
  }, []);

  useEffect(() => {
    if (open && item) {
      setLoading(true);
      setDetail(null);
      fetchAssetEntityDetail(item.id)
        .then((d) => {
          setDetail(d);
          // P0-4 修复：`??` 优先级高于 `?:`，原式被解析为 (policy ?? includes) ? "hash" : "none"，
          // 已有策略(deny/mask/none)一律回显 "hash"，保存即覆盖原策略。加括号修正。
          setMasking(
            d.masking_policy ?? ((d.sensitivity_level ?? "").includes("PII") ? "hash" : "none"),
          );
          setRetentionDays(d.retention_days ?? null);
          setLegalBasis(d.legal_basis ?? undefined);
        })
        .catch((err) => message.error(err instanceof Error ? err.message : "加载详情失败"))
        .finally(() => setLoading(false));
    }
  }, [open, item]);

  const canGovern = can("pii:review") || can("classification:rescan");

  const review = async (decision: "APPROVE" | "REJECT") => {
    if (!item) return;
    setSubmitting(true);
    try {
      await reviewAssetEntity(item.id, { decision });
      message.success(decision === "APPROVE" ? "复核通过" : "已驳回（保持待复核）");
      onChanged();
      fetchAssetEntityDetail(item.id).then(setDetail).catch(() => undefined);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "复核失败");
    } finally {
      setSubmitting(false);
    }
  };

  const saveMasking = async () => {
    if (!item) return;
    setSubmitting(true);
    try {
      await setAssetMaskingPolicy(item.id, masking);
      message.success("脱敏策略已更新");
      onChanged();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "设置失败");
    } finally {
      setSubmitting(false);
    }
  };

  const saveRetention = async () => {
    if (!item) return;
    setSubmitting(true);
    try {
      await setAssetRetention(item.id, { retention_days: retentionDays, legal_basis: legalBasis ?? null });
      message.success("保留期已更新");
      onChanged();
      fetchAssetEntityDetail(item.id).then(setDetail).catch(() => undefined);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "设置失败");
    } finally {
      setSubmitting(false);
    }
  };

  const submitOverride = async () => {
    if (!item || !overrideField) return;
    setSubmitting(true);
    try {
      await overrideAssetPiiField(item.id, {
        column: overrideField.column,
        suppressed: overrideSuppressed,
        reason: overrideReason || undefined,
      });
      message.success(overrideSuppressed ? "已标注为非 PII（误报）" : "已人工确认为 PII");
      setOverrideField(null);
      setOverrideReason("");
      onChanged();
      fetchAssetEntityDetail(item.id).then(setDetail).catch(() => undefined);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "标注失败");
    } finally {
      setSubmitting(false);
    }
  };

  const removeOverride = async (field: AssetPiiField) => {
    if (!item) return;
    try {
      await removeAssetPiiOverride(item.id, field.column);
      message.success("已撤销标注，恢复规则判定");
      onChanged();
      fetchAssetEntityDetail(item.id).then(setDetail).catch(() => undefined);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "撤销失败");
    }
  };

  const piiFields = (detail?.pii_fields ?? []).filter((f) => !f.suppressed);
  const suppressedFields = (detail?.pii_fields ?? []).filter((f) => f.suppressed);

  return (
    <ResizableDrawer
      open={open}
      onClose={onClose}
      storageKey="unisense.drawer.pii.width"
      defaultWidth={820}
      minWidth={620}
      title={
        <Space>
          <SafetyOutlined style={{ color: "#cf1322" }} />
          <span>PII 资产详情：{detail?.entity_name ?? item?.entity_name}</span>
          {detail?.sensitivity_level ? sensitivityTag(detail.sensitivity_level) : null}
        </Space>
      }
      destroyOnClose={false}
    >
      {loading || !detail ? (
        <Spin />
      ) : (
        <div>
          {/* 基础信息 */}
          <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
            <Descriptions.Item label="实体名称">{detail.entity_name}</Descriptions.Item>
            <Descriptions.Item label="实体类型">{ENTITY_TYPE_LABEL[detail.entity_type] ?? detail.entity_type}</Descriptions.Item>
            <Descriptions.Item label="数据源">
              {detail.source_name ? `${detail.source_name}（${detail.source_id}）` : detail.source_id}
            </Descriptions.Item>
            <Descriptions.Item label="业务域">{detail.domain ?? <Tag>未归属</Tag>}</Descriptions.Item>
            <Descriptions.Item label="责任人">
              {detail.owner_name ?? (detail.owner_id != null ? `#${detail.owner_id}` : <Tag color="red">无主</Tag>)}
            </Descriptions.Item>
            <Descriptions.Item label="敏感度">{sensitivityTag(detail.sensitivity_level)}</Descriptions.Item>
            <Descriptions.Item label="合规复核">
              {detail.compliance_reviewed ? (
                <Space>
                  <Tag color="green" icon={<CheckOutlined />}>已复核</Tag>
                  {detail.compliance_reviewed_at && (
                    <span style={{ fontSize: 12, color: "#888" }}>{formatCnTime(detail.compliance_reviewed_at)}</span>
                  )}
                </Space>
              ) : (
                <Tag color="gold" icon={<CloseOutlined />}>待复核</Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="保留期">
              {detail.retention_days ? (
                <Space>
                  <span>{detail.retention_days} 天</span>
                  {detail.legal_basis && <Tag>{detail.legal_basis}</Tag>}
                  {detail.retention_expiring && <Tag color="red">临近到期</Tag>}
                </Space>
              ) : (
                <Tag>未设置</Tag>
              )}
            </Descriptions.Item>
          </Descriptions>

          {/* 合规操作区 */}
          {canGovern && (
            <Card size="small" title="合规处置" style={{ marginBottom: 16 }}>
              <Row gutter={16} align="middle">
                <Col span={8}>
                  <Space>
                    <Button
                      type="primary"
                      danger={false}
                      icon={<CheckOutlined />}
                      loading={submitting}
                      disabled={Boolean(detail.compliance_reviewed)}
                      onClick={() => review("APPROVE")}
                    >
                      复核通过
                    </Button>
                    <Button
                      icon={<CloseOutlined />}
                      loading={submitting}
                      disabled={!detail.compliance_reviewed}
                      onClick={() => review("REJECT")}
                    >
                      驳回
                    </Button>
                  </Space>
                </Col>
                <Col span={10}>
                  <Space>
                    <span style={{ color: "#888" }}>脱敏策略</span>
                    <Select showSearch
                      style={{ width: 120 }}
                      value={masking}
                      onChange={setMasking}
                      options={Object.keys(MASKING_POLICY_LABEL).map((k) => ({ value: k, label: MASKING_POLICY_LABEL[k] }))}
                    />
                    <Button size="small" loading={submitting} onClick={saveMasking}>
                      保存
                    </Button>
                  </Space>
                </Col>
              </Row>
            </Card>
          )}

          {/* PII 字段级命中明细 */}
          <Card
            size="small"
            title={`PII 字段命中明细（${piiFields.length}）`}
            style={{ marginBottom: 16 }}
          >
            {piiFields.length === 0 && suppressedFields.length === 0 ? (
              <Empty description="该资产无字段级 PII 命中（人工复核或旧数据未生成明细）" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <Table
                dataSource={piiFields}
                rowKey={(f) => f.column}
                size="small"
                pagination={false}
                columns={[
                  { title: "字段名", dataIndex: "column", key: "column", width: 160 },
                  {
                    title: "类别",
                    dataIndex: "category",
                    key: "category",
                    width: 110,
                    render: (c: string) => (
                      <Tag color={PII_CATEGORY_COLOR[c] ?? "default"}>{PII_CATEGORY_LABEL[c] ?? c}</Tag>
                    ),
                  },
                  { title: "命中规则", dataIndex: "rule", key: "rule", width: 110 },
                  {
                    title: "置信度",
                    dataIndex: "confidence",
                    key: "confidence",
                    width: 90,
                    align: "right",
                    render: (v: number) => `${Math.round(v * 100)}%`,
                  },
                  {
                    title: "操作",
                    key: "action",
                    width: 160,
                    render: (_, f: AssetPiiField) =>
                      canGovern && (
                        <Popconfirm
                          title="标注该列为非 PII（误报）？"
                          description="撤销后由规则引擎重新判定"
                          okText="标注误报"
                          onConfirm={() => {
                            setOverrideField(f);
                            setOverrideSuppressed(true);
                            setOverrideReason("");
                            submitOverride();
                          }}
                        >
                          <Button size="small" type="link" danger>
                            误报标注
                          </Button>
                        </Popconfirm>
                      ),
                  },
                ]}
              />
            )}
            {suppressedFields.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <Divider orientation="left" plain style={{ margin: "8px 0" }}>
                  已人工标注字段（不计入 PII）
                </Divider>
                {suppressedFields.map((f) => (
                  <Tag key={f.column} closable={canGovern} onClose={() => removeOverride(f)} style={{ marginBottom: 4 }}>
                    {f.column}
                    {f.override_reason ? `（${f.override_reason}）` : ""}
                  </Tag>
                ))}
              </div>
            )}
          </Card>

          {/* 保留期设置 */}
          {canGovern && (
            <Card size="small" title="保留期与合法性" style={{ marginBottom: 16 }}>
              <Row gutter={16} align="middle">
                <Col span={6}>
                  <InputNumber
                    style={{ width: "100%" }}
                    placeholder="保留天数"
                    min={1}
                    value={retentionDays}
                    onChange={(v) => setRetentionDays(v ?? null)}
                  />
                </Col>
                <Col span={10}>
                  <Select showSearch
                    allowClear
                    style={{ width: "100%" }}
                    placeholder="合法性基础"
                    value={legalBasis}
                    onChange={setLegalBasis}
                    options={legalBasisOptions}
                  />
                </Col>
                <Col span={8}>
                  <Button loading={submitting} onClick={saveRetention}>
                    保存保留期
                  </Button>
                </Col>
              </Row>
              {detail.retention_expires_at && (
                <div style={{ marginTop: 8, fontSize: 12, color: "#888" }}>
                  到期时间：{formatCnTime(detail.retention_expires_at)}
                </div>
              )}
            </Card>
          )}
        </div>
      )}
    </ResizableDrawer>
  );
}

function ChangesTab() {
  const navigate = useNavigate();
  const [data, setData] = useState<AssetChanges | null>(null);
  const [days, setDays] = useState(7);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // 行点击 → 侧边栏详情（目录/指标/漂移三类）
  const [catalogItem, setCatalogItem] = useState<AssetChangeItem | null>(null);
  const [metricItem, setMetricItem] = useState<AssetChangeMetric | null>(null);
  const [driftItem, setDriftItem] = useState<AssetDriftItem | null>(null);
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.changes.pageSize", 10);

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetchAssetChanges({ days })
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : "加载变更失败"))
      .finally(() => setLoading(false));
  }, [days]);

  if (loading) return <Spin />;
  if (error) return <Alert type="error" message={error} />;
  if (!data) return <Empty />;

  return (
    <Card
      title={`最近 ${data.days} 天资产变更`}
      size="small"
      extra={
        <Select showSearch
          value={days}
          onChange={setDays}
          style={{ width: 120 }}
          options={[7, 14, 30].map((d) => ({ value: d, label: `近 ${d} 天` }))}
        />
      }
    >
      <Descriptions column={3} size="small" style={{ marginBottom: 16 }}>
        <Descriptions.Item label="新增/变更目录">{data.catalogs.length}</Descriptions.Item>
        <Descriptions.Item label="新增/变更指标">{data.metrics.length}</Descriptions.Item>
        <Descriptions.Item label="Schema 漂移">{data.drift?.length ?? 0}</Descriptions.Item>
      </Descriptions>
      <Table
        dataSource={data.catalogs}
        rowKey={(r) => `c-${r.id}`}
        size="small"
        pagination={{
          pageSize,
          showSizeChanger: true,
          pageSizeOptions: [...PAGE_SIZE_OPTIONS],
          onShowSizeChange,
        }}
        title={() => <b>目录</b>}
        onRow={(r) => ({
          onClick: () => setCatalogItem(r),
          style: { cursor: "pointer" },
        })}
        columns={[
          { title: "实体", dataIndex: "entity_name", key: "name", ellipsis: true },
          {
            title: "类型",
            dataIndex: "entity_type",
            key: "type",
            width: 90,
            render: (v: string) => ENTITY_TYPE_LABEL[v] ?? v,
          },
          {
            title: "变更",
            dataIndex: "change_type",
            key: "change",
            width: 80,
            render: (v: string) => {
              const meta = CHANGE_TYPE_META[v];
              return meta ? <Tag color={meta.color}>{meta.label}</Tag> : v;
            },
          },
          {
            title: "敏感度",
            dataIndex: "sensitivity_level",
            key: "sensitivity",
            width: 110,
            render: (s: string | null) => sensitivityTag(s),
          },
          {
            title: "数据源",
            key: "source",
            width: 150,
            ellipsis: true,
            render: (_, r) => (r.source_name ? `${r.source_name}（${r.source_id}）` : r.source_id),
          },
          {
            title: "责任人",
            key: "owner",
            width: 110,
            ellipsis: true,
            render: (_, r) => r.owner_name ?? (r.owner_id != null ? `#${r.owner_id}` : <Tag>无</Tag>),
          },
          {
            title: "更新时间",
            dataIndex: "updated_at",
            key: "updated",
            width: 180,
            render: (v: string) => formatCnTime(v),
          },
        ]}
      />
      <Table
        dataSource={data.metrics}
        rowKey={(r) => `m-${r.metric_code}`}
        size="small"
        pagination={{
          pageSize,
          showSizeChanger: true,
          pageSizeOptions: [...PAGE_SIZE_OPTIONS],
          onShowSizeChange,
        }}
        style={{ marginTop: 16 }}
        title={() => <b>指标</b>}
        onRow={(r) => ({
          onClick: () => setMetricItem(r),
          style: { cursor: "pointer" },
        })}
        columns={[
          {
            title: "编码",
            dataIndex: "metric_code",
            key: "code",
            ellipsis: true,
            render: (v: string) => (
              <a
                className="mono"
                onClick={(e) => {
                  e.stopPropagation();
                  navigate(`/detail/${encodeURIComponent(v)}`, {
                    state: { from: "assetmap-changes" },
                  });
                }}
              >
                {v}
              </a>
            ),
          },
          { title: "名称", dataIndex: "name", key: "name", ellipsis: true },
          {
            title: "变更",
            dataIndex: "change_type",
            key: "change",
            width: 80,
            render: (v: string) => {
              const meta = CHANGE_TYPE_META[v];
              return meta ? <Tag color={meta.color}>{meta.label}</Tag> : v;
            },
          },
          { title: "状态", dataIndex: "status", key: "status", width: 100, render: (v: string) => STATUS_LABEL[v] ?? v ?? "-" },
          { title: "版本", dataIndex: "version", key: "version", width: 70 },
          { title: "域", dataIndex: "domain", key: "domain", width: 100 },
          {
            title: "PII",
            dataIndex: "pii_flag",
            key: "pii",
            width: 70,
            render: (v: boolean) => (v ? <Tag color="red">PII</Tag> : null),
          },
          {
            title: "责任人",
            key: "owner",
            width: 110,
            ellipsis: true,
            render: (_, r) => r.owner_name ?? (r.owner_id != null ? `#${r.owner_id}` : <Tag>无</Tag>),
          },
          {
            title: "更新时间",
            dataIndex: "updated_at",
            key: "updated",
            width: 180,
            render: (v: string) => formatCnTime(v),
          },
        ]}
      />
      {data.drift && data.drift.length > 0 && (
        <Table
          dataSource={data.drift}
          rowKey={(r) => `d-${r.id}`}
          size="small"
          pagination={{ pageSize, showSizeChanger: true, pageSizeOptions: [...PAGE_SIZE_OPTIONS] }}
          style={{ marginTop: 16 }}
          title={() => <b>Schema 漂移明细</b>}
          onRow={(r) => ({
            onClick: () => setDriftItem(r),
            style: { cursor: "pointer" },
          })}
          columns={[
            { title: "实体", dataIndex: "entity_name", key: "name", ellipsis: true },
            { title: "变更类型", dataIndex: "change_type", key: "type", width: 140 },
            {
              title: "变更内容",
              key: "diff",
              ellipsis: true,
              render: (_, r) => (
                <span className="mono" style={{ fontSize: 12 }}>
                  {r.diff_json ? JSON.stringify(r.diff_json) : "-"}
                </span>
              ),
            },
            {
              title: "时间",
              dataIndex: "created_at",
              key: "created",
              width: 180,
              render: (v: string) => formatCnTime(v),
            },
          ]}
        />
      )}
      <ChangeCatalogDetailDrawer item={catalogItem} onClose={() => setCatalogItem(null)} />
      <ChangeMetricDetailDrawer item={metricItem} onClose={() => setMetricItem(null)} />
      <ChangeDriftDetailDrawer item={driftItem} onClose={() => setDriftItem(null)} />
    </Card>
  );
}

function MyAssetsTab() {
  const navigate = useNavigate();
  const [data, setData] = useState<AssetMyAssets | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.my-assets.pageSize", 10);

  useEffect(() => {
    fetchAssetMyAssets()
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : "加载我的资产失败"))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <Spin />;
  if (error) return <Alert type="error" message={error} />;
  if (!data) return <Empty />;

  const s = data.summary ?? {
    catalog_count: data.catalogs.length,
    metric_count: data.metrics.length,
    draft_count: 0,
    pii_count: 0,
    snapshot_covered: 0,
    snapshot_total: 0,
  };

  return (
    <div>
      {data.claimable_orphans > 0 && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="有待认领资产"
          description={`当前有 ${data.claimable_orphans} 个孤儿资产（无责任人）。可在「孤儿资产」或「概览」页查看并认领，认领后即可在此工作台统一管理。`}
        />
      )}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={12} md={4}>
          <Card size="small">
            <Statistic title="我的目录" value={s.catalog_count} />
          </Card>
        </Col>
        <Col xs={12} md={4}>
          <Card size="small">
            <Statistic title="我的指标" value={s.metric_count} />
          </Card>
        </Col>
        <Col xs={12} md={4}>
          <Card size="small">
            <Statistic
              title="草稿待发布"
              value={s.draft_count}
              valueStyle={{ color: s.draft_count > 0 ? "#d46b08" : undefined }}
            />
          </Card>
        </Col>
        <Col xs={12} md={4}>
          <Card size="small">
            <Statistic
              title="PII 指标"
              value={s.pii_count}
              valueStyle={{ color: s.pii_count > 0 ? "#cf1322" : undefined }}
            />
          </Card>
        </Col>
        <Col xs={12} md={4}>
          <Card size="small">
            <Statistic
              title="快照覆盖"
              value={s.snapshot_covered}
              suffix={`/ ${s.snapshot_total}`}
            />
          </Card>
        </Col>
      </Row>
      <Table
        dataSource={data.catalogs}
        rowKey={(r) => `c-${r.id}`}
        size="small"
        pagination={{
          pageSize,
          showSizeChanger: true,
          pageSizeOptions: [...PAGE_SIZE_OPTIONS],
          onShowSizeChange,
        }}
        title={() => <b>我的目录</b>}
        columns={[
          { title: "实体", dataIndex: "entity_name", key: "name", ellipsis: true },
          {
            title: "类型",
            dataIndex: "entity_type",
            key: "type",
            width: 90,
            render: (v: string) => ENTITY_TYPE_LABEL[v] ?? v,
          },
          {
            title: "敏感度",
            dataIndex: "sensitivity_level",
            key: "sensitivity",
            width: 110,
            render: (s2: string | null) => sensitivityTag(s2),
          },
          {
            title: "数据源",
            key: "source",
            width: 140,
            ellipsis: true,
            render: (_, r) => (r.source_name ? `${r.source_name}（${r.source_id}）` : r.source_id),
          },
          {
            title: "字段数",
            dataIndex: "column_count",
            key: "columns",
            width: 80,
            render: (v: number | null | undefined) => v ?? "-",
          },
          {
            title: "描述",
            dataIndex: "description",
            key: "desc",
            ellipsis: true,
            render: (v: string | null | undefined) =>
              v ?? <span className="muted">暂无</span>,
          },
          {
            title: "更新时间",
            dataIndex: "updated_at",
            key: "updated",
            width: 170,
            render: (v: string | null | undefined) => (v ? formatCnTime(v) : "-"),
          },
        ]}
      />
      <Table
        dataSource={data.metrics}
        rowKey={(r) => `m-${r.metric_code}`}
        size="small"
        pagination={{
          pageSize,
          showSizeChanger: true,
          pageSizeOptions: [...PAGE_SIZE_OPTIONS],
          onShowSizeChange,
        }}
        style={{ marginTop: 16 }}
        title={() => <b>我的指标</b>}
        onRow={(r) => ({
          onClick: () => navigate(`/detail/${encodeURIComponent(r.metric_code)}`),
          style: { cursor: "pointer" },
        })}
        columns={[
          {
            title: "编码",
            dataIndex: "metric_code",
            key: "code",
            ellipsis: true,
            render: (v: string) => (
              <a
                className="mono"
                onClick={(e) => {
                  e.stopPropagation();
                  navigate(`/detail/${encodeURIComponent(v)}`);
                }}
              >
                {v}
              </a>
            ),
          },
          { title: "名称", dataIndex: "name", key: "name", ellipsis: true },
          { title: "状态", dataIndex: "status", key: "status", width: 100, render: (v: string) => STATUS_LABEL[v] ?? v ?? "-" },
          {
            title: "类型",
            dataIndex: "type",
            key: "type",
            width: 90,
            render: (v: string | undefined) => METRIC_TYPE_LABEL[v ?? ""] ?? v ?? "-",
          },
          {
            title: "粒度",
            dataIndex: "granularity",
            key: "granularity",
            width: 90,
            render: (v: string | undefined) => GRANULARITY_LABEL[v ?? ""] ?? v ?? "-",
          },
          {
            title: "单位",
            dataIndex: "unit",
            key: "unit",
            width: 80,
            render: (v: string | undefined) => UNIT_LABEL[v ?? ""] ?? v ?? "-",
          },
          {
            title: "分级",
            dataIndex: "metric_tier",
            key: "tier",
            width: 70,
            render: (v: string | undefined) => <Tag color="blue">{METRIC_TIER_LABEL[v ?? ""] ?? v ?? "-"}</Tag>,
          },
          {
            title: "PII",
            dataIndex: "pii_flag",
            key: "pii",
            width: 70,
            render: (v: boolean) => (v ? <Tag color="red">PII</Tag> : null),
          },
          {
            title: "描述",
            dataIndex: "description",
            key: "desc",
            ellipsis: true,
            render: (v: string | null | undefined) =>
              v ?? <span className="muted">暂无</span>,
          },
        ]}
      />
    </div>
  );
}

export function AssetMap() {
  const { track } = useTracking();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  // 支持 ?tab= URL 直达（如从采集目录「返回变更追踪」跳回 ?tab=changes），
  // 避免返回时组件重新挂载丢失内部 Tabs 状态、永远回到默认「资产地图」。
  const urlTab = searchParams.get("tab") ?? "";
  const [activeTab, setActiveTab] = useState(
    urlTab && ["overview", "search", "graph", "heatmap", "description", "health", "pii", "changes", "mine", "owner", "orphans", "tables"].includes(urlTab)
      ? urlTab
      : "graph",
  );

  // 统一返回上一入口：优先回退浏览器历史（总览快捷入口等），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  useEffect(() => {
    track("view", undefined, "page", { page: "assetmap" });
  }, [track]);

  const tabItems = [
    {
      key: "overview",
      label: (
        <span>
          <GlobalOutlined /> 概览
        </span>
      ),
      children: <OverviewTab />,
    },
    {
      key: "search",
      label: (
        <span>
          <SearchOutlined /> 搜索
        </span>
      ),
      children: <SearchTab />,
    },
    {
      key: "graph",
      label: (
        <span>
          <ApartmentOutlined /> 资产地图
        </span>
      ),
      children: <GraphTab />,
    },
    {
      key: "heatmap",
      label: (
        <span>
          <HeatMapOutlined /> 热力视图
        </span>
      ),
      children: <HeatmapTab />,
    },
    {
      key: "description",
      label: (
        <span>
          <FileTextOutlined /> 描述缺失
        </span>
      ),
      // summary 模式：只读总览（统计卡 + 明细下钻），治理动作跳转采集目录完成（方案 A）
      children: (
        <DescriptionCoveragePanel
          variant="summary"
          onGovern={(entityName) =>
            navigate(
              entityName
                ? `/catalogs?focus=${encodeURIComponent(entityName)}&from=资产地图`
                : `/catalogs?from=资产地图`,
            )
          }
        />
      ),
    },
    {
      key: "health",
      label: (
        <span>
          <HeartOutlined /> 资产健康
        </span>
      ),
      children: <HealthTab />,
    },
    {
      key: "pii",
      label: (
        <span>
          <SafetyOutlined /> PII 合规
        </span>
      ),
      children: <PiiTab />,
    },
    {
      key: "changes",
      label: (
        <span>
          <TableOutlined /> 变更追踪
        </span>
      ),
      children: <ChangesTab />,
    },
    {
      key: "mine",
      label: (
        <span>
          <UserOutlined /> 我的资产
        </span>
      ),
      children: <MyAssetsTab />,
    },
    {
      key: "owner",
      label: (
        <span>
          <UserOutlined /> Owner 视图
        </span>
      ),
      children: <OwnerTab />,
    },
    {
      key: "orphans",
      label: (
        <span>
          <DeleteOutlined /> 孤儿资产
        </span>
      ),
      children: <OrphansTab />,
    },
    {
      key: "tables",
      label: (
        <span>
          <TableOutlined /> 数据表
        </span>
      ),
      children: <TablesTab />,
    },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
            返回
          </Button>
          <div className="page-kicker">Assets / Inventory</div>
          <h2>资产地图</h2>
          <p>目录、指标、敏感度、责任人——资产全貌一图纵览。</p>
        </div>
      </div>
      <Card styles={{ body: { paddingTop: 8 } }}>
        <Tabs activeKey={activeTab} onChange={setActiveTab} items={tabItems} />
      </Card>
    </div>
  );
}
