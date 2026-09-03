import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type Key,
  type ReactNode,
} from "react";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Input,
  Row,
  Col,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Progress,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  CheckOutlined,
  CloseOutlined,
  EditOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  clearBatchInferHistory,
  createBatchInferHistory,
  fetchAssetEntityDetail,
  fetchBatchInferHistory,
  fetchDescriptionCoverage,
  getBatchInferTask,
  inferColumnDescription,
  inferDescriptions,
  inferTableDescription,
  listCatalogDatabases,
  listDataSources,
  submitBatchInferTask,
  updateColumnDescription,
  updateTableDescription,
} from "../api";
import type {
  BatchInferHistoryEntry,
  BatchInferHistoryTable,
  BatchInferTask,
  DescriptionCoverage,
  TableCoverageItem,
} from "../api";
import type {
  AssetEntityDetail,
  DataSource,
  SchemaColumn,
} from "../types";
import { SchemaTable } from "./SchemaTable";
import { DrillDownDrawer } from "./assetmap/DrillDownDrawer";
import { ResizableDrawer } from "./ResizableDrawer";
import { ENTITY_TYPE_LABEL } from "../utils/enums";
import { formatCnTime } from "../utils/timeCn";
import { PAGE_SIZE_OPTIONS, usePersistentPageSize } from "../hooks/usePersistentPageSize";
import { usePermission } from "../hooks/usePermission";

/**
 * 概览指标 → 明细下钻的口径标识。
 * 每个口径对应一组对 per_table 的过滤/排序，点击指标数字后展示其贡献明细。
 */
type CoverageMetricKey =
  | "fieldCoverage"
  | "fieldsMissing"
  | "tablesMissing"
  | "totalTables";

export type DescriptionCoveragePanelProps = {
  /**
   * full（默认）：完整治理工作台——统计卡下钻 + 按表列缺失字段数表格 + 治理抽屉
   * （表级/字段级人工编辑与 LLM 推断）。采集目录嵌入使用。
   * summary：只读总览——统计卡下钻明细保留，治理动作经 onGovern 引导跳转采集目录。
   */
  variant?: "full" | "summary";
  /**
   * summary 模式回调：点击「前往采集目录治理」或下钻明细行时触发；
   * entityName 为待治理实体名（undefined 表示不指定具体表）。
   */
  onGovern?: (entityName?: string) => void;
};

/**
 * 暴露给父组件的命令式句柄：reload 触发覆盖数据重新拉取。
 * 采集目录主列表「刷新」按钮共享刷新治理面板（方案 D：去掉面板内重复刷新按钮）。
 */
export type DescriptionCoveragePanelHandle = {
  reload: () => void;
};

/**
 * 模块级 in-flight 去重集合（FR-023）：key -> 进行中的推断 Promise。
 *
 * LLM 推断是慢操作（数十秒）。用户退出页面再进入（组件卸载重建）时，
 * 组件内 loading 状态会丢失，若再次点击推断会对同一字段/表发起重复请求。
 * 该 Map 挂在模块级（跨组件实例共享），进行中的推断完成后才移除，从而
 * 在「退出再进」场景拦截重复调用。后端另有 Redis/进程内幂等兜底（409）。
 */
const inferInflight = new Map<string, Promise<unknown>>();

/** 若 key 对应的推断已在途中则返回 null（拦截）；否则执行并登记，完成时清理。 */
function runInflight<T>(key: string, task: () => Promise<T>): Promise<T> | null {
  if (inferInflight.has(key)) return null;
  const p = task().finally(() => inferInflight.delete(key));
  inferInflight.set(key, p);
  return p;
}

/** 后端 409 LLM_INFER_IN_PROGRESS：已有推断进行中（可能是其它会话/进程触发）。 */
function isInferInProgress(err: unknown): boolean {
  return (
    typeof err === "object" &&
    err !== null &&
    (err as { code?: string }).code === "LLM_INFER_IN_PROGRESS"
  );
}

/** 跨表批量推断：单张被选表的待执行动作（由 per_table 覆盖数据派生）。 */
type BatchTask = {
  catalog_id: number;
  entity_name: string;
  missing_fields: number;
  needs_table_desc: boolean;
  missing_field_names: string[];
};

/** 跨表批量推断：进度面板中单张表的实时状态。 */
type BatchProgressItem = {
  catalog_id: number;
  entity_name: string;
  status: "pending" | "running" | "done" | "error" | "cancelled";
  summary: string;
  /** 失败原因明细（成功为空，供 Tooltip 展示与重试定位）。 */
  detail?: string;
  /** 失败原因分类（汇总分桶与重试判断）。 */
  errorCategory?: BatchErrorCategory;
  /** 本次新增字段描述名（结果预览，成功后非空）。 */
  inferredNames?: string[];
  /** 本次新增字段描述数（结果汇总用）。 */
  added?: number;
  /** 本次跳过字段描述数（已有描述不覆盖）。 */
  skipped?: number;
};

/** 最近一次批量会话的失败表（localStorage 持久化，刷新后可一键重新勾选重试）。 */
type LastFailedTable = { catalog_id: number; entity_name: string };

/**
 * 批量失败原因分类（汇总分桶 + 重试可判断）：
 * - rate_limit：接口限流（429），短暂等待后重试通常可成功
 * - timeout：LLM 调用超时/网络中断，重试可成功
 * - in_progress：该表推断正在其它会话进行（后端幂等 409），重试大概率仍 409，建议稍后再试
 * - unknown：其它异常
 */
type BatchErrorCategory = "rate_limit" | "timeout" | "in_progress" | "unknown";

/**
 * 批量历史记录（localStorage 保留近 5 次会话，供查看与一键重新勾选重跑）。
 * 服务端持久化后条目携带 id/actor_name/tablesWithId（跨设备、团队可见）。
 */
type BatchHistoryEntry = {
  /** 服务端持久化 id（有值表示已写入服务端）。 */
  id?: number;
  /** 操作人姓名快照（服务端条目，团队治理动作可追溯）。 */
  actor_name?: string;
  ts: number;
  tables: string[];
  /** 服务端条目携带的表集（含 catalog_id，重新勾选更精确）。 */
  tablesWithId?: BatchInferHistoryTable[];
  done: number;
  failed: number;
  cancelled: number;
  added: number;
  elapsed: number;
  failedTables: LastFailedTable[];
};

/** 服务端条目 → 本地 BatchHistoryEntry 结构（created_at UTC ISO → epoch）。 */
function toLocalHistoryEntry(e: BatchInferHistoryEntry): BatchHistoryEntry {
  return {
    id: e.id,
    actor_name: e.actor_name ?? undefined,
    ts: new Date(e.created_at).getTime(),
    tables: e.tables.map((t) => t.entity_name),
    tablesWithId: e.tables,
    done: e.done,
    failed: e.failed,
    cancelled: e.cancelled,
    added: e.added,
    elapsed: e.elapsed,
    failedTables: e.failed_tables,
  };
}

/** 智能重试建议：按失败分桶给出重试策略（限流→降并发、超时→单表串行、并发冲突→稍后）。 */
type RetryAdvice = { mode: "lower" | "serial" | "wait" | "none"; message: string };

const BATCH_ERROR_LABEL: Record<BatchErrorCategory, string> = {
  rate_limit: "限流",
  timeout: "超时",
  in_progress: "并发",
  unknown: "未知",
};

/** 批量历史 localStorage key 与保留条数。 */
const BATCH_HISTORY_KEY = "unisense.desc-coverage.batchHistory";
const BATCH_HISTORY_LIMIT = 5;

/** 按错误特征归类批量失败原因（isInferInProgress 优先判定，再按文案匹配）。 */
function classifyBatchError(err: unknown): BatchErrorCategory {
  if (isInferInProgress(err)) return "in_progress";
  const msg = err instanceof Error ? err.message : String(err ?? "");
  const lower = msg.toLowerCase();
  if (/(rate\s*limit|429|限流)/.test(lower)) return "rate_limit";
  if (/(timeout|超时|etimedout|econnreset|econnaborted|read\s*timeout)/.test(lower)) {
    return "timeout";
  }
  return "unknown";
}

function readBatchHistory(): BatchHistoryEntry[] {
  try {
    const raw = localStorage.getItem(BATCH_HISTORY_KEY);
    return raw ? (JSON.parse(raw) as BatchHistoryEntry[]) : [];
  } catch {
    return [];
  }
}

function writeBatchHistory(entries: BatchHistoryEntry[]) {
  try {
    const trimmed = entries.slice(0, BATCH_HISTORY_LIMIT);
    if (trimmed.length > 0) {
      localStorage.setItem(BATCH_HISTORY_KEY, JSON.stringify(trimmed));
    } else {
      localStorage.removeItem(BATCH_HISTORY_KEY);
    }
  } catch {
    // localStorage 不可用时静默降级（不影响本次批量结果展示）
  }
}

function batchStatusTag(status: BatchProgressItem["status"]) {
  switch (status) {
    case "pending":
      return <Tag>等待</Tag>;
    case "running":
      return <Tag color="processing">推断中…</Tag>;
    case "done":
      return <Tag color="success">完成</Tag>;
    case "error":
      return <Tag color="error">失败</Tag>;
    case "cancelled":
      return <Tag>已取消</Tag>;
  }
}

/**
 * 把 Statistic 的 value 包装成可点击链接，点击触发下钻（沿用资产地图 OverviewTab 的交互）。
 */
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

function sensitivityTag(s: string | null | undefined) {
  if (!s) return <Tag>未知</Tag>;
  const color = s.includes("PII") ? "red" : SENSITIVITY_COLOR[s];
  return <Tag color={color}>{SENSITIVITY_LABEL[s] ?? s}</Tag>;
}

const SOURCE_TAG: Record<string, { label: string; color: string }> = {
  manual: { label: "人工编辑", color: "blue" },
  llm: { label: "LLM 推断", color: "purple" },
  schema: { label: "采集原始", color: "default" },
};

function descriptionSourceTag(source?: string | null) {
  if (!source) return null;
  const cfg = SOURCE_TAG[source];
  if (!cfg) return <Tag>{source}</Tag>;
  return <Tag color={cfg.color}>{cfg.label}</Tag>;
}

// ── 共享列片段（四个下钻口径 + 主表格复用）───────────────────────────────

const colTable: ColumnsType<TableCoverageItem>[number] = {
  title: "表 / 视图",
  dataIndex: "entity_name",
  key: "entity_name",
  ellipsis: true,
  render: (v: string) => <span className="mono">{v}</span>,
};

const colSource: ColumnsType<TableCoverageItem>[number] = {
  title: "数据源",
  dataIndex: "source_id",
  key: "source_id",
  width: 150,
  ellipsis: true,
  render: (v: string, r) =>
    r.source_name ? (
      <span>
        {r.source_name}
        <span className="muted">（{v}）</span>
      </span>
    ) : (
      v
    ),
};

const colDomain: ColumnsType<TableCoverageItem>[number] = {
  title: "域",
  dataIndex: "domain",
  key: "domain",
  width: 110,
  render: (v: string | null) => v ?? <span className="muted">-</span>,
};

const colType: ColumnsType<TableCoverageItem>[number] = {
  title: "类型",
  dataIndex: "entity_type",
  key: "entity_type",
  width: 90,
  render: (v: string) => ENTITY_TYPE_LABEL[v] ?? v,
};

const colSens: ColumnsType<TableCoverageItem>[number] = {
  title: "敏感度",
  dataIndex: "sensitivity_level",
  key: "sensitivity_level",
  width: 110,
  render: sensitivityTag,
};

const colOwner: ColumnsType<TableCoverageItem>[number] = {
  title: "责任人",
  dataIndex: "owner_name",
  key: "owner_name",
  width: 100,
  render: (v: string | null | undefined) => v ?? <span className="muted">-</span>,
};

const colTotal: ColumnsType<TableCoverageItem>[number] = {
  title: "字段数",
  dataIndex: "total_fields",
  key: "total_fields",
  width: 80,
  align: "right",
  sorter: (a, b) => a.total_fields - b.total_fields,
};

const colCovered: ColumnsType<TableCoverageItem>[number] = {
  title: "有描述",
  dataIndex: "covered_fields",
  key: "covered_fields",
  width: 80,
  align: "right",
  sorter: (a, b) => a.covered_fields - b.covered_fields,
};

const colMissing: ColumnsType<TableCoverageItem>[number] = {
  title: "缺失字段",
  dataIndex: "missing_fields",
  key: "missing_fields",
  width: 90,
  align: "right",
  sorter: (a, b) => a.missing_fields - b.missing_fields,
  render: (v: number) =>
    v > 0 ? <span style={{ color: "#cf1322" }}>{v}</span> : <span className="muted">{v}</span>,
};

/** 字段级覆盖进度条（字段描述覆盖率明细用）。 */
const colCoveragePct: ColumnsType<TableCoverageItem>[number] = {
  title: "覆盖率",
  dataIndex: "coverage_pct",
  key: "coverage_pct",
  width: 150,
  sorter: (a, b) => coveragePct(a) - coveragePct(b),
  render: (_v, r) => {
    const pct = coveragePct(r);
    return (
      <Space size={8}>
        <Progress
          percent={pct}
          size="small"
          strokeColor={pct >= 80 ? "#52c41a" : pct >= 50 ? "#faad14" : "#f5222d"}
          style={{ width: 80, margin: 0 }}
        />
        <span className={pct >= 80 ? undefined : pct >= 50 ? undefined : "muted"} style={{ fontSize: 12 }}>
          {pct}%
        </span>
      </Space>
    );
  },
};

/** 缺失字段名 Tag 列表（缺失字段明细用，最多展示 10 个后省略）。 */
const colMissingNames: ColumnsType<TableCoverageItem>[number] = {
  title: "缺失字段名",
  dataIndex: "missing_field_names",
  key: "missing_field_names",
  ellipsis: true,
  render: (v: string[] | undefined) => {
    if (!v || v.length === 0) return <span className="muted">-</span>;
    return (
      <Space size={[4, 4]} wrap>
        {v.slice(0, 10).map((n) => (
          <Tag key={n} color="red" className="mono">
            {n}
          </Tag>
        ))}
        {v.length > 10 ? <span className="muted">…{v.length - 10}</span> : null}
      </Space>
    );
  },
};

/** 表描述内容 + 来源标签（缺表描述明细用）。 */
const colDesc: ColumnsType<TableCoverageItem>[number] = {
  title: "表描述",
  dataIndex: "description",
  key: "description",
  ellipsis: true,
  render: (v: string | null | undefined, r) =>
    v ? (
      <span>
        {v} {descriptionSourceTag(r.description_source)}
      </span>
    ) : (
      <Tag color="orange">缺失</Tag>
    ),
};

/** 更新时间（上海时区中文，全部表资产明细用）。 */
const colUpdated: ColumnsType<TableCoverageItem>[number] = {
  title: "更新时间",
  dataIndex: "updated_at",
  key: "updated_at",
  width: 150,
  sorter: (a, b) =>
    (a.updated_at ? new Date(a.updated_at).getTime() : 0) -
    (b.updated_at ? new Date(b.updated_at).getTime() : 0),
  render: (v: string | null | undefined) =>
    v ? formatCnTime(v) : <span className="muted">-</span>,
};

/** 字段描述覆盖率（0-100）。 */
function coveragePct(r: TableCoverageItem): number {
  return r.total_fields > 0
    ? Math.round((r.covered_fields / r.total_fields) * 100)
    : 0;
}

/**
 * 四个下钻口径的差异化列：
 * - fieldCoverage 全局覆盖总览 → 强调覆盖率进度
 * - fieldsMissing 字段级治理   → 强调具体缺失字段名
 * - tablesMissing 表级补全     → 强调表描述现状 + 责任人
 * - totalTables   完整资产盘点 → 强调责任人 + 更新时间
 */
function buildMetricColumns(key: CoverageMetricKey): ColumnsType<TableCoverageItem> {
  switch (key) {
    case "fieldCoverage":
      return [colTable, colSource, colDomain, colTotal, colCovered, colMissing, colCoveragePct];
    case "fieldsMissing":
      return [colTable, colSource, colDomain, colSens, colMissingNames, colMissing, colTotal];
    case "tablesMissing":
      return [colTable, colSource, colDomain, colType, colSens, colDesc, colOwner];
    case "totalTables":
    default:
      return [
        colTable,
        colSource,
        colDomain,
        colType,
        colSens,
        colOwner,
        colTotal,
        colCovered,
        colMissing,
        colUpdated,
      ];
  }
}

/** 主表格（按表列缺失字段数）完整列。 */
function buildTableColumns(): ColumnsType<TableCoverageItem> {
  return [
    colTable,
    colSource,
    colDomain,
    colType,
    colSens,
    colDesc,
    colTotal,
    colCovered,
    colMissing,
  ];
}

/**
 * 描述缺失治理面板（方案 A：采集目录与资产地图共享同一套展示组件）。
 *
 * - full（采集目录）：统计卡下钻 + 按表列缺失字段数（治理优先级）+ 治理抽屉
 *   （表级/字段级 LLM 推断与人工编辑），运维/管理视角的完整治理工作台。
 * - summary（资产地图）：只读总览——统计卡下钻明细保留，治理动作引导跳转采集目录。
 */
export const DescriptionCoveragePanel = forwardRef<
  DescriptionCoveragePanelHandle,
  DescriptionCoveragePanelProps
>(function DescriptionCoveragePanel({ variant = "full", onGovern }, ref) {
  const isSummary = variant === "summary";
  const [coverage, setCoverage] = useState<DescriptionCoverage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // 治理筛选（full 模式）：数据源 + 库 + 表名关键词——服务端过滤统计卡与治理表格，
  // 批量推断/明细下钻天然只在当前筛选范围内（覆盖数据本身来自筛选后接口）
  const [sourceId, setSourceId] = useState("");
  const [sources, setSources] = useState<DataSource[]>([]);
  const [database, setDatabase] = useState("");
  const [databases, setDatabases] = useState<string[]>([]);
  const [keywordInput, setKeywordInput] = useState("");
  const [keyword, setKeyword] = useState("");
  const searchTimerRef = useRef<number | null>(null);
  // 按钮级权限点：无 catalog:infer-description 时隐藏 LLM 推断按钮（后端强制兜底）
  const canInferCatalog = usePermission().can("catalog:infer-description");
  // 编辑描述侧门修复：表级/字段级人工编辑也受 catalog:edit-description 管控
  const canEditDesc = usePermission().can("catalog:edit-description");

  // 详情抽屉
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detail, setDetail] = useState<AssetEntityDetail | null>(null);

  // 表级描述编辑态
  const [tableDescEditing, setTableDescEditing] = useState(false);
  const [tableDescDraft, setTableDescDraft] = useState("");
  const [tableDescSaving, setTableDescSaving] = useState(false);
  const [tableInferring, setTableInferring] = useState(false);

  // 跨表批量推断：主表格勾选多表 → 单弹窗（确认视图 → 进度视图）→ 并发逐表推断
  const [selectedRowKeys, setSelectedRowKeys] = useState<Key[]>([]);
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchStarted, setBatchStarted] = useState(false);
  const [batchRunning, setBatchRunning] = useState(false);
  const [batchFinished, setBatchFinished] = useState(false);
  const [batchProgress, setBatchProgress] = useState<BatchProgressItem[]>([]);
  /** 当前批次的完整任务集（供「重试失败项」复用，不受勾选清空影响）。 */
  const [batchTasks, setBatchTasks] = useState<BatchTask[]>([]);
  /** 并发数（1/2/3/5/8/10，默认 2），localStorage 持久化。 */
  const [batchConcurrency, setBatchConcurrency] = useState<number>(() => {
    const v = Number(localStorage.getItem("unisense.desc-coverage.batchConcurrency"));
    return Number.isInteger(v) && v >= 1 && v <= 10 ? v : 2;
  });
  /** 上次批量会话失败表（localStorage 持久化，刷新后可一键重新勾选重试）。 */
  const [lastFailed, setLastFailed] = useState<LastFailedTable[]>(() => {
    try {
      const raw = localStorage.getItem("unisense.desc-coverage.lastBatchFailed");
      return raw ? (JSON.parse(raw) as LastFailedTable[]) : [];
    } catch {
      return [];
    }
  });
  const [batchElapsed, setBatchElapsed] = useState(0);
  /** 批量历史（近 5 次会话，localStorage 持久化）。 */
  const [batchHistory, setBatchHistory] = useState<BatchHistoryEntry[]>(readBatchHistory);
  /** 批量面板内「历史记录」视图开关。 */
  const [historyOpen, setHistoryOpen] = useState(false);
  /** 运行中取消标志（ref 保证异步调度内可靠读写）。 */
  const cancelRef = useRef(false);
  /** 本批次 AbortController：取消时中止 in-flight 的 LLM 请求（快速结束，而非干等最慢请求自然完成）。 */
  const batchAbortRef = useRef<AbortController | null>(null);
  /** 点击取消后的即时反馈状态（按钮切「正在取消…」防重复点击）。 */
  const [cancelling, setCancelling] = useState(false);
  /**
   * 本次批量任务中已成功完成（服务端 progress.done / 本地 ok）的表 id 集合：
   * 用于把已补全的表「实时」从治理主列表移除（不等整批结束 load()）。
   * 任务终态/本地批次结束后由 load() 同步服务端数据并清空。
   */
  const [coveredIds, setCoveredIds] = useState<Set<number>>(() => new Set());
  /** 服务端批量任务逐表完成轮询句柄（提交后端任务后启动，终态/组件卸载清理）。 */
  const serverPollRef = useRef<number | null>(null);

  // 概览指标下钻明细（点击指标数字 → 该口径贡献的 per_table 子集）
  const [metricDrillOpen, setMetricDrillOpen] = useState(false);
  const [metricDrillTitle, setMetricDrillTitle] = useState("");
  const [metricDrillRows, setMetricDrillRows] = useState<TableCoverageItem[]>([]);
  // 各口径差异化列（见 buildMetricColumns）
  const [metricDrillColumns, setMetricDrillColumns] = useState<
    ColumnsType<TableCoverageItem>
  >([]);
  const { pageSize, onShowSizeChange } = usePersistentPageSize(
    "unisense.desc-coverage.pageSize",
    20,
  );

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setCoverage(
        await fetchDescriptionCoverage({
          source_id: sourceId || undefined,
          database: database || undefined,
          keyword: keyword || undefined,
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载描述覆盖统计失败");
    } finally {
      setLoading(false);
    }
  }

  /** 表名搜索防抖（350ms，对齐采集目录主列表）：输入即时更新，查询值延迟提交重新拉取。 */
  function scheduleKeyword(value: string) {
    setKeywordInput(value);
    if (searchTimerRef.current !== null) window.clearTimeout(searchTimerRef.current);
    searchTimerRef.current = window.setTimeout(() => setKeyword(value), 350);
  }

  /**
   * 从服务端加载批量推断历史（跨设备/团队可见，按时间倒序）。
   * 服务端返回非空记录才覆盖本地（避免服务端刚启用/被清空时误清用户本地缓存历史）；
   * 服务端不可用（网络/权限）时静默保留 localStorage 缓存。
   */
  async function loadServerHistory() {
    try {
      const entries = await fetchBatchInferHistory();
      if (entries.length > 0) {
        const mapped = entries.map(toLocalHistoryEntry);
        setBatchHistory(mapped);
        writeBatchHistory(mapped);
      }
    } catch {
      // 服务端不可用：保留 localStorage 缓存（离线可继续用）
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceId, database, keyword]);

  // 服务端批量历史与筛选无关，仅挂载拉一次
  useEffect(() => {
    void loadServerHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 治理筛选数据源下拉选项（与采集目录登记实体下拉同源，page_size 200 全量）；
  // summary 只读总览不展示筛选栏，跳过避免多余请求
  useEffect(() => {
    if (isSummary) return;
    listDataSources({ page: 1, page_size: 200 })
      .then((res) => setSources(res.items))
      .catch(() => {});
  }, [isSummary]);

  // 库名下拉选项（entity_name 前缀，随所选数据源联动收窄；无数据源时取全局库列表）
  useEffect(() => {
    if (isSummary) return;
    listCatalogDatabases(sourceId || undefined)
      .then(setDatabases)
      .catch(() => {});
  }, [isSummary, sourceId]);

  // 卸载清理表名搜索防抖定时器
  useEffect(
    () => () => {
      if (searchTimerRef.current !== null) window.clearTimeout(searchTimerRef.current);
    },
    [],
  );

  // 暴露 reload 给父组件：采集目录主列表「刷新」按钮共享刷新治理面板（方案 D）
  useImperativeHandle(ref, () => ({ reload: load }));

  // 并发数偏好持久化（下次进入保留用户选择）
  useEffect(() => {
    localStorage.setItem("unisense.desc-coverage.batchConcurrency", String(batchConcurrency));
  }, [batchConcurrency]);

  async function openDetail(catalogId: number) {
    // 关闭下钻明细抽屉：从「明细列表」下钻到「单表详情」时，列表抽屉让位，
    // 避免两个 Drawer 同时打开（antd 均挂 body Portal）导致详情被明细盖住无法查看。
    setMetricDrillOpen(false);
    setDetailOpen(true);
    setDetailLoading(true);
    setDetail(null);
    setTableDescEditing(false);
    try {
      setDetail(await fetchAssetEntityDetail(catalogId));
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

  // 概览指标 → 明细下钻：根据口径过滤/排序已加载的 per_table 子集
  function openMetricDrill(key: CoverageMetricKey) {
    if (!coverage) return;
    const byMissingDesc = (a: TableCoverageItem, b: TableCoverageItem) =>
      b.missing_fields - a.missing_fields;
    let rows: TableCoverageItem[];
    let title: string;
    switch (key) {
      case "fieldCoverage":
        // 字段描述覆盖率 = 已描述字段 / 总字段；展示各表字段覆盖，未完全覆盖的排在前面
        rows = [...coverage.per_table].sort(byMissingDesc);
        title = `字段描述覆盖率明细（各表字段覆盖 · 共 ${coverage.total_tables} 张表）`;
        break;
      case "fieldsMissing":
        // 缺失字段数 = 各表 missing_fields 之和；仅列出仍有缺失字段的表
        rows = coverage.per_table
          .filter((t) => t.missing_fields > 0)
          .sort(byMissingDesc);
        title = `缺失字段明细（${rows.length} 张表待补全字段描述 · 共 ${coverage.fields_missing_desc} 个字段）`;
        break;
      case "tablesMissing":
        // 缺表描述：仅列出 table_desc 为 false 的表
        rows = coverage.per_table
          .filter((t) => !t.table_desc)
          .sort(byMissingDesc);
        title = `缺表描述明细（${rows.length} 张表待补全表级描述）`;
        break;
      case "totalTables":
      default:
        rows = [...coverage.per_table];
        title = `全部表资产明细（共 ${coverage.total_tables} 张表）`;
        break;
    }
    setMetricDrillTitle(title);
    setMetricDrillRows(rows);
    setMetricDrillColumns(buildMetricColumns(key));
    setMetricDrillOpen(true);
  }

  async function handleFieldEdit(col: SchemaColumn, newDesc: string) {
    if (!detail) return;
    await updateColumnDescription(detail.id, col.name, newDesc);
    message.success(`字段「${col.name}」描述已保存`);
    await refreshDetail();
  }

  async function handleFieldInfer(col: SchemaColumn) {
    if (!detail) return;
    const key = `column:${detail.id}:${col.name}`;
    const p = runInflight(key, () =>
      inferColumnDescription(detail.id, col.name, {
        entity_name: detail.entity_name,
        column_type: col.type,
      }).then(() => {
        message.success(`字段「${col.name}」描述已生成`);
        return refreshDetail();
      }),
    );
    if (!p) {
      message.info("该字段的 LLM 推断正在进行中，请稍候");
      return;
    }
    try {
      await p;
    } catch (err) {
      if (isInferInProgress(err)) {
        message.info("该字段的 LLM 推断正在进行中，请稍候");
      } else {
        message.error(err instanceof Error ? err.message : "推断失败");
      }
    }
  }

  async function handleBatchInfer() {
    if (!detail) return;
    const key = `batch:${detail.id}`;
    const p = runInflight(key, () =>
      inferDescriptions(detail.id).then((res) => {
        message.success(
          `批量推断完成：成功 ${res.inferred.length}，跳过 ${res.skipped.length}，失败 ${res.failed.length}`,
        );
        return refreshDetail();
      }),
    );
    if (!p) {
      message.info("该表的批量推断正在进行中，请稍候");
      return;
    }
    try {
      await p;
    } catch (err) {
      if (isInferInProgress(err)) {
        message.info("该表的批量推断正在进行中，请稍候");
      } else {
        message.error(err instanceof Error ? err.message : "批量推断失败");
      }
    }
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

  async function handleTableDescInfer() {
    if (!detail) return;
    setTableInferring(true);
    const key = `table:${detail.id}`;
    const fields = Array.isArray(detail.schema_summary)
      ? detail.schema_summary.map((c) => ({ name: c.name, type: c.type }))
      : [];
    const p = runInflight(key, () =>
      inferTableDescription(detail.id, fields).then(() => {
        message.success("表级描述已生成");
        return refreshDetail();
      }),
    );
    if (!p) {
      setTableInferring(false);
      message.info("该表的表级推断正在进行中，请稍候");
      return;
    }
    try {
      await p;
    } catch (err) {
      if (isInferInProgress(err)) {
        message.info("该表的表级推断正在进行中，请稍候");
      } else {
        message.error(err instanceof Error ? err.message : "推断表描述失败");
      }
    } finally {
      setTableInferring(false);
    }
  }

  // ---- 跨表批量推断 ----

  /**
   * 单张表按需执行字段批量推断（missing_fields>0）与表描述推断（!table_desc）。
   * 各动作独立 try/catch：单动作失败不阻断另一动作，返回汇总文本、失败明细与统计。
   * 复用后端单表批量端点（FR-023 幂等 + in-flight 锁 + 审计），不覆盖已有 manual/llm 描述。
   */
  async function inferOneTable(
    task: BatchTask,
    signal?: AbortSignal,
  ): Promise<{
    ok: boolean;
    summary: string;
    detail?: string;
    added: number;
    skipped: number;
    errorCategory?: BatchErrorCategory;
    inferredNames: string[];
  }> {
    const parts: string[] = [];
    const errs: string[] = [];
    let ok = true;
    let added = 0;
    let skipped = 0;
    let errorCategory: BatchErrorCategory | undefined;
    const inferredNames: string[] = [];
    if (task.missing_fields > 0) {
      try {
        const res = await inferDescriptions(task.catalog_id, signal);
        added = res.inferred.length;
        skipped = res.skipped.length;
        inferredNames.push(...res.inferred.map((i) => i.column_name));
        parts.push(`字段 +${res.inferred.length}（跳过 ${res.skipped.length}）`);
        if (res.failed.length > 0) {
          ok = false;
          errs.push(`字段失败 ${res.failed.length} 个：${res.failed.slice(0, 3).join("、")}`);
        }
      } catch (err) {
        // 用户取消：把中止向上抛给调度层（标已取消），不按失败计
        if (signal?.aborted) throw err;
        ok = false;
        const cat = classifyBatchError(err);
        errorCategory = errorCategory ?? cat;
        const msg = isInferInProgress(err)
          ? "字段推断进行中（可能在其它会话执行）"
          : `字段推断失败：${err instanceof Error ? err.message : "未知错误"}`;
        parts.push(msg);
        errs.push(`[${BATCH_ERROR_LABEL[cat]}] ${msg}`);
      }
    }
    if (task.needs_table_desc) {
      try {
        await inferTableDescription(task.catalog_id, undefined, undefined, signal);
        parts.push("表描述已生成");
      } catch (err) {
        // 用户取消：把中止向上抛给调度层（标已取消），不按失败计
        if (signal?.aborted) throw err;
        ok = false;
        const cat = classifyBatchError(err);
        errorCategory = errorCategory ?? cat;
        const msg = isInferInProgress(err)
          ? "表描述推断进行中（可能在其它会话执行）"
          : `表描述推断失败：${err instanceof Error ? err.message : "未知错误"}`;
        parts.push(msg);
        errs.push(`[${BATCH_ERROR_LABEL[cat]}] ${msg}`);
      }
    }
    return {
      ok,
      summary: parts.join("；") || "无缺失描述",
      detail: errs.join("；") || undefined,
      added,
      skipped,
      errorCategory,
      inferredNames,
    };
  }

  /** 把失败表写入 localStorage 并同步 state（全部成功则清除），供刷新后一键重试。 */
  function persistLastFailed(failed: LastFailedTable[]) {
    setLastFailed(failed);
    try {
      if (failed.length > 0) {
        localStorage.setItem("unisense.desc-coverage.lastBatchFailed", JSON.stringify(failed));
      } else {
        localStorage.removeItem("unisense.desc-coverage.lastBatchFailed");
      }
    } catch {
      // localStorage 不可用时静默降级（不影响本次批量结果展示）
    }
  }

  /**
   * 有界并发逐表执行批量推断，实时更新进度；支持运行中取消（未启动任务标已取消）。
   * 重试语义：传入 initial 进度时保留已完成/已取消项，仅把 error 且命中 resetIds 的项
   * 重置为 pending 重新执行（汇总反映整批结果，而非重试子集）。
   * 全部完成后刷新覆盖数据、清空勾选，并把失败表持久化供下次进入一键重试。
   */
  async function runBatchInfer(
    tasks: BatchTask[],
    concurrency: number,
    initial?: BatchProgressItem[],
    resetIds?: Set<number>,
  ) {
    const progress: BatchProgressItem[] = tasks.map((t) => {
      const prev = initial?.find((p) => p.catalog_id === t.catalog_id);
      const shouldReset =
        prev?.status === "error" && (!resetIds || resetIds.has(t.catalog_id));
      if (prev && !shouldReset) return prev;
      return { catalog_id: t.catalog_id, entity_name: t.entity_name, status: "pending", summary: "" };
    });
    // 重试场景：initial 中已 done 的表保持从治理主列表移除（其描述上一轮已补全）
    const initialDoneIds = new Set(
      (initial ?? []).filter((p) => p.status === "done").map((p) => p.catalog_id),
    );
    setCoveredIds(initialDoneIds);
    setBatchProgress(progress);
    setBatchRunning(true);
    setBatchFinished(false);
    setCancelling(false);
    cancelRef.current = false;
    // 每次批次开始重建 AbortController（取消旧批次遗留引用），供取消时中止 in-flight
    batchAbortRef.current?.abort();
    batchAbortRef.current = new AbortController();
    const batchSignal = batchAbortRef.current.signal;
    const startMs = Date.now();
    const taskById = new Map(tasks.map((t) => [t.catalog_id, t]));
    const workIdx = progress
      .map((p, i) => (p.status === "pending" ? i : -1))
      .filter((i) => i >= 0);
    let next = 0;
    const worker = async () => {
      while (!cancelRef.current) {
        const k = next++;
        if (k >= workIdx.length) break;
        const i = workIdx[k];
        const task = taskById.get(progress[i].catalog_id);
        if (!task) continue;
        progress[i] = { ...progress[i], status: "running" };
        setBatchProgress([...progress]);
        let r: Awaited<ReturnType<typeof inferOneTable>>;
        try {
          r = await inferOneTable(task, batchSignal);
        } catch {
          // 用户取消（AbortSignal 已触发）→ 该表标已取消，不按失败计
          progress[i] = { ...progress[i], status: "cancelled", summary: "已取消" };
          setBatchProgress([...progress]);
          continue;
        }
        progress[i] = {
          ...progress[i],
          status: r.ok ? "done" : "error",
          summary: r.summary,
          detail: r.detail,
          errorCategory: r.errorCategory,
          inferredNames: r.inferredNames,
          added: r.added,
          skipped: r.skipped,
        };
        setBatchProgress([...progress]);
        // 该表已补全：实时从治理主列表移除（不等整批结束 load），并从勾选移除
        if (r.ok) {
          setCoveredIds((prev) => {
            const nx = new Set(prev);
            nx.add(task.catalog_id);
            return nx;
          });
          setSelectedRowKeys((prev) => prev.filter((k) => Number(k) !== task.catalog_id));
        }
      }
    };
    await Promise.all(
      Array.from({ length: Math.max(1, Math.min(concurrency, workIdx.length)) }, () => worker()),
    );
    const final = progress.map((p) =>
      p.status === "pending" ? { ...p, status: "cancelled" as const, summary: "未执行" } : p,
    );
    setBatchProgress(final);
    setBatchRunning(false);
    setBatchFinished(true);
    setCancelling(false);
    batchAbortRef.current = null;
    setBatchElapsed(Math.round((Date.now() - startMs) / 1000));
    // 写入批量历史（近 5 次）：含结果摘要与失败表，供历史视图查看与一键重跑；
    // 同时 best-effort 持久化到服务端（跨设备/团队可见，失败静默降级 localStorage）。
    const entry: BatchHistoryEntry = {
      ts: Date.now(),
      tables: tasks.map((t) => t.entity_name),
      tablesWithId: tasks.map((t) => ({ catalog_id: t.catalog_id, entity_name: t.entity_name })),
      done: final.filter((p) => p.status === "done").length,
      failed: final.filter((p) => p.status === "error").length,
      cancelled: final.filter((p) => p.status === "cancelled").length,
      added: final.reduce((s, p) => s + (p.added ?? 0), 0),
      elapsed: Math.round((Date.now() - startMs) / 1000),
      failedTables: final
        .filter((p) => p.status === "error")
        .map((p) => ({ catalog_id: p.catalog_id, entity_name: p.entity_name })),
    };
    setBatchHistory((prev) => {
      const next = [entry, ...prev].slice(0, BATCH_HISTORY_LIMIT);
      writeBatchHistory(next);
      return next;
    });
    void persistServerHistory(entry, tasks);
    if (cancelRef.current) return;
    await load();
    setSelectedRowKeys([]);
    // load 已同步服务端最新数据（成功表已不在待治理列表），coveredIds 使命完成
    setCoveredIds(new Set());
    persistLastFailed(
      final.filter((p) => p.status === "error").map((p) => ({
        catalog_id: p.catalog_id,
        entity_name: p.entity_name,
      })),
    );
  }

  /** 批量历史 best-effort 持久化到服务端（跨设备/团队可见）；失败静默降级 localStorage。 */
  async function persistServerHistory(entry: BatchHistoryEntry, tasks: BatchTask[]) {
    try {
      await createBatchInferHistory({
        tables: tasks.map((t) => ({ catalog_id: t.catalog_id, entity_name: t.entity_name })),
        done: entry.done,
        failed: entry.failed,
        cancelled: entry.cancelled,
        added: entry.added,
        elapsed: entry.elapsed,
        failed_tables: entry.failedTables,
      });
      // 刷新服务端列表（拿回 id/操作人快照，历史视图展示团队操作人）
      await loadServerHistory();
    } catch {
      // 服务端不可用：localStorage 缓存仍在，离线可继续用
    }
  }

  /** 批量推断入口：整批 in-flight 去重（key=表集签名），避免重复点击重复调 LLM。 */
  function startBatchInfer(
    tasks: BatchTask[],
    initial?: BatchProgressItem[],
    resetIds?: Set<number>,
    concurrency?: number,
  ) {
    const key = `cross:${tasks.map((t) => t.catalog_id).sort((a, b) => a - b).join(",")}`;
    const eff = concurrency ?? batchConcurrency;
    const p = runInflight(key, () => runBatchInfer(tasks, eff, initial, resetIds));
    if (!p) {
      message.info("该批量推断正在进行中，请稍候");
      return;
    }
    setBatchTasks(tasks);
    setBatchStarted(true);
  }

  /** 重试全部失败项：合并进同一批次视图，仅重置 error 项重新执行，其余结果保留。 */
  function retryBatch() {
    const failed = batchTasks.filter((t) =>
      batchProgress.some((p) => p.catalog_id === t.catalog_id && p.status === "error"),
    );
    if (failed.length === 0) return;
    startBatchInfer(batchTasks, batchProgress);
  }

  /** 智能重试建议：按失败分桶给出重试策略（限流→降并发、超时→单表串行、并发冲突→稍后）。 */
  function computeRetryAdvice(): RetryAdvice {
    if (batchErrorBuckets.rate_limit > 0) {
      return {
        mode: batchConcurrency > 1 ? "lower" : "none",
        message:
          batchConcurrency > 1
            ? `检测到限流（${batchErrorBuckets.rate_limit} 张），建议降低并发重试（自动降至 ${Math.max(1, batchConcurrency - 1)}）`
            : "检测到限流但已是最低并发，建议稍后重试",
      };
    }
    if (batchErrorBuckets.timeout > 0) {
      return {
        mode: "serial",
        message: `检测到超时（${batchErrorBuckets.timeout} 张），建议单表串行重试（并发 1，降低 LLM 压力）`,
      };
    }
    if (batchErrorBuckets.in_progress > 0) {
      return {
        mode: "wait",
        message: "部分表正在其它会话推断（409 并发冲突），建议稍后重试",
      };
    }
    return { mode: "none", message: "" };
  }

  /**
   * 智能重试：按失败分桶自动调整并发后重试——
   * - 限流：自动降至更低并发（min(当前-1, 1)），并持久化新并发偏好
   * - 超时：自动降为单表串行（并发 1）
   * - 并发冲突：提示稍后，不自动重试
   * - 其它/无失败：维持当前并发
   */
  function retryBatchSmart() {
    const failed = batchTasks.filter((t) =>
      batchProgress.some((p) => p.catalog_id === t.catalog_id && p.status === "error"),
    );
    if (failed.length === 0) return;
    const advice = computeRetryAdvice();
    if (advice.mode === "lower") {
      const next = Math.max(1, batchConcurrency - 1);
      setBatchConcurrency(next);
      startBatchInfer(batchTasks, batchProgress, undefined, next);
      return;
    }
    if (advice.mode === "serial") {
      setBatchConcurrency(1);
      startBatchInfer(batchTasks, batchProgress, undefined, 1);
      return;
    }
    if (advice.mode === "wait") {
      message.info(advice.message);
      return;
    }
    startBatchInfer(batchTasks, batchProgress);
  }

  /** 单表重试（进度表格失败行内的「重试」按钮）：仅重置该表，其余失败项保持失败。 */
  function retryOne(catalogId: number) {
    if (!batchTasks.some((t) => t.catalog_id === catalogId)) return;
    startBatchInfer(batchTasks, batchProgress, new Set([catalogId]));
  }

  /** 运行中取消：停止调度未启动任务，并中止进行中的 LLM 请求（快速结束批次，被中止表标已取消）。 */
  function cancelBatch() {
    cancelRef.current = true;
    batchAbortRef.current?.abort();
    setCancelling(true);
  }

  /** 关闭批量面板并重置状态（推断中不可关闭）。 */
  function closeBatch() {
    setBatchOpen(false);
    setBatchStarted(false);
    setBatchFinished(false);
    setBatchProgress([]);
    setBatchTasks([]);
    setHistoryOpen(false);
  }

  /** 打开批量面板（确认视图）：重置到未开始状态。 */
  function openBatch() {
    setBatchStarted(false);
    setBatchFinished(false);
    setBatchProgress([]);
    setHistoryOpen(false);
    setBatchOpen(true);
  }

  /** 组件卸载时清理服务端任务轮询（避免跨实例泄漏定时器）。 */
  useEffect(
    () => () => {
      if (serverPollRef.current) {
        window.clearInterval(serverPollRef.current);
        serverPollRef.current = null;
      }
    },
    [],
  );

  /**
   * 提交后端批量任务后，轮询单任务进度（复用 getBatchInferTask，4s/次）：
   * 每张表 progress.status=done → 实时加入 coveredIds（治理主列表即时移除该行）并从勾选移除；
   * 任务终态（completed/failed/cancelled）→ 停轮询 + load() 同步服务端最新数据 + 清空 coveredIds。
   * 失败/取消表不进 coveredIds——load 后仍留在列表供重试。
   */
  function trackServerTaskRemoval(taskId: number) {
    const stopPoll = () => {
      if (serverPollRef.current) {
        window.clearInterval(serverPollRef.current);
        serverPollRef.current = null;
      }
    };
    const apply = (t: BatchInferTask) => {
      const doneIds = t.progress
        .filter((p) => p.status === "done" && p.catalog_id != null)
        .map((p) => p.catalog_id as number);
      if (doneIds.length > 0) {
        setCoveredIds((prev) => {
          const nx = new Set(prev);
          doneIds.forEach((id) => nx.add(id));
          return nx;
        });
        setSelectedRowKeys((prev) => prev.filter((k) => !doneIds.includes(Number(k))));
      }
      if (!["pending", "running"].includes(t.status)) {
        stopPoll();
        void (async () => {
          await load();
          setCoveredIds(new Set());
        })();
      }
    };
    const pollOnce = async () => {
      try {
        apply(await getBatchInferTask(taskId));
      } catch {
        // 网络抖动静默（下轮继续）；任务在右下角任务中心仍有独立轮询展示
      }
    };
    stopPoll();
    // 启动即先拉一次（若任务已有完成表立即移除），此后每 4s 增量
    void pollOnce();
    serverPollRef.current = window.setInterval(pollOnce, 4000);
  }

  /**
   * 提交跨表批量推断为后端任务（方案 B：arq 执行 + 进度落库）。
   * 关闭本地进度弹窗，进度/结果经右下角「批量任务中心」（BatchInferCenter）跨页可见，
   * 刷新不丢（服务端 batch_llm_infer_task 行，前端轮询恢复）。
   */
  async function submitBackendBatch(tasks: BatchTask[]) {
    if (tasks.length === 0) return;
    try {
      const task = await submitBatchInferTask({
        tasks: tasks.map((t) => ({
          catalog_id: t.catalog_id,
          entity_name: t.entity_name,
          missing_fields: t.missing_fields,
          needs_table_desc: t.needs_table_desc,
        })),
        concurrency: batchConcurrency,
      });
      message.success(
        `批量推断任务已提交（#${task.id}，共 ${task.total} 张表）——进度与结果在右下角「批量任务中心」实时查看`,
      );
      setLastFailed([]);
      try {
        localStorage.removeItem("unisense.desc-coverage.lastBatchFailed");
      } catch {
        // localStorage 不可用时静默降级
      }
      closeBatch();
      await load();
      setSelectedRowKeys([]);
      // 逐表完成轮询：某表推断完成即实时从治理主列表移除，不等整批结束
      setCoveredIds(new Set());
      trackServerTaskRemoval(task.id);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "提交批量推断任务失败");
    }
  }

  /** 从上次失败记录一键恢复：重新勾选失败表并打开确认面板（并清除旧提示）。 */
  function relaunchLastFailed() {
    setSelectedRowKeys(lastFailed.map((f) => f.catalog_id));
    setLastFailed([]);
    try {
      localStorage.removeItem("unisense.desc-coverage.lastBatchFailed");
    } catch {
      // localStorage 不可用时静默降级
    }
    setHistoryOpen(false);
    setBatchOpen(true);
    setBatchStarted(false);
    setBatchProgress([]);
  }

  /**
   * 从批量历史一键重跑：重新勾选该次会话涉及的、当前仍可勾选的表（有失败表则优先失败表，
   * 无失败表则按表名匹配全量），并回到确认面板。表可能已被补全或删除，仅保留仍可勾选的。
   * 服务端条目携带 catalog_id（tablesWithId/failedTables），匹配更精确。
   */
  function relaunchHistory(entry: BatchHistoryEntry) {
    if (!coverage) return;
    const failedIds = new Set(entry.failedTables.map((f) => f.catalog_id));
    const failedNames = new Set(entry.failedTables.map((f) => f.entity_name));
    const allIds = new Set((entry.tablesWithId ?? []).map((t) => t.catalog_id));
    const allNames = new Set(entry.tables);
    const selectable = (t: TableCoverageItem) => !(t.missing_fields === 0 && !!t.table_desc);
    const ids = coverage.per_table
      .filter(
        (t) =>
          selectable(t) &&
          (failedIds.has(t.catalog_id) ||
            failedNames.has(t.entity_name) ||
            (entry.failedTables.length === 0 &&
              (allIds.has(t.catalog_id) || allNames.has(t.entity_name)))),
      )
      .map((t) => t.catalog_id);
    setSelectedRowKeys(ids);
    setHistoryOpen(false);
    setBatchStarted(false);
    setBatchFinished(false);
    setBatchProgress([]);
    setBatchOpen(true);
  }

  /** 清空批量历史（服务端当前用户自己的记录 + localStorage + state）。 */
  async function clearBatchHistory() {
    setBatchHistory([]);
    writeBatchHistory([]);
    try {
      await clearBatchInferHistory();
    } catch {
      // 服务端不可用：本地已清空
    }
  }

  /** 一键勾选所有存在描述缺失的表（字段缺失或表描述缺失）。 */
  function selectAllMissing() {
    if (!coverage) return;
    setSelectedRowKeys(
      coverage.per_table
        .filter((t) => !(t.missing_fields === 0 && !!t.table_desc))
        .map((t) => t.catalog_id),
    );
  }

  if (loading && !coverage) return <Spin tip="加载描述覆盖统计…" />;
  if (error) return <Alert type="error" message={error} />;
  if (!coverage) return <Empty description="暂无覆盖数据" />;

  const fieldCoveragePct =
    coverage.total_fields > 0
      ? Math.round((coverage.fields_with_desc / coverage.total_fields) * 100)
      : 0;
  const tableCoveragePct =
    coverage.total_tables > 0
      ? Math.round((coverage.tables_with_desc / coverage.total_tables) * 100)
      : 0;

  const tableCoverageCols: ColumnsType<TableCoverageItem> = buildTableColumns();
  const schemaColumns = Array.isArray(detail?.schema_summary)
    ? detail?.schema_summary
    : [];
  // 勾选表 → 待执行动作清单（字段缺失 + 表描述缺失，供确认弹窗展示与串行执行）
  const selectedTasks: BatchTask[] = coverage.per_table
    .filter((t) => selectedRowKeys.includes(t.catalog_id))
    .map((t) => ({
      catalog_id: t.catalog_id,
      entity_name: t.entity_name,
      missing_fields: t.missing_fields,
      needs_table_desc: !t.table_desc,
      missing_field_names: t.missing_field_names ?? [],
    }));

  // 批量进度汇总（结果卡展示；失败按原因分桶统计，如「失败 2 张（限流×1 · 超时×1）」）
  const batchDoneCount = batchProgress.filter((p) => p.status === "done").length;
  const batchErrorCount = batchProgress.filter((p) => p.status === "error").length;
  const batchCancelledCount = batchProgress.filter((p) => p.status === "cancelled").length;
  const batchAddedCount = batchProgress.reduce((s, p) => s + (p.added ?? 0), 0);
  const batchErrorBuckets = batchProgress.reduce<Record<BatchErrorCategory, number>>(
    (acc, p) => {
      if (p.status === "error" && p.errorCategory) acc[p.errorCategory] += 1;
      return acc;
    },
    { rate_limit: 0, timeout: 0, in_progress: 0, unknown: 0 },
  );
  const batchErrorBucketText = (
    Object.entries(batchErrorBuckets).filter(([, n]) => n > 0) as [
      BatchErrorCategory,
      number,
    ][]
  )
    .map(([c, n]) => `${BATCH_ERROR_LABEL[c]}×${n}`)
    .join(" · ");
  const batchSummaryText = `成功 ${batchDoneCount} 张 / 失败 ${batchErrorCount} 张${
    batchErrorBucketText ? `（${batchErrorBucketText}）` : ""
  }${batchCancelledCount > 0 ? ` / 取消 ${batchCancelledCount} 张` : ""} · 新增字段描述 ${batchAddedCount} 个 · 耗时 ${batchElapsed}s`;
  // 智能重试建议：仅在完成后有失败时计算（限流→降并发、超时→单表串行、并发冲突→稍后）
  const retryAdvice = batchFinished && batchErrorCount > 0 ? computeRetryAdvice() : null;

  // 方案 A：治理主列表只展示「仍需治理」的表（字段缺失或表描述缺失），
  // 已完全覆盖的表不再占位——单表/批量推断完成后 load() 刷新即从列表消失。
  // 批量推断运行中：本次任务已成功完成的表（coveredIds）实时从列表移除（不等整批结束）。
  // 全量表资产浏览保留在概览卡下钻（totalTables/fieldCoverage 明细，基于全量 per_table）。
  const governTableRows = coverage.per_table.filter(
    (t) => (t.missing_fields > 0 || !t.table_desc) && !coveredIds.has(t.catalog_id),
  );

  return (
    <div>
      {isSummary && (
        <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
          <Button
            type="primary"
            icon={<ThunderboltOutlined />}
            onClick={() => onGovern?.()}
          >
            前往采集目录治理
          </Button>
        </div>
      )}

      {!isSummary && (
        <Space style={{ marginBottom: 12 }} wrap>
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            placeholder="全部数据源"
            style={{ width: 220 }}
            value={sourceId || undefined}
            onChange={(v) => setSourceId(v || "")}
            options={sources.map((s) => ({
              value: s.source_id,
              label: `${s.name}（${s.source_id}）`,
            }))}
            data-testid="coverage-source-filter"
          />
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            placeholder="全部库"
            style={{ width: 180 }}
            value={database || undefined}
            onChange={(v) => setDatabase(v || "")}
            options={databases.map((d) => ({ value: d, label: d }))}
            data-testid="coverage-database-filter"
          />
          <Input
            allowClear
            placeholder="按表名筛选"
            style={{ width: 200 }}
            value={keywordInput}
            onChange={(e) => scheduleKeyword(e.target.value)}
            data-testid="coverage-keyword-filter"
          />
          {(sourceId || database || keyword) && (
            <Button
              size="small"
              onClick={() => {
                setSourceId("");
                setDatabase("");
                setKeyword("");
                setKeywordInput("");
              }}
            >
              重置筛选
            </Button>
          )}
          <span className="muted" style={{ fontSize: 12 }}>
            统计卡与治理表格按所选数据源 / 库 / 表名口径计算，批量推断仅在筛选范围内勾选
          </span>
        </Space>
      )}

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Card size="small">
            <Statistic
              title="字段描述覆盖率"
              value={fieldCoveragePct}
              suffix="%"
              valueRender={clickableValue(() => openMetricDrill("fieldCoverage"))}
              valueStyle={{ color: fieldCoveragePct >= 80 ? "#3f8600" : "#cf1322" }}
            />
            <div className="muted" style={{ fontSize: 12 }}>
              {coverage.fields_with_desc} / {coverage.total_fields} 字段有描述
              {" · "}
              <a
                href="#"
                onClick={(e) => {
                  e.preventDefault();
                  openMetricDrill("fieldCoverage");
                }}
              >
                查看明细
              </a>
            </div>
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic
              title="缺失字段数"
              value={coverage.fields_missing_desc}
              valueRender={clickableValue(() => openMetricDrill("fieldsMissing"))}
              valueStyle={{ color: coverage.fields_missing_desc > 0 ? "#cf1322" : "#3f8600" }}
            />
            <div className="muted" style={{ fontSize: 12 }}>
              待 LLM 推断或人工补全
              {" · "}
              <a
                href="#"
                onClick={(e) => {
                  e.preventDefault();
                  openMetricDrill("fieldsMissing");
                }}
              >
                查看明细
              </a>
            </div>
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic
              title="缺表描述"
              value={coverage.tables_missing_desc}
              valueRender={clickableValue(() => openMetricDrill("tablesMissing"))}
              valueStyle={{ color: coverage.tables_missing_desc > 0 ? "#cf1322" : "#3f8600" }}
            />
            <div className="muted" style={{ fontSize: 12 }}>
              {coverage.tables_with_desc} / {coverage.total_tables} 表已补全
              {" · "}
              <a
                href="#"
                onClick={(e) => {
                  e.preventDefault();
                  openMetricDrill("tablesMissing");
                }}
              >
                查看明细
              </a>
            </div>
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic
              title="表总数"
              value={coverage.total_tables}
              valueRender={clickableValue(() => openMetricDrill("totalTables"))}
            />
            <div className="muted" style={{ fontSize: 12 }}>
              表级描述覆盖率 {tableCoveragePct}%
              {" · "}
              <a
                href="#"
                onClick={(e) => {
                  e.preventDefault();
                  openMetricDrill("totalTables");
                }}
              >
                查看明细
              </a>
            </div>
          </Card>
        </Col>
      </Row>

      {!isSummary && lastFailed.length > 0 && !batchOpen && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={`上次批量推断有 ${lastFailed.length} 张表未完成（${lastFailed.map((f) => f.entity_name).slice(0, 3).join("、")}${lastFailed.length > 3 ? "…" : ""}）`}
          action={
            <Button size="small" disabled={!canInferCatalog} onClick={relaunchLastFailed}>
              重新勾选并重试
            </Button>
          }
        />
      )}

      {!isSummary && (
        <Card
          size="small"
          title="按表列缺失字段数（点击行查看详情并补全）"
          style={{ marginBottom: 16 }}
          extra={
            <Space>
              {canInferCatalog && (
                <>
                  <Tooltip title="一键勾选所有存在描述缺失的表（字段缺失或表描述缺失）">
                    <Button size="small" onClick={selectAllMissing}>
                      选全部有缺失
                    </Button>
                  </Tooltip>
                  <Tooltip title="勾选多张表后，批量 LLM 推断每张表缺失的字段描述与表级描述（已有描述不覆盖）">
                    <Button
                      size="small"
                      type="primary"
                      icon={<ThunderboltOutlined />}
                      disabled={selectedRowKeys.length === 0}
                      onClick={openBatch}
                    >
                      批量推断所选表
                      {selectedRowKeys.length > 0 ? `（${selectedRowKeys.length}）` : ""}
                    </Button>
                  </Tooltip>
                </>
              )}
            </Space>
          }
        >
          {batchOpen && (
            <Card
              size="small"
              style={{ marginBottom: 12 }}
              title={
                historyOpen
                  ? "批量推断历史"
                  : batchStarted
                    ? "批量 LLM 推断"
                    : "批量 LLM 推断确认"
              }
              extra={
                !batchRunning && !historyOpen && batchHistory.length > 0 ? (
                  <Button size="small" onClick={() => setHistoryOpen(true)}>
                    历史记录
                  </Button>
                ) : undefined
              }
              data-testid="batch-infer-panel"
            >
              {batchStarted && !historyOpen ? (
                <>
                  {batchFinished && (
                    <Alert
                      type={batchErrorCount > 0 ? "warning" : "success"}
                      showIcon
                      style={{ marginBottom: 12 }}
                      message={batchSummaryText}
                    />
                  )}
                  {retryAdvice && (
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 12 }}
                      message={retryAdvice.message}
                      action={
                        retryAdvice.mode === "lower" || retryAdvice.mode === "serial" ? (
                          <Button size="small" type="primary" onClick={retryBatchSmart}>
                            {retryAdvice.mode === "lower" ? "降低并发重试" : "单表串行重试"}
                          </Button>
                        ) : undefined
                      }
                    />
                  )}
                  {batchRunning && batchProgress.length > 0 && (
                    <Progress
                      percent={Math.round(
                        ((batchDoneCount + batchErrorCount) / batchProgress.length) * 100,
                      )}
                      size="small"
                      status="active"
                      style={{ marginBottom: 12 }}
                    />
                  )}
                  <Table<BatchProgressItem>
                    dataSource={batchProgress}
                    rowKey="catalog_id"
                    size="small"
                    pagination={false}
                    columns={[
                      {
                        title: "表",
                        dataIndex: "entity_name",
                        render: (v: string) => <span className="mono">{v}</span>,
                      },
                      {
                        title: "状态",
                        dataIndex: "status",
                        width: 96,
                        render: (v: BatchProgressItem["status"]) => batchStatusTag(v),
                      },
                      {
                        title: "结果",
                        dataIndex: "summary",
                        ellipsis: true,
                        render: (v: string, r) =>
                          r.detail ? (
                            <Tooltip title={r.detail}>
                              <span className="muted" style={{ color: "#cf1322" }}>
                                {v}
                              </span>
                            </Tooltip>
                          ) : (
                            v
                          ),
                      },
                      {
                        title: "新增字段",
                        dataIndex: "inferredNames",
                        width: 120,
                        render: (v: string[] | undefined) => {
                          if (!v || v.length === 0) return <span className="muted">-</span>;
                          return (
                            <Tooltip title={v.join("、")}>
                              <span className="mono" style={{ color: "#3f8600" }}>
                                +{v.length} 个
                              </span>
                            </Tooltip>
                          );
                        },
                      },
                      {
                        title: "操作",
                        width: 80,
                        render: (_, r) =>
                          !batchRunning && r.status === "error" ? (
                            <Button
                              size="small"
                              icon={<ReloadOutlined />}
                              onClick={() => retryOne(r.catalog_id)}
                            >
                              重试
                            </Button>
                          ) : null,
                      },
                    ]}
                  />
                  <div style={{ marginTop: 16, textAlign: "right" }}>
                    {batchRunning ? (
                      <Space>
                        <Button danger onClick={cancelBatch} disabled={cancelling}>
                          {cancelling ? "正在取消…" : "取消"}
                        </Button>
                        <Button type="primary" disabled>
                          {cancelling ? "正在停止…" : "推断中…"}
                        </Button>
                      </Space>
                    ) : (
                      <Space>
                        {batchErrorCount > 0 && (
                          <Button type="primary" danger icon={<ReloadOutlined />} onClick={retryBatch}>
                            重试失败项（{batchErrorCount}）
                          </Button>
                        )}
                        <Button type="primary" onClick={closeBatch}>
                          关闭
                        </Button>
                      </Space>
                    )}
                  </div>
                </>
              ) : historyOpen ? (
                <>
                  {batchHistory.length === 0 ? (
                    <Empty description="暂无批量历史" />
                  ) : (
                    <>
                      {batchHistory.map((h) => (
                        <div
                          key={h.ts}
                          style={{
                            marginBottom: 10,
                            padding: "8px 12px",
                            border: "1px solid #f0f0f0",
                            borderRadius: 6,
                          }}
                        >
                          <div
                            style={{
                              display: "flex",
                              justifyContent: "space-between",
                              alignItems: "center",
                              gap: 8,
                            }}
                          >
                            <Space size={8} wrap>
                              <span className="muted" style={{ fontSize: 12 }}>
                                {formatCnTime(new Date(h.ts).toISOString())}
                              </span>
                              {h.actor_name && <Tag color="blue">操作人：{h.actor_name}</Tag>}
                              <span>
                                成功 {h.done} · 失败 {h.failed}
                                {h.cancelled > 0 ? ` · 取消 ${h.cancelled}` : ""} · 新增 {h.added}{" "}
                                字段 · {h.elapsed}s
                              </span>
                            </Space>
                            <Space size={4}>
                              {h.failedTables.length > 0 && (
                                <Tooltip title={h.failedTables.map((f) => f.entity_name).join("、")}>
                                  <Tag color="error">失败 {h.failedTables.length}</Tag>
                                </Tooltip>
                              )}
                              <Button
                                size="small"
                                icon={<ReloadOutlined />}
                                onClick={() => relaunchHistory(h)}
                              >
                                重新勾选此批
                              </Button>
                            </Space>
                          </div>
                          {h.failedTables.length > 0 && (
                            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                              失败表：{h.failedTables.map((f) => f.entity_name).join("、")}
                            </div>
                          )}
                        </div>
                      ))}
                      <div style={{ marginTop: 16, display: "flex", justifyContent: "space-between" }}>
                        <Button size="small" danger onClick={clearBatchHistory}>
                          清空历史
                        </Button>
                        <Button size="small" onClick={() => setHistoryOpen(false)}>
                          返回
                        </Button>
                      </div>
                    </>
                  )}
                </>
              ) : (
                <>
                  <p style={{ marginBottom: 12 }}>
                    将为以下 <b>{selectedTasks.length}</b> 张表自动推断缺失描述（已有描述不会被覆盖）：
                  </p>
                  <div style={{ maxHeight: 320, overflowY: "auto" }}>
                    {selectedTasks.map((t) => (
                      <div key={t.catalog_id} style={{ marginBottom: 10 }}>
                        <Space size={4} wrap>
                          <span className="mono">{t.entity_name}</span>
                          {t.needs_table_desc && <Tag color="blue">表描述</Tag>}
                          {t.missing_fields > 0 && (
                            <Tag color="red">{t.missing_fields} 个缺失字段</Tag>
                          )}
                        </Space>
                        {t.missing_field_names.length > 0 && (
                          <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                            {t.missing_field_names.slice(0, 8).join("、")}
                            {t.missing_field_names.length > 8
                              ? `…等 ${t.missing_field_names.length} 个`
                              : ""}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                  <div
                    style={{
                      marginTop: 16,
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                    }}
                  >
                    <Space size={8}>
                      <span className="muted" style={{ fontSize: 12 }}>
                        并发数
                      </span>
                      <Select showSearch
                        size="small"
                        value={batchConcurrency}
                        onChange={setBatchConcurrency}
                        style={{ width: 64 }}
                        data-testid="batch-concurrency-select"
                        options={[
                          { value: 1, label: "1" },
                          { value: 2, label: "2" },
                          { value: 3, label: "3" },
                          { value: 5, label: "5" },
                          { value: 8, label: "8" },
                          { value: 10, label: "10" },
                        ]}
                      />
                      <Tooltip title="并发数越大越快，但会增加 LLM 并发调用；建议按接口限流设置">
                        <span className="muted" style={{ fontSize: 12 }}>
                          （已有描述不覆盖）
                        </span>
                      </Tooltip>
                    </Space>
                    <Space>
                      <Button onClick={closeBatch}>取消</Button>
                      <Button
                        type="primary"
                        icon={<ThunderboltOutlined />}
                        onClick={() => submitBackendBatch(selectedTasks)}
                      >
                        开始推断
                      </Button>
                    </Space>
                  </div>
                </>
              )}
            </Card>
          )}
          <Table<TableCoverageItem>
            dataSource={governTableRows}
            columns={tableCoverageCols}
            rowKey={(r) => r.catalog_id}
            size="small"
            rowSelection={
              canInferCatalog
                ? {
                    selectedRowKeys,
                    onChange: (keys) => setSelectedRowKeys(keys),
                    preserveSelectedRowKeys: true,
                    // 仅可选「有缺失」的表（字段缺失或表描述缺失），无缺失表禁用勾选
                    getCheckboxProps: (r) => ({
                      disabled: r.missing_fields === 0 && !!r.table_desc,
                    }),
                  }
                : undefined
            }
            pagination={{
              pageSize,
              showSizeChanger: true,
              pageSizeOptions: [...PAGE_SIZE_OPTIONS],
              onShowSizeChange,
              showTotal: (t) => `共 ${t} 张表`,
            }}
            onRow={(record) => ({
              onClick: (e) => {
                // 点击选择列复选框不打开详情（antd checkbox 冒泡到行）
                const target = e.target as HTMLElement;
                if (target.closest(".ant-table-selection-column")) return;
                openDetail(record.catalog_id);
              },
              style: { cursor: "pointer" },
            })}
          />
        </Card>
      )}

      {!isSummary && (
        <ResizableDrawer
          title={detail ? `详情：${detail.entity_name}` : "实体详情"}
          open={detailOpen}
          onClose={() => setDetailOpen(false)}
          storageKey="unisense.drawer.desc-table.width"
          defaultWidth={880}
          minWidth={600}
          zIndex={1050}
        >
          {detailLoading ? (
            <Spin tip="加载实体详情…" />
          ) : detail ? (
            <>
              <Descriptions column={2} bordered size="small">
                <Descriptions.Item label="实体名称">{detail.entity_name}</Descriptions.Item>
                <Descriptions.Item label="实体类型">
                  {ENTITY_TYPE_LABEL[detail.entity_type] ?? detail.entity_type}
                </Descriptions.Item>
                <Descriptions.Item label="数据源">{detail.source_id}</Descriptions.Item>
                <Descriptions.Item label="敏感度">{sensitivityTag(detail.sensitivity_level)}</Descriptions.Item>
                <Descriptions.Item label="表级描述">
                  {tableDescEditing ? (
                    <Space.Compact style={{ width: "100%" }}>
                      <Input.TextArea
                        value={tableDescDraft}
                        onChange={(e) => setTableDescDraft(e.target.value)}
                        autoSize={{ minRows: 2, maxRows: 5 }}
                        disabled={tableDescSaving}
                        style={{ flex: 1 }}
                      />
                      <Button
                        type="primary"
                        icon={<CheckOutlined />}
                        aria-label="保存表描述"
                        loading={tableDescSaving}
                        onClick={handleTableDescSave}
                      />
                      <Button
                        icon={<CloseOutlined />}
                        aria-label="取消表描述编辑"
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
              </Descriptions>
              <Card title="字段描述" size="small" style={{ marginTop: 16 }}>
                <SchemaTable
                  columns={schemaColumns}
                  editable={canEditDesc}
                  inferable={canInferCatalog}
                  canInfer={canInferCatalog}
                  onEdit={handleFieldEdit}
                  onInfer={handleFieldInfer}
                  onBatchInfer={handleBatchInfer}
                />
              </Card>
            </>
          ) : null}
        </ResizableDrawer>
      )}

      {/* 概览指标下钻明细：点击指标数字展示该口径贡献的 per_table 子集。
          行点击在 full 模式继续下钻治理抽屉；summary 模式引导跳转采集目录治理。 */}
      <DrillDownDrawer
        open={metricDrillOpen}
        title={metricDrillTitle}
        columns={metricDrillColumns as unknown as ColumnsType<Record<string, unknown>>}
        rows={metricDrillRows as unknown as Record<string, unknown>[]}
        loading={false}
        onClose={() => setMetricDrillOpen(false)}
        onRow={(record) =>
          isSummary
            ? {
                onClick: () => onGovern?.(record.entity_name as string),
                style: { cursor: "pointer" },
              }
            : {
                onClick: () => openDetail(record.catalog_id as number),
                style: { cursor: "pointer" },
              }
        }
      />
    </div>
  );
});
