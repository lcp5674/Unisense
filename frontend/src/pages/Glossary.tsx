import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, message, Tabs, Space, Descriptions, InputNumber } from "antd";
import { PlusOutlined, SendOutlined } from "@ant-design/icons";
import {
  listTerms,
  createTerm,
  getTerm,
  updateTerm,
  createTermRelation,
  submitTerm,
  deprecateTerm,
  listTermConflicts,
  resolveTermConflict,
  UnisenseApiError,
} from "../api";
import type { GlossaryTerm, GlossaryConflict } from "../types";

const STATUS_COLOR: Record<string, string> = { DRAFT: "default", PUBLISHED: "success", DEPRECATED: "error" };
const STATUS_LABEL: Record<string, string> = { DRAFT: "草稿", PUBLISHED: "已发布", DEPRECATED: "已废弃" };
const RELATION_TYPE_LABEL: Record<string, string> = {
  SYNONYM_OF: "同义（SYNONYM_OF）",
  BROADER_THAN: "上位（BROADER_THAN）",
  NARROWER_THAN: "下位（NARROWER_THAN）",
  RELATED_TO: "相关（RELATED_TO）",
};
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
  const [pageSize, setPageSize] = useState(20);
  const [searchParams] = useSearchParams();
  // 生命周期状态下钻（?status=，总览仪表「术语」资产卡片）作为初始筛选
  const urlStatus = searchParams.get("status") ?? "";
  const [status, setStatus] = useState(urlStatus);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();
  // 详情/编辑/关系管理：详情为只读弹窗，编辑与关系用独立 Form 避免与新建表单互相污染
  const [detailTerm, setDetailTerm] = useState<GlossaryTerm | null>(null);
  const [editTarget, setEditTarget] = useState<GlossaryTerm | null>(null);
  const [editForm] = Form.useForm();
  const [relationTarget, setRelationTarget] = useState<GlossaryTerm | null>(null);
  const [relationForm] = Form.useForm();
  // URL 直达关键词（?kw=，全局搜索跳术语）作为初始筛选，避免「先查全量再过滤」的竞态覆盖
  const urlKw = searchParams.get("kw") ?? "";
  const focusCode = searchParams.get("focus");
  // 并发查询防竞态：只有最后一次发起的请求允许落地结果
  const loadSeq = useRef(0);
  // 搜索框初始值承接 URL 关键词（首查即带过滤）
  const [search, setSearch] = useState(urlKw);

  async function load(overSearch?: string) {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      const res = await listTerms({ search: overSearch ?? search, status, page, page_size: pageSize });
      // 已有更新的请求发起，丢弃本次过时响应（防竞态覆盖）
      if (seq !== loadSeq.current) return;
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      if (seq !== loadSeq.current) return;
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }

  // 响应 URL 直达关键词变化（全局搜索 SPA 内跳转，同路由不 remount）；初始值已由 useState 承接，
  // 此处仅同步「URL 出现新关键词」的场景，并保留用户手动清空/修改搜索的能力。
  useEffect(() => {
    if (urlKw && urlKw !== search) {
      setSearch(urlKw);
      setPage(1);
      // search 不在 load 依赖中（手动搜索模式），此处直接用新值查询
      load(urlKw);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlKw]);

  // 响应 URL 状态参数变化（总览仪表「术语」资产卡片二次下钻）；status 在 load 依赖中，
  // setStatus 会经依赖链自动触发重查，无需手动 load
  useEffect(() => {
    if (urlStatus && urlStatus !== status) {
      setStatus(urlStatus);
      setPage(1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlStatus]);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize, status]);

  async function handleCreate(values: Record<string, unknown>) {
    try {
      await createTerm({
        term_code: values.term_code ? String(values.term_code) : undefined,
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

  // 详情：先用列表行数据即时展示，再拉取最新完整详情补全（owner/版本/时间戳等列外字段）
  async function openDetail(t: GlossaryTerm) {
    setDetailTerm(t);
    try {
      const full = await getTerm(t.term_code);
      setDetailTerm(full);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载详情失败");
    }
  }

  // 编辑：回填当前值（同义词逗号连接还原为表单输入格式）
  function openEdit(t: GlossaryTerm) {
    setEditTarget(t);
    editForm.setFieldsValue({
      name: t.name,
      definition: t.definition,
      domain: t.domain,
      synonyms: (t.synonyms ?? []).join(", "),
      boundary: t.boundary ?? "",
    });
  }

  async function handleUpdate(values: Record<string, unknown>) {
    if (!editTarget) return;
    try {
      await updateTerm(editTarget.term_code, {
        name: String(values.name),
        definition: String(values.definition),
        domain: String(values.domain),
        synonyms: values.synonyms ? String(values.synonyms).split(",").map((s) => s.trim()).filter(Boolean) : [],
        boundary: values.boundary ? String(values.boundary) : null,
      });
      message.success("术语已更新");
      setEditTarget(null);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "更新失败");
    }
  }

  // 关系管理：为当前术语建立与另一术语的关系（目标按数据库 id 定位）
  function openRelation(t: GlossaryTerm) {
    setRelationTarget(t);
    relationForm.resetFields();
    relationForm.setFieldsValue({ relation_type: "RELATED_TO" });
  }

  async function handleCreateRelation(values: Record<string, unknown>) {
    if (!relationTarget) return;
    try {
      await createTermRelation(relationTarget.term_code, {
        target_term_id: Number(values.target_term_id),
        relation_type: String(values.relation_type),
      });
      message.success("术语关系已建立");
      setRelationTarget(null);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "建立关系失败");
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
      width: 340,
      render: (_: unknown, t: GlossaryTerm) => (
        <Space wrap>
          <Button size="small" type="link" onClick={() => openDetail(t)}>详情</Button>
          <Button size="small" type="link" onClick={() => openEdit(t)}>编辑</Button>
          <Button size="small" type="link" onClick={() => openRelation(t)}>关系</Button>
          {t.status === "DRAFT" && (
            <Button size="small" type="primary" icon={<SendOutlined />} onClick={() => handleSubmit(t)}>提交</Button>
          )}
          <Button size="small" danger onClick={() => handleDeprecate(t)}>废弃</Button>
        </Space>
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
        pagination={{ current: page, pageSize, total, showSizeChanger: true, pageSizeOptions: [10, 20, 50, 100], onChange: (p, ps) => { setPage(p); setPageSize(ps); }, showTotal: (t) => `共 ${t} 条` }}
        locale={{ emptyText: "暂无术语" }}
        rowClassName={(r) => (focusCode && r.term_code === focusCode ? "ant-table-row-selected" : "")}
      />

      <Modal title="新建术语" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="创建">
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="term_code" label="术语编码" extra={<span className="mono" style={{ color: "#0E7C86" }}>留空则由系统自动生成</span>}>
            <Input className="mono" placeholder="留空自动生成" />
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

      {/* 术语详情：只读展示完整字段（含列外字段：owner/版本/时间戳） */}
      <Modal
        title={detailTerm ? `术语详情：${detailTerm.term_code}` : "术语详情"}
        open={detailTerm !== null}
        onCancel={() => setDetailTerm(null)}
        footer={<Button onClick={() => setDetailTerm(null)}>关闭</Button>}
      >
        {detailTerm && (
          <Descriptions column={1} size="small" bordered style={{ marginTop: 8 }}>
            <Descriptions.Item label="术语编码">{detailTerm.term_code}</Descriptions.Item>
            <Descriptions.Item label="名称">{detailTerm.name}</Descriptions.Item>
            <Descriptions.Item label="业务域">{detailTerm.domain}</Descriptions.Item>
            <Descriptions.Item label="状态"><Tag color={STATUS_COLOR[detailTerm.status]}>{STATUS_LABEL[detailTerm.status] ?? detailTerm.status}</Tag></Descriptions.Item>
            <Descriptions.Item label="定义">{detailTerm.definition}</Descriptions.Item>
            <Descriptions.Item label="同义词">{(detailTerm.synonyms ?? []).length ? (detailTerm.synonyms as string[]).join("、") : <span className="muted">—</span>}</Descriptions.Item>
            <Descriptions.Item label="边界说明">{detailTerm.boundary ?? <span className="muted">—</span>}</Descriptions.Item>
            <Descriptions.Item label="Owner ID"><span className="mono">{detailTerm.owner_id}</span></Descriptions.Item>
            <Descriptions.Item label="版本"><span className="mono">{detailTerm.version ?? 1}</span></Descriptions.Item>
            <Descriptions.Item label="创建时间">{detailTerm.created_at ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="更新时间">{detailTerm.updated_at ?? "—"}</Descriptions.Item>
          </Descriptions>
        )}
      </Modal>

      {/* 编辑术语：回填当前值，缺省字段不更新 */}
      <Modal
        title={editTarget ? `编辑术语：${editTarget.term_code}` : "编辑术语"}
        open={editTarget !== null}
        onCancel={() => setEditTarget(null)}
        onOk={() => editForm.submit()}
        okText="保存"
      >
        <Form form={editForm} layout="vertical" onFinish={handleUpdate} style={{ marginTop: 8 }}>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="domain" label="业务域" rules={[{ required: true }]}>
            <Input />
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

      {/* 关系管理：为目标术语建立术语间关系（目标按数据库 id 定位） */}
      <Modal
        title={relationTarget ? `建立关系：${relationTarget.term_code}` : "建立术语关系"}
        open={relationTarget !== null}
        onCancel={() => setRelationTarget(null)}
        onOk={() => relationForm.submit()}
        okText="建立"
      >
        <Form form={relationForm} layout="vertical" onFinish={handleCreateRelation} style={{ marginTop: 8 }}>
          <Form.Item
            name="target_term_id"
            label="目标术语 ID"
            extra={<span className="muted">目标术语的数据库 id，可在其详情弹窗查看</span>}
            rules={[{ required: true, message: "请输入目标术语 ID" }]}
          >
            <InputNumber className="mono" min={1} style={{ width: "100%" }} placeholder="如 3" />
          </Form.Item>
          <Form.Item name="relation_type" label="关系类型" rules={[{ required: true }]}>
            <Select options={Object.entries(RELATION_TYPE_LABEL).map(([v, label]) => ({ value: v, label }))} />
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
