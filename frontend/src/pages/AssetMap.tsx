import { useEffect, useState } from "react";
import { Card, Spin, Alert, Select, Switch, Tag, Row, Col, Statistic, Table, Empty, Tabs } from "antd";
import { ApartmentOutlined, HeatMapOutlined, UserOutlined, GlobalOutlined, DeleteOutlined, TableOutlined } from "@ant-design/icons";
import { Pie } from "@ant-design/charts";
import {
  fetchAssetGraph,
  fetchAssetHeatmap,
  fetchAssetOwnerView,
  fetchAssetSummary,
  fetchAssetClassification,
  fetchAssetMetricSummary,
  fetchAssetTables,
  fetchAssetOrphans,
} from "../api";
import type { AssetCatalogSummary, AssetClassificationSummary, AssetMetricSummary, AssetTableItem } from "../types";
import { useTracking } from "../hooks/useTracking";

interface GraphNode {
  id: string;
  type: string;
  label: string;
  pii?: boolean;
  domain?: string;
  owner?: string;
}
interface GraphEdge {
  source: string;
  target: string;
  type: string;
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

function normalizeBuckets(buckets: Array<Record<string, unknown>>) {
  return buckets.map((b) => ({
    key: String(b.key ?? "未知"),
    count: Number(b.count ?? b.total ?? 0),
    pii_count: Number(b.pii_count ?? 0),
    has_pii: "pii_count" in b,
  }));
}

function OverviewTab() {
  const [summary, setSummary] = useState<AssetCatalogSummary | null>(null);
  const [classification, setClassification] = useState<AssetClassificationSummary | null>(null);
  const [metricSummary, setMetricSummary] = useState<AssetMetricSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const [s, c, m] = await Promise.all([
          fetchAssetSummary(),
          fetchAssetClassification(),
          fetchAssetMetricSummary(),
        ]);
        setSummary(s);
        setClassification(c);
        setMetricSummary(m);
      } catch (err) {
        setError(err instanceof Error ? err.message : "加载资产概览失败");
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  if (loading) return <Spin />;
  if (error) return <Alert type="error" message={error} />;
  if (!summary || !metricSummary) return <Empty description="暂无资产数据" />;

  const sensData = Object.entries(classification?.by_sensitivity ?? {}).map(([k, v]) => ({
    type: SENSITIVITY_LABEL[k] ?? k,
    value: v,
  }));

  return (
    <div>
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={12} md={6}>
          <Statistic title="目录资产总数" value={summary.total} />
        </Col>
        <Col xs={12} md={6}>
          <Statistic title="指标总数" value={(metricSummary.by_domain ? Object.values(metricSummary.by_domain).reduce((a, b) => a + b, 0) : 0)} />
        </Col>
        <Col xs={12} md={6}>
          <Statistic title="孤儿资产" value={summary.orphan_assets} valueStyle={{ color: summary.orphan_assets > 0 ? "#d64545" : undefined }} />
        </Col>
        <Col xs={12} md={6}>
          <Statistic title="已发布指标" value={metricSummary.by_status?.PUBLISHED ?? 0} valueStyle={{ color: "#2e9e5b" }} />
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <Card title="目录实体类型" size="small">
            <Row gutter={[8, 8]}>
              {Object.entries(summary.by_entity_type ?? {}).map(([k, v]) => (
                <Col span={8} key={k}>
                  <Statistic title={k} value={v} />
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
              />
            )}
          </Card>
        </Col>
      </Row>
    </div>
  );
}

function GraphTab() {
  const [graphData, setGraphData] = useState<{ nodes: GraphNode[]; edges: GraphEdge[] } | null>(null);
  const [domain, setDomain] = useState<string | undefined>(undefined);
  const [piiOnly, setPiiOnly] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  if (loading && !graphData) return <Spin tip="加载图谱数据…" />;
  if (error) return <Alert type="error" message={error} />;
  if (!graphData) return <Empty description="暂无图谱数据" />;

  const domainOptions = [...new Set(graphData.nodes.map((n) => n.domain).filter(Boolean))].map((d) => ({
    label: d,
    value: d,
  }));

  return (
    <div>
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }} align="middle">
        <Col>
          <span className="muted">域筛选：</span>
          <Select allowClear placeholder="全部域" style={{ width: 200 }} value={domain} onChange={setDomain} options={domainOptions} />
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

      <Card title="图谱节点" size="small" style={{ marginBottom: 16 }}>
        <Table
          dataSource={graphData.nodes}
          rowKey="id"
          pagination={{ pageSize: 20 }}
          size="small"
          columns={[
            { title: "ID", dataIndex: "id", key: "id", ellipsis: true },
            { title: "类型", dataIndex: "type", key: "type", width: 80 },
            { title: "标签", dataIndex: "label", key: "label", ellipsis: true },
            {
              title: "PII",
              dataIndex: "pii",
              key: "pii",
              width: 70,
              render: (val: boolean) => (val ? <Tag color="red">PII</Tag> : <Tag>普通</Tag>),
            },
            { title: "域", dataIndex: "domain", key: "domain", width: 110 },
          ]}
        />
      </Card>
      <Card title="关联边" size="small">
        <Table
          dataSource={graphData.edges}
          rowKey={(r) => `${r.source}-${r.target}-${r.type}`}
          pagination={{ pageSize: 20 }}
          size="small"
          columns={[
            { title: "源", dataIndex: "source", key: "source", ellipsis: true },
            { title: "目标", dataIndex: "target", key: "target", ellipsis: true },
            { title: "类型", dataIndex: "type", key: "type", width: 160 },
          ]}
        />
      </Card>
    </div>
  );
}

function HeatmapTab() {
  const [dimension, setDimension] = useState("domain");
  const [buckets, setBuckets] = useState<Array<Record<string, unknown>>>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const data = await fetchAssetHeatmap(dimension);
        setBuckets(data.buckets ?? []);
      } catch (err) {
        setError(err instanceof Error ? err.message : "加载热力数据失败");
      } finally {
        setLoading(false);
      }
    }
    load();
  }, [dimension]);

  const rows = normalizeBuckets(buckets);

  return (
    <Card title="敏感分布热力" size="small" extra={
      <Select
        value={dimension}
        onChange={setDimension}
        style={{ width: 160 }}
        options={[
          { value: "domain", label: "按业务域" },
          { value: "sensitivity", label: "按敏感度" },
          { value: "dw_layer", label: "按数仓层" },
          { value: "owner", label: "按责任人" },
        ]}
      />
    }>
      {loading ? (
        <Spin />
      ) : error ? (
        <Alert type="error" message={error} />
      ) : rows.length === 0 ? (
        <Empty description="暂无热力数据" />
      ) : (
        rows.map((r) => {
          const pct = Math.round((r.pii_count / Math.max(r.count, 1)) * 100);
          return (
            <div key={r.key} style={{ marginBottom: 12 }}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 4 }}>
                <span style={{ fontWeight: 600 }}>{r.key}</span>
                <span className="muted">
                  {r.count} 项{r.has_pii && ` · PII ${r.pii_count}（${pct}%）`}
                </span>
              </div>
              <div style={{ height: 10, background: "var(--line-soft)", borderRadius: 5, overflow: "hidden" }}>
                <div
                  style={{
                    height: "100%",
                    width: `${Math.min(100, pct)}%`,
                    background: pct > 40 ? "var(--danger)" : pct > 15 ? "var(--signal)" : "var(--data)",
                    borderRadius: 5,
                    transition: "width 0.3s ease",
                  }}
                />
              </div>
            </div>
          );
        })
      )}
    </Card>
  );
}

function OwnerTab() {
  const [ownerId, setOwnerId] = useState<number | undefined>(undefined);
  const [ownerOptions, setOwnerOptions] = useState<Array<{ label: string; value: number }>>([]);
  const [view, setView] = useState<{ owner_id: number; metrics: { total: number; published: number; draft: number; pii_count: number; by_domain: Record<string, number> }; catalogs: { total: number } } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchAssetGraph({ depth: 1 })
      .then((g) => {
        const owners = [...new Set(g.nodes.map((n) => n.owner).filter(Boolean))].map((o) => Number(o));
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
            { title: "类型", dataIndex: "entity_type", key: "entity_type", width: 90 },
            {
              title: "敏感度",
              dataIndex: "sensitivity_level",
              key: "sensitivity",
              width: 110,
              render: (s: string) => <Tag color={SENSITIVITY_COLOR[s]}>{SENSITIVITY_LABEL[s] ?? s}</Tag>,
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

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetchAssetTables({ sensitivity, limit: 200 })
      .then((r) => setItems(r.items))
      .catch((err) => setError(err instanceof Error ? err.message : "加载数据表失败"))
      .finally(() => setLoading(false));
  }, [sensitivity]);

  return (
    <Card
      title="数据表目录"
      size="small"
      extra={
        <Select
          allowClear
          placeholder="全部敏感度"
          style={{ width: 160 }}
          value={sensitivity}
          onChange={setSensitivity}
          options={Object.keys(SENSITIVITY_LABEL).map((k) => ({ value: k, label: SENSITIVITY_LABEL[k] }))}
        />
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
            { title: "类型", dataIndex: "entity_type", key: "entity_type", width: 90 },
            {
              title: "敏感度",
              dataIndex: "sensitivity_level",
              key: "sensitivity",
              width: 110,
              render: (s: string) => <Tag color={SENSITIVITY_COLOR[s]}>{SENSITIVITY_LABEL[s] ?? s}</Tag>,
            },
            { title: "责任人", dataIndex: "owner_id", key: "owner", width: 90, render: (v: number | null) => v ?? <Tag>无</Tag> },
          ]}
        />
      )}
    </Card>
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
    { key: "graph", label: <span><ApartmentOutlined /> 图谱视图</span>, children: <GraphTab /> },
    { key: "heatmap", label: <span><HeatMapOutlined /> 热力视图</span>, children: <HeatmapTab /> },
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
