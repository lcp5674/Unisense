import { useEffect, useState } from "react";
import { Card, Select, Input, Button, Form, Space, Tag, Table, Tabs, Alert, message, Row, Col } from "antd";
import { PlayCircleOutlined, SafetyCertificateOutlined, KeyOutlined, DatabaseOutlined } from "@ant-design/icons";
import {
  consumeDryRun,
  consumeQuery,
  listMetrics,
  listSnapshots,
  listApiClients,
  mintClientToken,
  getConsumeToken,
  setConsumeToken,
  clearConsumeToken,
  UnisenseApiError,
} from "../api";
import type { DimensionExpr, DryRunResponse, QueryResponse, SnapshotResponse, ClientResponse } from "../types";
import { useTracking } from "../hooks/useTracking";
import { ObjectView, kvText } from "../utils/display";
import { DATE_RANGE_LABEL, GRANULARITY_LABEL } from "../utils/enums";

// 执行计划等对象字段名 → 中文（可读展示，避免裸 JSON 直出）
const CHECK_LABEL: Record<string, string> = {
  granularity: "粒度校验",
  authorization: "权限校验",
  staleness: "时效校验",
  metric_status: "指标状态校验",
};

const VALUE_FIELD_LABEL: Record<string, string> = {
  value: "指标值",
  total: "总计",
  count: "数量",
};

function PlanView({ plan }: { plan: Record<string, unknown> }) {
  return <ObjectView data={plan} />;
}

// 查询结果：rows 数组 → 动态列表格；非数组/空结果 → 结构化字段视图
function QueryResultTable({ data }: { data: Record<string, unknown> }) {
  const rows = Array.isArray(data.rows) ? (data.rows as Array<Record<string, unknown>>) : [];
  if (rows.length > 0) {
    const cols = Object.keys(rows[0]).map((k) => ({
      title: k,
      dataIndex: k,
      key: k,
      ellipsis: true,
      render: (v: unknown) =>
        typeof v === "object" && v !== null ? (
          <span className="mono" style={{ fontSize: 12 }}>{JSON.stringify(v)}</span>
        ) : (
          String(v ?? "")
        ),
    }));
    return (
      <div>
        <Table size="small" dataSource={rows} columns={cols} rowKey={(_, i) => String(i)} pagination={{ pageSize: 10 }} />
        <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
          共 {String(data.total ?? rows.length)} 行
          {data.elapsed_ms != null ? ` · 耗时 ${data.elapsed_ms} ms` : ""}
          {data.from_cache ? " · 来自缓存" : ""}
        </div>
      </div>
    );
  }
  return <ObjectView data={data} />;
}

export function QueryWorkspace() {
  const [metricOptions, setMetricOptions] = useState<Array<{ value: string; label: string }>>([]);
  const [metricCode, setMetricCode] = useState<string | undefined>(undefined);
  const [dateRange, setDateRange] = useState("last_30d");
  const [granularity, setGranularity] = useState<string | undefined>(undefined);
  const [comparison, setComparison] = useState<string | undefined>(undefined);
  const [dimInputs, setDimInputs] = useState<Array<{ name: string; value: string }>>([{ name: "", value: "" }]);
  const [acceptStale, setAcceptStale] = useState(false);
  const [dryRun, setDryRun] = useState<DryRunResponse | null>(null);
  const [query, setQuery] = useState<QueryResponse | null>(null);
  const [snapshots, setSnapshots] = useState<SnapshotResponse[]>([]);
  const [busy, setBusy] = useState<"dry" | "query" | "snap" | null>(null);
  const [tokenOk, setTokenOk] = useState(!!getConsumeToken());
  const { track } = useTracking();

  useEffect(() => {
    listMetrics({ page_size: 100 })
      .then((res) =>
        setMetricOptions(
          res.items.map((m) => ({ value: m.metric_code, label: `${m.metric_code} · ${m.name}` })),
        ),
      )
      .catch(() => {});
  }, []);

  useEffect(() => {
    setTokenOk(!!getConsumeToken());
  }, []);

  function buildRequest(): DimensionExpr[] {
    return dimInputs.filter((d) => d.name.trim() && d.value.trim()).map((d) => ({ name: d.name.trim(), value: d.value.trim() }));
  }

  async function handleDryRun() {
    if (!metricCode) { message.warning("请选择指标"); return; }
    setBusy("dry");
    try {
      const res = await consumeDryRun({
        metric_code: metricCode,
        dimensions: buildRequest(),
        date_range: dateRange,
        granularity,
        comparison,
        accept_stale: acceptStale,
      });
      setDryRun(res);
      setQuery(null);
      track("consume_dry_run", metricCode, "metric");
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "校验失败");
    } finally {
      setBusy(null);
    }
  }

  async function handleQuery() {
    if (!metricCode) { message.warning("请选择指标"); return; }
    setBusy("query");
    try {
      const res = await consumeQuery({
        metric_code: metricCode,
        dimensions: buildRequest(),
        date_range: dateRange,
        granularity,
        comparison,
        accept_stale: acceptStale,
      });
      setQuery(res);
      setDryRun(null);
      track("consume_query", metricCode, "metric");
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "查询失败");
    } finally {
      setBusy(null);
    }
  }

  async function handleSnapshots() {
    if (!metricCode) { message.warning("请选择指标"); return; }
    setBusy("snap");
    try {
      setSnapshots(await listSnapshots(metricCode, 50));
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载快照失败");
    } finally {
      setBusy(null);
    }
  }

  async function handleMintToken() {
    try {
      const clients: ClientResponse[] = await listApiClients();
      const active = clients.find((c) => c.status === "ACTIVE");
      if (!active) {
        message.warning("没有 ACTIVE 的 API 客户端，请先到「API 客户端」创建");
        return;
      }
      const { access_token } = await mintClientToken(active.client_id);
      setConsumeToken(access_token);
      setTokenOk(true);
      message.success(`已使用客户端 ${active.client_id} 签发消费令牌`);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "签发失败");
    }
  }

  const snapColumns = [
    { title: "ID", dataIndex: "id", key: "id", width: 70 },
    { title: "版本", dataIndex: "version", key: "version", width: 80, render: (v: number) => `v${v}` },
    { title: "维度", dataIndex: "dims", key: "dims", render: (v: Record<string, unknown>) => <span style={{ fontSize: 12 }}>{kvText(v)}</span> },
    { title: "时间范围", dataIndex: "date_range", key: "date_range" },
    { title: "值", dataIndex: "value_json", key: "value", render: (v: Record<string, unknown>) => <span className="mono" style={{ fontSize: 12 }}>{kvText(v, VALUE_FIELD_LABEL)}</span> },
    {
      title: "质量",
      dataIndex: "quality_flag",
      key: "quality",
      width: 90,
      render: (v: string | null) => (v ? <Tag color="warning">{v}</Tag> : <Tag color="success">正常</Tag>),
    },
    { title: "来源", dataIndex: "generated_by", key: "generated_by", width: 110 },
  ];

  const tabItems = [
    {
      key: "query",
      label: "查询执行",
      children: (
        <div>
          <Alert
            type={tokenOk ? "success" : "warning"}
            showIcon
            icon={<KeyOutlined />}
            style={{ marginBottom: 16 }}
            message={tokenOk ? "消费令牌已就绪（角色 consume）" : "需要消费令牌"}
            description={
              tokenOk ? (
                <span>
                  当前使用客户端令牌调用 /consume/query。{" "}
                  <a onClick={() => { clearConsumeToken(); setTokenOk(false); }}>清除令牌</a>
                </span>
              ) : (
                <span>
                  /consume/query 由 API 客户端（X-Api-Key + consume JWT）鉴权。可到「API 客户端」创建后一键签发。
                </span>
              )
            }
            action={
              !tokenOk && (
                <Button size="small" onClick={handleMintToken}>从客户端签发令牌</Button>
              )
            }
          />

          <Form layout="vertical">
            <Row gutter={[16, 0]}>
              <Col xs={24} md={12}>
                <Form.Item label="指标">
                  <Select
                    showSearch
                    value={metricCode}
                    onChange={setMetricCode}
                    options={metricOptions}
                    placeholder="选择指标编码"
                    filterOption={(input, opt) => (opt?.label ?? "").toLowerCase().includes(input.toLowerCase())}
                  />
                </Form.Item>
              </Col>
              <Col xs={24} md={12}>
                <Form.Item label="日期范围">
                  <Select
                    value={dateRange}
                    onChange={setDateRange}
                    options={["today", "last_7d", "last_30d", "last_90d", "ytd", "last_365d"].map((v) => ({ value: v, label: DATE_RANGE_LABEL[v] ?? v }))}
                  />
                </Form.Item>
              </Col>
              <Col xs={24} md={8}>
                <Form.Item label="粒度">
                  <Select
                    allowClear
                    value={granularity}
                    onChange={setGranularity}
                    placeholder="按日 / 周 / 月 / 季"
                    options={["day", "week", "month", "quarter"].map((v) => ({ value: v, label: GRANULARITY_LABEL[v] ?? v }))}
                  />
                </Form.Item>
              </Col>
              <Col xs={24} md={8}>
                <Form.Item label="对比">
                  <Select
                    allowClear
                    value={comparison}
                    onChange={setComparison}
                    placeholder="MoM / YoY"
                    options={[{ value: "MoM", label: "MoM 环比" }, { value: "YoY", label: "YoY 同比" }]}
                  />
                </Form.Item>
              </Col>
              <Col xs={24} md={8}>
                <Form.Item label="允许陈旧数据">
                  <Select value={acceptStale ? "true" : "false"} onChange={(v) => setAcceptStale(v === "true")} options={[{ value: "false", label: "否" }, { value: "true", label: "是" }]} />
                </Form.Item>
              </Col>
            </Row>

            <Form.Item label="维度过滤（名称 / 值）">
              <Space direction="vertical" style={{ width: "100%" }}>
                {dimInputs.map((d, i) => (
                  <Space key={i} style={{ display: "flex" }}>
                    <Input
                      placeholder="维度名（如 channel）"
                      className="mono"
                      value={d.name}
                      style={{ width: 220 }}
                      onChange={(e) => setDimInputs((prev) => prev.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))}
                    />
                    <Input
                      placeholder="维度值（如 app）"
                      className="mono"
                      value={d.value}
                      style={{ width: 220 }}
                      onChange={(e) => setDimInputs((prev) => prev.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))}
                    />
                    <Button
                      type="text"
                      danger
                      disabled={dimInputs.length === 1}
                      onClick={() => setDimInputs((prev) => prev.filter((_, j) => j !== i))}
                    >
                      删除
                    </Button>
                  </Space>
                ))}
                <Button size="small" onClick={() => setDimInputs((prev) => [...prev, { name: "", value: "" }])}>
                  + 添加维度
                </Button>
              </Space>
            </Form.Item>

            <Space>
              <Button icon={<SafetyCertificateOutlined />} onClick={handleDryRun} loading={busy === "dry"}>
                语义校验（dry-run）
              </Button>
              <Button type="primary" icon={<PlayCircleOutlined />} onClick={handleQuery} loading={busy === "query"}>
                执行查询
              </Button>
              <Button icon={<DatabaseOutlined />} onClick={handleSnapshots} loading={busy === "snap"}>
                加载快照
              </Button>
            </Space>
          </Form>

          {dryRun && (
            <div style={{ marginTop: 20 }}>
              <Alert
                type={dryRun.status === "ok" ? "success" : "error"}
                showIcon
                message={`语义校验：${dryRun.status === "ok" ? "通过" : "被拒绝"}`}
                style={{ marginBottom: 12 }}
              />
              {dryRun.checks && dryRun.checks.length > 0 && (
                <Card size="small" title="校验项" style={{ marginBottom: 12 }}>
                  {dryRun.checks.map((c, i) => {
                    const ok = (c as { ok?: boolean }).ok;
                    const checkName = CHECK_LABEL[String((c as { check?: string }).check ?? "")] ?? String((c as { check?: string }).check ?? "校验");
                    const detail = c.detail != null ? String(c.detail) : "";
                    return (
                      <div key={i} style={{ marginBottom: 4 }}>
                        <Tag color={ok ? "success" : "error"}>{ok ? "通过" : "未通过"}</Tag>
                        <span style={{ fontSize: 13 }}>{checkName}</span>
                        {detail && <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>{detail}</span>}
                      </div>
                    );
                  })}
                </Card>
              )}
              <Card size="small" title="执行计划" style={{ marginBottom: 12 }}>
                <PlanView plan={dryRun.execution_plan} />
              </Card>
              <Card size="small" title="元信息">
                <PlanView plan={dryRun.meta} />
              </Card>
            </div>
          )}

          {query && (
            <div style={{ marginTop: 20 }}>
              <Alert
                type={query.degraded ? "warning" : "success"}
                showIcon
                message={query.degraded ? "查询降级执行（部分能力不可用）" : "查询成功"}
                style={{ marginBottom: 12 }}
              />
              {query.data ? (
                <Card size="small" title="查询结果" style={{ marginBottom: 12 }}>
                  <QueryResultTable data={query.data} />
                </Card>
              ) : (
                <Alert type="info" message="无结果数据" style={{ marginBottom: 12 }} />
              )}
              <Card size="small" title="执行计划" style={{ marginBottom: 12 }}>
                <PlanView plan={query.execution_plan} />
              </Card>
              <Card size="small" title="元信息">
                <PlanView plan={query.meta} />
              </Card>
            </div>
          )}

          {snapshots.length > 0 && (
            <Card size="small" title={`快照（${snapshots.length}）`} style={{ marginTop: 20 }}>
              <Table dataSource={snapshots} columns={snapColumns} rowKey="id" size="small" pagination={{ pageSize: 10 }} />
            </Card>
          )}
        </div>
      ),
    },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Consumption / Query</div>
          <h2>查询工作台</h2>
          <p>基于指标语义的查询——先 dry-run 校验口径，再安全执行。</p>
        </div>
      </div>
      <Card><Tabs items={tabItems} /></Card>
    </div>
  );
}
