import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Popconfirm,
  Row,
  Segmented,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Tabs,
  message,
} from "antd";
import {
  ApartmentOutlined,
  ArrowDownOutlined,
  ArrowLeftOutlined,
  ArrowUpOutlined,
  CodeOutlined,
  DatabaseOutlined,
  PieChartOutlined,
  PlusOutlined,
  ReloadOutlined,
  ShareAltOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import {
  confirmStaleEdge,
  deleteLineageEdge,
  fetchLineageBrokenEdges,
  fetchLineageCoverage,
  fetchLineageEdgeDetail,
  fetchLineageOrphans,
  getCatalogDetail,
  lineageChannelRuns,
  lineageChannels,
  lineageEdges,
  lineageGraph,
  lineageImpact,
  lineageImpactPreview,
  lineageNodes,
  lineageRunDetail,
  lineageStale,
  parseLineage,
  restoreStaleEdge,
  UnisenseApiError,
} from "../api";
import type {
  CoverageBrokenEdgeItem,
  CoverageOrphanItem,
  DBCatalog,
  LineageChannel,
  LineageCoverage,
  LineageEdge,
  LineageEdgeDetail,
  LineageIngestRun,
  LineageNode,
  LineageNodeInfo,
  ParseLineageResult,
  StaleEdge,
  UpstreamDeps,
} from "../types";
import { AssetGraph, AssetGraphNode, AssetGraphEdge } from "../components/assetmap/AssetGraph";
import { MetricDetailDrawer } from "../components/assetmap/MetricDetailDrawer";
import { ManualEdgeModal } from "../components/lineage/ManualEdgeModal";
import { useTracking } from "../hooks/useTracking";
import { usePermission } from "../hooks/usePermission";
import { enumLabel, GRANULARITY_LABEL } from "../utils/enums";
import { formatCnTime } from "../utils/timeCn";
import { formatSql } from "../utils/sqlFormat";

const RISK_LEVEL_LABEL: Record<string, string> = {
  low: "低",
  medium: "中",
  high: "高",
  critical: "严重",
};

const EDGE_TYPE_LABEL: Record<string, string> = {
  DERIVED_FROM: "派生自",
  CONSUMED_BY: "被消费",
  LINEAGE_UP: "上游关联",
  LINEAGE_DOWN: "下游关联",
  EXTERNAL_BREAK: "断链",
  USES_DIMENSION: "使用维度",
  READS_COLUMN: "读取字段",
};

/** SQL 血缘解析支持的数据库方言（对齐后端 SourceTypeEnum 与 sqlglot dialect 名）。 */
const SQL_DIALECT_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "mysql", label: "MySQL" },
  { value: "postgres", label: "PostgreSQL" },
  { value: "hive", label: "Hive" },
  { value: "spark", label: "Spark" },
  { value: "doris", label: "Doris" },
  { value: "clickhouse", label: "ClickHouse" },
  { value: "starrocks", label: "StarRocks" },
];

/** 血缘采集来源通道友好名称（provenance → 中文标签；未知来源显示原始标识）。 */
const CHANNEL_LABEL: Record<string, string> = {
  sqlglot: "SQL 解析",
  dp_csv: "DP 同步",
  metric_definition: "指标定义",
  quickbi: "QuickBI",
  neo4j: "图同步",
  manual: "手动登记",
  external: "外部依赖",
};

/** 边详情页 provenance → 中文口径映射（Task B 边元数据展示）。未列出来源显示原始标识。 */
const EDGE_PROVENANCE_LABEL: Record<string, string> = {
  metric_definition: "指标口径",
  sql_parse: "SQL解析",
  dp_csv: "DP导入",
  metric_consumer: "指标消费",
};

/** 边详情/覆盖率治理：节点前缀 → 层级 Tag（颜色 + 中文类型标签，Task D 分层链路着色）。 */
const EDGE_NODE_TYPE_TAG: Record<string, { color: string; label: string }> = {
  metric: { color: "purple", label: "指标" },
  table: { color: "blue", label: "表" },
  field: { color: "cyan", label: "字段" },
  column: { color: "cyan", label: "字段" },
  dimension: { color: "geekblue", label: "维度" },
  consumer: { color: "green", label: "消费方" },
  query: { color: "gold", label: "查询" },
  external: { color: "default", label: "外部" },
};

/** 解析节点 id（``metric:xxx`` / ``table:xxx``）的层级 Tag，未知前缀返回 null（不渲染）。 */
function nodeTypeTag(id: string) {
  const colon = id.indexOf(":");
  const prefix = colon === -1 ? "" : id.slice(0, colon);
  const def = EDGE_NODE_TYPE_TAG[prefix];
  if (!def) return null;
  return <Tag color={def.color}>{def.label}</Tag>;
}

/** 血缘候选节点类型标签（影响分析选项框下拉分组）。 */
const NODE_TYPE_LABEL: Record<string, string> = {
  table: "表",
  metric: "指标",
  field: "字段",
  column: "字段",
  dimension: "维度",
  external: "外部",
  other: "节点",
};

const SENSITIVITY_COLOR: Record<string, string> = {
  INTERNAL: "default",
  CONFIDENTIAL: "orange",
  SECRET: "volcano",
  "PII-LOW": "cyan",
  "PII-MEDIUM": "gold",
  "PII-HIGH": "red",
};

/**
 * 图谱聚焦节点解析：node 参数可能是完整 id（``metric:xxx`` / ``table:xxx``）或裸编码
 * （指标详情「在图谱中查看」跳转只带裸 metric_code）。返回图中实际存在的节点 id。
 */
export function resolveRootId(node: string, nodes: AssetGraphNode[]): string | null {
  if (nodes.some((n) => n.id === node)) return node;
  for (const prefix of ["metric:", "table:", "field:"]) {
    const candidate = `${prefix}${node}`;
    if (nodes.some((n) => n.id === candidate)) return candidate;
  }
  return null;
}

/**
 * 以 root 节点为中心，沿血缘边双向 BFS 展开 maxHops 跳，返回自包含子图
 * （仅保留两端都在子图内的边）。用于「从指标详情跳转图谱时限定该指标上下游」，
 * 避免展示全量血缘节点。
 */
export function buildSubgraph(
  nodes: AssetGraphNode[],
  edges: AssetGraphEdge[],
  rootId: string,
  maxHops: number,
): { nodes: AssetGraphNode[]; edges: AssetGraphEdge[] } {
  const nodeById = new Map(nodes.map((n) => [n.id, n]));
  if (!nodeById.has(rootId)) return { nodes: [], edges: [] };
  const visited = new Set<string>([rootId]);
  let frontier: string[] = [rootId];
  const subEdges: AssetGraphEdge[] = [];
  const seen = new Set<string>();
  for (let hop = 0; hop < maxHops && frontier.length > 0; hop++) {
    const next: string[] = [];
    for (const nid of frontier) {
      for (const e of edges) {
        if (e.source !== nid && e.target !== nid) continue;
        const key = `${e.source}__${e.target}`;
        if (!seen.has(key)) {
          seen.add(key);
          subEdges.push(e);
        }
        const other = e.source === nid ? e.target : e.source;
        if (!visited.has(other)) {
          visited.add(other);
          next.push(other);
        }
      }
    }
    frontier = next;
  }
  const subNodes = Array.from(visited)
    .map((id) => nodeById.get(id))
    .filter((n): n is AssetGraphNode => Boolean(n));
  return { nodes: subNodes, edges: subEdges };
}

type Direction = "upstream" | "downstream" | "both";

/**
 * 从血缘边列表构建图谱数据（血缘查询/影响分析结果图形化展示用）。
 * 节点 id 去重、label 去类型前缀、type 由前缀推断（table:/metric:/field: → 对应类型，
 * external:/未知 → other）；边直接透传 source/target/edge_type。
 * 可选的 ``nodeMeta``（后端 /impact 与 /edges 携带的节点基础元数据）会合并进节点，
 * 使图节点具备 entity_id/domain/owner/pii——点击表节点可直达目录详情、节点按域/PII 着色。
 */
export function edgesToGraphData(
  edges: LineageEdge[],
  nodeMeta?: LineageNodeInfo[],
): { nodes: AssetGraphNode[]; edges: AssetGraphEdge[] } {
  const metaById = new Map((nodeMeta ?? []).map((m) => [m.id, m]));
  const nodeMap = new Map<string, AssetGraphNode>();
  const graphEdges: AssetGraphEdge[] = [];
  const addNode = (id: string) => {
    if (nodeMap.has(id)) return;
    const colon = id.indexOf(":");
    const prefix = colon === -1 ? "" : id.slice(0, colon);
    const label = colon === -1 ? id : id.slice(colon + 1);
    const type =
      prefix === "table" ? "table" : prefix === "metric" ? "metric" : prefix === "field" ? "field" : "other";
    const meta = metaById.get(id);
    nodeMap.set(id, {
      id,
      type,
      label: label || id,
      entity_id: meta?.entity_id ?? undefined,
      pii: meta?.pii,
      domain: meta?.domain ?? undefined,
      owner: meta?.owner ?? undefined,
    });
  };
  for (const e of edges) {
    addNode(e.source_node);
    addNode(e.target_node);
    graphEdges.push({ source: e.source_node, target: e.target_node, type: e.edge_type });
  }
  return { nodes: Array.from(nodeMap.values()), edges: graphEdges };
}

/**
 * 把本次 SQL 解析的表级/字段级边合并构建为血缘图谱数据（SQL 血缘解析页当页图谱展示）。
 * - 表级边：源表 → 目标表（后端返回 ``table:`` 前缀节点，label 去前缀展示表名）；
 * - 字段级边：源字段 → 目标字段（前端拼 ``field:表.列`` 节点，label 展示 表.列）。
 * 表/字段节点与边合并到同一张图，完整呈现本次解析的血缘流转（源 → 目标方向）。
 */
export function parseResultToGraphData(result: ParseLineageResult): {
  nodes: AssetGraphNode[];
  edges: AssetGraphEdge[];
} {
  const nodeMap = new Map<string, AssetGraphNode>();
  const graphEdges: AssetGraphEdge[] = [];
  const addNode = (id: string, type: "table" | "field") => {
    if (nodeMap.has(id)) return;
    const colon = id.indexOf(":");
    nodeMap.set(id, { id, type, label: colon === -1 ? id : id.slice(colon + 1) });
  };
  for (const e of result.table_lineage) {
    addNode(e.source, "table");
    addNode(e.target, "table");
    graphEdges.push({ source: e.source, target: e.target, type: "DERIVED_FROM" });
  }
  for (const f of result.field_lineage) {
    const srcId = `field:${f.source_table}${f.source_column ? `.${f.source_column}` : ".*"}`;
    const dstId = `field:${f.target_table}.${f.target_column}`;
    addNode(srcId, "field");
    addNode(dstId, "field");
    graphEdges.push({ source: srcId, target: dstId, type: "DERIVED_FROM" });
  }
  return { nodes: Array.from(nodeMap.values()), edges: graphEdges };
}

/** 把纯 SELECT（无落点）的上游依赖清单构建为「本次查询 → 表 → 字段」三层的上游依赖图谱。
 *  - 中心虚拟节点「本次查询」为最上层（本次查询动作的汇聚点）；
 *  - FROM/JOIN 源表挂在中心之下（本次查询 → 表）；
 *  - 读取字段挂在所属表之下（表 → 字段），形成「本次查询-表-字段」层级；
 *  - 无表前缀的裸列名（未限定列）直接挂在中心之下。
 * 边方向即血缘 DAG 方向（源在上、目标在下），仅展示，不写图谱。
 */
export function upstreamDepsToGraphData(deps: UpstreamDeps): {
  nodes: AssetGraphNode[];
  edges: AssetGraphEdge[];
} {
  const nodeMap = new Map<string, AssetGraphNode>();
  const graphEdges: AssetGraphEdge[] = [];
  const seenEdges = new Set<string>();
  const QUERY_ID = "query:本次查询";
  nodeMap.set(QUERY_ID, { id: QUERY_ID, type: "metric", label: "本次查询" });
  const addNode = (id: string, type: "table" | "field"): string => {
    if (!nodeMap.has(id)) nodeMap.set(id, { id, type, label: id.slice(id.indexOf(":") + 1) });
    return id;
  };
  const addEdge = (source: string, target: string) => {
    const key = `${source}__${target}`;
    if (seenEdges.has(key)) return;
    seenEdges.add(key);
    graphEdges.push({ source, target, type: "READS_FROM" });
  };
  // 第一层：本次查询 → 源表
  for (const t of deps.tables) addEdge(QUERY_ID, addNode(`table:${t}`, "table"));
  // 第二层：表 → 所属字段；裸列名（无表前缀）直接挂中心。
  // 表名可能带库前缀（如 wedw_dw.wy_zh_hospital_std_df.hosp_id），须按最后一个点
  // 拆分（列名不含点），避免把库名拆成表节点导致字段挂错表。
  for (const f of deps.fields) {
    const fieldId = addNode(`field:${f}`, "field");
    const dot = f.lastIndexOf(".");
    if (dot > 0) {
      // 字段限定列（表.列）：挂到所属表下；即使该表未出现在 tables 也补建表节点
      const tableId = addNode(`table:${f.slice(0, dot)}`, "table");
      addEdge(QUERY_ID, tableId);
      addEdge(tableId, fieldId);
    } else {
      addEdge(QUERY_ID, fieldId);
    }
  }
  return { nodes: Array.from(nodeMap.values()), edges: graphEdges };
}

/** 表/视图详情侧边栏（血缘图谱与血缘查询/影响分析图谱点击表节点时共用）。
 *  展示敏感度/所属源/Schema 完整度/字段清单/ETL SQL，并提供「在指标目录中查看」入口。 */
function TableDetailDrawer({ detail, open, onClose, loading }: {
  detail: DBCatalog | null;
  open: boolean;
  onClose: () => void;
  loading: boolean;
}) {
  const navigate = useNavigate();
  const { can } = usePermission();
  // 手动添加上下游（人工治理）
  const [manualOpen, setManualOpen] = useState(false);
  const [manualDirection, setManualDirection] = useState<"upstream" | "downstream">("downstream");
  // schema_json.columns 详细格式：{name, type, nullable, comment, default}
  const columns = (detail?.schema_def?.columns ?? []) as Array<Record<string, unknown>>;
  const columnData = columns
    .map((c, i) => ({
      key: i,
      name: String(c.name ?? ""),
      type: String(c.type ?? ""),
      nullable: c.nullable ? "是" : "否",
      comment: String(c.comment ?? ""),
    }))
    .filter((c) => c.name);

  function goToCatalog() {
    if (!detail) return;
    navigate(`/catalog?kw=${encodeURIComponent(detail.entity_name)}`);
  }

  return (
    <Drawer
      title={detail ? `${detail.entity_type === "VIEW" ? "视图" : "表"} · ${detail.entity_name}` : "表详情"}
      width={680}
      open={open}
      onClose={onClose}
      loading={loading}
      extra={
        <Button type="primary" onClick={goToCatalog} disabled={!detail}>
          在指标目录中查看
        </Button>
      }
    >
      {detail && (
        <div>
          <Descriptions size="small" column={2} bordered>
            <Descriptions.Item label="实体名称">{detail.entity_name}</Descriptions.Item>
            <Descriptions.Item label="实体类型">
              {detail.entity_type === "VIEW" ? "视图" : "表"}
            </Descriptions.Item>
            <Descriptions.Item label="敏感度">
              <Tag color={SENSITIVITY_COLOR[detail.sensitivity_level] ?? "default"}>
                {detail.sensitivity_level || "未分级"}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label="所属数据源">
              {detail.source_name ?? detail.source_id}
              {detail.source_deleted && <Tag color="red" style={{ marginLeft: 6 }}>源已删除</Tag>}
            </Descriptions.Item>
            <Descriptions.Item label="Schema 完整">
              {detail.schema_incomplete ? <Tag color="orange">不完整</Tag> : "完整"}
            </Descriptions.Item>
            <Descriptions.Item label="字段数">{columnData.length}</Descriptions.Item>
          </Descriptions>

          <h4 style={{ marginTop: 16 }}>字段清单（{columnData.length}）</h4>
          {columnData.length > 0 ? (
            <Table
              size="small"
              rowKey="key"
              dataSource={columnData}
              pagination={false}
              columns={[
                { title: "字段名", dataIndex: "name", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
                { title: "类型", dataIndex: "type", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
                { title: "可空", dataIndex: "nullable", width: 60 },
                { title: "注释", dataIndex: "comment" },
              ]}
            />
          ) : (
            <Empty description="该实体无字段元数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          )}

          {detail.etl_sql && (
            <>
              <h4 style={{ marginTop: 16 }}>ETL SQL</h4>
              <pre className="mono" style={{ fontSize: 12, background: "#f5f5f5", padding: 12, borderRadius: 6, maxHeight: 240, overflow: "auto" }}>
                {formatSql(detail.etl_sql)}
              </pre>
            </>
          )}

          <Space style={{ marginTop: 16 }}>
            <Button
              icon={<ArrowUpOutlined />}
              disabled={!can("lineage:write")}
              onClick={() => {
                setManualDirection("upstream");
                setManualOpen(true);
              }}
            >
              添加上游
            </Button>
            <Button
              icon={<ArrowDownOutlined />}
              disabled={!can("lineage:write")}
              onClick={() => {
                setManualDirection("downstream");
                setManualOpen(true);
              }}
            >
              添加下游
            </Button>
          </Space>
          <ManualEdgeModal
            open={manualOpen}
            onClose={() => setManualOpen(false)}
            baseNode={`table:${detail.entity_name}`}
            baseLabel={detail.entity_name}
            defaultDirection={manualDirection}
          />
        </div>
      )}
    </Drawer>
  );
}

/** 血缘图谱 Tab：进入即加载血缘图谱。指标/表节点点击均在本页以侧边栏
 *  展示详情（指标详情抽屉 / 表详情抽屉），不跳转页面，用户可再决定是否前往完整页面。
 *  支持 URL ``?node=xxx`` 聚焦：从指标详情「在图谱中查看」跳转时，仅展示该节点
 *  上下游子图（BFS 展开 3 跳），而非全量血缘节点。 */
function GraphTab() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [data, setData] = useState<{ nodes: AssetGraphNode[]; edges: AssetGraphEdge[] } | null>(null);
  const [loading, setLoading] = useState(false);
  // 来源通道筛选：all=全通道表级血缘（默认，含 DP 同步/SQL 解析/指标定义）；
  // 具体通道名=仅该通道；空=采集目录视角（指标+采集目录表小图）
  const [provenance, setProvenance] = useState<string>("all");
  // 聚焦节点：URL ?node= 参数（指标详情「在图谱中查看」跳转来源），限定该指标/表上下游
  const focusNode = searchParams.get("node")?.trim() || null;
  // 聚焦节点不在图谱中（无血缘数据）时的空态标记
  const [focusMiss, setFocusMiss] = useState(false);
  // 指标节点详情抽屉（侧边栏）
  const [metricDrawerOpen, setMetricDrawerOpen] = useState(false);
  const [metricCode, setMetricCode] = useState<string | null>(null);
  // 表节点详情抽屉
  const [detail, setDetail] = useState<DBCatalog | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  // 字段节点手动治理（Task：补齐所有节点类型入口）
  const [fieldNode, setFieldNode] = useState<AssetGraphNode | null>(null);
  const [fieldTableNode, setFieldTableNode] = useState<AssetGraphNode | null>(null);
  const [fieldManualOpen, setFieldManualOpen] = useState(false);
  const [fieldManualDir, setFieldManualDir] = useState<"upstream" | "downstream">("downstream");
  // 维度/消费方节点轻量详情 + 手动治理（Task：补齐所有节点类型入口）
  const [metaNode, setMetaNode] = useState<{ id: string; label: string; kind: "dimension" | "consumer"; domain?: string } | null>(null);
  const [metaNodeOpen, setMetaNodeOpen] = useState(false);
  const [metaManualOpen, setMetaManualOpen] = useState(false);
  const [metaManualDir, setMetaManualDir] = useState<"upstream" | "downstream">("downstream");
  const { track } = useTracking();
  const { can } = usePermission();

  // 请求序号令牌（P1 竞态修复）：provenance/聚焦切换时旧响应不覆盖新数据
  const loadSeqRef = useRef(0);

  async function load() {
    const seq = ++loadSeqRef.current;
    setLoading(true);
    setFocusMiss(false);
    try {
      const d = await lineageGraph({ limit: 2000, provenance: provenance || undefined });
      if (seq !== loadSeqRef.current) return; // 旧响应丢弃
      let nodes = d.nodes as AssetGraphNode[];
      let edges = d.edges as AssetGraphEdge[];
      if (focusNode) {
        const rootId = resolveRootId(focusNode, nodes);
        if (!rootId) {
          // 聚焦节点不存在于血缘图中：展示空态而非全量
          setFocusMiss(true);
          setData({ nodes: [], edges: [] });
          return;
        }
        const sub = buildSubgraph(nodes, edges, rootId, 3);
        nodes = sub.nodes;
        edges = sub.edges;
        if (nodes.length === 0) setFocusMiss(true);
      }
      if (seq !== loadSeqRef.current) return; // 聚焦子图构建期间的竞态丢弃
      setData({ nodes, edges });
      track("lineage_graph_view");
    } catch (err) {
      if (seq !== loadSeqRef.current) return; // 旧请求失败不弹错
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载血缘图谱失败");
      setData(null);
    } finally {
      if (seq === loadSeqRef.current) setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusNode, provenance]);

  /** 清除聚焦：回到全量图谱并移除 URL node 参数（保持地址与视图一致）。 */
  function clearFocus() {
    setSearchParams({}, { replace: true });
  }

  async function openTableDetail(node: AssetGraphNode) {
    const entityId = node.entity_id;
    if (!entityId) {
      message.warning("该表节点缺少目录实体标识，无法查看详情");
      return;
    }
    setDetailOpen(true);
    setDetailLoading(true);
    setDetail(null);
    try {
      setDetail(await getCatalogDetail(entityId));
      track("lineage_table_detail", node.label, "table");
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载表详情失败");
      setDetailOpen(false);
    } finally {
      setDetailLoading(false);
    }
  }

  function handleNodeClick(node: AssetGraphNode) {
    if (node.type === "metric") {
      // 本页侧边栏展示指标详情，不跳转页面（保留「前往完整详情」按钮作为补充入口）
      setMetricCode(node.id.replace(/^metric:/, ""));
      setMetricDrawerOpen(true);
    } else if (node.type === "table") {
      void openTableDetail(node);
    } else if (node.type === "dimension") {
      // 维度节点：轻量抽屉展示详情 + 手动添加上下游
      setMetaNode({ id: node.id, label: node.label, kind: "dimension", domain: node.domain });
      setMetaNodeOpen(true);
    } else if (node.type === "consumer") {
      // 消费方节点：轻量抽屉展示详情 + 手动添加上下游
      setMetaNode({ id: node.id, label: node.label, kind: "consumer", domain: node.domain });
      setMetaNodeOpen(true);
    } else if (node.type === "field" || node.type === "column") {
      // 字段节点：展示字段名 + 所属表入口 + 手动添加上下游（baseNode=column:）
      setFieldNode(node);
      const tbl = node.label.split(".").slice(0, -1).join(".");
      setFieldTableNode({ ...node, label: tbl } as AssetGraphNode);
    }
  }

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
          刷新
        </Button>
        <Select
          value={provenance}
          onChange={setProvenance}
          style={{ width: 180 }}
          options={[
            { value: "all", label: "全部血缘（含 DP/SQL/指标）" },
            { value: "dp_csv", label: "DP 同步血缘" },
            { value: "sqlglot", label: "SQL 解析血缘" },
            { value: "metric_definition", label: "指标定义血缘" },
            { value: "", label: "采集目录视角（指标+目录表）" },
          ]}
        />
        {focusNode && (
          <Tag color="blue" style={{ padding: "3px 10px", fontSize: 13 }}>
            聚焦：{focusNode} 的上下游血缘
            <Button type="link" size="small" style={{ padding: 0, marginLeft: 6 }} onClick={clearFocus}>
              清除
            </Button>
          </Tag>
        )}
        <span className="muted" style={{ fontSize: 13 }}>
          {data
            ? focusNode
              ? `已限定为 ${data.nodes.length} 节点 · ${data.edges.length} 条血缘边`
              : `共 ${data.nodes.length} 节点 · ${data.edges.length} 条血缘边`
            : "加载血缘图谱…"}
          ，点击节点：指标 / 表视图均在本页侧边栏展示详情
        </span>
      </Space>
      {data && data.nodes.length > 0 ? (
        <AssetGraph
          nodes={data.nodes}
          edges={data.edges}
          height={560}
          onNodeClick={handleNodeClick}
          // 血缘总览默认隐藏字段节点，聚焦子图同样隐藏，聚焦指标/表主干
          showFields={false}
          // 语义泳道：指标/表分带（表带在上、指标带下），表→指标血缘方向自然分层
          lanes
        />
      ) : (
        !loading &&
        (focusMiss ? (
          <Empty
            description={`「${focusNode}」暂无血缘数据。可在「SQL 血缘解析」粘贴 SQL 入库，或运行 scripts/import_dp_lineage.py 导入。`}
          >
            <Button type="primary" onClick={clearFocus}>
              查看全量血缘图谱
            </Button>
          </Empty>
        ) : (
          <Empty description="暂无血缘图谱数据。可在「SQL 血缘解析」粘贴 SQL 入库，或运行 scripts/import_dp_lineage.py 导入。" />
        ))
      )}

      {/* 表/视图详情侧边栏：点击血缘图谱中的表节点打开（不跳转页面） */}
      <TableDetailDrawer
        detail={detail}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        loading={detailLoading}
      />

      {/* 指标节点详情侧边栏：点击血缘图谱中的指标节点打开（不跳转页面） */}
      <MetricDetailDrawer
        open={metricDrawerOpen}
        metricCode={metricCode}
        onClose={() => setMetricDrawerOpen(false)}
      />

      {/* 字段节点信息：展示字段名 + 所属表入口 + 手动添加上下游（Task：补齐所有节点类型入口） */}
      <Drawer
        title="字段信息"
        width={480}
        open={fieldNode != null}
        onClose={() => setFieldNode(null)}
      >
        {fieldNode && (
          <>
            <Descriptions column={2} bordered size="small">
              <Descriptions.Item label="字段名">{fieldNode.label}</Descriptions.Item>
              <Descriptions.Item label="类型">字段</Descriptions.Item>
              <Descriptions.Item label="所属表">
                {fieldTableNode?.label ?? <span className="muted">不在当前视图</span>}
              </Descriptions.Item>
              <Descriptions.Item label="业务域">
                {fieldNode.domain ?? <span className="muted">-</span>}
              </Descriptions.Item>
              <Descriptions.Item label="PII">
                {fieldNode.pii ? <Tag color="red">含 PII</Tag> : <Tag>否</Tag>}
              </Descriptions.Item>
            </Descriptions>
            <Space style={{ marginTop: 16 }} direction="vertical">
              <span className="muted" style={{ fontSize: 13 }}>
                手动登记该字段的血缘关系：
              </span>
              <Space>
                <Button
                  icon={<ArrowUpOutlined />}
                  disabled={!can("lineage:write")}
                  onClick={() => {
                    setFieldManualDir("upstream");
                    setFieldManualOpen(true);
                  }}
                >
                  添加上游
                </Button>
                <Button
                  icon={<ArrowDownOutlined />}
                  disabled={!can("lineage:write")}
                  onClick={() => {
                    setFieldManualDir("downstream");
                    setFieldManualOpen(true);
                  }}
                >
                  添加下游
                </Button>
              </Space>
            </Space>
          </>
        )}
      </Drawer>
      {fieldNode && (
        <ManualEdgeModal
          open={fieldManualOpen}
          onClose={() => setFieldManualOpen(false)}
          baseNode={fieldNode.id}
          baseLabel={fieldNode.label}
          defaultDirection={fieldManualDir}
          onSuccess={() => void load()}
        />
      )}

      {/* 维度 / 消费方节点轻量详情抽屉 + 手动治理（Task：补齐所有节点类型入口） */}
      <Drawer
        title={metaNode ? `${metaNode.kind === "dimension" ? "维度" : "消费方"} · ${metaNode.label}` : "节点详情"}
        width={480}
        open={metaNodeOpen}
        onClose={() => setMetaNodeOpen(false)}
      >
        {metaNode && (
          <>
            <Descriptions column={2} bordered size="small">
              <Descriptions.Item label="节点类型">
                <Tag color={metaNode.kind === "dimension" ? "geekblue" : "green"}>
                  {metaNode.kind === "dimension" ? "维度" : "消费方"}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="节点标识">
                <span className="mono">{metaNode.id}</span>
              </Descriptions.Item>
              <Descriptions.Item label="名称">{metaNode.label}</Descriptions.Item>
              <Descriptions.Item label="业务域">
                {metaNode.domain ?? <span className="muted">-</span>}
              </Descriptions.Item>
            </Descriptions>
            <span className="muted" style={{ display: "block", marginTop: 16, fontSize: 13 }}>
              手动登记该{metaNode.kind === "dimension" ? "维度" : "消费方"}的血缘关系：
            </span>
            <Space style={{ marginTop: 8 }}>
              <Button
                icon={<ArrowUpOutlined />}
                disabled={!can("lineage:write")}
                onClick={() => {
                  setMetaManualDir("upstream");
                  setMetaManualOpen(true);
                }}
              >
                添加上游
              </Button>
              <Button
                icon={<ArrowDownOutlined />}
                disabled={!can("lineage:write")}
                onClick={() => {
                  setMetaManualDir("downstream");
                  setMetaManualOpen(true);
                }}
              >
                添加下游
              </Button>
            </Space>
          </>
        )}
      </Drawer>
      {metaNode && (
        <ManualEdgeModal
          open={metaManualOpen}
          onClose={() => setMetaManualOpen(false)}
          baseNode={metaNode.id}
          baseLabel={metaNode.label}
          defaultDirection={metaManualDir}
          onSuccess={() => void load()}
        />
      )}
    </div>
  );
}

function ImpactTab() {
  const [node, setNode] = useState("");
  const [nodeOptions, setNodeOptions] = useState<LineageNode[]>([]);
  const [nodeLoading, setNodeLoading] = useState(false);
  const [searchWord, setSearchWord] = useState("");
  const [direction, setDirection] = useState<Direction>("downstream");
  const [edges, setEdges] = useState<LineageEdge[]>([]);
  const [total, setTotal] = useState(0);
  const [risk, setRisk] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // 查询结果的血缘视图（图形化展示，替代纯文字边列表为主展示）
  const [graphData, setGraphData] = useState<{
    nodes: AssetGraphNode[];
    edges: AssetGraphEdge[];
  } | null>(null);
  // 节点详情侧边栏（点击图谱节点展示具体信息，对齐血缘图谱/资产地图交互）
  const [metricDrawerOpen, setMetricDrawerOpen] = useState(false);
  const [metricCode, setMetricCode] = useState<string | null>(null);
  const [detail, setDetail] = useState<DBCatalog | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  // 字段节点信息（field 无独立详情表：展示字段名 + 所属表入口）
  const [fieldNode, setFieldNode] = useState<AssetGraphNode | null>(null);
  const [fieldTableNode, setFieldTableNode] = useState<AssetGraphNode | null>(null);
  // 边详情抽屉（Task B）：点击边明细表格某行打开
  const [edgeDetailId, setEdgeDetailId] = useState<number | null>(null);
  const [edgeDetailOpen, setEdgeDetailOpen] = useState(false);
  const { track } = useTracking();

  /** 候选节点：无关键词加载 top-N 预加载，有关键词远程搜索指定节点。 */
  async function loadNodes(kw?: string) {
    setNodeLoading(true);
    try {
      setNodeOptions(await lineageNodes(kw || undefined, 50));
    } catch {
      // 候选节点加载失败不阻断查询（仍可手动输入节点）
    } finally {
      setNodeLoading(false);
    }
  }

  // 进入 Tab 即预加载候选节点
  useEffect(() => {
    void loadNodes();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function loadImpact() {
    if (!node.trim()) {
      message.warning("请输入节点（指标编码或表名）");
      return;
    }
    setLoading(true);
    try {
      const data =
        direction === "downstream"
          ? await lineageImpact({ node: node.trim(), direction, max_hops: 5 })
          : await lineageEdges({ node: node.trim(), direction });
      const items = Array.isArray(data.items) ? data.items : (data as unknown as LineageEdge[]);
      setEdges(items);
      setTotal(data.total ?? items.length);
      // 构建血缘视图（节点/边），供力导向图展示；合并后端节点元数据（entity_id/域/PII）
      setGraphData(items.length > 0 ? edgesToGraphData(items, data.nodes) : null);
      track("lineage_query", node.trim(), "node");
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "查询失败");
      setEdges([]);
      setTotal(0);
      setGraphData(null);
    } finally {
      setLoading(false);
    }
  }

  async function previewImpact() {
    // Select 选择值带 metric: 前缀，预览接口期望裸指标编码，去前缀规范化
    const code = node.trim().replace(/^metric:/, "");
    if (!code) {
      message.warning("请输入指标编码");
      return;
    }
    setLoading(true);
    try {
      const p = await lineageImpactPreview(code, "schema_drift");
      setRisk(
        `受影响指标 ${p.affected_metrics.length} · 物理表 ${p.affected_tables.length} · 消费方 ${p.affected_consumers.length} · 风险等级 ${RISK_LEVEL_LABEL[p.risk_level] ?? p.risk_level}`,
      );
      track("lineage_preview", node.trim(), "node");
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "预览失败");
    } finally {
      setLoading(false);
    }
  }

  /** 表/视图详情侧边栏：按 entity_id 拉取目录详情（与血缘图谱交互一致）。 */
  async function openTableDetail(entityId: number) {
    setDetailOpen(true);
    setDetailLoading(true);
    setDetail(null);
    try {
      setDetail(await getCatalogDetail(entityId));
      track("lineage_table_detail", String(entityId), "table");
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载表详情失败");
      setDetailOpen(false);
    } finally {
      setDetailLoading(false);
    }
  }

  /** 点击图谱节点 → 侧边栏展示具体信息（指标详情 / 表详情 / 字段信息）。 */
  function handleNodeClick(clicked: AssetGraphNode) {
    if (clicked.type === "metric") {
      // 侧边栏展示指标详情，不跳转页面（保留「前往完整详情」入口）
      setMetricCode(clicked.id.replace(/^metric:/, ""));
      setMetricDrawerOpen(true);
    } else if (clicked.type === "table") {
      if (clicked.entity_id != null) {
        void openTableDetail(clicked.entity_id);
      } else {
        // 血缘边引用但未在目录中（可能尚未采集）：仅提示，边明细仍可查
        message.info(`「${clicked.label}」未在元数据目录中（可能尚未采集），仅展示血缘关系`);
      }
    } else if (clicked.type === "field") {
      // field:{table}.{col} → 推导所属表节点，提供表详情入口
      const tableId = `table:${clicked.id.slice("field:".length).split(".").slice(0, -1).join(".")}`;
      const tableNode = graphData?.nodes.find((n) => n.id === tableId) ?? null;
      setFieldNode(clicked);
      setFieldTableNode(tableNode);
    } else if (clicked.type === "dimension" || clicked.type === "consumer") {
      // 影响图中维度/消费方节点：引导去血缘图谱查看详情/手动登记
      message.info(
        `「${clicked.label}」是${clicked.type === "dimension" ? "维度" : "消费方"}节点，详情与手动登记血缘请在「血缘图谱」Tab 中点击该节点操作`,
      );
    } else {
      message.info(`节点「${clicked.label}」暂不支持查看详情`);
    }
  }

  const columns = [
    { title: "源", dataIndex: "source_node", key: "source", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
    { title: "目标", dataIndex: "target_node", key: "target", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
    { title: "类型", dataIndex: "edge_type", key: "type", render: (v: string) => <Tag>{EDGE_TYPE_LABEL[v] ?? v}</Tag> },
    { title: "粒度", dataIndex: "granularity", key: "granularity", width: 100, render: (v: string) => enumLabel(GRANULARITY_LABEL, v) },
    { title: "来源", dataIndex: "provenance", key: "provenance", width: 110, render: (v: string) => <Tag color="blue">{CHANNEL_LABEL[v] ?? v}</Tag> },
    { title: "置信度", dataIndex: "confidence", key: "confidence", width: 90, render: (v: number) => `${(v * 100).toFixed(0)}%` },
    { title: "PII", dataIndex: "pii_inherited", key: "pii", width: 70, render: (v?: boolean) => (v ? <Tag color="red">PII</Tag> : null) },
  ];

  // 搜索词非空且候选里无完全匹配时，兜底提供「使用输入值」选项（支持自由指定节点）
  const hasExact = nodeOptions.some((n) => n.id === searchWord);
  const customOption: LineageNode[] =
    searchWord.trim() && !hasExact
      ? [{ id: searchWord.trim(), label: `使用「${searchWord.trim()}」`, count: 0, type: "other" }]
      : [];

  return (
    <div>
      <Space style={{ marginBottom: 16 }} wrap>
        <Select
          showSearch
          allowClear
          loading={nodeLoading}
          value={node || undefined}
          placeholder="选择或搜索节点（表 / 指标 / 字段）"
          style={{ width: 360 }}
          className="mono"
          filterOption={(input, opt) => {
            const raw = String(opt?.value ?? "").toLowerCase();
            const label = String(opt?.label ?? "").toLowerCase();
            return raw.includes(input.toLowerCase()) || label.includes(input.toLowerCase());
          }}
          onSearch={(v) => {
            setSearchWord(v);
            // 输入关键词 → 远程搜索候选节点
            void loadNodes(v);
          }}
          onChange={(v) => setNode(v ?? "")}
          onKeyDown={(e) => {
            if (e.key === "Enter") void loadImpact();
          }}
          options={[...nodeOptions, ...customOption].map((n) => ({
            value: n.id,
            label: (
              <span>
                <Tag style={{ marginRight: 6 }}>{NODE_TYPE_LABEL[n.type] ?? n.type}</Tag>
                <span>{n.label}</span>
                <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>{n.count} 边</span>
              </span>
            ),
          }))}
        />
        <Select
          value={direction}
          onChange={(v) => setDirection(v)}
          style={{ width: 140 }}
          options={[
            { value: "downstream", label: "下游影响" },
            { value: "upstream", label: "上游来源" },
            { value: "both", label: "双向" },
          ]}
        />
        <Button type="primary" onClick={loadImpact} loading={loading}>
          查询
        </Button>
        <Button onClick={previewImpact} loading={loading}>
          变更影响预览
        </Button>
      </Space>

      {risk && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="变更影响预览（what-if）"
          description={risk}
        />
      )}

      {graphData && graphData.nodes.length > 0 && (
        <Card
          size="small"
          title={`血缘视图 · ${node} 的${
            direction === "upstream" ? "上游来源" : direction === "downstream" ? "下游影响" : "双向关系"
          }（${graphData.nodes.length} 节点 · ${graphData.edges.length} 条边）`}
          style={{ marginBottom: 16 }}
        >
          <AssetGraph
            nodes={graphData.nodes}
            edges={graphData.edges}
            height={420}
            onNodeClick={handleNodeClick}
            lanes
          />
        </Card>
      )}

      {edges.length > 0 ? (
        <Table
          dataSource={edges}
          columns={columns}
          rowKey="id"
          pagination={false}
          size="small"
          footer={() => `共 ${total} 条血缘边`}
          onRow={(edge: LineageEdge) => ({
            onClick: () => {
              setEdgeDetailId(edge.id);
              setEdgeDetailOpen(true);
            },
            style: { cursor: "pointer" },
          })}
        />
      ) : (
        !loading && (
          <p className="muted" style={{ textAlign: "center", padding: 24 }}>
            输入节点后查询血缘关系
          </p>
        )
      )}

      {/* 表/视图详情侧边栏：点击影响分析图中的表节点打开（不跳转页面） */}
      <TableDetailDrawer
        detail={detail}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        loading={detailLoading}
      />

      {/* 指标节点详情侧边栏：点击影响分析图中的指标节点打开（不跳转页面） */}
      <MetricDetailDrawer
        open={metricDrawerOpen}
        metricCode={metricCode}
        onClose={() => setMetricDrawerOpen(false)}
      />

      {/* 字段节点信息：field 无独立详情表，展示字段名/所属表，可跳转所属表详情 */}
      <Drawer
        title="字段信息"
        width={480}
        open={fieldNode != null}
        onClose={() => setFieldNode(null)}
      >
        {fieldNode && (
          <>
            <Descriptions column={2} bordered size="small">
              <Descriptions.Item label="字段名">{fieldNode.label}</Descriptions.Item>
              <Descriptions.Item label="类型">字段</Descriptions.Item>
              <Descriptions.Item label="所属表">
                {fieldTableNode?.label ?? <span className="muted">不在当前视图</span>}
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
                  const eid = fieldTableNode.entity_id as number;
                  setFieldNode(null);
                  void openTableDetail(eid);
                }}
              >
                查看所属表详情
              </Button>
            )}
          </>
        )}
      </Drawer>

      {/* 边详情抽屉（Task B）：点击边明细表格某行打开（边元数据 + 变更历史 + 分层着色） */}
      <EdgeDetailDrawer
        edgeId={edgeDetailId}
        open={edgeDetailOpen}
        onClose={() => setEdgeDetailOpen(false)}
        onDeleted={() => void loadImpact()}
      />
    </div>
  );
}

function ParseTab() {
  const [sql, setSql] = useState("");
  const [dialect, setDialect] = useState("mysql");
  const [targetTable, setTargetTable] = useState("");
  const [result, setResult] = useState<ParseLineageResult | null>(null);
  const [loading, setLoading] = useState(false);
  const { track } = useTracking();
  const { can } = usePermission();

  async function handleParse() {
    if (!sql.trim()) {
      message.warning("请输入 SQL");
      return;
    }
    setLoading(true);
    try {
      const res = await parseLineage(sql, dialect, targetTable);
      setResult(res);
      message.success("血缘解析完成");
      track("lineage_parse", undefined, "sql", { dialect });
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "解析失败");
    } finally {
      setLoading(false);
    }
  }

  const tableColumns = [
    { title: "源表", dataIndex: "source", key: "source", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
    { title: "目标表", dataIndex: "target", key: "target", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
  ];
  const fieldColumns = [
    { title: "源字段", dataIndex: "source", key: "source", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
    { title: "目标字段", dataIndex: "target", key: "target", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
    {
      title: "派生表达式",
      dataIndex: "expression",
      key: "expression",
      render: (v: string | null) => (v ? <span className="mono" style={{ fontSize: 12, color: "#8c8c8c" }}>{v}</span> : <span className="muted">—</span>),
    },
  ];

  // 纯查询无落点：展示「上游依赖」而非空态（方案 B：只读展示读取的表/字段，不写图谱）
  const showUpstream = result !== null && result.upstream_deps != null;
  // 真正无血缘（SQL 非法/无 FROM 等，且无落点信息）才展示空态
  const showEmpty =
    result !== null &&
    !showUpstream &&
    result.table_lineage.length === 0 &&
    result.field_lineage.length === 0;
  // 本次解析血缘图谱：表级/字段级边合并为一张图（有边才展示）
  const resultGraph =
    result !== null && (result.table_lineage.length > 0 || result.field_lineage.length > 0)
      ? parseResultToGraphData(result)
      : null;
  // 纯 SELECT 无落点：上游依赖也画成图谱（中心「本次查询」+ 源表/字段，仅展示不写图谱）
  const upstreamGraph =
    showUpstream && result?.upstream_deps ? upstreamDepsToGraphData(result.upstream_deps) : null;

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Select
          value={dialect}
          onChange={setDialect}
          style={{ width: 160 }}
          options={SQL_DIALECT_OPTIONS}
        />
        <Input
          allowClear
          value={targetTable}
          onChange={(e) => setTargetTable(e.target.value)}
          placeholder="目标表名（可选）：纯 SELECT 指定落点生成血缘"
          style={{ width: 300 }}
          className="mono"
        />
        <Button type="primary" icon={<CodeOutlined />} onClick={handleParse} loading={loading} disabled={!can("lineage:write")}>
          解析血缘
        </Button>
      </Space>
      <Input.TextArea
        rows={10}
        className="mono"
        value={sql}
        onChange={(e) => setSql(e.target.value)}
        placeholder="-- 粘贴 SQL，解析表级/字段级血缘并写入图谱。纯 SELECT 可在左侧填写「目标表名」，把查询结果落成正式血缘。&#10;SELECT order_id, user_id, amount FROM dwd_finance_order WHERE dt = '2026-08-01'"
        style={{ fontSize: 13 }}
      />
      {result && (
        <Alert
          type="success"
          showIcon
          style={{ marginTop: 12 }}
          message="解析结果"
          description={`表级边 ${result.table_edges} · 字段级边 ${result.field_edges} · 图谱写入 ${result.graph_written ? "成功" : "未写入"}`}
        />
      )}
      {resultGraph && (
        <Card
          size="small"
          title={`本次解析 · 血缘图谱（${resultGraph.nodes.length} 节点 · ${resultGraph.edges.length} 条边）`}
          style={{ marginTop: 16 }}
        >
          <AssetGraph nodes={resultGraph.nodes} edges={resultGraph.edges} height={420} lanes />
        </Card>
      )}
      {result && result.table_lineage.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <h4 style={{ marginBottom: 8 }}>本次解析 · 表级血缘（{result.table_lineage.length}）</h4>
          <Table
            size="small"
            rowKey={(r) => `${r.source}__${r.target}`}
            dataSource={result.table_lineage}
            columns={tableColumns}
            pagination={false}
          />
        </div>
      )}
      {result && result.field_lineage.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <h4 style={{ marginBottom: 8 }}>本次解析 · 字段级血缘（{result.field_lineage.length}）</h4>
          <Table
            size="small"
            rowKey={(r) => `${r.source}__${r.target}__${r.expression ?? ""}`}
            dataSource={result.field_lineage.map((f) => ({
              source: f.source_column ? `${f.source_table}.${f.source_column}` : `${f.source_table}.*`,
              target: `${f.target_table}.${f.target_column}`,
              expression: f.expression,
            }))}
            columns={fieldColumns}
            pagination={false}
          />
        </div>
      )}
      {showUpstream && result?.upstream_deps && (
        <div style={{ marginTop: 16 }}>
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 8 }}
            message="该查询为纯 SELECT 且未指定落点，未生成血缘边（未写入图谱）"
            description="如需把查询结果落成正式血缘，请在「目标表名」填入结果表名后重新解析。"
          />
          {upstreamGraph && (
            <Card
              size="small"
              title={`本次查询 · 上游依赖图谱（${upstreamGraph.nodes.length} 节点 · ${upstreamGraph.edges.length} 条边）`}
              style={{ marginTop: 16, marginBottom: 16 }}
            >
              <AssetGraph nodes={upstreamGraph.nodes} edges={upstreamGraph.edges} height={360} lanes />
            </Card>
          )}
          <h4 style={{ marginBottom: 8 }}>
            本次查询 · 上游依赖（{result.upstream_deps.tables.length} 表 / {result.upstream_deps.fields.length} 字段）
          </h4>
          <div style={{ marginBottom: 8 }}>
            {result.upstream_deps.tables.map((t) => (
              <Tag key={`t-${t}`} color="blue" style={{ marginBottom: 4 }}>
                <DatabaseOutlined /> {t}
              </Tag>
            ))}
            {result.upstream_deps.tables.length === 0 && <span className="muted">未读取任何表</span>}
          </div>
          <div>
            {result.upstream_deps.fields.map((f) => (
              <Tag key={`f-${f}`} style={{ marginBottom: 4 }}>
                {f}
              </Tag>
            ))}
            {result.upstream_deps.fields.length === 0 && <span className="muted">未读取任何字段</span>}
          </div>
        </div>
      )}
      {showEmpty && (
        <Empty
          style={{ marginTop: 16 }}
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="本次解析未产生血缘边（SQL 无法解析或无可读取源）"
        />
      )}
    </div>
  );
}

const CHANNEL_STATUS_LABEL: Record<string, string> = {
  running: "采集中",
  success: "成功",
  failed: "失败",
};

const STALE_STATUS_COLOR: Record<string, string> = {
  running: "processing",
  success: "success",
  failed: "error",
};

/**
 * 采集运行详情主体：运行元信息 + 按运行类型展示的具体信息。
 * - SQL 解析：解析上下文（方言/落点/资产节点/解析人）+ SQL 原文 + 表级/字段级边明细；
 * - 批量采集：本次新增/更新边明细；
 * - 无详情快照：降级提示（仍展示计数摘要）。
 */
function RunDetailBody({ run }: { run: LineageIngestRun }) {
  const detail = run.detail;
  const isSqlParse = detail?.kind === "sql_parse";
  const isBatch = detail?.kind === "batch";

  const tableLineage = detail?.table_lineage ?? [];
  const fieldLineage = detail?.field_lineage ?? [];
  const addedEdges = detail?.added_edges ?? [];
  const updatedEdges = detail?.updated_edges ?? [];

  const tableColumns = [
    { title: "源表", dataIndex: "source", key: "source", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
    { title: "目标表", dataIndex: "target", key: "target", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
  ];
  const fieldColumns = [
    { title: "源字段", dataIndex: "source", key: "source", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
    { title: "目标字段", dataIndex: "target", key: "target", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
    { title: "派生表达式", dataIndex: "expression", key: "expression", render: (v: string | null) => (v ? <span className="mono" style={{ fontSize: 12, color: "#8c8c8c" }}>{v}</span> : <span className="muted">—</span>) },
  ];
  const edgeColumns = [
    { title: "源节点", dataIndex: "source", key: "source", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
    { title: "目标节点", dataIndex: "target", key: "target", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
  ];

  return (
    <div>
      <Descriptions size="small" column={2} bordered>
        <Descriptions.Item label="来源通道">
          {CHANNEL_LABEL[run.source] ?? run.source}
          <span className="muted mono" style={{ marginLeft: 6, fontSize: 12 }}>{run.source}</span>
        </Descriptions.Item>
        <Descriptions.Item label="运行时间">{formatCnTime(run.run_at)}</Descriptions.Item>
        <Descriptions.Item label="状态">
          <Badge status={STALE_STATUS_COLOR[run.status] as "success" | "processing" | "error"} text={CHANNEL_STATUS_LABEL[run.status] ?? run.status} />
        </Descriptions.Item>
        <Descriptions.Item label="总边数">{run.total_edges}</Descriptions.Item>
        <Descriptions.Item label="新增 / 更新">
          <Tag color="green">+{run.added_count}</Tag>
          <Tag color="blue">~{run.updated_count}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="未再出现 / 新失效 / 恢复">
          {run.missing_count} / {run.stale_flagged_count} / {run.restored_count}
        </Descriptions.Item>
      </Descriptions>

      {run.error && <Alert type="error" style={{ marginTop: 12 }} message="运行失败" description={run.error} />}

      {isSqlParse && (
        <>
          <h4 style={{ marginTop: 16 }}>解析上下文</h4>
          <Descriptions size="small" column={2}>
            <Descriptions.Item label="方言">{detail?.dialect || "默认"}</Descriptions.Item>
            <Descriptions.Item label="目标表（落点）">{detail?.target_table || "—"}</Descriptions.Item>
            <Descriptions.Item label="上游资产节点">{detail?.source_node || "—"}</Descriptions.Item>
            <Descriptions.Item label="解析人">#{detail?.actor_id ?? "—"}</Descriptions.Item>
          </Descriptions>

          {detail?.sql ? (
            <>
              <h4 style={{ marginTop: 16 }}>SQL 原文</h4>
              <pre className="mono" style={{ fontSize: 12, background: "#f5f5f5", padding: 12, borderRadius: 6, maxHeight: 260, overflow: "auto" }}>
                {formatSql(detail.sql)}
              </pre>
            </>
          ) : null}

          {tableLineage.length > 0 && (
            <>
              <h4 style={{ marginTop: 16 }}>本次解析 · 表级边（{tableLineage.length}）</h4>
              <Table size="small" rowKey={(r) => `${r.source}__${r.target}`} dataSource={tableLineage} columns={tableColumns} pagination={false} />
            </>
          )}

          {fieldLineage.length > 0 && (
            <>
              <h4 style={{ marginTop: 16 }}>本次解析 · 字段级边（{fieldLineage.length}）</h4>
              <Table
                size="small"
                rowKey={(r) => `${r.source}__${r.target}__${r.expression ?? ""}`}
                dataSource={fieldLineage.map((f) => ({
                  source: f.source_column ? `${f.source_table}.${f.source_column}` : `${f.source_table}.*`,
                  target: `${f.target_table}.${f.target_column}`,
                  expression: f.expression,
                }))}
                columns={fieldColumns}
                pagination={false}
              />
            </>
          )}

          {tableLineage.length === 0 && fieldLineage.length === 0 && (
            <Empty style={{ marginTop: 16 }} description="该次解析未落成血缘边（纯 SELECT 未指定落点）" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          )}
        </>
      )}

      {isBatch && (
        <>
          {addedEdges.length > 0 && (
            <>
              <h4 style={{ marginTop: 16 }}>新增边（{addedEdges.length}）</h4>
              <Table size="small" rowKey={(r) => `${r.source}__${r.target}`} dataSource={addedEdges.map(([s, t]) => ({ source: s, target: t }))} columns={edgeColumns} pagination={false} />
            </>
          )}
          {updatedEdges.length > 0 && (
            <>
              <h4 style={{ marginTop: 16 }}>更新边（{updatedEdges.length}）</h4>
              <Table size="small" rowKey={(r) => `${r.source}__${r.target}`} dataSource={updatedEdges.map(([s, t]) => ({ source: s, target: t }))} columns={edgeColumns} pagination={false} />
            </>
          )}
          {addedEdges.length === 0 && updatedEdges.length === 0 && (
            <Empty style={{ marginTop: 16 }} description="本次运行无新增/更新边明细（纯统计轮次）" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          )}
        </>
      )}

      {!isSqlParse && !isBatch && (
        <Empty style={{ marginTop: 16 }} description="该运行记录暂无详情快照（历史记录未保存详情）" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      )}
    </div>
  );
}

function ChannelsTab() {
  const [channels, setChannels] = useState<LineageChannel[]>([]);
  const [stale, setStale] = useState<StaleEdge[]>([]);
  const [runs, setRuns] = useState<LineageIngestRun[]>([]);
  const [activeSource, setActiveSource] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // 运行历史行详情（点击行 → 拉取单条运行详情快照 → Drawer 展示具体信息）
  const [detailRun, setDetailRun] = useState<LineageIngestRun | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const { track } = useTracking();
  const { can } = usePermission();

  async function loadChannels() {
    setLoading(true);
    try {
      const [ch, st] = await Promise.all([lineageChannels(), lineageStale()]);
      setChannels(ch);
      setStale(st);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载采集通道失败");
    } finally {
      setLoading(false);
    }
  }

  // 进入 Tab 即加载采集通道总览与失效队列（此前仅点「刷新」才加载，易被误认为通道缺失）
  useEffect(() => {
    void loadChannels();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function loadRuns(source: string) {
    setActiveSource(source);
    try {
      setRuns(await lineageChannelRuns(source));
      track("lineage_channel_runs", source, "source");
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载运行历史失败");
    }
  }

  /** 点击运行历史行：拉取单条运行详情快照（SQL 原文/方言/落点/边明细 或 批量变更边明细）。 */
  async function openRunDetail(run: LineageIngestRun) {
    setDetailRun(run);
    setDetailOpen(true);
    setDetailLoading(true);
    try {
      setDetailRun(await lineageRunDetail(run.id));
      track("lineage_run_detail", String(run.id), "run");
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载运行详情失败");
      setDetailOpen(false);
    } finally {
      setDetailLoading(false);
    }
  }

  async function handleConfirm(edge: StaleEdge) {
    try {
      await confirmStaleEdge(edge.id);
      message.success("已确认失效并删除该血缘边");
      track("lineage_stale_confirm", String(edge.id), "edge");
      await loadChannels();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    }
  }

  async function handleRestore(edge: StaleEdge) {
    try {
      await restoreStaleEdge(edge.id);
      message.success("已恢复该血缘边");
      track("lineage_stale_restore", String(edge.id), "edge");
      await loadChannels();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    }
  }

  const runColumns = [
    { title: "运行时间", dataIndex: "run_at", key: "run_at", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> },
    { title: "状态", dataIndex: "status", key: "status", width: 90, render: (v: string) => <Badge status={STALE_STATUS_COLOR[v] as "success" | "processing" | "error"} text={CHANNEL_STATUS_LABEL[v] ?? v} /> },
    { title: "总边数", dataIndex: "total_edges", key: "total", width: 80 },
    { title: "新增", dataIndex: "added_count", key: "added", width: 70, render: (v: number) => <Tag color="green">+{v}</Tag> },
    { title: "更新", dataIndex: "updated_count", key: "updated", width: 70, render: (v: number) => <Tag color="blue">~{v}</Tag> },
    { title: "未再出现", dataIndex: "missing_count", key: "missing", width: 80 },
    { title: "新失效", dataIndex: "stale_flagged_count", key: "stale", width: 80, render: (v: number) => (v ? <Tag color="orange">{v}</Tag> : 0) },
    { title: "恢复", dataIndex: "restored_count", key: "restored", width: 70, render: (v: number) => (v ? <Tag color="cyan">{v}</Tag> : 0) },
    {
      title: "详情",
      key: "detail",
      width: 70,
      render: (_: unknown, run: LineageIngestRun) => (
        <Button size="small" type="link" onClick={(e) => { e.stopPropagation(); void openRunDetail(run); }}>
          查看
        </Button>
      ),
    },
  ];

  const staleColumns = [
    { title: "源", dataIndex: "source_node", key: "source", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
    { title: "目标", dataIndex: "target_node", key: "target", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
    { title: "来源", dataIndex: "provenance", key: "provenance", width: 110, render: (v: string) => <Tag color="blue">{v}</Tag> },
    { title: "连续未确认", dataIndex: "missing_count", key: "missing", width: 110, render: (v: number) => <Tag color={v >= 3 ? "red" : "orange"}>{v} 轮</Tag> },
    { title: "进入失效", dataIndex: "stale_since", key: "since", width: 160, render: (v?: string) => <span className="mono" style={{ fontSize: 12 }}>{v ? formatCnTime(v) : "—"}</span> },
    {
      title: "操作",
      key: "action",
      width: 160,
      render: (_: unknown, edge: StaleEdge) => (
        <Space>
          <Popconfirm title="确认删除该失效血缘边？" onConfirm={() => handleConfirm(edge)}>
            <Button size="small" danger disabled={!can("lineage:manage-edge")}>确认删除</Button>
          </Popconfirm>
          <Popconfirm title="恢复该血缘边？" onConfirm={() => handleRestore(edge)}>
            <Button size="small" disabled={!can("lineage:manage-edge")}>恢复</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 16 }} wrap>
        <Button icon={<ReloadOutlined />} onClick={loadChannels} loading={loading}>
          刷新
        </Button>
        <span className="muted" style={{ fontSize: 13 }}>
          各来源通道（DP 同步 / SQL 解析 / 数据接口）的采集运行与失效治理。连续多轮未确认的边进入失效队列，由人工处置。
        </span>
      </Space>

      {channels.length === 0 && !loading ? (
        <Empty description="暂无血缘采集通道。运行 scripts/import_dp_lineage.py 或通过 SQL 解析写入血缘。" />
      ) : (
        <Row gutter={[16, 16]}>
          {channels.map((c) => {
            const last = c.last_run;
            return (
              <Col xs={24} sm={12} lg={8} key={c.source}>
                <Card
                  size="small"
                  title={
                    <Space>
                      <DatabaseOutlined />
                      <span>{CHANNEL_LABEL[c.source] ?? c.source}</span>
                      {CHANNEL_LABEL[c.source] && (
                        <span className="muted mono" style={{ fontSize: 12 }}>{c.source}</span>
                      )}
                    </Space>
                  }
                  extra={last ? <Badge status={STALE_STATUS_COLOR[last.status] as "success" | "processing" | "error"} text={CHANNEL_STATUS_LABEL[last.status] ?? last.status} /> : null}
                  onClick={() => loadRuns(c.source)}
                  style={{ cursor: "pointer" }}
                >
                  <Row gutter={8}>
                    <Col span={8}><Statistic title="血缘边" value={c.edge_count} /></Col>
                    <Col span={8}><Statistic title="涉及节点" value={c.node_count} /></Col>
                    <Col span={8}><Statistic title="失效边" value={c.stale_count} valueStyle={{ color: c.stale_count ? "#cf1322" : undefined }} /></Col>
                  </Row>
                  <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
                    {last
                      ? `最近采集 ${last.run_at ? formatCnTime(last.run_at) : "—"} · 新增 +${last.added_count} · 更新 ~${last.updated_count} · 失效 ${last.stale_flagged_count}`
                      : "尚无采集运行记录（点击查看详情）"}
                  </div>
                </Card>
              </Col>
            );
          })}
        </Row>
      )}

      {activeSource && (
        <Card size="small" title={`运行历史 · ${CHANNEL_LABEL[activeSource] ?? activeSource}`} style={{ marginTop: 16 }}>
          <Table
            dataSource={runs}
            columns={runColumns}
            rowKey="id"
            size="small"
            pagination={{ pageSize: 10, showSizeChanger: false }}
            onRow={(run: LineageIngestRun) => ({
              onClick: () => void openRunDetail(run),
              style: { cursor: "pointer" },
            })}
          />
        </Card>
      )}

      <Card size="small" title={<Space><SyncOutlined />失效队列（{stale.length}）</Space>} style={{ marginTop: 16 }}>
        {stale.length === 0 ? (
          <Empty description="暂无失效血缘边" />
        ) : (
          <Table
            dataSource={stale}
            columns={staleColumns}
            rowKey="id"
            size="small"
            pagination={{ pageSize: 10, showSizeChanger: false }}
          />
        )}
      </Card>

      <Drawer
        title={detailRun ? `运行详情 · ${CHANNEL_LABEL[detailRun.source] ?? detailRun.source}` : "运行详情"}
        width={720}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        loading={detailLoading}
      >
        {detailRun && <RunDetailBody run={detailRun} />}
      </Drawer>
    </div>
  );
}

/** 血缘边详情抽屉（Task B）：单条边元数据 + 变更历史。
 *  点击血缘查询结果的某条边打开；provenance 映射中文、节点按前缀分层着色（Task D）。 */
function EdgeDetailDrawer({
  edgeId,
  open,
  onClose,
  onDeleted,
}: {
  edgeId: number | null;
  open: boolean;
  onClose: () => void;
  onDeleted?: () => void;
}) {
  const [detail, setDetail] = useState<LineageEdgeDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const { track } = useTracking();
  const { can } = usePermission();

  useEffect(() => {
    if (!open || edgeId == null) {
      if (!open) setDetail(null);
      return;
    }
    setLoading(true);
    setDetail(null);
    fetchLineageEdgeDetail(edgeId)
      .then((d) => {
        setDetail(d);
        track("lineage_edge_detail", String(edgeId), "edge");
      })
      .catch((err) => {
        message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载边详情失败");
        setDetail(null);
      })
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, edgeId]);

  const historyColumns = [
    { title: "变更前", dataIndex: "before_value", key: "before", render: (v?: string) => (v ? <span className="mono" style={{ fontSize: 12 }}>{v}</span> : <span className="muted">—</span>) },
    { title: "变更原因", dataIndex: "change_reason", key: "reason", render: (v?: string) => (v ? <Tag>{v}</Tag> : <span className="muted">—</span>) },
    { title: "变更时间", dataIndex: "changed_at", key: "changedAt", render: (v?: string) => <span className="mono" style={{ fontSize: 12 }}>{v ? formatCnTime(v) : "—"}</span> },
  ];

  async function handleDelete() {
    if (edgeId == null) return;
    setDeleting(true);
    try {
      await deleteLineageEdge(edgeId);
      message.success("该血缘边已删除（仅删除此边，不影响关联节点）");
      onClose();
      onDeleted?.();
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "删除血缘边失败",
      );
    } finally {
      setDeleting(false);
    }
  }

  return (
    <Drawer
      title="血缘边详情"
      width={640}
      open={open}
      footer={
        detail ? (
          <div style={{ textAlign: "right" }}>
            <Popconfirm
              title="删除此血缘边"
              description={`确定删除「${detail.source_node} → ${detail.target_node}」？仅删除这条边，不影响关联节点。`}
              okText="删除"
              okButtonProps={{ danger: true }}
              onConfirm={handleDelete}
            >
              <Button danger loading={deleting} disabled={!can("lineage:manage-edge")}>删除此边</Button>
            </Popconfirm>
          </div>
        ) : null
      }
      onClose={onClose}
      loading={loading}
    >
      {detail && (
        <div>
          <Descriptions size="small" column={2} bordered>
            <Descriptions.Item label="源节点" span={2}>
              <Space size={6} wrap>
                {nodeTypeTag(detail.source_node)}
                <span className="mono" style={{ fontSize: 12 }}>{detail.source_node}</span>
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label="目标节点" span={2}>
              <Space size={6} wrap>
                {nodeTypeTag(detail.target_node)}
                <span className="mono" style={{ fontSize: 12 }}>{detail.target_node}</span>
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label="边类型">{EDGE_TYPE_LABEL[detail.edge_type] ?? detail.edge_type}</Descriptions.Item>
            <Descriptions.Item label="粒度">{enumLabel(GRANULARITY_LABEL, detail.granularity)}</Descriptions.Item>
            <Descriptions.Item label="置信度">
              {detail.confidence != null ? `${(detail.confidence * 100).toFixed(0)}%` : "—"}
            </Descriptions.Item>
            <Descriptions.Item label="来源">
              <Tag color="blue">{EDGE_PROVENANCE_LABEL[detail.provenance ?? ""] ?? detail.provenance ?? "—"}</Tag>
            </Descriptions.Item>
            {detail.pii_inherited ? (
              <Descriptions.Item label="PII"><Tag color="red">继承 PII</Tag></Descriptions.Item>
            ) : null}
            <Descriptions.Item label="创建时间">{detail.created_at ? formatCnTime(detail.created_at) : "—"}</Descriptions.Item>
          </Descriptions>

          <h4 style={{ marginTop: 16 }}>变更历史（{detail.history.length}）</h4>
          {detail.history.length > 0 ? (
            <Table
              size="small"
              rowKey={(r) => `${r.id ?? r.source_node ?? ""}__${r.changed_at ?? ""}`}
              dataSource={detail.history}
              columns={historyColumns}
              pagination={false}
            />
          ) : (
            <Empty style={{ marginTop: 8 }} image={Empty.PRESENTED_IMAGE_SIMPLE} description="该边暂无变更历史" />
          )}
        </div>
      )}
    </Drawer>
  );
}

/** 血缘覆盖率治理 Tab（Task A，P0）：覆盖率统计卡 + 孤立指标 / 断链边明细。 */
function CoverageTab() {
  const [coverage, setCoverage] = useState<LineageCoverage | null>(null);
  const [orphans, setOrphans] = useState<CoverageOrphanItem[]>([]);
  const [broken, setBroken] = useState<CoverageBrokenEdgeItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [activeList, setActiveList] = useState<"orphans" | "broken">("orphans");
  const navigate = useNavigate();
  const { track } = useTracking();
  const { can } = usePermission();
  // 断链边人工修复：以断链 source 节点为目标打开手动登记弹窗
  const [repairOpen, setRepairOpen] = useState(false);
  const [repairNode, setRepairNode] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    try {
      const [cov, o, b] = await Promise.all([
        fetchLineageCoverage(),
        fetchLineageOrphans(),
        fetchLineageBrokenEdges(50),
      ]);
      setCoverage(cov);
      setOrphans(o.items);
      setBroken(b.items);
      track("lineage_coverage_view");
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载覆盖率失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const orphanColumns = [
    {
      title: "指标编码",
      dataIndex: "metric_code",
      key: "code",
      render: (v: string) => (
        <Button type="link" style={{ padding: 0 }} onClick={() => navigate(`/detail/${encodeURIComponent(v)}`)}>
          <span className="mono" style={{ fontSize: 12 }}>{v}</span>
        </Button>
      ),
    },
    { title: "名称", dataIndex: "name", key: "name", render: (v?: string) => v || <span className="muted">—</span> },
    { title: "业务域", dataIndex: "domain", key: "domain", render: (v?: string | null) => v || <span className="muted">—</span> },
    { title: "状态", dataIndex: "status", key: "status", render: (v?: string) => v || <span className="muted">—</span> },
  ];

  const brokenColumns = [
    { title: "源", dataIndex: "source_node", key: "source", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
    { title: "目标", dataIndex: "target_node", key: "target", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
    { title: "类型", dataIndex: "edge_type", key: "type", render: (v: string) => <Tag>{EDGE_TYPE_LABEL[v] ?? v}</Tag> },
    { title: "来源", dataIndex: "provenance", key: "provenance", render: (v: string) => <Tag color="blue">{CHANNEL_LABEL[v] ?? v}</Tag> },
    {
      title: "操作",
      key: "action",
      width: 90,
      render: (_: unknown, r: CoverageBrokenEdgeItem) => (
        <Button
          type="link"
          size="small"
          style={{ padding: 0 }}
          icon={<PlusOutlined />}
          disabled={!can("lineage:write")}
          onClick={() => {
            setRepairNode(r.source_node);
            setRepairOpen(true);
          }}
        >
          修复
        </Button>
      ),
    },
  ];

  const emptyBoth = orphans.length === 0 && broken.length === 0;

  return (
    <div>
      <Space style={{ marginBottom: 16 }} wrap>
        <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
          刷新
        </Button>
        <span className="muted" style={{ fontSize: 13 }}>
          血缘全覆盖治理：有多少指标/表接入了血缘、多少孤立，以及断链的边（人工修复入口）。
        </span>
      </Space>

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={12} lg={6}>
          <Card size="small"><Statistic title="指标总数" value={coverage?.metric_total ?? "—"} /></Card>
        </Col>
        <Col xs={12} lg={6}>
          <Card size="small"><Statistic title="有血缘指标" value={coverage?.metric_with_lineage ?? "—"} /></Card>
        </Col>
        <Col xs={12} lg={6}>
          <Card size="small" style={{ cursor: "pointer", border: orphans.length ? "1px solid #faad14" : undefined }} onClick={() => setActiveList("orphans")}>
            <Statistic title="孤立指标" value={coverage?.metric_orphan ?? "—"} valueStyle={{ color: coverage?.metric_orphan ? "#faad14" : "#52c41a" }} />
          </Card>
        </Col>
        <Col xs={12} lg={6}>
          <Card size="small" style={{ cursor: "pointer", border: broken.length ? "1px solid #ff4d4f" : undefined }} onClick={() => setActiveList("broken")}>
            <Statistic title="断链边" value={coverage?.broken_edges ?? "—"} valueStyle={{ color: coverage?.broken_edges ? "#cf1322" : "#52c41a" }} />
          </Card>
        </Col>
      </Row>

      {emptyBoth ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="暂无孤立指标/断链，血缘覆盖良好"
        />
      ) : (
        <Card
          size="small"
          title={
            <Space size="middle">
              <span>治理明细</span>
              <Segmented
                value={activeList}
                onChange={(k) => setActiveList(k as "orphans" | "broken")}
                options={[
                  { value: "orphans", label: `孤立指标（${orphans.length}）` },
                  { value: "broken", label: `断链边（${broken.length}）` },
                ]}
              />
            </Space>
          }
        >
          {activeList === "orphans" ? (
            orphans.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无孤立指标，血缘覆盖良好" />
            ) : (
              <Table
                rowKey={(r) => r.metric_code}
                dataSource={orphans}
                columns={orphanColumns}
                size="small"
                pagination={false}
              />
            )
          ) : broken.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无断链边，血缘覆盖良好" />
          ) : (
            <Table
              rowKey="id"
              dataSource={broken}
              columns={brokenColumns}
              size="small"
              pagination={{ pageSize: 50, showSizeChanger: false }}
            />
          )}
        </Card>
      )}
      {repairNode && (
        <ManualEdgeModal
          open={repairOpen}
          onClose={() => setRepairOpen(false)}
          baseNode={repairNode}
          baseLabel="断链节点"
          defaultDirection="downstream"
          onSuccess={() => void load()}
        />
      )}
    </div>
  );
}

export function LineageView() {
  const navigate = useNavigate();

  // 统一返回上一入口：优先回退浏览器历史（资产地图等入口），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  const tabItems = [
    { key: "graph", label: <span><ShareAltOutlined /> 血缘图谱</span>, children: <GraphTab /> },
    { key: "impact", label: <span><ApartmentOutlined /> 血缘查询 / 影响分析</span>, children: <ImpactTab /> },
    { key: "parse", label: <span><CodeOutlined /> SQL 血缘解析</span>, children: <ParseTab /> },
    { key: "channels", label: <span><DatabaseOutlined /> 采集通道</span>, children: <ChannelsTab /> },
    { key: "coverage", label: <span><PieChartOutlined /> 覆盖治理</span>, children: <CoverageTab /> },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
            返回
          </Button>
          <div className="page-kicker">Lineage / Impact</div>
          <h2>血缘视图</h2>
          <p>血缘图谱总览、上下游血缘查询、what-if 变更影响预览、SQL 血缘解析入库、采集通道增量运维。</p>
        </div>
      </div>
      <Card styles={{ body: { paddingTop: 16 } }}>
        <Tabs items={tabItems} />
      </Card>
    </div>
  );
}
