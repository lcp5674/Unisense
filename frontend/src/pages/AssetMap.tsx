import { useEffect, useState } from "react";
import { Card, Spin, Alert, Select, Switch, Typography, Row, Col, Statistic, Tag, Table } from "antd";
import { ApartmentOutlined, HeatMapOutlined, UserOutlined } from "@ant-design/icons";
import { fetchAssetGraph } from "../api";
import { useTracking } from "../hooks/useTracking";

const { Title } = Typography;

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

export function AssetMap() {
  const [activeTab, setActiveTab] = useState<"graph" | "heatmap" | "owner">("graph");
  const [graphData, setGraphData] = useState<{ nodes: GraphNode[]; edges: GraphEdge[] } | null>(null);
  const [domain, setDomain] = useState<string | undefined>(undefined);
  const [piiOnly, setPiiOnly] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { track } = useTracking();

  useEffect(() => {
    track("view", undefined, "page", { page: "assetmap" });
  }, [track]);

  useEffect(() => {
    if (activeTab === "graph") {
      loadGraph();
    }
  }, [activeTab, domain, piiOnly]);

  const loadGraph = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchAssetGraph({ domain, depth: 3, pii_only: piiOnly });
      setGraphData(data);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "加载图谱数据失败";
      setError(message);
    } finally {
      setLoading(false);
    }
  };

  const renderGraph = () => {
    if (loading) return <Spin tip="加载图谱数据..." />;
    if (error) return <Alert type="error" message={error} />;
    if (!graphData) return <Alert type="info" message="暂无图谱数据" />;

    const { nodes, edges } = graphData;

    return (
      <div>
        <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
          <Col>
            <span>域筛选：</span>
            <Select
              allowClear
              placeholder="选择域"
              style={{ width: 200 }}
              value={domain}
              onChange={(val) => setDomain(val)}
              options={[...new Set(nodes.map((n) => n.domain).filter(Boolean))].map((d) => ({
                label: d,
                value: d,
              }))}
            />
          </Col>
          <Col>
            <span>仅 PII：</span>
            <Switch checked={piiOnly} onChange={setPiiOnly} />
          </Col>
          <Col>
            <Statistic title="节点数" value={nodes.length} />
          </Col>
          <Col>
            <Statistic title="边数" value={edges.length} />
          </Col>
        </Row>

        {/* 简化力导向图渲染：节点列表 + 关系列表 */}
        <Card title="图谱节点" size="small" style={{ marginBottom: 16 }}>
          <Table
            dataSource={nodes}
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
                width: 60,
                render: (val: boolean) =>
                  val ? <Tag color="red">PII</Tag> : <Tag>普通</Tag>,
              },
              { title: "域", dataIndex: "domain", key: "domain", width: 100 },
            ]}
          />
        </Card>

        <Card title="关联边" size="small">
          <Table
            dataSource={edges}
            rowKey={(r) => `${r.source}-${r.target}-${r.type}`}
            pagination={{ pageSize: 20 }}
            size="small"
            columns={[
              { title: "源", dataIndex: "source", key: "source", ellipsis: true },
              { title: "目标", dataIndex: "target", key: "target", ellipsis: true },
              { title: "类型", dataIndex: "type", key: "type", width: 150 },
            ]}
          />
        </Card>
      </div>
    );
  };

  const renderHeatmap = () => {
    // Placeholder for heatmap rendering - would use @ant-design/charts in production
    return (
      <Card title="敏感分布热力图">
        <Alert
          type="info"
          message="热力图需要 @ant-design/charts Heatmap 组件渲染，当前显示数据表格"
        />
        <p style={{ marginTop: 16 }}>
          请通过 API <code>GET /api/v1/assetmap/heatmap?dimension=domain</code> 获取数据
        </p>
      </Card>
    );
  };

  const renderOwnerView = () => {
    return (
      <Card title="责任人视图">
        <Alert
          type="info"
          message="请通过 API GET /api/v1/assetmap/owner-view?owner_id=X 获取指定责任人的资产统计"
        />
      </Card>
    );
  };

  return (
    <div style={{ padding: 24 }}>
      <Title level={3}>
        <ApartmentOutlined /> 资产地图
      </Title>

      <Row gutter={16} style={{ marginBottom: 24 }}>
        <Col>
          <Card
            hoverable
            style={{ borderColor: activeTab === "graph" ? "#1890ff" : undefined }}
            onClick={() => setActiveTab("graph")}
          >
            <Statistic title="图谱视图" value="" prefix={<ApartmentOutlined />} />
          </Card>
        </Col>
        <Col>
          <Card
            hoverable
            style={{ borderColor: activeTab === "heatmap" ? "#1890ff" : undefined }}
            onClick={() => setActiveTab("heatmap")}
          >
            <Statistic title="热力图" value="" prefix={<HeatMapOutlined />} />
          </Card>
        </Col>
        <Col>
          <Card
            hoverable
            style={{ borderColor: activeTab === "owner" ? "#1890ff" : undefined }}
            onClick={() => setActiveTab("owner")}
          >
            <Statistic title="责任人视图" value="" prefix={<UserOutlined />} />
          </Card>
        </Col>
      </Row>

      {activeTab === "graph" && renderGraph()}
      {activeTab === "heatmap" && renderHeatmap()}
      {activeTab === "owner" && renderOwnerView()}
    </div>
  );
}
