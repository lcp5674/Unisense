import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
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
  SearchOutlined,
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
  lineageStale,
  parseLineage,
  restoreStaleEdge,
  UnisenseApiError,
} from "../api";
import type { DBCatalog, LineageChannel, LineageEdge, LineageIngestRun, StaleEdge } from "../types";
import { AssetGraph, AssetGraphNode, AssetGraphEdge } from "../components/assetmap/AssetGraph";
import { useTracking } from "../hooks/useTracking";
import { enumLabel, GRANULARITY_LABEL } from "../utils/enums";
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

const SENSITIVITY_COLOR: Record<string, string> = {
  INTERNAL: "default",
  CONFIDENTIAL: "orange",
  SECRET: "volcano",
  "PII-LOW": "cyan",
  "PII-MEDIUM": "gold",
  "PII-HIGH": "red",
};

type Direction = "upstream" | "downstream" | "both";

/** 血缘图谱 Tab：进入即加载全量血缘图谱（力导向图）。指标节点点击跳详情；
 *  表/视图节点点击在本页 Drawer 展示表详情，由用户决定是否跳转指标目录。 */
function GraphTab() {
  const navigate = useNavigate();
  const [data, setData] = useState<{ nodes: AssetGraphNode[]; edges: AssetGraphEdge[] } | null>(null);
  const [loading, setLoading] = useState(false);
  // 表节点详情抽屉
  const [detail, setDetail] = useState<DBCatalog | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const { track } = useTracking();

  async function load() {
    setLoading(true);
    try {
      const d = await lineageGraph({ limit: 2000 });
      setData({ nodes: d.nodes as AssetGraphNode[], edges: d.edges as AssetGraphEdge[] });
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
  }, []);

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
      const code = node.id.replace(/^metric:/, "");
      navigate(`/detail/${encodeURIComponent(code)}`);
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
        <span className="muted" style={{ fontSize: 13 }}>
          {data ? `共 ${data.nodes.length} 节点 · ${data.edges.length} 条血缘边` : "加载血缘图谱…"}
          ，点击节点：指标 → 指标详情；表/视图 → 本页查看表详情
        </span>
      </Space>
      {data && data.nodes.length > 0 ? (
        <AssetGraph
          nodes={data.nodes}
          edges={data.edges}
          height={560}
          onNodeClick={handleNodeClick}
          // 血缘总览默认隐藏字段节点，聚焦表/指标主干，减少视觉噪声
          showFields={false}
        />
      ) : (
        !loading && (
          <Empty description="暂无血缘图谱数据。可在「SQL 血缘解析」粘贴 SQL 入库，或运行 scripts/import_dp_lineage.py 导入。" />
        )
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
    </div>
  );
}

function ImpactTab() {
  const [node, setNode] = useState("");
  const [direction, setDirection] = useState<Direction>("downstream");
  const [edges, setEdges] = useState<LineageEdge[]>([]);
  const [total, setTotal] = useState(0);
  const [risk, setRisk] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const { track } = useTracking();

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
    if (!node.trim()) {
      message.warning("请输入指标编码");
      return;
    }
    setLoading(true);
    try {
      const p = await lineageImpactPreview(node.trim(), "schema_drift");
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
    { title: "来源", dataIndex: "provenance", key: "provenance", width: 110, render: (v: string) => <Tag color="blue">{v}</Tag> },
    { title: "置信度", dataIndex: "confidence", key: "confidence", width: 90, render: (v: number) => `${(v * 100).toFixed(0)}%` },
    { title: "PII", dataIndex: "pii_inherited", key: "pii", width: 70, render: (v?: boolean) => (v ? <Tag color="red">PII</Tag> : null) },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 16 }} wrap>
        <Input
          placeholder="节点（table:库.表 / metric:编码 / 指标编码）"
          value={node}
          onChange={(e) => setNode(e.target.value)}
          onPressEnter={loadImpact}
          prefix={<SearchOutlined />}
          className="mono"
          style={{ width: 320 }}
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
  const [result, setResult] = useState<{ table_edges: number; field_edges: number; graph_written: boolean } | null>(null);
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

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Select
          value={dialect}
          onChange={setDialect}
          style={{ width: 160 }}
          options={["mysql", "postgres", "hive", "spark", "clickhouse", "duckdb"].map((v) => ({ value: v, label: v }))}
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
    { title: "运行时间", dataIndex: "run_at", key: "run_at", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v?.replace("T", " ").slice(0, 19)}</span> },
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
    { title: "进入失效", dataIndex: "stale_since", key: "since", width: 160, render: (v?: string) => <span className="mono" style={{ fontSize: 12 }}>{v?.replace("T", " ").slice(0, 19)}</span> },
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
                  title={<Space><DatabaseOutlined /><span className="mono">{c.source}</span></Space>}
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
                      ? `最近采集 ${last.run_at?.replace("T", " ").slice(0, 19)} · 新增 +${last.added_count} · 更新 ~${last.updated_count} · 失效 ${last.stale_flagged_count}`
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
