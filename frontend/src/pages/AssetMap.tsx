import { useEffect, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Row,
  Col,
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
} from "@ant-design/icons";
import { Bar, Pie } from "@ant-design/charts";
import {
  assignAssetOwner,
  batchAssignAssetOwner,
  batchReclassifyAssetSensitivity,
  downloadAssetExport,
  fetchAssetChanges,
  fetchAssetEntityDetail,
  fetchAssetGraph,
  fetchAssetHealth,
  fetchAssetHeatmapMatrix,
  fetchAssetMetricSummary,
  fetchAssetMyAssets,
  fetchAssetOrphans,
  fetchAssetOwnerView,
  fetchAssetPiiOverview,
  fetchAssetSearch,
  fetchAssetSummary,
  fetchAssetTables,
  getMetric,
  inferColumnDescription,
  inferDescriptions,
  inferMetricDescription,
  inferTableDescription,
  listCatalogs,
  listDomainTree,
  listMetrics,
  listSnapshots,
  listUsers,
  queryMetricInternal,
  reclassifyAssetSensitivity,
  updateColumnDescription,
  updateMetricDescription,
  updateTableDescription,
} from "../api";
import type {
  AssetCatalogSummary,
  AssetChanges,
  AssetEntityDetail,
  AssetHealthSummary,
  AssetHeatmapMatrix,
  AssetMetricSummary,
  AssetMyAssets,
  AssetOwnerView,
  AssetPiiOverview,
  AssetSearchItem,
  AssetTableItem,
  MetricResponse,
  SchemaColumn,
  SnapshotResponse,
  SubjectDomainTreeNode,
} from "../types";
import { useTracking } from "../hooks/useTracking";
import { SchemaTable } from "../components/SchemaTable";
import { ENTITY_TYPE_LABEL, SOURCE_HEALTH_LABEL } from "../utils/enums";
import { formatCnTime } from "../utils/timeCn";
import { AssetGraph } from "../components/assetmap/AssetGraph";
import type { AssetGraphNode, AssetGraphEdge } from "../components/assetmap/AssetGraph";
import { DescriptionCoverageTab } from "../components/assetmap/DescriptionCoverageTab";
import { DrillDownDrawer } from "../components/assetmap/DrillDownDrawer";

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
const CATALOG_COLUMNS: ColumnsType<DrillRow> = [
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
  {
    title: "责任人",
    dataIndex: "owner_id",
    width: 80,
    render: (v) => (v == null ? <Tag>无</Tag> : v),
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
  { title: "状态", dataIndex: "status", width: 100 },
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

function OverviewTab() {
  const [summary, setSummary] = useState<AssetCatalogSummary | null>(null);
  const [metricSummary, setMetricSummary] = useState<AssetMetricSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // 明细下钻抽屉状态（指标点击值 → 明细表）
  const [drillOpen, setDrillOpen] = useState(false);
  const [drillLoading, setDrillLoading] = useState(false);
  const [drillTitle, setDrillTitle] = useState("");
  const [drillColumns, setDrillColumns] = useState<ColumnsType<DrillRow>>([]);
  const [drillRows, setDrillRows] = useState<DrillRow[]>([]);

  useEffect(() => {
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const [s, m] = await Promise.all([fetchAssetSummary(), fetchAssetMetricSummary()]);
        setSummary(s);
        setMetricSummary(m);
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
  ) {
    setDrillTitle(title);
    setDrillColumns(columns);
    setDrillOpen(true);
    setDrillLoading(true);
    setDrillRows([]);
    try {
      setDrillRows(await loader());
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载明细失败");
    } finally {
      setDrillLoading(false);
    }
  }

  function drillCatalogs(params?: { entity_type?: string; sensitivity_level?: string }) {
    const title = params?.entity_type
      ? `实体类型：${ENTITY_TYPE_LABEL[params.entity_type] ?? params.entity_type}`
      : params?.sensitivity_level
        ? `敏感度：${SENSITIVITY_LABEL[params.sensitivity_level] ?? params.sensitivity_level}`
        : "目录资产明细";
    return openDrill(title, CATALOG_COLUMNS, async () => {
      const r = await listCatalogs({ ...params, page_size: 200 });
      return r.items as unknown as DrillRow[];
    });
  }

  function drillMetrics(status?: string) {
    return openDrill(status ? "已发布指标明细" : "指标明细", METRIC_COLUMNS, async () => {
      const r = await listMetrics({ ...(status ? { status } : {}), page_size: 100 });
      return r.items as unknown as DrillRow[];
    });
  }

  function drillOrphans() {
    return openDrill("孤儿资产明细", ORPHAN_COLUMNS, async () => {
      const r = await fetchAssetOrphans();
      return r.items as unknown as DrillRow[];
    });
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

      <DrillDownDrawer
        open={drillOpen}
        title={drillTitle}
        columns={drillColumns}
        rows={drillRows}
        loading={drillLoading}
        onClose={() => setDrillOpen(false)}
      />
    </div>
  );
}

function GraphTab() {
  const navigate = useNavigate();
  const [graphData, setGraphData] = useState<{
    nodes: AssetGraphNode[];
    edges: AssetGraphEdge[];
  } | null>(null);
  const [domain, setDomain] = useState<string | undefined>(undefined);
  const [piiOnly, setPiiOnly] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 实体详情抽屉（table/field 节点下钻）
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detail, setDetail] = useState<AssetEntityDetail | null>(null);
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

  async function loadGraph() {
    setLoading(true);
    setError(null);
    try {
      // depth=2：从指标出发 2 层收敛，避免 500+ 节点挤成一团（depth 越大展开越多）
      const data = await fetchAssetGraph({ domain, depth: 2, pii_only: piiOnly });
      setGraphData(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载图谱数据失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadGraph();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [domain, piiOnly]);

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

  async function handleFieldInfer(col: SchemaColumn) {
    if (!detail) return;
    await inferColumnDescription(detail.id, col.name, {
      entity_name: detail.entity_name,
      column_type: col.type,
    });
    message.success(`字段「${col.name}」描述已生成`);
    await refreshDetail();
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

  async function handleTableDescInfer() {
    if (!detail) return;
    setTableInferring(true);
    try {
      const fields = Array.isArray(detail.schema_summary)
        ? detail.schema_summary.map((c) => ({ name: c.name, type: c.type }))
        : [];
      await inferTableDescription(detail.id, fields);
      message.success("表级描述已生成");
      await refreshDetail();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "推断表描述失败");
    } finally {
      setTableInferring(false);
    }
  }

  async function openMetric(code: string) {
    setMetricOpen(true);
    setMetricLoading(true);
    setMetricData(null);
    setMetricDescEditing(false);
    setMetricQueryRows(null);
    setMetricQueryMeta(null);
    try {
      const [m, snaps] = await Promise.all([
        getMetric(code),
        listSnapshots(code, 50).catch(() => []),
      ]);
      setMetricData(m);
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
      onOk: () => navigate(`/catalog?kw=${encodeURIComponent(entityName)}`),
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
          <span className="muted">域筛选：</span>
          <Select
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
        />
      </Card>

      {/* 指标详情抽屉：明细 + 补充描述（TD §12.1） */}
      <Drawer
        title={
          metricData
            ? `指标详情：${metricData.name}（${metricData.metric_code}）`
            : "指标详情"
        }
        open={metricOpen}
        onClose={() => setMetricOpen(false)}
        width={760}
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
              <Descriptions.Item label="类型">{metricData.type}</Descriptions.Item>
              <Descriptions.Item label="粒度">{metricData.granularity}</Descriptions.Item>
              <Descriptions.Item label="单位">{metricData.unit}</Descriptions.Item>
              <Descriptions.Item label="聚合方式">{metricData.aggregation}</Descriptions.Item>
              <Descriptions.Item label="时间语义">{metricData.time_semantics}</Descriptions.Item>
              <Descriptions.Item label="新鲜度">{metricData.freshness}</Descriptions.Item>
              <Descriptions.Item label="数仓层">{metricData.dw_layer}</Descriptions.Item>
              <Descriptions.Item label="指标分级">{metricData.metric_tier}</Descriptions.Item>
              <Descriptions.Item label="状态">{metricData.status}</Descriptions.Item>
              <Descriptions.Item label="可加性">{metricData.additivity}</Descriptions.Item>
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
              {metricSnapshots.length === 0 ? (
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
      </Drawer>

       <Drawer
         title={detail ? `实体详情：${detail.entity_name}` : "实体详情"}
         open={detailOpen}
         onClose={() => setDetailOpen(false)}
         width={720}
       >
        {detailLoading ? (
          <Spin tip="加载实体详情…" />
        ) : detail ? (
          <>
            <Descriptions column={1} bordered size="small">
              <Descriptions.Item label="实体名称">{detail.entity_name}</Descriptions.Item>
              <Descriptions.Item label="实体类型">{detail.entity_type}</Descriptions.Item>
              <Descriptions.Item label="数据源">{detail.source_id}</Descriptions.Item>
              <Descriptions.Item label="敏感度">
                {sensitivityTag(detail.sensitivity_level)}
                {detailHasPii && (
                  <Tag color="red" style={{ marginLeft: 8 }}>
                    含 PII
                  </Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="责任人">
                {detail.owner_id != null ? `#${detail.owner_id}` : <Tag>无</Tag>}
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
                    </Space>
                  </Space>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="字段描述">
                <SchemaTable
                  columns={Array.isArray(detail.schema_summary) ? detail.schema_summary : []}
                  editable
                  inferable
                  onEdit={handleFieldEdit}
                  onInfer={handleFieldInfer}
                  onBatchInfer={handleBatchInfer}
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
            {(detail.lineage_edges?.length ?? 0) > 0 && (
              <Card title="血缘边明细" size="small" style={{ marginTop: 16 }}>
                <Table
                  dataSource={detail.lineage_edges}
                  rowKey={(e, i) => `${e.source}-${e.target}-${i}`}
                  size="small"
                  pagination={false}
                  columns={[
                    {
                      title: "源",
                      dataIndex: "source",
                      key: "source",
                      ellipsis: true,
                      render: (v: string) => (
                        <span className="mono" style={{ fontSize: 12 }}>
                          {v}
                        </span>
                      ),
                    },
                    {
                      title: "目标",
                      dataIndex: "target",
                      key: "target",
                      ellipsis: true,
                      render: (v: string) => (
                        <span className="mono" style={{ fontSize: 12 }}>
                          {v}
                        </span>
                      ),
                    },
                    { title: "类型", dataIndex: "edge_type", key: "type", width: 120 },
                    { title: "粒度", dataIndex: "granularity", key: "granularity", width: 80 },
                  ]}
                />
              </Card>
            )}
          </>
        ) : null}
      </Drawer>

      <Drawer
        title="字段信息"
        open={fieldNode != null}
        onClose={() => setFieldNode(null)}
        width={440}
      >
        {fieldNode && (
          <>
            <Descriptions column={1} bordered size="small">
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
      </Drawer>
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
    try {
      if (assetType === "metric") {
        const r = await listMetrics({ domain, pii_flag: sensKey === "PII", page_size: 100 });
        setDrillRows(r.items as unknown as DrillRow[]);
      } else {
        const r = await listCatalogs({ sensitivity_level: sensKey, domain, page_size: 200 });
        setDrillRows(r.items as unknown as DrillRow[]);
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
        onClose={() => setDrillOpen(false)}
      />
    </div>
  );
}

function OwnerTab() {
  const [ownerId, setOwnerId] = useState<number | undefined>(undefined);
  const [ownerOptions, setOwnerOptions] = useState<Array<{ label: string; value: number }>>([]);
  const [view, setView] = useState<AssetOwnerView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 指标统计值下钻抽屉（点击数字 → 该口径的指标明细）
  const [drillOpen, setDrillOpen] = useState(false);
  const [drillLoading, setDrillLoading] = useState(false);
  const [drillTitle, setDrillTitle] = useState("");
  const [drillRows, setDrillRows] = useState<DrillRow[]>([]);

  useEffect(() => {
    fetchAssetGraph({ depth: 1 })
      .then((g) => {
        const owners = [...new Set(g.nodes.map((n) => n.owner).filter(Boolean))].map((o) =>
          Number(o),
        );
        if (owners.length > 0) {
          const opts = owners.map((id) => ({ label: `责任人 #${id}`, value: id }));
          setOwnerOptions(opts);
          setOwnerId((prev) => prev ?? opts[0].value);
        } else {
          // 图谱暂无责任人信息时，回退展示责任人 #1
          const fallback = [{ label: "责任人 #1", value: 1 }];
          setOwnerOptions(fallback);
          setOwnerId((prev) => prev ?? 1);
        }
      })
      .catch(() => {});
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
    setDrillTitle(`责任人 #${ownerId} 指标明细${parts ? `（${parts}）` : ""}`);
    setDrillOpen(true);
    setDrillLoading(true);
    setDrillRows([]);
    try {
      const r = await listMetrics({
        owner_id: ownerId,
        ...(opts?.status ? { status: opts.status } : {}),
        ...(opts?.domain ? { domain: opts.domain } : {}),
        ...(opts?.piiFlag !== undefined ? { pii_flag: opts.piiFlag } : {}),
        page_size: 100,
      });
      setDrillRows(r.items as unknown as DrillRow[]);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载指标明细失败");
    } finally {
      setDrillLoading(false);
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

  return (
    <div>
      <Card
        title="责任人视图"
        size="small"
        extra={
          ownerOptions.length > 0 ? (
            <Select
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
          <Row gutter={[16, 16]}>
            <Col span={6}>
              <Statistic
                title="指标总数"
                value={view.metrics.total}
                valueRender={clickableValue(() => drillMetrics())}
              />
            </Col>
            <Col span={6}>
              <Statistic
                title="已发布"
                value={view.metrics.published}
                valueStyle={{ color: "#2e9e5b" }}
                valueRender={clickableValue(() => drillMetrics({ status: "PUBLISHED" }))}
              />
            </Col>
            <Col span={6}>
              <Statistic
                title="草稿"
                value={view.metrics.draft}
                valueRender={clickableValue(() => drillMetrics({ status: "DRAFT" }))}
              />
            </Col>
            <Col span={6}>
              <Statistic
                title="PII 指标"
                value={view.metrics.pii_count}
                valueStyle={{ color: "#d64545" }}
                valueRender={clickableValue(() => drillMetrics({ piiFlag: true }))}
              />
            </Col>
            <Col span={12}>
              <div style={{ marginTop: 8 }}>
                <span className="muted" style={{ fontSize: 13 }}>
                  域分布（点击查看该域明细）
                </span>
                <Row gutter={[8, 8]} style={{ marginTop: 8 }}>
                  {Object.entries(view.metrics.by_domain ?? {}).map(([k, v]) => (
                    <Col span={8} key={k}>
                      <Statistic
                        title={k}
                        value={v}
                        valueRender={clickableValue(() => drillMetrics({ domain: k }))}
                      />
                    </Col>
                  ))}
                </Row>
              </div>
            </Col>
            <Col span={12}>
              <div style={{ marginTop: 8 }}>
                <span className="muted" style={{ fontSize: 13 }}>
                  目录资产
                </span>
                <div style={{ fontSize: 28, fontWeight: 600, fontFamily: "var(--font-display)" }}>
                  {view.catalogs.total}
                </div>
              </div>
            </Col>
          </Row>
        )}
      </Card>
      <DrillDownDrawer
        open={drillOpen}
        title={drillTitle}
        columns={METRIC_COLUMNS}
        rows={drillRows}
        loading={drillLoading}
        onClose={() => setDrillOpen(false)}
      />
    </div>
  );
}

function OrphansTab() {
  const [items, setItems] = useState<AssetTableItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchAssetOrphans()
      .then((r) => setItems(r.items))
      .catch((err) => setError(err instanceof Error ? err.message : "加载孤儿资产失败"))
      .finally(() => setLoading(false));
  }, []);

  return (
    <Card
      title="孤儿资产（无责任人）"
      size="small"
      extra={<Statistic title="数量" value={items.length} valueStyle={{ fontSize: 18 }} />}
    >
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
          pagination={{ pageSize: 20 }}
          columns={[
            { title: "数据源", dataIndex: "source_id", key: "source_id" },
            { title: "实体", dataIndex: "entity_name", key: "entity_name", ellipsis: true },
            {
              title: "类型",
              dataIndex: "entity_type",
              key: "entity_type",
              width: 90,
              render: (v: string) => ENTITY_TYPE_LABEL[v] ?? v,
            },
            {
              title: "敏感度",
              dataIndex: "sensitivity_level",
              key: "sensitivity",
              width: 110,
              render: (s: string | null | undefined) => sensitivityTag(s),
            },
          ]}
        />
      )}
    </Card>
  );
}

function TablesTab() {
  const [items, setItems] = useState<AssetTableItem[]>([]);
  const [sensitivity, setSensitivity] = useState<string | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detail, setDetail] = useState<AssetEntityDetail | null>(null);
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

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const r = await fetchAssetTables({ sensitivity, limit: 200 });
      setItems(r.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载数据表失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sensitivity]);

  // 责任人候选：加载失败不阻塞治理入口（仅下拉无选项）
  useEffect(() => {
    listUsers()
      .then((users) =>
        setOwnerOptions(
          users
            .filter((u) => u.status === "active")
            .map((u) => ({ label: `${u.display_name || u.username} (#${u.id})`, value: u.id })),
        ),
      )
      .catch(() => {});
  }, []);

  // 打开治理设置 Modal（single 传单个 entity_id；batch 传勾选 id 列表）
  function openGov(entityIds: number[], onSaved?: () => void) {
    setGovEntityIds(entityIds);
    setGovOnSaved(() => onSaved ?? null);
    setGovOpen(true);
  }

  // 提交：只发送用户实际填写的字段；owner 选「解除归属」哨兵值 → null
  async function handleGovSubmit() {
    const values = govForm.getFieldsValue();
    const ids = govEntityIds;
    const calls: Promise<unknown>[] = [];
    const sens = values.sensitivity_level as string | undefined;
    if (sens) {
      calls.push(
        ids.length > 1
          ? batchReclassifyAssetSensitivity(ids, sens)
          : reclassifyAssetSensitivity(ids[0], sens),
      );
    }
    // owner 未动（undefined）不提交；显式「解除归属」→ null
    if (values.owner_id !== undefined && values.owner_id !== null) {
      const ownerId = values.owner_id === "__none__" ? null : (values.owner_id as number);
      calls.push(
        ids.length > 1
          ? batchAssignAssetOwner(ids, ownerId)
          : assignAssetOwner(ids[0], ownerId),
      );
    }
    if (calls.length === 0) {
      message.warning("请选择要设置的责任人或敏感度");
      return;
    }
    setGovSaving(true);
    try {
      await Promise.all(calls);
      message.success(`已更新 ${ids.length} 项资产`);
      setGovOpen(false);
      govForm.resetFields();
      setSelectedRowKeys([]);
      await load();
      govOnSaved?.();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存失败");
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
      await downloadAssetExport({ sensitivity });
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
          <Select
            allowClear
            placeholder="全部敏感度"
            style={{ width: 160 }}
            value={sensitivity}
            onChange={setSensitivity}
            options={Object.keys(SENSITIVITY_LABEL).map((k) => ({
              value: k,
              label: SENSITIVITY_LABEL[k],
            }))}
          />
          <Button icon={<DownloadOutlined />} onClick={handleExport}>
            导出 CSV
          </Button>
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
        </Space>
      }
    >
      {loading ? (
        <Spin />
      ) : error ? (
        <Alert type="error" message={error} />
      ) : (
        <Table
          dataSource={items}
          rowKey={(r) => `${r.source_id}-${r.entity_name}`}
          size="small"
          pagination={{ pageSize: 20 }}
          rowSelection={{
            selectedRowKeys,
            onChange: (keys) => setSelectedRowKeys(keys),
          }}
          columns={[
            { title: "数据源", dataIndex: "source_id", key: "source_id" },
            { title: "实体", dataIndex: "entity_name", key: "entity_name", ellipsis: true },
            {
              title: "类型",
              dataIndex: "entity_type",
              key: "entity_type",
              width: 90,
              render: (v: string) => ENTITY_TYPE_LABEL[v] ?? v,
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
              width: 90,
              render: (v: number | null) => v ?? <Tag>无</Tag>,
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
                  <Button
                    type="link"
                    size="small"
                    icon={<SettingOutlined />}
                    disabled={record.id == null}
                    onClick={() => {
                      if (record.id != null) openGov([record.id]);
                    }}
                  >
                    设置
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      )}
      <Drawer
        title={detail ? `实体详情：${detail.entity_name}` : "实体详情"}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        width={720}
        destroyOnClose={false}
      >
        {detailLoading ? (
          <Spin tip="加载实体详情…" />
        ) : detail ? (
          <>
            <Descriptions column={1} bordered size="small">
              <Descriptions.Item label="实体名称">
                {detail.entity_name}
                <Button
                  type="link"
                  size="small"
                  icon={<SettingOutlined />}
                  style={{ paddingLeft: 8 }}
                  onClick={() => openGov([detail.id], refreshDetail)}
                >
                  设置
                </Button>
              </Descriptions.Item>
              <Descriptions.Item label="实体类型">{detail.entity_type}</Descriptions.Item>
              <Descriptions.Item label="数据源">{detail.source_id}</Descriptions.Item>
              <Descriptions.Item label="敏感度">
                {sensitivityTag(detail.sensitivity_level)}
                {hasPii && (
                  <Tag color="red" style={{ marginLeft: 8 }}>
                    含 PII
                  </Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="责任人">
                {detail.owner_id != null ? `#${detail.owner_id}` : <Tag>无</Tag>}
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
            {(detail.lineage_edges?.length ?? 0) > 0 && (
              <Card title="血缘边明细" size="small" style={{ marginTop: 16 }}>
                <Table
                  dataSource={detail.lineage_edges}
                  rowKey={(e, i) => `${e.source}-${e.target}-${i}`}
                  size="small"
                  pagination={false}
                  columns={[
                    {
                      title: "源",
                      dataIndex: "source",
                      key: "source",
                      ellipsis: true,
                      render: (v: string) => (
                        <span className="mono" style={{ fontSize: 12 }}>
                          {v}
                        </span>
                      ),
                    },
                    {
                      title: "目标",
                      dataIndex: "target",
                      key: "target",
                      ellipsis: true,
                      render: (v: string) => (
                        <span className="mono" style={{ fontSize: 12 }}>
                          {v}
                        </span>
                      ),
                    },
                    { title: "类型", dataIndex: "edge_type", key: "type", width: 120 },
                    { title: "粒度", dataIndex: "granularity", key: "granularity", width: 80 },
                  ]}
                />
              </Card>
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
                    { title: "关系", dataIndex: "edge_type", key: "edge", width: 140 },
                  ]}
                />
              </Card>
            )}
          </>
        ) : null}
      </Drawer>
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
            extra="留空表示不修改；选择「解除归属」将清空责任人"
          >
            <Select
              allowClear
              placeholder="选择责任人"
              options={[
                { label: "（解除归属）", value: "__none__" },
                ...ownerOptions,
              ]}
            />
          </Form.Item>
          <Form.Item name="sensitivity_level" label="敏感度" extra="留空表示不修改">
            <Select
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
        <Select
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
            pagination={{ pageSize: 20 }}
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
                render: (t: string) => (
                  <Tag color={t === "metric" ? "purple" : "blue"}>
                    {t === "metric" ? "指标" : "目录"}
                  </Tag>
                ),
              },
              {
                title: "名称",
                dataIndex: "name",
                key: "name",
                ellipsis: true,
                render: (v: string, r: AssetSearchItem) =>
                  r.type === "metric" ? (
                    <a
                      className="mono"
                      onClick={(e) => {
                        e.stopPropagation();
                        navigate(`/detail/${encodeURIComponent(v)}`);
                      }}
                    >
                      {v}
                    </a>
                  ) : v,
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
                title: "域",
                dataIndex: "domain",
                key: "domain",
                width: 120,
                render: (v: string | null) => v ?? "-",
              },
              {
                title: "状态",
                dataIndex: "status",
                key: "status",
                width: 110,
                render: (v: string | null) => v ?? "-",
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
  const staleCount = data.stale_assets.length;

  return (
    <div>
      <Row gutter={16} style={{ marginBottom: 16 }}>
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
        <Col span={6}>
          <Card size="small">
            <Statistic
              title={`${data.stale_days} 天未更新`}
              value={staleCount}
              valueStyle={{ color: staleCount > 0 ? "#d46b08" : "#3f8600" }}
            />
          </Card>
        </Col>
      </Row>

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
    </div>
  );
}

function PiiTab() {
  const [data, setData] = useState<AssetPiiOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchAssetPiiOverview()
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : "加载 PII 视图失败"))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <Spin />;
  if (error) return <Alert type="error" message={error} />;
  if (!data) return <Empty />;

  const sensRows = Object.entries(data.by_sensitivity).map(([k, v]) => ({ key: k, count: v }));
  const domainRows = Object.entries(data.by_domain).map(([k, v]) => ({ key: k, count: v }));

  return (
    <div>
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Card size="small">
            <Statistic
              title="PII 指标数"
              value={data.pii_metric_count}
              valueStyle={{ color: "#cf1322" }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic
              title="PII 目录数"
              value={data.pii_catalog_count}
              valueStyle={{ color: "#cf1322" }}
            />
          </Card>
        </Col>
      </Row>
      <Row gutter={16}>
        <Col span={12}>
          <Card title="按敏感级分布" size="small">
            <Table
              dataSource={sensRows}
              rowKey="key"
              size="small"
              pagination={false}
              columns={[
                {
                  title: "敏感级",
                  dataIndex: "key",
                  key: "key",
                  render: (k: string) => sensitivityTag(k),
                },
                { title: "数量", dataIndex: "count", key: "count", align: "right" },
              ]}
            />
          </Card>
        </Col>
        <Col span={12}>
          <Card title="按业务域分布" size="small">
            <Table
              dataSource={domainRows}
              rowKey="key"
              size="small"
              pagination={false}
              columns={[
                { title: "域", dataIndex: "key", key: "key" },
                { title: "数量", dataIndex: "count", key: "count", align: "right" },
              ]}
            />
          </Card>
        </Col>
      </Row>
    </div>
  );
}

function ChangesTab() {
  const navigate = useNavigate();
  const [data, setData] = useState<AssetChanges | null>(null);
  const [days, setDays] = useState(7);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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
        <Select
          value={days}
          onChange={setDays}
          style={{ width: 120 }}
          options={[7, 14, 30].map((d) => ({ value: d, label: `近 ${d} 天` }))}
        />
      }
    >
      <Descriptions column={2} size="small" style={{ marginBottom: 16 }}>
        <Descriptions.Item label="新增/变更目录">{data.catalogs.length}</Descriptions.Item>
        <Descriptions.Item label="新增/变更指标">{data.metrics.length}</Descriptions.Item>
      </Descriptions>
      <Table
        dataSource={data.catalogs}
        rowKey={(r) => `c-${r.id}`}
        size="small"
        pagination={{ pageSize: 10 }}
        title={() => <b>目录</b>}
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
            render: (s: string | null) => sensitivityTag(s),
          },
          { title: "源", dataIndex: "source_id", key: "source", width: 120 },
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
        pagination={{ pageSize: 10 }}
        style={{ marginTop: 16 }}
        title={() => <b>指标</b>}
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
          { title: "状态", dataIndex: "status", key: "status", width: 110 },
          { title: "域", dataIndex: "domain", key: "domain", width: 110 },
          {
            title: "PII",
            dataIndex: "pii_flag",
            key: "pii",
            width: 70,
            render: (v: boolean) => (v ? <Tag color="red">PII</Tag> : null),
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
    </Card>
  );
}

function MyAssetsTab() {
  const navigate = useNavigate();
  const [data, setData] = useState<AssetMyAssets | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchAssetMyAssets()
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : "加载我的资产失败"))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <Spin />;
  if (error) return <Alert type="error" message={error} />;
  if (!data) return <Empty />;

  return (
    <div>
      <Descriptions column={3} size="small" style={{ marginBottom: 16 }}>
        <Descriptions.Item label="我的目录">{data.catalogs.length}</Descriptions.Item>
        <Descriptions.Item label="我的指标">{data.metrics.length}</Descriptions.Item>
        <Descriptions.Item label="责任人 ID">#{data.owner_id}</Descriptions.Item>
      </Descriptions>
      <Table
        dataSource={data.catalogs}
        rowKey={(r) => `c-${r.id}`}
        size="small"
        pagination={{ pageSize: 10 }}
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
            render: (s: string | null) => sensitivityTag(s),
          },
          { title: "源", dataIndex: "source_id", key: "source", width: 120 },
        ]}
      />
      <Table
        dataSource={data.metrics}
        rowKey={(r) => `m-${r.metric_code}`}
        size="small"
        pagination={{ pageSize: 10 }}
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
          { title: "状态", dataIndex: "status", key: "status", width: 110 },
          { title: "域", dataIndex: "domain", key: "domain", width: 110 },
          {
            title: "PII",
            dataIndex: "pii_flag",
            key: "pii",
            width: 70,
            render: (v: boolean) => (v ? <Tag color="red">PII</Tag> : null),
          },
        ]}
      />
    </div>
  );
}

export function AssetMap() {
  const [activeTab, setActiveTab] = useState("graph");
  const { track } = useTracking();
  const navigate = useNavigate();

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
      children: <DescriptionCoverageTab />,
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
