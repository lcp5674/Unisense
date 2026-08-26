import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Empty,
  Progress,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
} from "antd";
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { getSqlInferEval, runSqlInferEval } from "../api";
import type {
  SqlInferEvalCase,
  SqlInferEvalData,
  SqlInferEvalRunSummary,
} from "../types";

const { Paragraph, Text, Title } = Typography;

/** 成功率指标卡配置（数值 → 百分比）。 */
function pct(v: number | null | undefined): number {
  if (v == null) return 0;
  return Math.round(v * 1000) / 10;
}

function metricColor(v: number): string {
  if (v >= 99) return "#52c41a";
  if (v >= 95) return "#1677ff";
  if (v >= 90) return "#faad14";
  return "#ff4d4f";
}

function DialectTag({ dialect }: { dialect: string }) {
  const map: Record<string, string> = {
    hive: "Hive",
    oracle: "Oracle",
    spark: "Spark",
    clickhouse: "ClickHouse",
    trino: "Trino",
    postgres: "PostgreSQL",
    mysql: "MySQL",
    doris: "Doris",
  };
  return <Tag color="geekblue">{map[dialect] ?? dialect}</Tag>;
}

export function SqlInferEval() {
  const [data, setData] = useState<SqlInferEvalData | null>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getSqlInferEval();
      setData(res);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "评测数据加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleRun = async () => {
    setRunning(true);
    try {
      await runSqlInferEval();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "评测运行失败");
    } finally {
      setRunning(false);
    }
  };

  const report = data?.report;
  const latest = data?.latest_run;

  // 用例明细：实际结果（report.cases）与样本（dataset）按 case_id 合并
  const caseRows = (data?.report.cases ?? []).map((c) => {
    const sample = data?.dataset?.find((d) => d.case_id === c.case_id);
    return { ...c, note: sample?.note ?? "", sql: sample?.sql ?? "" };
  });

  const caseColumns: ColumnsType<SqlInferEvalCase & { note: string; sql: string }> = [
    {
      title: "用例",
      dataIndex: "case_id",
      width: 180,
      render: (v: string) => <Text code>{v}</Text>,
    },
    { title: "方言", dataIndex: "dialect", width: 110, render: (v: string) => <DialectTag dialect={v} /> },
    {
      title: "结果",
      dataIndex: "exact",
      width: 90,
      render: (v: boolean) =>
        v ? (
          <Tag color="success" icon={<CheckCircleOutlined />}>通过</Tag>
        ) : (
          <Tag color="error" icon={<CloseCircleOutlined />}>失败</Tag>
        ),
    },
    {
      title: "度量召回率",
      dataIndex: "measure_recall",
      width: 110,
      render: (v: number | null) => (v == null ? "—" : `${pct(v)}%`),
    },
    {
      title: "度量精确率",
      dataIndex: "measure_precision",
      width: 110,
      render: (v: number | null) => (v == null ? "—" : `${pct(v)}%`),
    },
    {
      title: "周期",
      dataIndex: "period_match",
      width: 110,
      render: (v: boolean | null, r) => (v == null ? "—" : v ? `匹配（${r.expected_period}）` : `期望 ${r.expected_period} → 实际 ${r.pred_period}`),
    },
    { title: "说明", dataIndex: "note", ellipsis: true },
  ];

  const historyColumns: ColumnsType<SqlInferEvalRunSummary> = [
    {
      title: "运行时间",
      dataIndex: "ran_at",
      width: 180,
      render: (v: string | null) => (v ? new Date(v).toLocaleString() : "—"),
    },
    {
      title: "完全匹配",
      dataIndex: "exact_rate",
      width: 110,
      render: (v: number) => `${pct(v)}%`,
    },
    {
      title: "度量召回率",
      dataIndex: "measure_recall",
      width: 110,
      render: (v: number | null) => (v == null ? "—" : `${pct(v)}%`),
    },
    {
      title: "度量精确率",
      dataIndex: "measure_precision",
      width: 110,
      render: (v: number | null) => (v == null ? "—" : `${pct(v)}%`),
    },
    {
      title: "用例数",
      dataIndex: "total",
      width: 80,
      render: (v: number, r) => `${r.exact_count}/${v}`,
    },
    {
      title: "耗时",
      dataIndex: "elapsed_ms",
      width: 90,
      render: (v: number) => `${v}ms`,
    },
  ];

  const expandCase = (record: SqlInferEvalCase & { note: string; sql: string }) => {
    const sample = data?.dataset?.find((d) => d.case_id === record.case_id);
    return (
      <div style={{ padding: "8px 4px" }}>
        <Paragraph type="secondary">{record.note}</Paragraph>
        <Collapse
          size="small"
          items={[
            {
              key: "sql",
              label: "样本 SQL",
              children: (
                <pre style={{ maxHeight: 320, overflow: "auto", fontSize: 12, whiteSpace: "pre-wrap" }}>
                  {record.sql}
                </pre>
              ),
            },
            {
              key: "detail",
              label: "期望 vs 实际（度量/表/周期）",
              children: (
                <Space direction="vertical" size={4} style={{ width: "100%" }}>
                  <Text>期望度量：{sample?.expected_measures?.join("；") ?? "—"}</Text>
                  <Text>期望表：{sample?.expected_tables?.join("，") ?? "—"}</Text>
                  <Text>期望周期：{sample?.expected_period ?? "—"}</Text>
                  {record.missing_measures.length > 0 && (
                    <Text type="danger">缺失度量：{record.missing_measures.join("；")}</Text>
                  )}
                  {record.extra_measures.length > 0 && (
                    <Text type="warning">多余度量：{record.extra_measures.join("；")}</Text>
                  )}
                  {record.missing_tables.length > 0 && (
                    <Text type="danger">缺失表：{record.missing_tables.join("，")}</Text>
                  )}
                  {record.extra_tables.length > 0 && (
                    <Text type="warning">多余表：{record.extra_tables.join("，")}</Text>
                  )}
                  {record.period_match === false && (
                    <Text type="danger">周期不符：期望 {record.expected_period}，实际 {record.pred_period}</Text>
                  )}
                </Space>
              ),
            },
          ]}
        />
      </div>
    );
  };

  return (
    <div style={{ padding: 16 }}>
      <Card
        title={
          <Space>
            <Title level={5} style={{ margin: 0 }}>
              SQL 智能推断解析成功率
            </Title>
            <Tag color="blue">规则解析评测集</Tag>
          </Space>
        }
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
              刷新
            </Button>
            <Button
              type="primary"
              icon={<PlayCircleOutlined />}
              onClick={() => void handleRun()}
              loading={running}
            >
              运行评测并记录
            </Button>
          </Space>
        }
      >
        {error && (
          <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />
        )}
        {!data && loading ? (
          <div style={{ textAlign: "center", padding: 48 }}>
            <Spin size="large" />
          </div>
        ) : !report ? (
          <Empty description="评测数据为空" />
        ) : (
          <>
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message={
                latest
                  ? `最近一次记录：${new Date(latest.ran_at ?? "").toLocaleString()} · 完全匹配 ${pct(latest.exact_rate)}% · 耗时 ${latest.elapsed_ms}ms`
                  : "评测集为确定性计算（规则解析 vs 人工核对期望），本页实时计算当前成功率；点「运行评测并记录」留存历史用于趋势。"
              }
            />
            <Row gutter={[12, 12]}>
              <Col span={8}>
                <Card size="small">
                  <Progress
                    type="dashboard"
                    percent={pct(report.exact_rate)}
                    strokeColor={metricColor(pct(report.exact_rate))}
                    format={(p) => <span style={{ fontSize: 20 }}>{p}%</span>}
                  />
                  <div style={{ textAlign: "center" }}>
                    <Text strong>端到端完全匹配率</Text>
                    <br />
                    <Text type="secondary">
                      {report.exact_count}/{report.total} 用例 度量+表+周期全等
                    </Text>
                  </div>
                </Card>
              </Col>
              <Col span={16}>
                <Row gutter={[12, 12]}>
                  <Col span={8}>
                    <Statistic
                      title="度量级召回率"
                      value={pct(report.measure_recall)}
                      suffix="%"
                      valueStyle={{ color: metricColor(pct(report.measure_recall)) }}
                    />
                  </Col>
                  <Col span={8}>
                    <Statistic
                      title="度量级精确率"
                      value={pct(report.measure_precision)}
                      suffix="%"
                      valueStyle={{ color: metricColor(pct(report.measure_precision)) }}
                    />
                  </Col>
                  <Col span={8}>
                    <Statistic
                      title="表级召回率"
                      value={pct(report.table_recall)}
                      suffix="%"
                      valueStyle={{ color: metricColor(pct(report.table_recall)) }}
                    />
                  </Col>
                  <Col span={8}>
                    <Statistic
                      title="表级精确率"
                      value={pct(report.table_precision)}
                      suffix="%"
                      valueStyle={{ color: metricColor(pct(report.table_precision)) }}
                    />
                  </Col>
                  <Col span={8}>
                    <Statistic
                      title="周期匹配率"
                      value={pct(report.period_match_rate)}
                      suffix="%"
                      valueStyle={{ color: metricColor(pct(report.period_match_rate)) }}
                    />
                  </Col>
                  <Col span={8}>
                    <Statistic
                      title="评测样本数"
                      value={report.total}
                      suffix="条"
                    />
                  </Col>
                </Row>
              </Col>
            </Row>
          </>
        )}
      </Card>

      <Card title="成功率历史趋势" style={{ marginTop: 16 }}>
        {data && data.history.length > 0 ? (
          <Table
            rowKey="id"
            size="small"
            columns={historyColumns}
            dataSource={data.history}
            pagination={{ pageSize: 10 }}
          />
        ) : (
          <Empty description="尚无运行记录——点「运行评测并记录」留存首次基线" />
        )}
      </Card>

      <Card title="评测样本明细" style={{ marginTop: 16 }}>
        <Table
          rowKey="case_id"
          size="small"
          columns={caseColumns}
          dataSource={caseRows}
          expandable={{ expandedRowRender: expandCase }}
          pagination={false}
        />
      </Card>
    </div>
  );
}
