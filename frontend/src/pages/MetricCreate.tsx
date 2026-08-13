import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Button,
  Card,
  Checkbox,
  Form,
  Input,
  Segmented,
  Select,
  Space,
  Typography,
  message,
} from "antd";
import { createMetric, fetchAssetSearch, UnisenseApiError } from "../api";
import type { MetricCreateRequest, MetricType, MetricTier } from "../types";

const { Title, Paragraph } = Typography;
const { TextArea } = Input;

export function MetricCreate() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [form] = Form.useForm();
  // 口径录入模式：expression（结构化 JSON）↔ sql（SQL 语句）
  const [mode, setMode] = useState<"expression" | "sql">("expression");
  const [sqlText, setSqlText] = useState("");
  const [sourceTables, setSourceTables] = useState<string[]>([]);
  const [tableOptions, setTableOptions] = useState<{ value: string; label: string }[]>([]);
  const [tableSearching, setTableSearching] = useState(false);

  async function searchTables(q: string) {
    if (!q.trim()) return;
    setTableSearching(true);
    try {
      const res = await fetchAssetSearch({ q: q.trim(), type: "table", limit: 20 });
      setTableOptions(
        res.items.map((it) => ({ value: it.name, label: it.name })),
      );
    } catch {
      setTableOptions([]);
    } finally {
      setTableSearching(false);
    }
  }

  function buildDefinitionJson(values: Record<string, unknown>): Record<string, unknown> | null {
    const tables = sourceTables.length ? { source_tables: sourceTables } : {};
    if (mode === "sql") {
      const sql = sqlText.trim();
      if (!sql) {
        message.error("口径 SQL 模式请输入 SQL 语句");
        return null;
      }
      return { sql, ...tables };
    }
    let def: Record<string, unknown>;
    try {
      def = values.definition ? JSON.parse(String(values.definition)) : {};
    } catch {
      message.error("口径定义需为合法 JSON");
      return null;
    }
    return { ...def, ...tables };
  }

  async function handleSubmit(values: Record<string, unknown>) {
    setLoading(true);
    const definitionJson = buildDefinitionJson(values);
    if (!definitionJson) {
      setLoading(false);
      return;
    }
    const req: MetricCreateRequest = {
      metric_code: String(values.metric_code),
      name: String(values.name),
      domain: String(values.domain),
      type: String(values.type) as MetricType,
      granularity: String(values.granularity || "daily"),
      unit: String(values.unit || ""),
      aggregation: String(values.aggregation) as MetricCreateRequest["aggregation"],
      time_semantics: String(values.time_semantics) as MetricCreateRequest["time_semantics"],
      freshness: String(values.freshness) as MetricCreateRequest["freshness"],
      dw_layer: String(values.dw_layer) as MetricCreateRequest["dw_layer"],
      metric_tier: String(values.metric_tier || "T2") as MetricTier,
      definition_json: definitionJson,
      pii_flag: Boolean(values.pii_flag),
    };
    try {
      const created = await createMetric(req);
      message.success(`创建草稿成功：${created.metric_code}`);
      navigate(`/detail/${created.metric_code}`);
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message} (${err.code})` : "创建失败",
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <Title level={3}>注册指标（草稿）</Title>
      <Card>
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSubmit}
          initialValues={{
            type: "atomic",
            granularity: "daily",
            aggregation: "SUM",
            time_semantics: "PERIOD",
            freshness: "T1",
            dw_layer: "DWS",
            metric_tier: "T2",
            pii_flag: false,
          }}
        >
          <Space style={{ width: "100%" }} direction="vertical" size="middle">
            <Form.Item name="metric_code" label="指标编码" rules={[{ required: true }]}>
              <Input />
            </Form.Item>
            <Form.Item name="name" label="名称" rules={[{ required: true }]}>
              <Input />
            </Form.Item>
            <Form.Item name="domain" label="业务域" rules={[{ required: true }]}>
              <Input />
            </Form.Item>
            <Form.Item name="type" label="类型">
              <Select
                options={[
                  { value: "atomic", label: "atomic" },
                  { value: "derived", label: "derived" },
                  { value: "composite", label: "composite" },
                ]}
              />
            </Form.Item>
            <Form.Item name="granularity" label="粒度">
              <Input />
            </Form.Item>
            <Form.Item name="unit" label="单位">
              <Input />
            </Form.Item>
            <Form.Item name="aggregation" label="聚合">
              <Select
                options={[
                  { value: "SUM", label: "SUM" },
                  { value: "AVG", label: "AVG" },
                  { value: "COUNT", label: "COUNT" },
                  { value: "COUNT_DISTINCT", label: "COUNT_DISTINCT" },
                  { value: "LAST_VALUE", label: "LAST_VALUE" },
                ]}
              />
            </Form.Item>
            <Form.Item name="time_semantics" label="时间语义">
              <Select
                options={[
                  { value: "PERIOD", label: "PERIOD" },
                  { value: "YTD", label: "YTD" },
                  { value: "TTM", label: "TTM" },
                  { value: "AVG", label: "AVG" },
                ]}
              />
            </Form.Item>
            <Form.Item name="freshness" label="新鲜度">
              <Select
                options={[
                  { value: "REALTIME", label: "REALTIME" },
                  { value: "T1", label: "T1" },
                  { value: "HOURLY", label: "HOURLY" },
                ]}
              />
            </Form.Item>
            <Form.Item name="dw_layer" label="数仓层">
              <Select
                options={[
                  { value: "ODS", label: "ODS" },
                  { value: "DWD", label: "DWD" },
                  { value: "DWS", label: "DWS" },
                  { value: "ADS", label: "ADS" },
                  { value: "DM", label: "DM" },
                ]}
              />
            </Form.Item>
            <Form.Item name="metric_tier" label="分级">
              <Select
                options={[
                  { value: "T1", label: "T1" },
                  { value: "T2", label: "T2" },
                  { value: "T3", label: "T3" },
                ]}
              />
            </Form.Item>
            <Form.Item name="pii_flag" label="含 PII" valuePropName="checked">
              <Checkbox>含 PII</Checkbox>
            </Form.Item>

            {/* 关联数据表：复用资产地图搜索，锚定指标取数来源 */}
            <Form.Item label="关联数据表">
              <Select
                mode="multiple"
                allowClear
                showSearch
                placeholder="搜索并选择口径引用的数据表（如 catalog.sales.orders）"
                value={sourceTables}
                onChange={(v: string[]) => setSourceTables(v)}
                onSearch={searchTables}
                loading={tableSearching}
                notFoundContent={null}
                options={tableOptions}
                filterOption={false}
              />
            </Form.Item>

            {/* 口径定义：表达式 ↔ SQL 双模式 */}
            <Form.Item label="口径定义">
              <Segmented
                block
                value={mode}
                onChange={(v) => setMode(v as "expression" | "sql")}
                options={[
                  { value: "expression", label: "表达式（结构化）" },
                  { value: "sql", label: "SQL 模式" },
                ]}
              />
            </Form.Item>
            {mode === "expression" ? (
              <Form.Item name="definition" label="口径定义 (JSON)">
                <TextArea
                  rows={5}
                  placeholder='{"expr": "sum(amount)", "filters": [], "source_fields": ["orders.amount"]}'
                />
              </Form.Item>
            ) : (
              <Form.Item label="口径 SQL">
                <TextArea
                  rows={5}
                  value={sqlText}
                  onChange={(e) => setSqlText(e.target.value)}
                  placeholder={"SELECT SUM(amount) AS gmv\nFROM catalog.sales.orders\nWHERE dt >= '2026-01-01'"}
                  className="mono"
                />
                <Paragraph type="secondary" style={{ marginTop: 4, fontSize: 12 }}>
                  后端将用 sqlglot 校验 SQL 语法；不合法将拒绝提交。
                </Paragraph>
              </Form.Item>
            )}

            <Form.Item>
              <Button type="primary" htmlType="submit" loading={loading}>
                创建草稿
              </Button>
            </Form.Item>
          </Space>
        </Form>
      </Card>
    </div>
  );
}
