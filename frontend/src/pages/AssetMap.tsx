import { useEffect, useState } from "react";
import { Alert, Button, Card, Descriptions, Drawer, Empty, Row, Col, Input, Select, Space, Spin, Statistic, Switch, Table, Tabs, Tag, message } from "antd";
import { ApartmentOutlined, DeleteOutlined, DownloadOutlined, EyeOutlined, GlobalOutlined, HeartOutlined, HeatMapOutlined, SafetyOutlined, SearchOutlined, TableOutlined, UserOutlined } from "@ant-design/icons";
import { Pie } from "@ant-design/charts";
import {
  downloadAssetExport,
  fetchAssetChanges,
  fetchAssetClassification,
  fetchAssetEntityDetail,
  fetchAssetGraph,
  fetchAssetHealth,
  fetchAssetHeatmap,
  fetchAssetMetricSummary,
  fetchAssetMyAssets,
  fetchAssetOrphans,
  fetchAssetOwnerView,
  fetchAssetPiiOverview,
  fetchAssetSearch,
  fetchAssetSummary,
  fetchAssetTables,
} from "../api";
import type {
  AssetCatalogSummary,
  AssetChanges,
  AssetClassificationSummary,
  AssetEntityDetail,
  AssetHealthSummary,
  AssetMetricSummary,
  AssetMyAssets,
  AssetPiiOverview,
  AssetSearchItem,
  AssetTableItem,
} from "../types";
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

// 敏感度渲染：历史后端 to_dict 曾剥离该字段，值可能为 null/undefined，此处做防御；
// 值含 "PII" 一律标红（无论是否精确匹配枚举）。
function sensitivityTag(s: string | null | undefined) {
  if (!s) return <Tag>未知</Tag>;
  const color = s.includes("PII") ? "red" : SENSITIVITY_COLOR[s];
  return <Tag color={color}>{SENSITIVITY_LABEL[s] ?? s}</Tag>;
}

function renderSchemaSummary(summary: string | Record<string, unknown> | null | undefined) {
  if (summary == null || summary === "") return <span className="muted">-</span>;
  if (typeof summary === "string") return <span>{summary}</span>;
  return (
    <pre style={{ margin: 0, maxHeight: 240, overflow: "auto", fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
      {JSON.stringify(summary, null, 2)}
    </pre>
  );
}

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
            { title: "类型", dataIndex: "entity_type", key: "entity_type", width: 90 },
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
              <span style={{ fontFamily: "monospace", wordBreak: "break-all" }}>{detail.content_signature ?? "-"}</span>
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
                  {detail.source_health.health_status}
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
              { title: "实体类型", dataIndex: "entity_type", key: "entity_type", width: 100 },
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
                  { title: "状态", dataIndex: "health_status", key: "status", width: 100, render: () => <Tag color="red">unhealthy</Tag> },
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
          { title: "类型", dataIndex: "entity_type", key: "type", width: 90 },
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
          { title: "类型", dataIndex: "entity_type", key: "type", width: 90 },
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
    { key: "graph", label: <span><ApartmentOutlined /> 图谱视图</span>, children: <GraphTab /> },
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
