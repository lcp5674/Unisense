import { useEffect, useState } from "react";
import { Card, Tag, Tabs, Statistic, Row, Col } from "antd";
import {
  fetchObsMetricsQuality,
  fetchObsMetricsApi,
  fetchObsMetricsNotifications,
  fetchObsMetricsLineage,
} from "../api";
import { QUALITY_LEVEL_LABEL, NOTIFY_STATUS_LABEL } from "../utils/enums";

function MetricsTab() {
  const [quality, setQuality] = useState<{ by_level: Record<string, number>; by_status: Record<string, number>; total: number } | null>(null);
  const [api, setApi] = useState<Record<string, number> | null>(null);
  const [notif, setNotif] = useState<{ by_status: Record<string, number>; event_total: number; event_notified: number } | null>(null);
  const [lineage, setLineage] = useState<{ edges: number } | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      fetchObsMetricsQuality(),
      fetchObsMetricsApi(),
      fetchObsMetricsNotifications(),
      fetchObsMetricsLineage(),
    ])
      .then(([q, a, n, l]) => {
        setQuality(q);
        setApi(a);
        setNotif(n);
        setLineage(l);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) return null;

  return (
    <div>
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Statistic title="质量事件" value={quality?.total ?? 0} />
        </Col>
        <Col span={6}>
          <Statistic title="事件已通知" value={notif?.event_notified ?? 0} suffix={`/ ${notif?.event_total ?? 0}`} />
        </Col>
        <Col span={6}>
          <Statistic title="血缘边数" value={lineage?.edges ?? 0} />
        </Col>
        <Col span={6}>
          <Statistic title="API 动作类型" value={api ? Object.keys(api).length : 0} />
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={8}>
          <Card title="质量事件级别分布" size="small">
            {Object.entries(quality?.by_level ?? {}).map(([k, v]) => (
              <div key={k} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderBottom: "1px solid var(--line-soft)" }}>
                <Tag color={k === "ERROR" ? "error" : k === "WARN" ? "warning" : "default"}>{QUALITY_LEVEL_LABEL[k] ?? k}</Tag>
                <span className="mono">{v}</span>
              </div>
            ))}
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card title="通知投递状态" size="small">
            {Object.entries(notif?.by_status ?? {}).map(([k, v]) => (
              <div key={k} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderBottom: "1px solid var(--line-soft)" }}>
                <Tag color={k === "FAILED" ? "error" : k === "SENT" ? "success" : "warning"}>{NOTIFY_STATUS_LABEL[k] ?? k}</Tag>
                <span className="mono">{v}</span>
              </div>
            ))}
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card title="API 动作分布" size="small">
            {Object.entries(api ?? {}).slice(0, 12).map(([k, v]) => (
              <div key={k} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderBottom: "1px solid var(--line-soft)" }}>
                <span className="mono" style={{ fontSize: 12 }}>{k}</span>
                <span className="mono">{v}</span>
              </div>
            ))}
          </Card>
        </Col>
      </Row>
    </div>
  );
}

export function Observability() {
  const tabItems = [
    { key: "metrics", label: "运行指标", children: <MetricsTab /> },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Operation / Observability</div>
          <h2>可观测中心</h2>
          <p>质量/通知/血缘/API 跨模块运行聚合读数。用户反馈与 NPS 见「用户反馈」。</p>
        </div>
      </div>
      <Card styles={{ body: { paddingTop: 8 } }}>
        <Tabs items={tabItems} />
      </Card>
    </div>
  );
}
