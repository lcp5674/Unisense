import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Divider,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
  App,
} from "antd";
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import {
  createEvalSample,
  deleteEvalSample,
  getSqlInferEval,
  listEvalSamples,
  previewEvalSample,
  runSqlInferEval,
  updateEvalSample,
} from "../api";
import type {
  EvalSample,
  EvalSampleIn,
  EvalSamplePreview,
  SqlInferEvalCase,
  SqlInferEvalData,
  SqlInferEvalExpectedMeasure,
  SqlInferEvalRunSummary,
} from "../types";

const { Paragraph, Text, Title } = Typography;

const AGG_OPTIONS = [
  "SUM",
  "AVG",
  "COUNT",
  "COUNT_DISTINCT",
  "LAST_VALUE",
  "FIRST_VALUE",
  "MAX",
  "MIN",
  "MEDIAN",
  "PERCENTILE",
];
const PERIOD_OPTIONS = ["hour", "day", "week", "month", "quarter", "year"];

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

/** "期望 vs 实际"对照表行（度量/源表/周期 三行）。 */
interface EvalDetailRow {
  key: string;
  dim: string;
  expected: string[];
  actual: string[];
  /** 期望有、实际无（期望列标红）。 */
  missing: string[];
  /** 实际有、期望无（实际列标橙）。 */
  extra: string[];
  matched: boolean;
  /** 判定列文案（匹配 / N 缺失 · M 多余 / 期望→实际 / —）。 */
  verdict: string;
}

/** 标签列表：命中高亮集合的项按指定色标记（缺失红 / 多余橙）。 */
function EvalTagList({
  list,
  highlight,
  color,
  emptyText = "—",
}: {
  list: string[];
  highlight: Set<string>;
  color?: string;
  emptyText?: string;
}) {
  if (!list.length) return <Text type="secondary">{emptyText}</Text>;
  return (
    <Space size={[0, 4]} wrap>
      {list.map((t) => (
        <Tag key={t} color={highlight.has(t) ? color : undefined}>
          {t}
        </Tag>
      ))}
    </Space>
  );
}

/** 判定列：匹配绿色 / 差异红色 / 无判定灰色。 */
function EvalVerdict({ matched, verdict }: { matched: boolean; verdict: string }) {
  if (matched) return <Tag color="success">匹配</Tag>;
  if (verdict === "—") return <Text type="secondary">—</Text>;
  return <Tag color="error">{verdict}</Tag>;
}

export function SqlInferEval() {
  const { message } = App.useApp();
  const [data, setData] = useState<SqlInferEvalData | null>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // ---- 自定义样本管理 ----
  const [samples, setSamples] = useState<EvalSample[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<EvalSample | null>(null);
  const [saving, setSaving] = useState(false);
  const [preview, setPreview] = useState<EvalSamplePreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [form] = Form.useForm();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [res, sampleRes] = await Promise.all([getSqlInferEval(), listEvalSamples()]);
      setData(res);
      setSamples(sampleRes.items);
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

  const openCreate = () => {
    setEditing(null);
    setPreview(null);
    form.resetFields();
    setModalOpen(true);
  };

  const openEdit = (s: EvalSample) => {
    setEditing(s);
    setPreview(null);
    form.setFieldsValue({
      case_id: s.case_id,
      dialect: s.dialect,
      sql: s.sql,
      expected_period: s.expected_period,
      expected_measures: s.expected_measures.length
        ? s.expected_measures
        : [{ column: "", agg: "SUM", alias: "", table: "" }],
      expected_tables: s.expected_tables,
      note: s.note,
    });
    setModalOpen(true);
  };

  const handlePreview = async () => {
    const sql = form.getFieldValue("sql");
    if (!sql || !String(sql).trim()) {
      message.warning("请先填写样本 SQL 再预览解析");
      return;
    }
    setPreviewLoading(true);
    try {
      const res = await previewEvalSample(String(sql));
      setPreview(res);
    } catch (e) {
      message.error(e instanceof Error ? `预览失败：${e.message}` : "预览失败");
    } finally {
      setPreviewLoading(false);
    }
  };

  const handleSave = async () => {
    let values: {
      case_id?: string;
      dialect?: string;
      sql?: string;
      expected_period?: string;
      expected_measures?: SqlInferEvalExpectedMeasure[];
      expected_tables?: string[];
      note?: string;
    };
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    const payload: EvalSampleIn = {
      case_id: String(values.case_id ?? "").trim(),
      dialect: String(values.dialect ?? "hive").trim(),
      sql: values.sql ?? "",
      expected_period: values.expected_period ?? "day",
      expected_measures: (values.expected_measures ?? [])
        .map((m) => ({ column: (m.column ?? "").trim(), agg: m.agg, alias: m.alias, table: m.table }))
        .filter((m) => m.column),
      expected_tables: (values.expected_tables ?? []).map((t) => String(t).trim()).filter(Boolean),
      note: values.note ?? "",
    };
    setSaving(true);
    try {
      if (editing) {
        await updateEvalSample(editing.id, payload);
        message.success("评测样本已更新");
      } else {
        await createEvalSample(payload);
        message.success("评测样本已创建");
      }
      setModalOpen(false);
      await load();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (s: EvalSample) => {
    try {
      await deleteEvalSample(s.id);
      message.success(`评测样本 ${s.case_id} 已删除`);
      await load();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "删除失败");
    }
  };

  const report = data?.report;
  const latest = data?.latest_run;

  // 用例明细：实际结果（report.cases）与样本（dataset）按 case_id 合并，补来源/操作
  const caseRows = (data?.report.cases ?? []).map((c) => {
    const sample = data?.dataset?.find((d) => d.case_id === c.case_id);
    const custom = samples.find((s) => s.case_id === c.case_id);
    return {
      ...c,
      note: sample?.note ?? "",
      sql: sample?.sql ?? "",
      source: sample?.source ?? (custom ? "custom" : "builtin"),
      customSample: custom ?? null,
    };
  });

  const caseColumns: ColumnsType<
    SqlInferEvalCase & { note: string; sql: string; source: "builtin" | "custom"; customSample: EvalSample | null }
  > = [
    {
      title: "用例",
      dataIndex: "case_id",
      width: 180,
      render: (v: string) => <Text code>{v}</Text>,
    },
    { title: "方言", dataIndex: "dialect", width: 110, render: (v: string) => <DialectTag dialect={v} /> },
    {
      title: "来源",
      dataIndex: "source",
      width: 90,
      render: (v: "builtin" | "custom") =>
        v === "custom" ? <Tag color="purple">自定义</Tag> : <Tag>内置</Tag>,
    },
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
    {
      title: "操作",
      key: "ops",
      width: 90,
      render: (_, r) =>
        r.customSample ? (
          <Space size={4}>
            <Button
              type="link"
              size="small"
              icon={<EditOutlined />}
              onClick={() => openEdit(r.customSample as EvalSample)}
            >
              编辑
            </Button>
            <Popconfirm
              title={`删除评测样本 ${r.case_id}？`}
              description="软删可恢复；将不再参与成功率统计"
              okButtonProps={{ danger: true }}
              onConfirm={() => handleDelete(r.customSample as EvalSample)}
            >
              <Button type="link" size="small" danger icon={<DeleteOutlined />}>
                删除
              </Button>
            </Popconfirm>
          </Space>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
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
    const expMeasures = sample?.expected_measures ?? [];
    const predMeasures = record.pred_measures ?? [];
    const expTables = sample?.expected_tables ?? [];
    const predTables = record.pred_tables ?? [];
    const missingM = record.missing_measures ?? [];
    const extraM = record.extra_measures ?? [];
    const missingT = record.missing_tables ?? [];
    const extraT = record.extra_tables ?? [];
    const mDiff = missingM.length + extraM.length;
    const tDiff = missingT.length + extraT.length;
    const periodOk = record.period_match === true;
    const periodBad = record.period_match === false;
    const rows: EvalDetailRow[] = [
      {
        key: "measures",
        dim: "度量",
        expected: expMeasures,
        actual: predMeasures,
        missing: missingM,
        extra: extraM,
        matched: mDiff === 0,
        verdict: mDiff === 0 ? "匹配" : `${missingM.length} 缺失 · ${extraM.length} 多余`,
      },
      {
        key: "tables",
        dim: "源表",
        expected: expTables,
        actual: predTables,
        missing: missingT,
        extra: extraT,
        matched: tDiff === 0,
        verdict: tDiff === 0 ? "匹配" : `${missingT.length} 缺失 · ${extraT.length} 多余`,
      },
      {
        key: "period",
        dim: "周期",
        expected: record.expected_period ? [record.expected_period] : [],
        actual: record.pred_period ? [record.pred_period] : [],
        missing: periodBad && record.expected_period ? [record.expected_period] : [],
        extra: periodBad && record.pred_period ? [record.pred_period] : [],
        matched: periodOk,
        verdict: periodOk
          ? "匹配"
          : periodBad
            ? `期望 ${record.expected_period ?? "—"} → 实际 ${record.pred_period ?? "未识别"}`
            : "—",
      },
    ];
    const detailColumns: ColumnsType<EvalDetailRow> = [
      { title: "维度", dataIndex: "dim", width: 64 },
      {
        title: "期望",
        dataIndex: "expected",
        render: (list: string[], r) => (
          <EvalTagList list={list} highlight={new Set(r.missing)} color="error" />
        ),
      },
      {
        title: "实际",
        dataIndex: "actual",
        render: (list: string[], r) => (
          <EvalTagList list={list} highlight={new Set(r.extra)} color="warning" emptyText="未识别" />
        ),
      },
      {
        title: "判定",
        dataIndex: "verdict",
        width: 130,
        render: (v: string, r) => <EvalVerdict matched={r.matched} verdict={v} />,
      },
    ];
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
                <>
                  <Space size={16} wrap style={{ marginBottom: 8 }}>
                    <Text type="secondary">
                      <Tag color="error" style={{ marginRight: 4 }}>
                        缺失
                      </Tag>
                      期望有、实际未解析出
                    </Text>
                    <Text type="secondary">
                      <Tag color="warning" style={{ marginRight: 4 }}>
                        多余
                      </Tag>
                      实际解析出、期望未声明
                    </Text>
                  </Space>
                  <Table
                    size="small"
                    rowKey="key"
                    pagination={false}
                    columns={detailColumns}
                    dataSource={rows}
                  />
                </>
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

      <Card
        title="评测样本明细"
        style={{ marginTop: 16 }}
        extra={
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            新增样本
          </Button>
        }
      >
        <Table
          rowKey="case_id"
          size="small"
          columns={caseColumns}
          dataSource={caseRows}
          expandable={{ expandedRowRender: expandCase }}
          pagination={false}
        />
      </Card>

      <Modal
        title={editing ? `编辑评测样本：${editing.case_id}` : "新增评测样本"}
        open={modalOpen}
        onOk={() => void handleSave()}
        onCancel={() => setModalOpen(false)}
        confirmLoading={saving}
        width={820}
        okText="保存"
        cancelText="取消"
        destroyOnClose
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{
            dialect: "hive",
            expected_period: "day",
            // 默认空度量列表：期望度量可留空（样本只校验表/周期），用户点「添加度量」再填
            expected_measures: [],
            expected_tables: [],
          }}
        >
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item
                name="case_id"
                label="样本编码"
                rules={[{ required: true, message: "请输入样本编码" }]}
              >
                <Input placeholder="如 my_case（唯一，与内置基线不可重复）" disabled={!!editing} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="dialect" label="方言">
                <Select
                  options={[
                    "hive", "spark", "oracle", "clickhouse", "trino", "postgres",
                    "mysql", "doris", "starrocks", "other",
                  ].map((d) => ({ value: d, label: d === "other" ? "其他" : d }))}
                />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item
            name="sql"
            label="样本 SQL"
            rules={[{ required: true, message: "请输入样本 SQL" }]}
          >
            <Input.TextArea rows={6} placeholder="粘贴完整 SQL 脚本（多语句 ETL / 方言写法）" />
          </Form.Item>
          <Space style={{ marginBottom: preview ? 12 : 0 }}>
            <Button icon={<ThunderboltOutlined />} loading={previewLoading} onClick={() => void handlePreview()}>
              预览解析
            </Button>
            {!preview && <Text type="secondary">先预览规则实际解析结果，再填写期望对照</Text>}
          </Space>
          {preview && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message={`规则解析实际画像：度量 ${preview.measures.length} 个 · 源表 ${preview.source_tables.length} 个 · 周期 ${preview.period ?? "—"}`}
              description={
                <Space direction="vertical" size={2}>
                  {preview.measures.map((m, i) => (
                    <Text key={i} code>
                      {m.column} · {m.agg ?? "DERIVED"}
                      {m.alias ? `（${m.alias}）` : ""}
                      {m.table ? ` ← ${m.table}` : ""}
                    </Text>
                  ))}
                  {preview.measures.length === 0 && (
                    <Text type="warning">
                      未解析出聚合度量——期望度量若填非空，保存后该样本将记为失败（可入库追踪待修缺口）
                    </Text>
                  )}
                  <Text type="secondary">源表：{preview.source_tables.join("，") || "—"}</Text>
                </Space>
              }
            />
          )}
          <Divider />
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item
                name="expected_period"
                label="期望周期"
                rules={[{ required: true, message: "请选择期望周期" }]}
              >
                <Select options={PERIOD_OPTIONS.map((p) => ({ value: p, label: p }))} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="expected_tables" label="期望源表（回车添加）">
                <Select mode="tags" placeholder="如 ods.orders" tokenSeparators={[","]} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item label="期望度量（列名 + 聚合方式；留空表示该样本只校验表/周期）">
            <Form.List name="expected_measures">
              {(fields, { add, remove }) => (
                <>
                  {fields.map(({ key, name }) => (
                    <Space key={key} align="baseline" style={{ display: "flex", marginBottom: 4 }}>
                      <Form.Item
                        name={[name, "column"]}
                        noStyle
                        rules={[{ required: true, message: "列名必填" }]}
                      >
                        <Input placeholder="度量列" style={{ width: 170 }} />
                      </Form.Item>
                      <Form.Item name={[name, "agg"]} noStyle>
                        <Select
                          placeholder="聚合"
                          style={{ width: 160 }}
                          options={AGG_OPTIONS.map((a) => ({ value: a, label: a }))}
                          allowClear
                        />
                      </Form.Item>
                      <Form.Item name={[name, "alias"]} noStyle>
                        <Input placeholder="别名（可选）" style={{ width: 130 }} />
                      </Form.Item>
                      <Form.Item name={[name, "table"]} noStyle>
                        <Input placeholder="源表（可选）" style={{ width: 140 }} />
                      </Form.Item>
                      <Button
                        type="text"
                        danger
                        icon={<DeleteOutlined />}
                        onClick={() => remove(name)}
                        aria-label="删除度量行"
                      />
                    </Space>
                  ))}
                  <Button
                    type="dashed"
                    block
                    icon={<PlusOutlined />}
                    onClick={() => add({ column: "", agg: "SUM", alias: "", table: "" })}
                  >
                    添加度量
                  </Button>
                </>
              )}
            </Form.List>
          </Form.Item>
          <Form.Item name="note" label="说明">
            <Input.TextArea rows={2} placeholder="样本说明（缺陷场景 / 期望行为）" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
