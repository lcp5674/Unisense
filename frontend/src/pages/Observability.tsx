import { useEffect, useState } from "react";
import { Alert, Button, Card, Tag, Tabs, Statistic, Row, Col, Space, Tooltip } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
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
  QUALITY_SEVERITY_IMPACT,
  QUALITY_RULE_RISK,
  QUALITY_PATTERN_LABEL,
  QUALITY_EVENT_STATUS_LABEL,
  NOTIFY_STATUS_LABEL,
  SOURCE_HEALTH_LABEL,
  METRIC_STATUS_LABEL,
  METRIC_HEALTH_LEVEL_LABEL,
  DEP_STATUS_LABEL,
  CIRCUIT_STATE_LABEL,
  COLLECTION_RUN_STATUS_LABEL,
  RULE_TYPE_LABEL,
} from "../utils/enums";
import { auditActionLabel } from "../utils/auditI18n";
import { formatCnTime, parseBackendTime, timeAgoCn } from "../utils/timeCn";

const rowStyle = {
  display: "flex",
  justifyContent: "space-between",
  padding: "6px 0",
  borderBottom: "1px solid var(--line-soft)",
};

/** 上海时区精确到秒的时间（含日期），供「数据更新于」展示——秒级变化让刷新前后肉眼可见 */
function formatCnTimeSec(value: string): string {
  const d = parseBackendTime(value);
  if (!d) return "—";
  const date = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(d);
  const time = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).format(d);
  return `${date} ${time}`;
}

function MetricsTab() {
  const [quality, setQuality] = useState<{ by_level: Record<string, number>; by_status: Record<string, number>; total: number } | null>(null);
  const [api, setApi] = useState<Record<string, number> | null>(null);
  const [notif, setNotif] = useState<{ by_status: Record<string, number>; event_total: number; event_notified: number } | null>(null);
  const [lineage, setLineage] = useState<{ edges: number } | null>(null);
  const [events, setEvents] = useState<QualityEventItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<string | null>(null);

  async function load(silent = false) {
    if (silent) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    try {
      const [q, a, n, l, e] = await Promise.all([
        fetchObsMetricsQuality(),
        fetchObsMetricsApi(),
        fetchObsMetricsNotifications(),
        fetchObsMetricsLineage(),
        fetchObsQualityEvents(),
      ]);
      setQuality(q);
      setApi(a);
      setNotif(n);
      setLineage(l);
      setEvents(e.items ?? []);
      setLastUpdatedAt(new Date().toISOString());
    } catch {
      // 保留旧数据，静默失败不打断页面
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (loading) return null;

  return (
    <div>
      {/* 数据获取时间 + 手动刷新（刷新前后秒级时间变化即反馈） */}
      <Row justify="space-between" align="middle" style={{ marginBottom: 12 }}>
        <span className="muted" style={{ fontSize: 12 }}>
          {lastUpdatedAt ? (
            <Tooltip title={formatCnTime(lastUpdatedAt)}>
              <span>
                数据更新于 <span className="mono" style={{ color: "var(--ink)" }}>{formatCnTimeSec(lastUpdatedAt)}</span>（上海时区）
              </span>
            </Tooltip>
          ) : (
            "尚未获取数据"
          )}
        </span>
        <Button size="small" icon={<ReloadOutlined />} loading={refreshing} onClick={() => load(true)}>
          刷新
        </Button>
      </Row>

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
                const rs = e.repair_suggestion as Record<string, unknown> | null;
                const pattern = rs?.pattern ? String(rs.pattern) : null;
                const suggestedAction = rs?.suggested_action ? String(rs.suggested_action) : null;
                const ownerHint = rs?.owner_hint ? String(rs.owner_hint) : null;
                const suggestedSql = rs?.suggested_sql ? String(rs.suggested_sql) : null;
                // 处理留痕：谁在何时 ACK/RESOLVE/CLOSE（六要素之"谁/何时"）
                const traces = [
                  { label: "确认", who: e.ack_by_name, at: e.ack_at, note: e.ack_note },
                  { label: "解决", who: e.resolved_by_name, at: e.resolved_at },
                  { label: "关闭", who: e.closed_by_name, at: e.closed_at },
                ].filter((t) => t.who || t.at);
                return (
                  <div key={e.id} style={{ ...rowStyle, flexDirection: "column", alignItems: "stretch", gap: 6, padding: "10px 0" }}>
                    {/* 资产 + 状态 + 时间 */}
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
                            <span style={{ fontSize: 13, fontWeight: 600 }}>{e.metric_name}</span>
                          </Tooltip>
                        ) : (
                          <span style={{ fontSize: 12 }} className="muted">指标 #{e.metric_id}</span>
                        )}
                        {e.metric_domain ? <Tag color="geekblue">{e.metric_domain}</Tag> : null}
                      </Space>
                      {e.created_at ? (
                        <span className="mono" style={{ fontSize: 12, flex: "0 0 auto" }}>
                          <Tooltip title={formatCnTime(e.created_at)}>{timeAgoCn(e.created_at)}</Tooltip>
                        </span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </div>
                    {/* 影响风险：严重级影响说明 + 规则风险 */}
                    <div style={{ fontSize: 12 }}>
                      <span style={{ color: e.level === "P0" ? "var(--danger)" : e.level === "P1" ? "var(--warn)" : "var(--muted)" }}>
                        {QUALITY_SEVERITY_IMPACT[e.level] ?? ""}
                      </span>
                      <span style={{ color: "var(--muted)", marginLeft: 8 }}>
                        {QUALITY_RULE_RISK[e.rule_type] ?? ""}
                      </span>
                    </div>
                    {/* 事件：异常模式 + 观测值 vs 阈值 */}
                    {pattern || violation ? (
                      <div style={{ fontSize: 12, color: "var(--muted)" }}>
                        {pattern ? <Tag style={{ marginRight: 4 }}>{QUALITY_PATTERN_LABEL[pattern] ?? pattern}</Tag> : null}
                        {violation ? (
                          <span>
                            观测值 / 阈值：<span className="mono" style={{ color: e.level === "P0" || e.level === "P1" ? "var(--danger)" : undefined }}>{violation}</span>
                          </span>
                        ) : null}
                      </div>
                    ) : null}
                    {/* 处理留痕：谁在何时 ACK/RESOLVE/CLOSE */}
                    {traces.length > 0 ? (
                      <div style={{ fontSize: 12, color: "var(--muted)", display: "flex", flexWrap: "wrap", gap: 12 }}>
                        {traces.map((t) => (
                          <span key={t.label}>
                            {t.label}：<span style={{ color: "var(--ink)" }}>{t.who ?? "—"}</span>
                            {t.at ? (
                              <span className="mono" style={{ marginLeft: 4 }}>
                                <Tooltip title={formatCnTime(t.at)}>{timeAgoCn(t.at)}</Tooltip>
                              </span>
                            ) : null}
                            {t.note ? <span style={{ marginLeft: 4 }}>（{t.note}）</span> : null}
                          </span>
                        ))}
                      </div>
                    ) : null}
                    {/* 解决建议：责任方 + 处置动作 + 诊断 SQL */}
                    {suggestedAction ? (
                      <div style={{ fontSize: 12, background: "var(--signal-soft)", borderRadius: 6, padding: "8px 12px", marginTop: 2 }}>
                        <div style={{ fontWeight: 600, marginBottom: 4, color: "var(--ink)" }}>解决建议</div>
                        <div>{suggestedAction}</div>
                        {ownerHint ? (
                          <div style={{ marginTop: 4, color: "var(--muted)" }}>
                            责任方：{ownerHint}
                            {rs?.upstream_task ? <span className="mono" style={{ marginLeft: 6 }}>（{String(rs.upstream_task)}）</span> : null}
                          </div>
                        ) : null}
                        {suggestedSql ? (
                          <div style={{ marginTop: 4 }}>
                            <span style={{ color: "var(--muted)" }}>诊断 SQL：</span>
                            <pre className="mono" style={{ margin: "4px 0 0", fontSize: 11, whiteSpace: "pre-wrap" }}>{suggestedSql}</pre>
                          </div>
                        ) : null}
                      </div>
                    ) : null}
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

// 核心依赖类型 → 业务术语（dependency_health.dependency_type）
const DEP_TYPE_LABEL: Record<string, string> = {
  LLM: "AI 模型",
  OLAP: "OLAP 引擎",
  GRAPH: "图数据库",
  ES: "搜索引擎",
  DATASOURCE: "数据源",
  NOTIFICATION: "通知渠道",
};

/** 顶部状态条：依赖健康 / 熔断 / 采集运行中 / 数据新鲜度（运维第一信号一眼可见） */
function StatusStrip({ o }: { o: ObsOverview }) {
  const deps = o.system.dependencies;
  const coll = o.system.collection;
  const healthy = deps.by_status.HEALTHY ?? 0;
  const degraded = (deps.by_status.DEGRADED ?? 0) + (deps.by_status.UNAVAILABLE ?? 0);
  return (
    <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
      <Col xs={12} lg={6}>
        <Card size="small">
          <Statistic
            title="核心依赖健康"
            value={deps.total ? `${healthy}/${deps.total}` : "—"}
            suffix={deps.total ? "正常" : ""}
            valueStyle={{ color: degraded > 0 ? "var(--warn)" : "var(--ok)" }}
          />
          {degraded > 0 ? (
            <div style={{ fontSize: 12, color: "var(--warn)" }}>降级/不可用 {degraded} 个</div>
          ) : null}
        </Card>
      </Col>
      <Col xs={12} lg={6}>
        <Card size="small">
          <Statistic
            title="熔断开启"
            value={deps.circuit_open}
            valueStyle={{ color: deps.circuit_open > 0 ? "var(--danger)" : undefined }}
          />
          <div style={{ fontSize: 12, color: deps.circuit_open > 0 ? "var(--danger)" : "var(--muted)" }}>
            {deps.circuit_open > 0 ? "依赖熔断，需立即处置" : "无熔断"}
          </div>
        </Card>
      </Col>
      <Col xs={12} lg={6}>
        <Card size="small">
          <Statistic
            title="采集运行中"
            value={coll.running}
            suffix={coll.total ? `/ ${coll.total}` : ""}
            valueStyle={{ color: coll.running > 0 ? "var(--signal)" : undefined }}
          />
          {coll.failed > 0 ? (
            <div style={{ fontSize: 12, color: "var(--danger)" }}>{coll.failed} 个任务失败</div>
          ) : null}
        </Card>
      </Col>
      <Col xs={12} lg={6}>
        <Card size="small">
          <Statistic title="数据新鲜度" value={coll.last_collected_at ? timeAgoCn(coll.last_collected_at) : "—"} valueStyle={{ fontSize: 16 }} />
          <div style={{ fontSize: 12, color: "var(--muted)" }}>
            {coll.last_collected_at ? <Tooltip title={formatCnTime(coll.last_collected_at)}>最近一次采集完成</Tooltip> : "尚无采集记录"}
          </div>
        </Card>
      </Col>
    </Row>
  );
}

/** 核心依赖健康卡：实时状态 / 熔断 / 延迟 / 错误率，熔断 OPEN 红色高亮 */
function DependencyCard({ deps }: { deps: ObsOverview["system"]["dependencies"] }) {
  return (
    <Card
      title="核心依赖健康"
      size="small"
      extra={deps.total ? <Tag color={deps.circuit_open > 0 ? "error" : "default"}>{deps.total} 个依赖</Tag> : null}
    >
      {deps.items.length === 0 ? (
        <div style={rowStyle}>暂无依赖探测</div>
      ) : (
        deps.items.map((d) => {
          const unhealthy = d.status !== "HEALTHY";
          const open = d.circuit_state === "OPEN";
          return (
            <div
              key={d.dependency_id}
              style={{
                ...rowStyle,
                borderBottom: "none",
                flexDirection: "column",
                alignItems: "stretch",
                gap: 4,
                padding: "8px 10px",
                marginBottom: 6,
                border: `1px solid ${open ? "var(--danger)" : "var(--line-soft)"}`,
                borderRadius: 6,
                background: open ? "rgba(214,69,69,0.06)" : unhealthy ? "rgba(199,119,0,0.05)" : undefined,
              }}
            >
              <Space size={6} wrap>
                <span style={{ fontWeight: 600 }}>{DEP_TYPE_LABEL[d.dependency_type] ?? d.dependency_type}</span>
                <Tag color={d.status === "HEALTHY" ? "success" : d.status === "DEGRADED" ? "warning" : "error"}>
                  {DEP_STATUS_LABEL[d.status] ?? d.status}
                </Tag>
                <Tag color={open ? "error" : d.circuit_state === "HALF_OPEN" ? "warning" : "default"}>
                  {CIRCUIT_STATE_LABEL[d.circuit_state] ?? d.circuit_state}
                </Tag>
              </Space>
              <div style={{ fontSize: 12, color: "var(--muted)" }}>
                {d.consecutive_failures > 0 ? <span>连续失败 {d.consecutive_failures} 次 · </span> : null}
                {d.latency_p95_ms != null ? <span>P95 {d.latency_p95_ms}ms · </span> : null}
                <span>错误率 {d.error_rate_pct}%</span>
                {d.last_check_at ? (
                  <span>
                    {" "}
                    · <Tooltip title={formatCnTime(d.last_check_at)}>探测于 {timeAgoCn(d.last_check_at)}</Tooltip>
                  </span>
                ) : null}
              </div>
            </div>
          );
        })
      )}
    </Card>
  );
}

/** 采集链路健康卡：运行状态分布 / 成功率 / 最近采集时间 */
function CollectionCard({ c }: { c: ObsOverview["system"]["collection"] }) {
  return (
    <Card title="采集链路健康" size="small" extra={c.total ? <Tag>{c.total} 次运行</Tag> : null}>
      {Object.entries(c.by_status).map(([k, v]) => (
        <div key={k} style={rowStyle}>
          <Tag color={k === "FAILED" ? "error" : k === "RUNNING" ? "processing" : "success"}>
            {COLLECTION_RUN_STATUS_LABEL[k] ?? k}
          </Tag>
          <span className="mono">{v}</span>
        </div>
      ))}
      {Object.keys(c.by_status).length === 0 ? <div style={rowStyle}>暂无采集记录</div> : null}
      <div style={rowStyle}>
        <span>采集成功率</span>
        <span className="mono" style={{ color: c.success_rate_pct < 100 ? "var(--warn)" : "var(--ok)" }}>
          {c.success_rate_pct}%
        </span>
      </div>
      <div style={{ ...rowStyle, borderBottom: "none" }}>
        <span>最近采集</span>
        {c.last_collected_at ? (
          <span>
            <Tooltip title={formatCnTime(c.last_collected_at)}>{timeAgoCn(c.last_collected_at)}</Tooltip>
          </span>
        ) : (
          <span className="muted">—</span>
        )}
      </div>
    </Card>
  );
}

/** 指标健康度卡：分布 / 覆盖率 / 平均分 + 低健康 Top 指标（治理风险聚焦） */
function MetricHealthCard({ h }: { h: ObsOverview["quality"]["metric_health"] }) {
  return (
    <Card title="指标健康度" size="small">
      <Row gutter={[8, 8]}>
        {Object.entries(h.by_level).map(([k, v]) => (
          <Col span={12} key={k}>
            <div style={rowStyle}>
              <Tag color={k === "EXCELLENT" ? "green" : k === "GOOD" ? "blue" : k === "WARNING" ? "orange" : "red"}>
                {METRIC_HEALTH_LEVEL_LABEL[k] ?? k}
              </Tag>
              <span className="mono">{v}</span>
            </div>
          </Col>
        ))}
        {Object.keys(h.by_level).length === 0 ? <div style={rowStyle}>暂无健康评分</div> : null}
      </Row>
      <div style={rowStyle}>
        <span>健康覆盖率</span>
        <span className="mono">{h.coverage_pct}%</span>
      </div>
      <div style={{ ...rowStyle, borderBottom: h.top_risk.length ? "1px solid var(--line-soft)" : "none" }}>
        <span>平均分</span>
        <span className="mono">{h.avg_score}</span>
      </div>
      {h.top_risk.length ? (
        <div style={{ marginTop: 8 }}>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4, color: "var(--danger)" }}>
            低健康指标 Top {h.top_risk.length}（按评分升序）
          </div>
          {h.top_risk.map((r) => (
            <div key={r.metric_id} style={{ ...rowStyle, padding: "4px 0" }}>
              <span>
                {r.metric_name ? (
                  <Tooltip title={r.metric_code ?? undefined}>
                    <span style={{ fontWeight: 600 }}>{r.metric_name}</span>
                    {r.metric_code ? <span className="mono muted" style={{ marginLeft: 4, fontSize: 12 }}>{r.metric_code}</span> : null}
                  </Tooltip>
                ) : r.metric_code ? (
                  <span className="mono">{r.metric_code}</span>
                ) : (
                  <span>指标 #{r.metric_id}</span>
                )}{" "}
                <span style={{ color: "var(--danger)", fontWeight: 600 }}>{r.score} 分</span>
              </span>
              <span className="muted" style={{ fontSize: 12 }}>{r.missing_dimensions?.join("、") ?? "—"}</span>
            </div>
          ))}
        </div>
      ) : null}
    </Card>
  );
}

/** 血缘健康卡：边数 / 失效 / 接入成功 / 最近接入 */
function LineageCard({ l }: { l: ObsOverview["quality"]["lineage"] }) {
  return (
    <Card title="血缘健康" size="small">
      <Row gutter={[8, 8]}>
        <Col span={12}><Statistic title="血缘边" value={l.edges} /></Col>
        <Col span={12}>
          <Statistic title="失效边" value={l.stale} valueStyle={{ color: l.stale > 0 ? "var(--danger)" : undefined }} />
        </Col>
        <Col span={12}><Statistic title="接入成功" value={l.ingest_success} /></Col>
        <Col span={12}>
          <Statistic
            title="最近接入"
            value={l.last_ingest_at ? timeAgoCn(l.last_ingest_at) : "—"}
            valueStyle={{ fontSize: 14 }}
          />
        </Col>
      </Row>
    </Card>
  );
}

/** 治理风险雷达：PII 待复核 / 授权到期 / Schema 漂移 + 治理积压（backlog） */
function RiskCard({ r, backlog }: { r: ObsOverview["risks"]; backlog: ObsOverview["backlog"] }) {
  return (
    <Card title="治理风险雷达" size="small">
      <div style={rowStyle}>
        <Tag color={r.pii_review_pending > 0 ? "error" : "default"}>PII 待复核</Tag>
        <span className="mono">{r.pii_review_pending}</span>
      </div>
      <div style={rowStyle}>
        <Tag color={r.grants_expiring_soon > 0 ? "warning" : "default"}>授权即将到期</Tag>
        <span className="mono">{r.grants_expiring_soon}</span>
      </div>
      <div style={rowStyle}>
        <Tag color={r.schema_drift_7d > 0 ? "warning" : "default"}>7 天 Schema 漂移</Tag>
        <span className="mono">{r.schema_drift_7d}</span>
      </div>
      <div style={rowStyle}>
        <Tag color={backlog.open_conflicts > 0 ? "warning" : "default"}>待处理冲突</Tag>
        <span className="mono">{backlog.open_conflicts}</span>
      </div>
      <div style={rowStyle}>
        <Tag color={backlog.pending_quality_events > 0 ? "warning" : "default"}>未关闭质量事件</Tag>
        <span className="mono">{backlog.pending_quality_events}</span>
      </div>
      <div style={rowStyle}>
        <Tag color={backlog.review_metrics > 0 ? "warning" : "default"}>待审核指标</Tag>
        <span className="mono">{backlog.review_metrics}</span>
      </div>
      <div style={{ ...rowStyle, borderBottom: "none" }}>
        <Tag color={backlog.open_escalations > 0 ? "warning" : "default"}>未闭环升级</Tag>
        <span className="mono">{backlog.open_escalations}</span>
      </div>
    </Card>
  );
}

/** 资产规模卡：指标状态 / 术语 / 维度 / 域 / 数据源 + 数据源健康分布 + 消费接入 */
function AssetCard({ o }: { o: ObsOverview }) {
  return (
    <Card title="资产规模" size="small">
      {Object.entries(o.assets.metrics_by_status).map(([k, v]) => (
        <div key={k} style={rowStyle}>
          <Tag>{METRIC_STATUS_LABEL[k] ?? k}</Tag>
          <span className="mono">{v}</span>
        </div>
      ))}
      <div key="terms" style={rowStyle}><span>术语</span><span className="mono">{o.assets.terms}</span></div>
      <div key="dims" style={rowStyle}><span>维度</span><span className="mono">{o.assets.dimensions}</span></div>
      <div key="domains" style={rowStyle}><span>主题域</span><span className="mono">{o.assets.domains}</span></div>
      <div key="srcs" style={rowStyle}><span>数据源</span><span className="mono">{o.assets.sources}</span></div>
      <div style={{ marginTop: 6, paddingTop: 6, borderTop: "1px dashed var(--line)" }}>
        <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>数据源健康</div>
        {Object.keys(o.sources.by_health).length === 0 ? (
          <div style={rowStyle}>暂无数据源</div>
        ) : (
          Object.entries(o.sources.by_health).map(([k, v]) => (
            <div key={k} style={rowStyle}>
              <Tag color={k === "healthy" ? "success" : k === "degraded" ? "warning" : k === "unhealthy" ? "error" : "default"}>
                {SOURCE_HEALTH_LABEL[k] ?? k}
              </Tag>
              <span className="mono">{v}</span>
            </div>
          ))
        )}
      </div>
      <div style={{ marginTop: 6, paddingTop: 6, borderTop: "1px dashed var(--line)" }}>
        <Row gutter={[8, 8]}>
          <Col span={12}><Statistic title="接入方总数" value={o.clients.total} /></Col>
          <Col span={12}><Statistic title="活跃接入方" value={o.clients.active} /></Col>
        </Row>
      </div>
    </Card>
  );
}

/** 近 N 天趋势柱状（缺失日期补 0，最高值归一化） */
function TrendCard({ title, data }: { title: string; data: Array<{ date: string; count: number }> }) {
  const max = Math.max(1, ...data.map((d) => d.count));
  return (
    <Card title={title} size="small">
      <div style={{ display: "flex", alignItems: "flex-end", gap: 6, height: 88, padding: "8px 0" }}>
        {data.map((d) => (
          <div key={d.date} style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", gap: 2 }}>
            <span className="mono" style={{ fontSize: 11 }}>{d.count}</span>
            <div
              style={{
                width: "100%",
                height: Math.max(2, Math.round((d.count / max) * 52)),
                background: d.count ? "var(--data)" : "var(--line-soft)",
                borderRadius: "3px 3px 0 0",
              }}
            />
            <span style={{ fontSize: 10, color: "var(--muted)" }}>{d.date.slice(5)}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}

/** 平台概览：企业级运营总览——系统健康 / 资产质量 / 风险雷达 / 近 7 天趋势 */
function OverviewTab({ active }: { active?: boolean }) {
  const [overview, setOverview] = useState<ObsOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<string | null>(null);

  async function load(silent = false) {
    if (silent) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    try {
      const data = await fetchObsOverview();
      setOverview(data);
      setError(null);
      setLastUpdatedAt(new Date().toISOString());
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 时效性：每次切回「平台概览」Tab 时静默刷新，反映最新聚合（停留其他 Tab 期间数据可能已变化）
  useEffect(() => {
    if (active) {
      load(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

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
      {/* 数据获取时间 + 手动刷新（刷新前后秒级时间变化即反馈） */}
      <Row justify="space-between" align="middle" style={{ marginBottom: 12 }}>
        <span className="muted" style={{ fontSize: 12 }}>
          {lastUpdatedAt ? (
            <Tooltip title={formatCnTime(lastUpdatedAt)}>
              <span>
                数据更新于 <span className="mono" style={{ color: "var(--ink)" }}>{formatCnTimeSec(lastUpdatedAt)}</span>（上海时区）
              </span>
            </Tooltip>
          ) : (
            "尚未获取数据"
          )}
        </span>
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={refreshing}
          onClick={() => load(true)}
        >
          刷新
        </Button>
      </Row>

      {/* 顶部状态条：运维第一信号 */}
      <StatusStrip o={overview} />

      {/* 系统健康：核心依赖 + 采集链路 */}
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} lg={12}><DependencyCard deps={overview.system.dependencies} /></Col>
        <Col xs={24} lg={12}><CollectionCard c={overview.system.collection} /></Col>
      </Row>

      {/* 资产质量：指标健康度 + 血缘健康 */}
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} lg={12}><MetricHealthCard h={overview.quality.metric_health} /></Col>
        <Col xs={24} lg={12}><LineageCard l={overview.quality.lineage} /></Col>
      </Row>

      {/* 风险雷达 + 资产规模 */}
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} lg={12}><RiskCard r={overview.risks} backlog={overview.backlog} /></Col>
        <Col xs={24} lg={12}><AssetCard o={overview} /></Col>
      </Row>

      {/* 近 7 天趋势 */}
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}><TrendCard title="近 7 天指标新增" data={overview.trends.metrics_created} /></Col>
        <Col xs={24} lg={12}><TrendCard title="近 7 天采集运行" data={overview.trends.collections} /></Col>
      </Row>
    </div>
  );
}

export function Observability() {
  const [activeTab, setActiveTab] = useState("overview");
  const tabItems = [
    {
      key: "overview",
      label: "平台概览",
      children: <OverviewTab active={activeTab === "overview"} />,
    },
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
        <Tabs items={tabItems} activeKey={activeTab} onChange={setActiveTab} />
      </Card>
    </div>
  );
}
