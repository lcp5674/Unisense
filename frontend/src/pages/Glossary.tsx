import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, message, Tabs, Space } from "antd";
import { PlusOutlined, SendOutlined } from "@ant-design/icons";
import {
  listTerms,
  createTerm,
  submitTerm,
  deprecateTerm,
  listTermConflicts,
  resolveTermConflict,
  UnisenseApiError,
} from "../api";
import type { GlossaryTerm, GlossaryConflict } from "../types";

const STATUS_COLOR: Record<string, string> = { DRAFT: "default", PUBLISHED: "success", DEPRECATED: "error" };
const STATUS_LABEL: Record<string, string> = { DRAFT: "草稿", PUBLISHED: "已发布", DEPRECATED: "已废弃" };
const CONFLICT_TYPE_LABEL: Record<string, string> = {
  alias_overlap: "同义别名冲突",
  name_overlap: "同名冲突",
  definition_overlap: "语义漂移",
};
const CONFLICT_STATUS_LABEL: Record<string, string> = {
  OPEN: "待处理",
  RESOLVED: "已解决",
  IGNORED: "已忽略",
};

function TermsTab() {
  const [items, setItems] = useState<GlossaryTerm[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();
  const [searchParams] = useSearchParams();
  const focusCode = searchParams.get("focus");

  async function load() {
    setLoading(true);
    try {
      const res = await listTerms({ search, status, page, page_size: 20 });
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, status]);

  async function handleCreate(values: Record<string, unknown>) {
    try {
      await createTerm({
        term_code: String(values.term_code),
        name: String(values.name),
        definition: String(values.definition),
        domain: String(values.domain),
        synonyms: values.synonyms ? String(values.synonyms).split(",").map((s) => s.trim()).filter(Boolean) : [],
        boundary: values.boundary ? String(values.boundary) : null,
      });
      message.success("术语已创建（已自动触发冲突检测）");
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "创建失败");
    }
  }

  async function handleSubmit(t: GlossaryTerm) {
    try {
      await submitTerm(t.term_code);
      message.success("已提交发布");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "提交失败");
    }
  }

  async function handleDeprecate(t: GlossaryTerm) {
    try {
      await deprecateTerm(t.term_code);
      message.success("已废弃");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "废弃失败");
    }
  }

  const columns = [
    { title: "编码", dataIndex: "term_code", key: "term_code", render: (v: string) => <span className="mono">{v}</span> },
    { title: "名称", dataIndex: "name", key: "name" },
    { title: "定义", dataIndex: "definition", key: "definition", ellipsis: true },
    { title: "域", dataIndex: "domain", key: "domain", width: 120 },
    { title: "同义词", dataIndex: "synonyms", key: "synonyms", width: 160, render: (v: unknown[]) => (v?.length ? v.join("、") : <span className="muted">—</span>) },
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
      render: (_: unknown, t: GlossaryTerm) =>
        t.status === "DRAFT" ? (
          <Space>
            <Button size="small" type="primary" icon={<SendOutlined />} onClick={() => handleSubmit(t)}>提交</Button>
            <Button size="small" danger onClick={() => handleDeprecate(t)}>废弃</Button>
          </Space>
        ) : (
          <Button size="small" danger onClick={() => handleDeprecate(t)}>废弃</Button>
        ),
    },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Input.Search
          placeholder="搜索术语名/定义/编码"
          allowClear
          style={{ width: 260 }}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onSearch={() => { setPage(1); load(); }}
        />
        <Select
          allowClear
          placeholder="全部状态"
          style={{ width: 140 }}
          value={status || undefined}
          onChange={(v) => { setStatus(v || ""); setPage(1); }}
          options={[{ value: "DRAFT", label: "草稿" }, { value: "PUBLISHED", label: "已发布" }, { value: "DEPRECATED", label: "已废弃" }]}
        />
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>新建术语</Button>
        <span className="muted">共 {total} 条</span>
      </Space>

      <Table
        dataSource={items}
        columns={columns}
        rowKey="term_code"
        loading={loading}
        pagination={{ current: page, pageSize: 20, total, onChange: setPage, showTotal: (t) => `共 ${t} 条` }}
        locale={{ emptyText: "暂无术语" }}
        rowClassName={(r) => (focusCode && r.term_code === focusCode ? "ant-table-row-selected" : "")}
      />

      <Modal title="新建术语" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="创建">
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="term_code" label="术语编码" rules={[{ required: true }]}>
            <Input className="mono" placeholder="如 GMV" />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如 成交总额" />
          </Form.Item>
          <Form.Item name="domain" label="业务域" rules={[{ required: true }]}>
            <Input placeholder="如 finance" />
          </Form.Item>
          <Form.Item name="definition" label="定义" rules={[{ required: true }]}>
            <Input.TextArea rows={3} />
          </Form.Item>
          <Form.Item name="synonyms" label="同义词（逗号分隔）">
            <Input placeholder="GMV, gross merchandise volume" />
          </Form.Item>
          <Form.Item name="boundary" label="边界说明">
            <Input.TextArea rows={2} placeholder="如：不含退款订单" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

function ConflictsTab() {
  const [items, setItems] = useState<GlossaryConflict[]>([]);
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const res = await listTermConflicts();
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

  async function handleResolve(c: GlossaryConflict, decision: string) {
    try {
      await resolveTermConflict(c.id, decision);
      message.success(decision === "RESOLVED" ? "已解决冲突" : "已忽略冲突");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    }
  }

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 70 },
    { title: "术语", dataIndex: "term_id", key: "term", width: 90, render: (v: number) => <span className="mono">#{v}</span> },
    { title: "冲突类型", dataIndex: "conflict_type", key: "type", width: 180, render: (v: string) => CONFLICT_TYPE_LABEL[v] ?? v },
    { title: "关联术语", dataIndex: "ref_term_id", key: "refTerm", render: (v: number | null) => v ? <span className="mono">#{v}</span> : <span className="muted">—</span> },
    { title: "关联指标", dataIndex: "ref_metric_id", key: "refMetric", render: (v: number | null) => v ? <span className="mono">#{v}</span> : <span className="muted">—</span> },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (s: string) => <Tag color={s === "OPEN" ? "warning" : "success"}>{CONFLICT_STATUS_LABEL[s] ?? s}</Tag>,
    },
    {
      title: "操作",
      key: "actions",
      width: 160,
      render: (_: unknown, c: GlossaryConflict) =>
        c.status === "OPEN" ? (
          <Space>
            <Button size="small" type="primary" onClick={() => handleResolve(c, "RESOLVED")}>解决</Button>
            <Button size="small" onClick={() => handleResolve(c, "IGNORED")}>忽略</Button>
          </Space>
        ) : (
          <Tag>已处理</Tag>
        ),
    },
  ];

  return (
    <Table dataSource={items} columns={columns} rowKey="id" loading={loading} pagination={false} locale={{ emptyText: "暂无术语冲突" }} />
  );
}

export function Glossary() {
  const tabItems = [
    { key: "terms", label: "术语列表", children: <TermsTab /> },
    { key: "conflicts", label: "术语冲突", children: <ConflictsTab /> },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Governance / Glossary</div>
          <h2>术语表</h2>
          <p>业务术语统一定义——创建即触发冲突检测，保证全组织口径一致。</p>
        </div>
      </div>
      <Card styles={{ body: { paddingTop: 8 } }}>
        <Tabs items={tabItems} />
      </Card>
    </div>
  );
}
