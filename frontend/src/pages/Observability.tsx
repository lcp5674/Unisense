import { useEffect, useState } from "react";
import { Alert, Card, Tag, Tabs, Statistic, Row, Col, Space, Tooltip } from "antd";
import {
  fetchObsMetricsQuality,
  fetchObsMetricsApi,
  fetchObsMetricsNotifications,
  fetchObsMetricsLineage,
  fetchObsOverview,
  fetchObsQualityEvents,
} from "../api";
import type { ObsOverview, QualityEventItem } from "../types";
import {
  QUALITY_SEVERITY_LABEL,
  QUALITY_EVENT_STATUS_LABEL,
  NOTIFY_STATUS_LABEL,
  SOURCE_HEALTH_LABEL,
  METRIC_STATUS_LABEL,
  RULE_TYPE_LABEL,
} from "../utils/enums";
import { auditActionLabel } from "../utils/auditI18n";
import { formatCnTime, timeAgoCn } from "../utils/timeCn";

const rowStyle = {
  display: "flex",
  justifyContent: "space-between",
  padding: "6px 0",
  borderBottom: "1px solid var(--line-soft)",
};

function MetricsTab() {
  const [quality, setQuality] = useState<{ by_level: Record<string, number>; by_status: Record<string, number>; total: number } | null>(null);
  const [api, setApi] = useState<Record<string, number> | null>(null);
  const [notif, setNotif] = useState<{ by_status: Record<string, number>; event_total: number; event_notified: number } | null>(null);
  const [lineage, setLineage] = useState<{ edges: number } | null>(null);
  const [events, setEvents] = useState<QualityEventItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      fetchObsMetricsQuality(),
      fetchObsMetricsApi(),
      fetchObsMetricsNotifications(),
      fetchObsMetricsLineage(),
      fetchObsQualityEvents(),
    ])
      .then(([q, a, n, l, e]) => {
        setQuality(q);
        setApi(a);
        setNotif(n);
        setLineage(l);
        setEvents(e.items ?? []);
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
              <div key={k} style={rowStyle}>
                <Tag color={k === "P0" ? "error" : k === "P1" ? "orange" : "default"}>{QUALITY_SEVERITY_LABEL[k] ?? k}</Tag>
                <span className="mono">{v}</span>
              </div>
            ))}
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card title="通知投递状态" size="small">
            {Object.entries(notif?.by_status ?? {}).map(([k, v]) => (
              <div key={k} style={rowStyle}>
                <Tag color={k === "FAILED" ? "error" : k === "SENT" ? "success" : "warning"}>{NOTIFY_STATUS_LABEL[k] ?? k}</Tag>
                <span className="mono">{v}</span>
              </div>
            ))}
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card title="API 动作分布" size="small">
            {Object.entries(api ?? {}).slice(0, 12).map(([k, v]) => (
              <div key={k} style={rowStyle}>
                <span style={{ fontSize: 12 }}>{auditActionLabel(k)}</span>
                <span className="mono">{v}</span>
              </div>
            ))}
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 8 }}>
        <Col xs={24}>
          <Card title="最近质量事件" size="small">
            {events.length === 0 ? (
              <div style={{ ...rowStyle, borderBottom: "none" }}>暂无质量事件</div>
            ) : (
              events.map((e) => {
                const violation =
                  e.obs_value != null && e.threshold != null
                    ? `${e.obs_value} / ${e.threshold}`
                    : e.obs_value != null
                      ? String(e.obs_value)
                      : null;
                return (
                  <div key={e.id} style={{ ...rowStyle, flexDirection: "column", alignItems: "stretch", gap: 4 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                      <Space size={6} wrap>
                        <Tag color={e.level === "P0" ? "error" : e.level === "P1" ? "orange" : "default"}>
                          {QUALITY_SEVERITY_LABEL[e.level] ?? e.level}
                        </Tag>
                        <Tag>{RULE_TYPE_LABEL[e.rule_type] ?? e.rule_type}</Tag>
                        <Tag color={e.status === "OPEN" ? "red" : e.status === "ACK" ? "orange" : e.status === "RESOLVED" ? "blue" : "green"}>
                          {QUALITY_EVENT_STATUS_LABEL[e.status] ?? e.status}
                        </Tag>
                        {e.metric_name ? (
                          <Tooltip title={e.metric_code}>
                            <span style={{ fontSize: 13, fontWeight: 500 }}>{e.metric_name}</span>
                          </Tooltip>
                        ) : (
                          <span style={{ fontSize: 12 }} className="muted">指标 #{e.metric_id}</span>
                        )}
                      </Space>
                      {e.created_at ? (
                        <span className="mono" style={{ fontSize: 12, flex: "0 0 auto" }}>
                          <Tooltip title={formatCnTime(e.created_at)}>{timeAgoCn(e.created_at)}</Tooltip>
                        </span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </div>
                    {violation && (
                      <div style={{ fontSize: 12, color: "var(--muted)" }}>
                        观测值 / 阈值：<span className="mono" style={{ color: e.level === "P0" || e.level === "P1" ? "var(--danger)" : undefined }}>{violation}</span>
                        {e.ack_note ? <span> · 处理说明：{e.ack_note}</span> : null}
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </Card>
        </Col>
      </Row>
    </div>
  );
}

/** 平台概览：数据源健康 / 治理积压 / 资产规模 / 消费接入（生产视角一次拉齐） */
function OverviewTab() {
  const [overview, setOverview] = useState<ObsOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchObsOverview()
      .then(setOverview)
      .catch((err) => setError(err instanceof Error ? err.message : "加载失败"))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div style={{ padding: "32px 0", textAlign: "center", color: "var(--text-tertiary)" }}>
        加载平台概览…
      </div>
    );
  }
  if (!overview) {
    return <Alert type="error" showIcon message="平台概览加载失败" description={error ?? ""} />;
  }

  return (
    <div>
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} lg={12}>
          <Card title="数据源健康" size="small">
            {Object.keys(overview.sources.by_health).length === 0 ? (
              <div style={rowStyle}>暂无数据源</div>
            ) : (
              Object.entries(overview.sources.by_health).map(([k, v]) => (
                <div key={k} style={rowStyle}>
                  <Tag color={k === "healthy" ? "success" : k === "unhealthy" ? "error" : "default"}>
                    {SOURCE_HEALTH_LABEL[k] ?? k}
                  </Tag>
                  <span className="mono">{v}</span>
                </div>
              ))
            )}
            <div key="total" style={{ ...rowStyle, borderBottom: "none" }}>
              <span>数据源总数</span>
              <span className="mono" style={{ fontWeight: 600 }}>{overview.sources.total}</span>
            </div>
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card title="治理积压" size="small">
            <Row gutter={[8, 8]}>
              <Col span={12}><Statistic title="待处理冲突" value={overview.backlog.open_conflicts} /></Col>
              <Col span={12}><Statistic title="未关闭质量事件" value={overview.backlog.pending_quality_events} /></Col>
              <Col span={12}><Statistic title="待审核指标" value={overview.backlog.review_metrics} /></Col>
              <Col span={12}><Statistic title="未闭环升级" value={overview.backlog.open_escalations} /></Col>
            </Row>
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <Card title="资产规模" size="small">
            {Object.entries(overview.assets.metrics_by_status).map(([k, v]) => (
              <div key={k} style={rowStyle}>
                <Tag>{METRIC_STATUS_LABEL[k] ?? k}</Tag>
                <span className="mono">{v}</span>
              </div>
            ))}
            <div key="terms" style={rowStyle}>
              <span>术语</span>
              <span className="mono">{overview.assets.terms}</span>
            </div>
            <div key="dimensions" style={rowStyle}>
              <span>维度</span>
              <span className="mono">{overview.assets.dimensions}</span>
            </div>
            <div key="domains" style={rowStyle}>
              <span>主题域</span>
              <span className="mono">{overview.assets.domains}</span>
            </div>
            <div key="sources" style={{ ...rowStyle, borderBottom: "none" }}>
              <span>数据源</span>
              <span className="mono">{overview.assets.sources}</span>
            </div>
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card title="消费接入" size="small">
            <Row gutter={[8, 8]}>
              <Col span={12}><Statistic title="接入方总数" value={overview.clients.total} /></Col>
              <Col span={12}><Statistic title="活跃接入方" value={overview.clients.active} /></Col>
            </Row>
          </Card>
        </Col>
      </Row>
    </div>
  );
}

export function Observability() {
  const tabItems = [
    { key: "overview", label: "平台概览", children: <OverviewTab /> },
    { key: "metrics", label: "运行指标", children: <MetricsTab /> },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Operation / Observability</div>
          <h2>可观测中心</h2>
          <p>平台运营总览：数据源健康、治理积压、资产规模、消费接入与质量/通知/血缘/API 运行读数。用户反馈与 NPS 见「用户反馈」。</p>
        </div>
      </div>
      <Card styles={{ body: { paddingTop: 8 } }}>
        <Tabs items={tabItems} />
      </Card>
    </div>
  );
}
