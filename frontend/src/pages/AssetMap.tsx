import { useEffect, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Row,
  Col,
  Input,
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
  DeleteOutlined,
  DownloadOutlined,
  EyeOutlined,
  GlobalOutlined,
  HeartOutlined,
  HeatMapOutlined,
  SafetyOutlined,
  SearchOutlined,
  TableOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { Heatmap, Pie } from "@ant-design/charts";
import {
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
  listCatalogs,
  listMetrics,
} from "../api";
import type {
  AssetCatalogSummary,
  AssetChanges,
  AssetEntityDetail,
  AssetHealthSummary,
  AssetMetricSummary,
  AssetMyAssets,
  AssetPiiOverview,
  AssetSearchItem,
  AssetTableItem,
  SchemaColumn,
} from "../types";
import { useTracking } from "../hooks/useTracking";
import { ENTITY_TYPE_LABEL, SOURCE_HEALTH_LABEL } from "../utils/enums";
import { AssetGraph } from "../components/assetmap/AssetGraph";
import type { AssetGraphNode, AssetGraphEdge } from "../components/assetmap/AssetGraph";
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
  if (Array.isArray(summary)) {
    // SchemaColumn[]（并行会话引入的类型）：紧凑列名清单，避免依赖外部渲染组件
    return (
      <div style={{ lineHeight: 1.8 }}>
        {summary.map((c) => (
          <div key={c.name} style={{ fontSize: 12 }}>
            <span className="mono">{c.name}</span>
            {c.type ? <span className="muted" style={{ marginLeft: 6 }}>{c.type}</span> : null}
            {c.description ? (
              <span className="muted" style={{ marginLeft: 6 }}>· {c.description}</span>
            ) : null}
          </div>
        ))}
      </div>
    );
  }
  return <span className="muted">-</span>;
}

type DrillRow = Record<string, unknown>;

// 下钻明细列（目录 / 指标 / 孤儿三种口径）
const CATALOG_COLUMNS: ColumnsType<DrillRow> = [
  { title: "数据源", dataIndex: "source_id", width: 130 },
  { title: "实体", dataIndex: "entity_name", ellipsis: true },
  { title: "类型", dataIndex: "entity_type", width: 90, render: (v) => ENTITY_TYPE_LABEL[v as string] ?? v },
  { title: "敏感度", dataIndex: "sensitivity_level", width: 110, render: (s) => sensitivityTag(s as string | null | undefined) },
  { title: "责任人", dataIndex: "owner_id", width: 80, render: (v) => (v == null ? <Tag>无</Tag> : v) },
];

const METRIC_COLUMNS: ColumnsType<DrillRow> = [
  { title: "编码", dataIndex: "metric_code", ellipsis: true, render: (v) => <span className="mono">{v as string}</span> },
  { title: "名称", dataIndex: "name", ellipsis: true },
  { title: "域", dataIndex: "domain", width: 110 },
  { title: "状态", dataIndex: "status", width: 100 },
  { title: "PII", dataIndex: "pii_flag", width: 70, render: (v) => (v ? <Tag color="red">PII</Tag> : null) },
];

const ORPHAN_COLUMNS: ColumnsType<DrillRow> = [
  { title: "数据源", dataIndex: "source_id", width: 130 },
  { title: "实体", dataIndex: "entity_name", ellipsis: true },
  { title: "类型", dataIndex: "entity_type", width: 90, render: (v) => ENTITY_TYPE_LABEL[v as string] ?? v },
  { title: "敏感度", dataIndex: "sensitivity_level", width: 110, render: (s) => sensitivityTag(s as string | null | undefined) },
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

  const totalMetrics = metricSummary.by_domain ? Object.values(metricSummary.by_domain).reduce((a, b) => a + b, 0) : 0;
  const sensData = Object.entries(summary.by_sensitivity ?? {}).map(([k, v]) => ({
    type: SENSITIVITY_LABEL[k] ?? k,
    key: k,
    value: v,
  }));

  return (
    <div>
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={12} md={6}>
          <Statistic title="目录资产总数" value={summary.total} valueRender={clickableValue(() => drillCatalogs())} />
        </Col>
        <Col xs={12} md={6}>
          <Statistic title="指标总数" value={totalMetrics} valueRender={clickableValue(() => drillMetrics())} />
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
  const [graphData, setGraphData] = useState<{ nodes: AssetGraphNode[]; edges: AssetGraphEdge[] } | null>(null);
  const [domain, setDomain] = useState<string | undefined>(undefined);
  const [piiOnly, setPiiOnly] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 实体详情抽屉（table/field 节点下钻）
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detail, setDetail] = useState<AssetEntityDetail | null>(null);
  // 字段信息抽屉（field 节点无 entity_id 时的兜底展示 + 所属表入口）
  const [fieldNode, setFieldNode] = useState<AssetGraphNode | null>(null);
  const [fieldTableNode, setFieldTableNode] = useState<AssetGraphNode | null>(null);

  async function loadGraph() {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchAssetGraph({ domain, depth: 3, pii_only: piiOnly });
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

  function handleNodeClick(node: AssetGraphNode) {
    if (node.type === "metric") {
      navigate(`/detail/${encodeURIComponent(node.label)}`);
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
        />
      </Card>

      <Drawer
        title={detail ? `实体详情：${detail.entity_name}` : "实体详情"}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        width={560}
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
                {detailHasPii && <Tag color="red" style={{ marginLeft: 8 }}>含 PII</Tag>}
              </Descriptions.Item>
              <Descriptions.Item label="责任人">
                {detail.owner_id != null ? `#${detail.owner_id}` : <Tag>无</Tag>}
              </Descriptions.Item>
              <Descriptions.Item label="Schema 状态">
                {detail.schema_incomplete ? <Tag color="orange">不完整</Tag> : <Tag color="green">完整</Tag>}
              </Descriptions.Item>
              <Descriptions.Item label="Schema 摘要">{renderSchemaSummary(detail.schema_summary)}</Descriptions.Item>
              <Descriptions.Item label="源健康">
                {detail.source_health ? (
                  <Tag color={detail.source_health.health_status === "healthy" ? "green" : detail.source_health.health_status === "unhealthy" ? "red" : "default"}>
                    {SOURCE_HEALTH_LABEL[detail.source_health.health_status] ?? detail.source_health.health_status}
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

function HeatmapTab() {
  const [matrix, setMatrix] = useState<{
    cells: Array<{ domain: string; sensitivity: string; count: number; pii_count: number }>;
    columns: string[];
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 单元格下钻抽屉（域 × 敏感级 → 该敏感级目录明细）
  const [drillOpen, setDrillOpen] = useState(false);
  const [drillLoading, setDrillLoading] = useState(false);
  const [drillTitle, setDrillTitle] = useState("");
  const [drillRows, setDrillRows] = useState<DrillRow[]>([]);
  // 色阶主题（0 值浅灰 + 非 0 由浅到深）
  const [colorTheme, setColorTheme] = useState<"blue" | "warm" | "green">("blue");
  const colorRanges: Record<string, string[]> = {
    blue: ["#f0f0f0", "#d6e4ff", "#1677ff", "#003eb3"],
    warm: ["#f0f0f0", "#fff1d6", "#ffa940", "#d4380d"],
    green: ["#f0f0f0", "#d9f7be", "#52c41a", "#135200"],
  };

  useEffect(() => {
    async function load() {
      setLoading(true);
      setError(null);
      try {
        setMatrix(await fetchAssetHeatmapMatrix());
      } catch (err) {
        setError(err instanceof Error ? err.message : "加载热力矩阵失败");
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  async function openCellDrill(sensKey: string, domain: string) {
    setDrillTitle(`${domain} · ${SENSITIVITY_LABEL[sensKey] ?? sensKey} 资产明细`);
    setDrillOpen(true);
    setDrillLoading(true);
    setDrillRows([]);
    try {
      const r = await listCatalogs({ sensitivity_level: sensKey, page_size: 200 });
      setDrillRows(r.items as unknown as DrillRow[]);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载明细失败");
    } finally {
      setDrillLoading(false);
    }
  }

  if (loading) return <Spin />;
  if (error) return <Alert type="error" message={error} />;
  if (!matrix) return <Empty description="暂无热力数据" />;

  const heatData = matrix.cells.map((c) => ({
    x: SENSITIVITY_LABEL[c.sensitivity] ?? c.sensitivity,
    sensKey: c.sensitivity,
    y: c.domain,
    value: c.count,
    piiCount: c.pii_count,
  }));
  const maxValue = Math.max(1, ...heatData.map((d) => d.value));
  const domainCount = new Set(heatData.map((d) => d.y)).size;
  const totalCount = heatData.reduce((a, b) => a + b.value, 0);
  const piiTotal = heatData.reduce((a, c) => a + c.piiCount, 0);
  // 域越多越需要高度，避免 y 轴标签/单元格被压缩
  const chartHeight = Math.max(420, domainCount * 34 + 90);

  return (
    <div>
      <Card
        title="敏感分布热力矩阵（业务域 × 敏感级别）"
        size="small"
        extra={
          <Space wrap>
            <Segmented
              size="small"
              value={colorTheme}
              onChange={(v) => setColorTheme(v as "blue" | "warm" | "green")}
              options={[
                { label: "蓝阶", value: "blue" },
                { label: "暖阶", value: "warm" },
                { label: "绿阶", value: "green" },
              ]}
            />
            <span className="muted">
              共 {totalCount} 项 · PII {piiTotal} 项 · 悬停查看 / 点击单元格下钻明细
            </span>
          </Space>
        }
      >
        {heatData.length === 0 ? (
          <Empty description="暂无热力数据" />
        ) : (
          <Heatmap
            data={heatData}
            xField="x"
            yField="y"
            colorField="value"
            height={chartHeight}
            shape="square"
            // 0 值映射浅灰，非 0 按量由浅到深蓝渐变；域标签过长时省略、悬停看全名
            scale={{
              color: {
                type: "linear",
                domain: [0, maxValue],
                range: colorRanges[colorTheme],
              },
            }}
            label={{
              text: "value",
              style: { fontSize: 11 },
              display: (d: { value: number }) => d.value > 0,
            }}
            style={{ inset: 3 }}
            legend={{ color: { title: "资产数" } }}
            axis={{
              y: {
                label: {
                  formatter: (v: string) => (v.length > 10 ? `${v.slice(0, 10)}…` : v),
                },
              },
            }}
            tooltip={{
              title: (d: { y: string; x: string }) => `${d.y} × ${d.x}`,
              items: [
                (d: { value: number }) => ({ name: "资产数", value: d.value }),
                (d: { piiCount: number }) => ({ name: "含 PII", value: d.piiCount }),
              ],
            }}
            onReady={(plot) => {
              plot.on(
                "element:click",
                (evt: { data?: { data?: { sensKey?: string; y?: string } } }) => {
                  const key = evt?.data?.data?.sensKey;
                  const domain = evt?.data?.data?.y;
                  if (key && domain) openCellDrill(key, domain);
                },
              );
            }}
          />
        )}
      </Card>
      <DrillDownDrawer
        open={drillOpen}
        title={drillTitle}
        columns={CATALOG_COLUMNS}
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
  const [view, setView] = useState<{
    owner_id: number;
    metrics: {
      total: number;
      published: number;
      draft: number;
      pii_count: number;
      by_domain: Record<string, number>;
    };
    catalogs: { total: number };
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  return (
    <Card
      title="责任人视图"
      size="small"
      extra={
        ownerOptions.length > 0 ? (
          <Select style={{ width: 180 }} value={ownerId} onChange={setOwnerId} options={ownerOptions} />
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
          <Col span={6}><Statistic title="指标总数" value={view.metrics.total} /></Col>
          <Col span={6}><Statistic title="已发布" value={view.metrics.published} valueStyle={{ color: "#2e9e5b" }} /></Col>
          <Col span={6}><Statistic title="草稿" value={view.metrics.draft} /></Col>
          <Col span={6}><Statistic title="PII 指标" value={view.metrics.pii_count} valueStyle={{ color: "#d64545" }} /></Col>
          <Col span={12}>
            <div style={{ marginTop: 8 }}>
              <span className="muted" style={{ fontSize: 13 }}>域分布</span>
              <Row gutter={[8, 8]} style={{ marginTop: 8 }}>
                {Object.entries(view.metrics.by_domain ?? {}).map(([k, v]) => (
                  <Col span={8} key={k}><Statistic title={k} value={v} /></Col>
                ))}
              </Row>
            </div>
          </Col>
          <Col span={12}>
            <div style={{ marginTop: 8 }}>
              <span className="muted" style={{ fontSize: 13 }}>目录资产</span>
              <div style={{ fontSize: 28, fontWeight: 600, fontFamily: "var(--font-display)" }}>{view.catalogs.total}</div>
            </div>
          </Col>
        </Row>
      )}
    </Card>
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
    <Card title="孤儿资产（无责任人）" size="small" extra={<Statistic title="数量" value={items.length} valueStyle={{ fontSize: 18 }} />}>
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
            { title: "类型", dataIndex: "entity_type", key: "entity_type", width: 90, render: (v: string) => ENTITY_TYPE_LABEL[v] ?? v },
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

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetchAssetTables({ sensitivity, limit: 200 })
      .then((r) => setItems(r.items))
      .catch((err) => setError(err instanceof Error ? err.message : "加载数据表失败"))
      .finally(() => setLoading(false));
  }, [sensitivity]);

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
            options={Object.keys(SENSITIVITY_LABEL).map((k) => ({ value: k, label: SENSITIVITY_LABEL[k] }))}
          />
          <Button icon={<DownloadOutlined />} onClick={handleExport}>
            导出 CSV
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
          columns={[
            { title: "数据源", dataIndex: "source_id", key: "source_id" },
            { title: "实体", dataIndex: "entity_name", key: "entity_name", ellipsis: true },
            { title: "类型", dataIndex: "entity_type", key: "entity_type", width: 90, render: (v: string) => ENTITY_TYPE_LABEL[v] ?? v },
            {
              title: "敏感度",
              dataIndex: "sensitivity_level",
              key: "sensitivity",
              width: 110,
              render: (s: string | null | undefined) => sensitivityTag(s),
            },
            { title: "责任人", dataIndex: "owner_id", key: "owner", width: 90, render: (v: number | null) => v ?? <Tag>无</Tag> },
            {
              title: "操作",
              key: "action",
              width: 80,
              render: (_: unknown, record: AssetTableItem) => (
                <Button
                  type="link"
                  size="small"
                  icon={<EyeOutlined />}
                  disabled={record.id == null}
                  onClick={() => openDetail(record)}
                >
                  详情
                </Button>
              ),
            },
          ]}
        />
      )}
      <Drawer
        title={detail ? `实体详情：${detail.entity_name}` : "实体详情"}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        width={560}
        destroyOnClose={false}
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
              {hasPii && (
                <Tag color="red" style={{ marginLeft: 8 }}>含 PII</Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="责任人">
              {detail.owner_id != null ? `#${detail.owner_id}` : <Tag>无</Tag>}
            </Descriptions.Item>
            <Descriptions.Item label="Schema 状态">
              {detail.schema_incomplete ? <Tag color="orange">不完整</Tag> : <Tag color="green">完整</Tag>}
            </Descriptions.Item>
            <Descriptions.Item label="内容指纹">
              {detail.content_signature ? (
                <Tooltip title={detail.content_signature}>
                  <span style={{ fontFamily: "monospace", cursor: "help" }}>{detail.content_signature.slice(0, 16)}…</span>
                </Tooltip>
              ) : (
                <span className="muted">-</span>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="Schema 摘要">{renderSchemaSummary(detail.schema_summary)}</Descriptions.Item>
            <Descriptions.Item label="关联血缘">
              <Button
                type="link"
                size="small"
                disabled={lineageCount <= 0}
                onClick={() => message.info(`实体「${detail.entity_name}」关联血缘 ${lineageCount} 条`) }
              >
                关联血缘 {lineageCount} 条
              </Button>
            </Descriptions.Item>
            <Descriptions.Item label="源健康">
              {detail.source_health ? (
                <Tag color={detail.source_health.health_status === "healthy" ? "green" : detail.source_health.health_status === "unhealthy" ? "red" : "default"}>
                  {SOURCE_HEALTH_LABEL[detail.source_health.health_status] ?? detail.source_health.health_status}
                </Tag>
              ) : (
                <span className="muted">未知</span>
              )}
              {detail.source_health?.last_health_check ? (
                <span className="muted" style={{ marginLeft: 8 }}>检查于 {new Date(detail.source_health.last_health_check).toLocaleString()}</span>
              ) : null}
            </Descriptions.Item>
            <Descriptions.Item label="新鲜度">
              <div className="muted" style={{ fontSize: 12 }}>
                <div>创建：{detail.created_at ? new Date(detail.created_at).toLocaleString() : "-"}</div>
                <div>更新：{detail.updated_at ? new Date(detail.updated_at).toLocaleString() : "-"}</div>
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
                  { title: "源", dataIndex: "source", key: "source", ellipsis: true, render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
                  { title: "目标", dataIndex: "target", key: "target", ellipsis: true, render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
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
                  { title: "指标", dataIndex: "metric_node", key: "metric", ellipsis: true, render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
                  { title: "关系", dataIndex: "edge_type", key: "edge", width: 140 },
                ]}
              />
            </Card>
          )}
          </>
        ) : null}
      </Drawer>
    </Card>
  );
}

// ----------------------------------------------------------------
// 产品补充（FR-18 生产化）：全局搜索 / 资产健康 / PII 合规 / 变更追踪 / 我的资产
// ----------------------------------------------------------------

function SearchTab() {
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
            columns={[
              { title: "类型", dataIndex: "type", key: "type", width: 90, render: (t: string) => <Tag color={t === "metric" ? "purple" : "blue"}>{t === "metric" ? "指标" : "目录"}</Tag> },
              { title: "名称", dataIndex: "name", key: "name", ellipsis: true, render: (v: string, r: AssetSearchItem) => (r.type === "metric" ? <span className="mono">{v}</span> : v) },
              { title: "实体类型", dataIndex: "entity_type", key: "entity_type", width: 100, render: (v: string) => ENTITY_TYPE_LABEL[v] ?? v },
              { title: "敏感度", dataIndex: "sensitivity_level", key: "sensitivity", width: 110, render: (s: string | null) => sensitivityTag(s) },
              { title: "域", dataIndex: "domain", key: "domain", width: 120, render: (v: string | null) => v ?? "-" },
              { title: "状态", dataIndex: "status", key: "status", width: 110, render: (v: string | null) => v ?? "-" },
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
          <Card size="small"><Statistic title="不健康数据源" value={unhealthyCount} valueStyle={{ color: unhealthyCount > 0 ? "#cf1322" : "#3f8600" }} /></Card>
        </Col>
        <Col span={6}>
          <Card size="small"><Statistic title="Schema 不完整" value={incompleteCount} valueStyle={{ color: incompleteCount > 0 ? "#d46b08" : "#3f8600" }} /></Card>
        </Col>
        <Col span={6}>
          <Card size="small"><Statistic title="孤儿资产" value={data.orphan_assets} valueStyle={{ color: data.orphan_assets > 0 ? "#d46b08" : "#3f8600" }} /></Card>
        </Col>
        <Col span={6}>
          <Card size="small"><Statistic title={`${data.stale_days} 天未更新`} value={staleCount} valueStyle={{ color: staleCount > 0 ? "#d46b08" : "#3f8600" }} /></Card>
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
                  { title: "状态", dataIndex: "health_status", key: "status", width: 100, render: (v: string) => <Tag color="red">{SOURCE_HEALTH_LABEL[v] ?? v}</Tag> },
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
                  { title: "更新时间", dataIndex: "updated_at", key: "updated", width: 170, render: (v: string) => new Date(v).toLocaleString() },
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
          <Card size="small"><Statistic title="PII 指标数" value={data.pii_metric_count} valueStyle={{ color: "#cf1322" }} /></Card>
        </Col>
        <Col span={6}>
          <Card size="small"><Statistic title="PII 目录数" value={data.pii_catalog_count} valueStyle={{ color: "#cf1322" }} /></Card>
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
                { title: "敏感级", dataIndex: "key", key: "key", render: (k: string) => sensitivityTag(k) },
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
          { title: "类型", dataIndex: "entity_type", key: "type", width: 90, render: (v: string) => ENTITY_TYPE_LABEL[v] ?? v },
          { title: "敏感度", dataIndex: "sensitivity_level", key: "sensitivity", width: 110, render: (s: string | null) => sensitivityTag(s) },
          { title: "源", dataIndex: "source_id", key: "source", width: 120 },
          { title: "更新时间", dataIndex: "updated_at", key: "updated", width: 180, render: (v: string) => new Date(v).toLocaleString() },
        ]}
      />
      <Table
        dataSource={data.metrics}
        rowKey={(r) => `m-${r.metric_code}`}
        size="small"
        pagination={{ pageSize: 10 }}
        style={{ marginTop: 16 }}
        title={() => <b>指标</b>}
        columns={[
          { title: "编码", dataIndex: "metric_code", key: "code", ellipsis: true, render: (v: string) => <span className="mono">{v}</span> },
          { title: "名称", dataIndex: "name", key: "name", ellipsis: true },
          { title: "状态", dataIndex: "status", key: "status", width: 110 },
          { title: "域", dataIndex: "domain", key: "domain", width: 110 },
          { title: "PII", dataIndex: "pii_flag", key: "pii", width: 70, render: (v: boolean) => (v ? <Tag color="red">PII</Tag> : null) },
          { title: "更新时间", dataIndex: "updated_at", key: "updated", width: 180, render: (v: string) => new Date(v).toLocaleString() },
        ]}
      />
    </Card>
  );
}

function MyAssetsTab() {
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
          { title: "类型", dataIndex: "entity_type", key: "type", width: 90, render: (v: string) => ENTITY_TYPE_LABEL[v] ?? v },
          { title: "敏感度", dataIndex: "sensitivity_level", key: "sensitivity", width: 110, render: (s: string | null) => sensitivityTag(s) },
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
        columns={[
          { title: "编码", dataIndex: "metric_code", key: "code", ellipsis: true, render: (v: string) => <span className="mono">{v}</span> },
          { title: "名称", dataIndex: "name", key: "name", ellipsis: true },
          { title: "状态", dataIndex: "status", key: "status", width: 110 },
          { title: "域", dataIndex: "domain", key: "domain", width: 110 },
          { title: "PII", dataIndex: "pii_flag", key: "pii", width: 70, render: (v: boolean) => (v ? <Tag color="red">PII</Tag> : null) },
        ]}
      />
    </div>
  );
}

export function AssetMap() {
  const [activeTab, setActiveTab] = useState("graph");
  const { track } = useTracking();

  useEffect(() => {
    track("view", undefined, "page", { page: "assetmap" });
  }, [track]);

  const tabItems = [
    { key: "overview", label: <span><GlobalOutlined /> 概览</span>, children: <OverviewTab /> },
    { key: "search", label: <span><SearchOutlined /> 搜索</span>, children: <SearchTab /> },
    { key: "graph", label: <span><ApartmentOutlined /> 资产地图</span>, children: <GraphTab /> },
    { key: "heatmap", label: <span><HeatMapOutlined /> 热力视图</span>, children: <HeatmapTab /> },
    { key: "health", label: <span><HeartOutlined /> 资产健康</span>, children: <HealthTab /> },
    { key: "pii", label: <span><SafetyOutlined /> PII 合规</span>, children: <PiiTab /> },
    { key: "changes", label: <span><TableOutlined /> 变更追踪</span>, children: <ChangesTab /> },
    { key: "mine", label: <span><UserOutlined /> 我的资产</span>, children: <MyAssetsTab /> },
    { key: "owner", label: <span><UserOutlined /> Owner 视图</span>, children: <OwnerTab /> },
    { key: "orphans", label: <span><DeleteOutlined /> 孤儿资产</span>, children: <OrphansTab /> },
    { key: "tables", label: <span><TableOutlined /> 数据表</span>, children: <TablesTab /> },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
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
