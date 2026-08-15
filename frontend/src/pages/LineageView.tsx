import { useEffect, useState } from "react";
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
  CodeOutlined,
  DatabaseOutlined,
  ReloadOutlined,
  ShareAltOutlined,
  SyncOutlined,
  ArrowLeftOutlined,
} from "@ant-design/icons";
import {
  confirmStaleEdge,
  getCatalogDetail,
  lineageChannelRuns,
  lineageChannels,
  lineageEdges,
  lineageGraph,
  lineageImpact,
  lineageImpactPreview,
  lineageNodes,
  lineageStale,
  parseLineage,
  restoreStaleEdge,
  UnisenseApiError,
} from "../api";
import type {
  DBCatalog,
  LineageChannel,
  LineageEdge,
  LineageIngestRun,
  LineageNode,
  ParseLineageResult,
  StaleEdge,
} from "../types";
import { AssetGraph, AssetGraphNode, AssetGraphEdge } from "../components/assetmap/AssetGraph";
import { MetricDetailDrawer } from "../components/assetmap/MetricDetailDrawer";
import { useTracking } from "../hooks/useTracking";
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

/** 血缘候选节点类型标签（影响分析选项框下拉分组）。 */
const NODE_TYPE_LABEL: Record<string, string> = {
  table: "表",
  metric: "指标",
  field: "字段",
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

/** 血缘图谱 Tab：进入即加载血缘图谱。指标/表节点点击均在本页以侧边栏
 *  展示详情（指标详情抽屉 / 表详情抽屉），不跳转页面，用户可再决定是否前往完整页面。
 *  支持 URL ``?node=xxx`` 聚焦：从指标详情「在图谱中查看」跳转时，仅展示该节点
 *  上下游子图（BFS 展开 3 跳），而非全量血缘节点。 */
function GraphTab() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [data, setData] = useState<{ nodes: AssetGraphNode[]; edges: AssetGraphEdge[] } | null>(null);
  const [loading, setLoading] = useState(false);
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
  const { track } = useTracking();

  async function load() {
    setLoading(true);
    setFocusMiss(false);
    try {
      const d = await lineageGraph({ limit: 2000 });
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
      setData({ nodes, edges });
      track("lineage_graph_view");
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载血缘图谱失败");
      setData(null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusNode]);

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
    }
  }

  function goToCatalog() {
    if (!detail) return;
    navigate(`/catalog?kw=${encodeURIComponent(detail.entity_name)}`);
  }

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

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
          刷新
        </Button>
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

      <Drawer
        title={detail ? `${detail.entity_type === "VIEW" ? "视图" : "表"} · ${detail.entity_name}` : "表详情"}
        width={680}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        loading={detailLoading}
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
          </div>
        )}
      </Drawer>

      {/* 指标节点详情侧边栏：点击血缘图谱中的指标节点打开（不跳转页面） */}
      <MetricDetailDrawer
        open={metricDrawerOpen}
        metricCode={metricCode}
        onClose={() => setMetricDrawerOpen(false)}
      />
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
      setEdges(data.items ?? data);
      setTotal(data.total ?? (Array.isArray(data) ? data.length : 0));
      track("lineage_query", node.trim(), "node");
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "查询失败");
      setEdges([]);
      setTotal(0);
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

      {edges.length > 0 ? (
        <Table
          dataSource={edges}
          columns={columns}
          rowKey="id"
          pagination={false}
          size="small"
          footer={() => `共 ${total} 条血缘边`}
        />
      ) : (
        !loading && (
          <p className="muted" style={{ textAlign: "center", padding: 24 }}>
            输入节点后查询血缘关系
          </p>
        )
      )}
    </div>
  );
}

function ParseTab() {
  const [sql, setSql] = useState("");
  const [dialect, setDialect] = useState("mysql");
  const [result, setResult] = useState<ParseLineageResult | null>(null);
  const [loading, setLoading] = useState(false);
  const { track } = useTracking();

  async function handleParse() {
    if (!sql.trim()) {
      message.warning("请输入 SQL");
      return;
    }
    setLoading(true);
    try {
      const res = await parseLineage(sql, dialect);
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

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Select
          value={dialect}
          onChange={setDialect}
          style={{ width: 160 }}
          options={SQL_DIALECT_OPTIONS}
        />
        <Button type="primary" icon={<CodeOutlined />} onClick={handleParse} loading={loading}>
          解析血缘
        </Button>
      </Space>
      <Input.TextArea
        rows={10}
        className="mono"
        value={sql}
        onChange={(e) => setSql(e.target.value)}
        placeholder="-- 粘贴 SQL，解析表级/字段级血缘并写入图谱&#10;SELECT order_id, user_id, amount FROM dwd_finance_order WHERE dt = '2026-08-01'"
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
      {result && result.table_lineage.length === 0 && result.field_lineage.length === 0 && (
        <Empty
          style={{ marginTop: 16 }}
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="本次解析未产生血缘边（SQL 为纯查询或无写入目标）"
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

function ChannelsTab() {
  const [channels, setChannels] = useState<LineageChannel[]>([]);
  const [stale, setStale] = useState<StaleEdge[]>([]);
  const [runs, setRuns] = useState<LineageIngestRun[]>([]);
  const [activeSource, setActiveSource] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const { track } = useTracking();

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
            <Button size="small" danger>确认删除</Button>
          </Popconfirm>
          <Popconfirm title="恢复该血缘边？" onConfirm={() => handleRestore(edge)}>
            <Button size="small">恢复</Button>
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
        <Card size="small" title={`运行历史 · ${activeSource}`} style={{ marginTop: 16 }}>
          <Table
            dataSource={runs}
            columns={runColumns}
            rowKey="id"
            size="small"
            pagination={{ pageSize: 10, showSizeChanger: false }}
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
