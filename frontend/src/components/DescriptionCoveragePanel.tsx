import { useEffect, useState, type ReactNode } from "react";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Input,
  Row,
  Col,
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
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  fetchAssetEntityDetail,
  fetchDescriptionCoverage,
  inferColumnDescription,
  inferDescriptions,
  inferTableDescription,
  updateColumnDescription,
  updateTableDescription,
} from "../api";
import type {
  DescriptionCoverage,
  TableCoverageItem,
} from "../api";
import type {
  AssetEntityDetail,
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
export function DescriptionCoveragePanel({
  variant = "full",
  onGovern,
}: DescriptionCoveragePanelProps) {
  const isSummary = variant === "summary";
  const [coverage, setCoverage] = useState<DescriptionCoverage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
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
      setCoverage(await fetchDescriptionCoverage());
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载描述覆盖统计失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

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

      {!isSummary && (
        <Card
          size="small"
          title="按表列缺失字段数（点击行查看详情并补全）"
          style={{ marginBottom: 16 }}
          extra={
            <Button size="small" icon={<ThunderboltOutlined />} onClick={load} loading={loading}>
              刷新
            </Button>
          }
        >
          <Table<TableCoverageItem>
            dataSource={coverage.per_table}
            columns={tableCoverageCols}
            rowKey={(r) => r.catalog_id}
            size="small"
            pagination={{
              pageSize,
              showSizeChanger: true,
              pageSizeOptions: [...PAGE_SIZE_OPTIONS],
              onShowSizeChange,
              showTotal: (t) => `共 ${t} 张表`,
            }}
            onRow={(record) => ({
              onClick: () => openDetail(record.catalog_id),
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
}
