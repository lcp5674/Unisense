import { useEffect, useState } from "react";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, message, Tabs, Space } from "antd";
import { PlusOutlined, SendOutlined } from "@ant-design/icons";
import {
  listDimensions,
  createDimension,
  publishDimension,
  deprecateDimension,
  listDimensionMappings,
  createDimensionMapping,
  listReconciliations,
  submitReconciliation,
  reviewReconciliation,
  listDimensionMembers,
  createDimensionMember,
  listMetrics,
  UnisenseApiError,
} from "../api";
import type { Dimension, DimensionMapping, Reconciliation, DimensionMember, MetricResponse } from "../types";

const STATUS_COLOR: Record<string, string> = { DRAFT: "default", PUBLISHED: "success", DEPRECATED: "error" };
const STATUS_LABEL: Record<string, string> = { DRAFT: "草稿", PUBLISHED: "已发布", DEPRECATED: "已废弃" };
const RECON_STATUS_LABEL: Record<string, string> = {
  PENDING: "待复核",
  APPROVED: "已通过",
  REJECTED: "已驳回",
};

function DimensionsTab() {
  const [items, setItems] = useState<Dimension[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();

  async function load() {
    setLoading(true);
    try {
      const res = await listDimensions();
      setItems(res.items);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleCreate(values: Record<string, unknown>) {
    try {
      await createDimension({
        dim_code: String(values.dim_code),
        name: String(values.name),
        domain: String(values.domain),
        type: String(values.type ?? "SCD1"),
        description: values.description ? String(values.description) : null,
      });
      message.success("维度已创建");
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "创建失败");
    }
  }

  async function handlePublish(d: Dimension) {
    try {
      await publishDimension(d.dim_code);
      message.success("已发布");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "发布失败");
    }
  }

  async function handleDeprecate(d: Dimension) {
    try {
      await deprecateDimension(d.dim_code);
      message.success("已废弃");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "废弃失败");
    }
  }

  const columns = [
    { title: "编码", dataIndex: "dim_code", key: "dim_code", render: (v: string) => <span className="mono">{v}</span> },
    { title: "名称", dataIndex: "name", key: "name" },
    { title: "域", dataIndex: "domain", key: "domain", width: 130 },
    { title: "类型", dataIndex: "type", key: "type", width: 90, render: (v: string) => <Tag>{v}</Tag> },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (s: string) => <Tag color={STATUS_COLOR[s]}>{STATUS_LABEL[s] ?? s}</Tag>,
    },
    {
      title: "操作",
      key: "actions",
      width: 150,
      render: (_: unknown, d: Dimension) =>
        d.status !== "DEPRECATED" ? (
          <Space>
            {d.status !== "PUBLISHED" && <Button size="small" type="primary" onClick={() => handlePublish(d)}>发布</Button>}
            <Button size="small" danger onClick={() => handleDeprecate(d)}>废弃</Button>
          </Space>
        ) : (
          <Tag>已停用</Tag>
        ),
    },
  ];

  return (
    <div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "flex-end" }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>新建维度</Button>
      </div>
      <Table dataSource={items} columns={columns} rowKey="dim_code" loading={loading} pagination={false} locale={{ emptyText: "暂无维度" }} />

      <Modal title="新建维度" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="创建">
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="dim_code" label="维度编码" rules={[{ required: true }]}>
            <Input className="mono" placeholder="如 dim_channel" />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如 渠道" />
          </Form.Item>
          <Form.Item name="domain" label="业务域" rules={[{ required: true }]}>
            <Input placeholder="如 finance" />
          </Form.Item>
          <Form.Item name="type" label="缓慢变化维类型">
            <Select options={[{ value: "SCD1", label: "SCD1 覆盖" }, { value: "SCD2", label: "SCD2 历史" }]} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

function MembersTab() {
  const [dims, setDims] = useState<Dimension[]>([]);
  const [dimCode, setDimCode] = useState<string | undefined>(undefined);
  const [members, setMembers] = useState<DimensionMember[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();

  useEffect(() => {
    listDimensions().then((r) => setDims(r.items)).catch(() => {});
  }, []);

  useEffect(() => {
    if (!dimCode) return;
    setLoading(true);
    listDimensionMembers(dimCode)
      .then((r) => setMembers(r.items))
      .catch((err) => message.error(err instanceof UnisenseApiError ? err.message : "加载成员失败"))
      .finally(() => setLoading(false));
  }, [dimCode]);

  async function handleCreate(values: Record<string, unknown>) {
    if (!dimCode) return;
    try {
      await createDimensionMember({
        dim_code: dimCode,
        member_code: String(values.member_code),
        member_name: String(values.member_name),
        parent_code: values.parent_code ? String(values.parent_code) : null,
        path: values.path ? String(values.path) : null,
      });
      message.success("成员已创建");
      setModalOpen(false);
      form.resetFields();
      setLoading(true);
      listDimensionMembers(dimCode).then((r) => setMembers(r.items)).finally(() => setLoading(false));
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "创建失败");
    }
  }

  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <Select
          placeholder="选择维度"
          style={{ width: 260 }}
          value={dimCode}
          onChange={setDimCode}
          options={dims.map((d) => ({ value: d.dim_code, label: `${d.dim_code} · ${d.name}` }))}
        />
        <Button icon={<PlusOutlined />} disabled={!dimCode} onClick={() => setModalOpen(true)}>新增成员</Button>
      </Space>
      <Table
        dataSource={members}
        rowKey="member_code"
        loading={loading}
        size="small"
        pagination={false}
        locale={{ emptyText: "请选择维度查看成员" }}
        columns={[
          { title: "成员编码", dataIndex: "member_code", key: "member_code", render: (v: string) => <span className="mono">{v}</span> },
          { title: "名称", dataIndex: "member_name", key: "member_name" },
          { title: "父级", dataIndex: "parent_code", key: "parent_code", render: (v: string | null) => v ?? <span className="muted">—</span> },
          { title: "路径", dataIndex: "path", key: "path", render: (v: string | null) => v && <span className="mono">{v}</span> },
          {
            title: "状态",
            dataIndex: "status",
            key: "status",
            width: 100,
            render: (s: string) => <Tag color={STATUS_COLOR[s]}>{STATUS_LABEL[s] ?? s}</Tag>,
          },
        ]}
      />

      <Modal title="新增维度成员" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="创建">
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="member_code" label="成员编码" rules={[{ required: true }]}>
            <Input className="mono" />
          </Form.Item>
          <Form.Item name="member_name" label="成员名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="parent_code" label="父级编码">
            <Input className="mono" />
          </Form.Item>
          <Form.Item name="path" label="层级路径">
            <Input className="mono" placeholder="如 /渠道/线上" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

function MappingsTab() {
  const [items, setItems] = useState<DimensionMapping[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();

  async function load() {
    setLoading(true);
    try {
      const res = await listDimensionMappings();
      setItems(res.items);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleCreate(values: Record<string, unknown>) {
    try {
      await createDimensionMapping({
        source_dim_code: String(values.source_dim_code),
        target_dim_code: String(values.target_dim_code),
        mapping_type: String(values.mapping_type ?? "EQUIVALENT"),
        expression: values.expression ? String(values.expression) : null,
      });
      message.success("映射已创建");
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "创建失败");
    }
  }

  const columns = [
    { title: "源维度", dataIndex: "source_dim_code", key: "source", render: (v: string) => <span className="mono">{v}</span> },
    { title: "目标维度", dataIndex: "target_dim_code", key: "target", render: (v: string) => <span className="mono">{v}</span> },
    { title: "映射类型", dataIndex: "mapping_type", key: "type", width: 130, render: (v: string) => <Tag color={v === "EQUIVALENT" ? "success" : "warning"}>{v === "EQUIVALENT" ? "等价" : "部分"}</Tag> },
    { title: "表达式", dataIndex: "expression", key: "expr", render: (v: string | null) => v ? <span className="mono">{v}</span> : <span className="muted">—</span> },
  ];

  return (
    <div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "flex-end" }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>新建映射</Button>
      </div>
      <Table dataSource={items} columns={columns} rowKey="id" loading={loading} pagination={false} locale={{ emptyText: "暂无维度映射" }} />

      <Modal title="新建维度映射" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="创建">
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="source_dim_code" label="源维度编码" rules={[{ required: true }]}>
            <Input className="mono" />
          </Form.Item>
          <Form.Item name="target_dim_code" label="目标维度编码" rules={[{ required: true }]}>
            <Input className="mono" />
          </Form.Item>
          <Form.Item name="mapping_type" label="映射类型">
            <Select options={[{ value: "EQUIVALENT", label: "等价" }, { value: "PARTIAL", label: "部分" }]} />
          </Form.Item>
          <Form.Item name="expression" label="映射表达式">
            <Input.TextArea rows={2} className="mono" placeholder="如 CASE WHEN ..." />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

function ReconciliationsTab() {
  const [items, setItems] = useState<Reconciliation[]>([]);
  const [metrics, setMetrics] = useState<MetricResponse[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();

  async function load() {
    setLoading(true);
    try {
      const res = await listReconciliations();
      setItems(res.items);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    listMetrics({ page_size: 100 }).then((r) => setMetrics(r.items)).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleCreate(values: Record<string, unknown>) {
    try {
      await submitReconciliation({
        metric_id: Number(values.metric_id),
        expected_expr: String(values.expected_expr),
        actual_expr: String(values.actual_expr),
        diff_summary: values.diff_summary ? String(values.diff_summary) : null,
      });
      message.success("对账已提交");
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "提交失败");
    }
  }

  async function handleReview(r: Reconciliation, decision: string) {
    try {
      await reviewReconciliation(r.id, decision);
      message.success("复核完成");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "复核失败");
    }
  }

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 70 },
    { title: "指标", dataIndex: "metric_id", key: "metric", width: 90, render: (v: number) => <span className="mono">#{v}</span> },
    { title: "期望口径", dataIndex: "expected_expr", key: "expected", ellipsis: true, render: (v: string) => <span className="mono">{v}</span> },
    { title: "实际口径", dataIndex: "actual_expr", key: "actual", ellipsis: true, render: (v: string) => <span className="mono">{v}</span> },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (s: string) => <Tag color={s === "APPROVED" ? "success" : s === "REJECTED" ? "error" : "warning"}>{RECON_STATUS_LABEL[s] ?? s}</Tag>,
    },
    {
      title: "操作",
      key: "actions",
      width: 150,
      render: (_: unknown, r: Reconciliation) =>
        r.status === "PENDING" ? (
          <Space>
            <Button size="small" type="primary" onClick={() => handleReview(r, "APPROVED")}>通过</Button>
            <Button size="small" danger onClick={() => handleReview(r, "REJECTED")}>驳回</Button>
          </Space>
        ) : null,
    },
  ];

  return (
    <div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "flex-end" }}>
        <Button type="primary" icon={<SendOutlined />} onClick={() => setModalOpen(true)}>提交对账</Button>
      </div>
      <Table dataSource={items} columns={columns} rowKey="id" loading={loading} pagination={false} locale={{ emptyText: "暂无对账记录" }} />

      <Modal title="提交维度对账" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="提交">
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="metric_id" label="指标" rules={[{ required: true }]}>
            <Select showSearch options={metrics.map((m) => ({ value: m.id, label: `${m.metric_code} · ${m.name}` }))} placeholder="选择指标" />
          </Form.Item>
          <Form.Item name="expected_expr" label="期望口径（语义端）" rules={[{ required: true }]}>
            <Input.TextArea rows={2} className="mono" />
          </Form.Item>
          <Form.Item name="actual_expr" label="实际口径（应用端）" rules={[{ required: true }]}>
            <Input.TextArea rows={2} className="mono" />
          </Form.Item>
          <Form.Item name="diff_summary" label="差异摘要">
            <Input />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

export function Dimensions() {
  const tabItems = [
    { key: "dims", label: "维度列表", children: <DimensionsTab /> },
    { key: "members", label: "成员管理", children: <MembersTab /> },
    { key: "mappings", label: "维度映射", children: <MappingsTab /> },
    { key: "reconcile", label: "对账记录", children: <ReconciliationsTab /> },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Governance / Dimensions</div>
          <h2>维度管理</h2>
          <p>维度定义、成员、跨维度映射与口径对账——保证维度语义一致。</p>
        </div>
      </div>
      <Card styles={{ body: { paddingTop: 8 } }}>
        <Tabs items={tabItems} />
      </Card>
    </div>
  );
}
