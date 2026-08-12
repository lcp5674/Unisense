import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, message, Space } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import { listTemplates, createMetric, UnisenseApiError } from "../api";
import type { MetricCreateRequest, MetricTemplate, MetricType } from "../types";
import { useTracking } from "../hooks/useTracking";

export function Templates() {
  const [items, setItems] = useState<MetricTemplate[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [instantiateTarget, setInstantiateTarget] = useState<MetricTemplate | null>(null);
  const [form] = Form.useForm();
  const navigate = useNavigate();
  const { track } = useTracking();

  async function load() {
    setLoading(true);
    try {
      setItems(await listTemplates({ is_active: true }));
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message} (${err.code})` : "加载模板失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleCreate(values: Record<string, unknown>) {
    setLoading(true);
    try {
      const created = await createMetric({
        metric_code: String(values.metric_code),
        name: String(values.name),
        domain: String(values.domain),
        type: (String(values.type) as MetricType) ?? "atomic",
        granularity: String(values.granularity || "daily"),
        unit: String(values.unit || ""),
        aggregation: (String(values.aggregation) as MetricCreateRequest["aggregation"]) ?? "SUM",
        time_semantics: (String(values.time_semantics) as MetricCreateRequest["time_semantics"]) ?? "PERIOD",
        freshness: (String(values.freshness) as MetricCreateRequest["freshness"]) ?? "T1",
        dw_layer: (String(values.dw_layer) as MetricCreateRequest["dw_layer"]) ?? "DWS",
        definition_json: {},
      });
      message.success(`已按模板创建：${created.metric_code}`);
      track("template_instantiate", created.metric_code, "template");
      setModalOpen(false);
      navigate(`/detail/${created.metric_code}`);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message} (${err.code})` : "创建失败");
    } finally {
      setLoading(false);
    }
  }

  function openInstantiate(tpl: MetricTemplate) {
    setInstantiateTarget(tpl);
    form.resetFields();
    form.setFieldsValue({
      metric_code: tpl.code,
      name: tpl.name,
      domain: tpl.domain,
      type: tpl.type ?? "atomic",
      granularity: tpl.granularity ?? "daily",
      unit: tpl.unit ?? "",
      aggregation: tpl.aggregation ?? "SUM",
      time_semantics: tpl.time_semantics ?? "PERIOD",
      freshness: tpl.freshness ?? "T1",
      dw_layer: tpl.dw_layer ?? "DWS",
    });
    setModalOpen(true);
  }

  const columns = [
    { title: "模板编码", dataIndex: "code", key: "code", render: (v: string) => <span className="mono">{v}</span> },
    { title: "名称", dataIndex: "name", key: "name" },
    { title: "域", dataIndex: "domain", key: "domain", width: 140 },
    { title: "类型", dataIndex: "type", key: "type", width: 100 },
    { title: "粒度", dataIndex: "granularity", key: "granularity", width: 100 },
    { title: "聚合", dataIndex: "aggregation", key: "aggregation", width: 120 },
    { title: "分级", dataIndex: "metric_tier", key: "metric_tier", width: 90, render: (v: string) => <Tag>{v}</Tag> },
    { title: "必填字段", dataIndex: "required_fields", key: "required_fields", render: (v: string[] | null) => (v?.length ? v.join("、") : <span className="muted">—</span>) },
    {
      title: "操作",
      key: "actions",
      width: 140,
      render: (_: unknown, t: MetricTemplate) => (
        <Button type="link" onClick={() => openInstantiate(t)}>实例化指标</Button>
      ),
    },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Assets / Templates</div>
          <h2>指标模板</h2>
          <p>标准化的指标创建模板——一键实例化，默认口径自动合并。</p>
        </div>
        <Button icon={<PlusOutlined />} onClick={load} loading={loading}>刷新</Button>
      </div>

      <Card>
        <Table
          dataSource={items}
          columns={columns}
          rowKey="id"
          loading={loading}
          pagination={false}
          locale={{ emptyText: "暂无模板" }}
        />
      </Card>

      <Modal
        title={instantiateTarget ? `从模板实例化：${instantiateTarget.name}` : "创建指标"}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.submit()}
        okText="实例化创建"
        confirmLoading={loading}
        width={560}
      >
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Space style={{ width: "100%" }} wrap>
            <Form.Item name="metric_code" label="指标编码" rules={[{ required: true }]} style={{ width: 240 }}>
              <Input className="mono" />
            </Form.Item>
            <Form.Item name="name" label="名称" rules={[{ required: true }]} style={{ width: 260 }}>
              <Input />
            </Form.Item>
            <Form.Item name="domain" label="业务域" rules={[{ required: true }]} style={{ width: 240 }}>
              <Input />
            </Form.Item>
            <Form.Item name="type" label="类型" style={{ width: 240 }}>
              <Select options={[{ value: "atomic", label: "atomic" }, { value: "derived", label: "derived" }, { value: "composite", label: "composite" }]} />
            </Form.Item>
            <Form.Item name="granularity" label="粒度" style={{ width: 240 }}>
              <Input />
            </Form.Item>
            <Form.Item name="unit" label="单位" style={{ width: 240 }}>
              <Input />
            </Form.Item>
            <Form.Item name="aggregation" label="聚合" style={{ width: 240 }}>
              <Select options={["SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE"].map((v) => ({ value: v, label: v }))} />
            </Form.Item>
            <Form.Item name="time_semantics" label="时间语义" style={{ width: 240 }}>
              <Select options={["PERIOD", "YTD", "TTM", "AVG"].map((v) => ({ value: v, label: v }))} />
            </Form.Item>
            <Form.Item name="freshness" label="新鲜度" style={{ width: 240 }}>
              <Select options={["REALTIME", "T1", "HOURLY"].map((v) => ({ value: v, label: v }))} />
            </Form.Item>
            <Form.Item name="dw_layer" label="数仓层" style={{ width: 240 }}>
              <Select options={["ODS", "DWD", "DWS", "ADS", "DM"].map((v) => ({ value: v, label: v }))} />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </div>
  );
}
