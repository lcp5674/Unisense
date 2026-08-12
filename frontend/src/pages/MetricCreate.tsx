import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Button,
  Card,
  Checkbox,
  Form,
  Input,
  Select,
  message,
  Space,
  Typography,
} from "antd";
import { createMetric, UnisenseApiError } from "../api";
import type { MetricCreateRequest, MetricType, MetricTier } from "../types";

const { Title } = Typography;
const { TextArea } = Input;

export function MetricCreate() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [form] = Form.useForm();

  async function handleSubmit(values: Record<string, unknown>) {
    setLoading(true);
    let definitionJson: Record<string, unknown>;
    try {
      definitionJson = values.definition ? JSON.parse(String(values.definition)) : {};
    } catch {
      message.error("口径定义需为合法 JSON");
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
            <Form.Item name="definition" label="口径定义 (JSON)">
              <TextArea
                rows={5}
                placeholder='{"expr": "sum(amount)", "filters": []}'
              />
            </Form.Item>
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
