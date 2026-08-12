import { useEffect, useState } from "react";
import { Card, Col, Row, Statistic, Spin, Alert, Typography } from "antd";
import {
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  ClockCircleOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import { fetchDashboard } from "../api";
import { useTracking } from "../hooks/useTracking";

const { Title } = Typography;

interface DashboardData {
  total_metrics: number;
  published_count: number;
  draft_count: number;
  deprecated_count: number;
  conflict_count: number;
  review_pending_count: number;
  avg_review_hours: number;
  pii_metric_count: number;
  quality_anomaly_count: number;
  top_domains: Array<{ domain: string; count: number }>;
}

export function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { track } = useTracking();

  useEffect(() => {
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const res = await fetchDashboard();
        setData(res);
        track("dashboard_view", undefined, "dashboard");
      } catch (err) {
        setError(err instanceof Error ? err.message : "加载驾驶舱失败");
      } finally {
        setLoading(false);
      }
    }
    load();
  }, [track]);

  if (loading) {
    return (
      <div style={{ textAlign: "center", padding: 48 }}>
        <Spin size="large" tip="加载驾驶舱数据..." />
      </div>
    );
  }

  if (error) {
    return <Alert type="error" message="加载失败" description={error} showIcon />;
  }

  if (!data) return null;

  return (
    <div>
      <Title level={3}>治理驾驶舱</Title>

      {/* 全局健康度卡片 */}
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col span={6}>
          <Card>
            <Statistic
              title="指标总数"
              value={data.total_metrics}
              prefix={<CheckCircleOutlined />}
              valueStyle={{ color: "#1890ff" }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="已发布"
              value={data.published_count}
              valueStyle={{ color: "#52c41a" }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="待审核"
              value={data.review_pending_count}
              prefix={<ClockCircleOutlined />}
              valueStyle={{ color: "#faad14" }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="冲突数"
              value={data.conflict_count}
              prefix={<ExclamationCircleOutlined />}
              valueStyle={{ color: data.conflict_count > 0 ? "#ff4d4f" : "#52c41a" }}
            />
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col span={6}>
          <Card>
            <Statistic title="草稿" value={data.draft_count} />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic title="已废弃" value={data.deprecated_count} />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="PII 指标"
              value={data.pii_metric_count}
              prefix={<SafetyCertificateOutlined />}
              valueStyle={{ color: "#722ed1" }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="平均审核耗时(h)"
              value={data.avg_review_hours}
              precision={1}
              suffix="小时"
            />
          </Card>
        </Col>
      </Row>

      {/* 质量异常 */}
      {data.quality_anomaly_count > 0 && (
        <Alert
          type="warning"
          message={`当前存在 ${data.quality_anomaly_count} 个质量异常，请及时处理`}
          showIcon
          style={{ marginBottom: 24 }}
        />
      )}

      {/* 域分布 Top5 */}
      {data.top_domains && data.top_domains.length > 0 && (
        <Card title="域分布 Top5" style={{ marginBottom: 24 }}>
          <Row gutter={[16, 16]}>
            {data.top_domains.slice(0, 5).map((d, i) => (
              <Col span={Math.floor(24 / Math.min(data.top_domains.length, 5))} key={d.domain}>
                <Statistic title={`${i + 1}. ${d.domain}`} value={d.count} suffix="个指标" />
              </Col>
            ))}
          </Row>
        </Card>
      )}
    </div>
  );
}
