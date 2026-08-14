import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, message, Tabs, Space, Drawer, Descriptions, Popconfirm } from "antd";
import { DeleteOutlined, EditOutlined, PlusOutlined, SendOutlined } from "@ant-design/icons";
import {
  listDimensions,
  createDimension,
  getDimension,
  updateDimension,
  publishDimension,
  deprecateDimension,
  bindMetricDimension,
  listDimensionMappings,
  createDimensionMapping,
  updateDimensionMapping,
  deleteDimensionMapping,
  listReconciliations,
  submitReconciliation,
  reviewReconciliation,
  listDimensionMembers,
  createDimensionMember,
  updateDimensionMember,
  deleteDimensionMember,
  listDimensionMetrics,
  listMetrics,
  listDomainTree,
  listUsers,
  UnisenseApiError,
} from "../api";
import type {
  Dimension,
  DimensionMapping,
  Reconciliation,
  DimensionMember,
  DimensionMetricBinding,
  MetricResponse,
  SubjectDomainTreeNode,
  UserBrief,
} from "../types";

const STATUS_COLOR: Record<string, string> = { DRAFT: "default", PUBLISHED: "success", DEPRECATED: "error" };
const STATUS_LABEL: Record<string, string> = { DRAFT: "草稿", PUBLISHED: "已发布", DEPRECATED: "已废弃" };
const RECON_STATUS_LABEL: Record<string, string> = {
  PENDING: "待复核",
  APPROVED: "已通过",
  REJECTED: "已驳回",
};
// 指标-维度绑定角色中文标签（对齐后端 MetricDimensionRole 枚举）
const ROLE_LABEL: Record<string, string> = {
  PARTITION: "PARTITION 分区",
  SPLICE: "SPLICE 拼接",
  FILTER: "FILTER 过滤",
};

// 缓慢变化维类型全集（对齐后端 DimensionType 枚举：SCD0-SCD6）
const SCD_TYPE_OPTIONS = [
  { value: "SCD0", label: "SCD0 原样保留" },
  { value: "SCD1", label: "SCD1 覆盖旧值" },
  { value: "SCD2", label: "SCD2 保留历史" },
  { value: "SCD3", label: "SCD3 有限历史" },
  { value: "SCD4", label: "SCD4 历史表" },
  { value: "SCD6", label: "SCD6 混合" },
];

// 指标-维度关联角色（对齐后端 MetricDimensionRole 枚举）
const ROLE_OPTIONS = [
  { value: "PARTITION", label: "PARTITION 分区" },
  { value: "SPLICE", label: "SPLICE 拼接" },
  { value: "FILTER", label: "FILTER 过滤" },
];

// 递归展平主题域树 → code → 中文名映射（业务域选项框用）
function flattenDomainNames(nodes: SubjectDomainTreeNode[], acc: Map<string, string>) {
  for (const n of nodes) {
    acc.set(n.code, n.name);
    if (n.children?.length) flattenDomainNames(n.children, acc);
  }
}

function DimensionsTab() {
  const [searchParams] = useSearchParams();
  // URL 直达参数（?kw=）作为初始筛选，避免「先查全量再过滤」的竞态覆盖
  const urlKw = searchParams.get("kw") ?? "";
  // 生命周期状态下钻（?status=，总览仪表「维度」资产卡片）作为初始筛选
  const urlStatus = searchParams.get("status") ?? "";
  const [items, setItems] = useState<Dimension[]>([]);
  const [keyword, setKeyword] = useState(urlKw);
  const [status, setStatus] = useState(urlStatus);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();
  // 编辑态：复用新建表单布局，打开时预填当前维度值
  const [editTarget, setEditTarget] = useState<Dimension | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [editSaving, setEditSaving] = useState(false);
  const [editForm] = Form.useForm();
  // 绑定指标态：选择指标 + 维度内角色（partition/filter/group 等）
  const [bindTarget, setBindTarget] = useState<Dimension | null>(null);
  const [bindSaving, setBindSaving] = useState(false);
  const [bindForm] = Form.useForm();
  // 绑定指标下拉候选（指标列表）
  const [metrics, setMetrics] = useState<MetricResponse[]>([]);
  // 业务域树 → 中文名映射（新建/编辑维度的业务域选项框）
  const [domainMap, setDomainMap] = useState<Map<string, string>>(new Map());
  // 责任人 ID → 中文名映射（「责任人」列渲染）
  const [users, setUsers] = useState<UserBrief[]>([]);
  // 绑定 Modal 中「默认成员」下拉候选（当前维度的成员列表）
  const [bindMembers, setBindMembers] = useState<DimensionMember[]>([]);
  // 详情抽屉：维度详情 + 绑定指标 / 成员 / 映射三个子表格
  const [detailTarget, setDetailTarget] = useState<Dimension | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailMetrics, setDetailMetrics] = useState<DimensionMetricBinding[]>([]);
  const [detailMembers, setDetailMembers] = useState<DimensionMember[]>([]);
  const [detailMappings, setDetailMappings] = useState<DimensionMapping[]>([]);
  // 并发查询防竞态：只有最后一次发起的请求允许落地结果
  const loadSeq = useRef(0);

  // 支持从全局搜索栏经 ?kw= 直达定位；初始值已由 useState 承接，
  // 此处仅同步「URL 出现新筛选值」的场景，并保留用户手动清空筛选的能力。
  useEffect(() => {
    if (urlKw && urlKw !== keyword) setKeyword(urlKw);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlKw]);

  // 响应 URL 状态参数变化（总览仪表「维度」资产卡片二次下钻）；status 在 load 依赖中自动重查
  useEffect(() => {
    if (urlStatus && urlStatus !== status) setStatus(urlStatus);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlStatus]);

  // 编辑 Modal 打开时预填当前维度值（基于列表行，getDimension 拉最新后覆盖）
  useEffect(() => {
    if (editOpen && editTarget) {
      editForm.setFieldsValue({
        name: editTarget.name,
        domain: editTarget.domain,
        type: editTarget.type,
        description: editTarget.description ?? undefined,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editOpen, editTarget]);

  // 绑定指标候选（指标列表，失败静默不影响列表主流程）；同时加载业务域树供选项框使用
  useEffect(() => {
    listMetrics({ page_size: 200 })
      .then((r) => setMetrics(r.items))
      .catch(() => {});
    listDomainTree()
      .then((tree) => {
        const m = new Map<string, string>();
        flattenDomainNames(tree, m);
        setDomainMap(m);
      })
      .catch(() => {});
    // 责任人候选（失败静默：责任人列回退「用户 #id」）
    listUsers().then(setUsers).catch(() => {});
  }, []);

  async function load() {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      const res = await listDimensions({
        keyword: keyword || undefined,
        status: status || undefined,
      });
      // 已有更新的请求发起，丢弃本次过时响应（防竞态覆盖）
      if (seq !== loadSeq.current) return;
      setItems(res.items);
    } catch (err) {
      if (seq !== loadSeq.current) return;
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keyword, status]);

  async function handleCreate(values: Record<string, unknown>) {
    try {
      await createDimension({
        dim_code: values.dim_code ? String(values.dim_code) : undefined,
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

  // 打开编辑：先拉取最新详情确保基于最新数据（详情端点接线）
  async function openEdit(d: Dimension) {
    setEditTarget(d);
    setEditOpen(true);
    try {
      const fresh = await getDimension(d.dim_code);
      editForm.setFieldsValue({
        name: fresh.name,
        domain: fresh.domain,
        type: fresh.type,
        description: fresh.description ?? undefined,
      });
    } catch {
      // 详情拉取失败不阻塞：仍可用列表数据编辑
    }
  }

  async function handleEdit(values: Record<string, unknown>) {
    if (!editTarget) return;
    setEditSaving(true);
    try {
      await updateDimension(editTarget.dim_code, {
        name: values.name ? String(values.name) : undefined,
        domain: values.domain ? String(values.domain) : undefined,
        type: values.type ? String(values.type) : undefined,
        description: values.description ? String(values.description) : null,
      });
      message.success("维度已更新");
      setEditOpen(false);
      editForm.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "更新失败");
    } finally {
      setEditSaving(false);
    }
  }

  async function handleBind(values: Record<string, unknown>) {
    if (!bindTarget) return;
    setBindSaving(true);
    try {
      await bindMetricDimension({
        metric_id: Number(values.metric_id),
        dim_code: bindTarget.dim_code,
        role: String(values.role ?? "FILTER"),
        default_member: values.default_member ? String(values.default_member) : null,
      });
      message.success(`指标已绑定到维度「${bindTarget.dim_code}」`);
      setBindTarget(null);
      bindForm.resetFields();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "绑定失败");
    } finally {
      setBindSaving(false);
    }
  }

  // 打开详情抽屉：并行拉取绑定指标 / 成员 / 映射，任一失败静默降级（不影响整体展示）
  function openDetail(d: Dimension) {
    setDetailTarget(d);
    setDetailMetrics([]);
    setDetailMembers([]);
    setDetailMappings([]);
    setDetailLoading(true);
    listDimensionMetrics(d.dim_code).then((r) => setDetailMetrics(r.items)).catch(() => setDetailMetrics([]));
    listDimensionMembers(d.dim_code).then((r) => setDetailMembers(r.items)).catch(() => setDetailMembers([]));
    listDimensionMappings().then((r) => {
      // 仅展示与该维度相关的映射（源或目标）
      setDetailMappings(r.items.filter((m) => m.source_dim_code === d.dim_code || m.target_dim_code === d.dim_code));
    }).catch(() => setDetailMappings([])).finally(() => setDetailLoading(false));
  }

  // 责任人 ID → 中文名（无记录回退「用户 #id」）
  const ownerName = (ownerId: number) =>
    users.find((u) => u.id === ownerId)?.display_name ?? `用户 #${ownerId}`;

  const columns = [
    { title: "编码", dataIndex: "dim_code", key: "dim_code", render: (v: string) => <span className="mono">{v}</span> },
    { title: "名称", dataIndex: "name", key: "name" },
    { title: "业务域", dataIndex: "domain", key: "domain", width: 130, render: (v: string) => domainMap.get(v) ?? v },
    { title: "责任人", dataIndex: "owner_id", key: "owner", width: 120, render: (v: number) => ownerName(v) },
    { title: "类型", dataIndex: "type", key: "type", width: 90, render: (v: string) => <Tag>{v}</Tag> },
    { title: "绑定指标", dataIndex: "metric_count", key: "metric_count", width: 90, render: (v?: number) => (v ?? 0) > 0 ? v : <span className="muted">0</span> },
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
      width: 300,
      render: (_: unknown, d: Dimension) =>
        d.status !== "DEPRECATED" ? (
          <Space size={4} wrap>
            <Button size="small" onClick={() => openDetail(d)}>详情</Button>
            <Button size="small" onClick={() => openEdit(d)}>编辑</Button>
            <Button
              size="small"
              onClick={async () => {
                bindForm.resetFields();
                setBindTarget(d);
                // 打开时重新加载指标候选（确保与指标目录一致，带状态标签可区分）
                try {
                  const r = await listMetrics({ page_size: 200 });
                  setMetrics(r.items);
                } catch { /* 静默：已有候选可降级 */ }
                // 加载该维度成员作为「默认成员」下拉候选
                try {
                  const r = await listDimensionMembers(d.dim_code);
                  setBindMembers(r.items);
                } catch {
                  setBindMembers([]);
                }
              }}
            >
              绑定指标
            </Button>
            {d.status !== "PUBLISHED" && <Button size="small" type="primary" onClick={() => handlePublish(d)}>发布</Button>}
            <Button size="small" danger onClick={() => handleDeprecate(d)}>废弃</Button>
          </Space>
        ) : (
          <Space size={4} wrap>
            <Button size="small" onClick={() => openDetail(d)}>详情</Button>
            <Tag>已停用</Tag>
          </Space>
        ),
    },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Input.Search
          placeholder="搜索维度编码 / 名称 / 描述"
          allowClear
          style={{ width: 260 }}
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          onSearch={() => load()}
        />
        <Select
          placeholder="状态筛选"
          allowClear
          style={{ width: 140 }}
          value={status || undefined}
          onChange={(v) => setStatus(v ?? "")}
          options={[
            { value: "DRAFT", label: "草稿" },
            { value: "PUBLISHED", label: "已发布" },
            { value: "DEPRECATED", label: "已废弃" },
          ]}
        />
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>新建维度</Button>
      </Space>
      <Table
        dataSource={items}
        columns={columns}
        rowKey="dim_code"
        loading={loading}
        pagination={false}
        locale={{ emptyText: "暂无维度" }}
        onRow={(d) => ({
          onClick: (e) => {
            // 点击行打开详情抽屉；但需避开行内按钮/链接，避免与操作按钮触发冲突
            if ((e.target as HTMLElement).closest("button, a")) return;
            openDetail(d);
          },
        })}
      />

      <Modal title="新建维度" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="创建">
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="dim_code" label="维度编码" extra={<span className="mono" style={{ color: "#0E7C86" }}>留空则由系统自动生成</span>}>
            <Input className="mono" placeholder="留空自动生成" />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如 渠道" />
          </Form.Item>
          <Form.Item name="domain" label="业务域" rules={[{ required: true }]}>
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择业务域"
              options={Array.from(domainMap.entries()).map(([code, name]) => ({
                value: code,
                label: `${name}（${code}）`,
              }))}
            />
          </Form.Item>
          <Form.Item name="type" label="缓慢变化维类型">
            <Select options={SCD_TYPE_OPTIONS} placeholder="选择类型" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={editTarget ? `编辑维度：${editTarget.dim_code}` : "编辑维度"}
        open={editOpen}
        onCancel={() => {
          setEditOpen(false);
          editForm.resetFields();
        }}
        onOk={() => editForm.submit()}
        okText="保存"
        confirmLoading={editSaving}
      >
        <Form form={editForm} layout="vertical" onFinish={handleEdit} style={{ marginTop: 8 }}>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如 渠道" />
          </Form.Item>
          <Form.Item name="domain" label="业务域" rules={[{ required: true }]}>
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择业务域"
              options={Array.from(domainMap.entries()).map(([code, name]) => ({
                value: code,
                label: `${name}（${code}）`,
              }))}
            />
          </Form.Item>
          <Form.Item name="type" label="缓慢变化维类型">
            <Select options={SCD_TYPE_OPTIONS} placeholder="选择类型" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={bindTarget ? `绑定指标 → ${bindTarget.dim_code}` : "绑定指标"}
        open={bindTarget != null}
        onCancel={() => setBindTarget(null)}
        onOk={() => bindForm.submit()}
        okText="绑定"
        confirmLoading={bindSaving}
      >
        <Form
          form={bindForm}
          layout="vertical"
          initialValues={{ role: "FILTER" }}
          onFinish={handleBind}
          style={{ marginTop: 8 }}
        >
          <Form.Item name="metric_id" label="指标" rules={[{ required: true }]}>
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择指标"
              notFoundContent={metrics.length === 0 ? "暂无指标，请先在指标目录创建" : "无匹配指标"}
              options={metrics.map((m) => ({
                value: m.id,
                label: `${m.metric_code} · ${m.name}`,
              }))}
            />
          </Form.Item>
          <Form.Item name="role" label="维度角色" extra="标识该指标如何消费此维度">
            <Select options={ROLE_OPTIONS} placeholder="选择角色" />
          </Form.Item>
          <Form.Item name="default_member" label="默认成员">
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="选择该维度下的成员"
              notFoundContent={bindMembers.length === 0 ? "该维度暂无成员" : "无匹配成员"}
              options={bindMembers.map((mem) => ({
                value: mem.member_code,
                label: mem.path ? `${mem.path}（${mem.member_name}）` : `${mem.member_code} · ${mem.member_name}`,
              }))}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        title={detailTarget ? `维度详情：${detailTarget.dim_code} · ${detailTarget.name}` : "维度详情"}
        open={detailTarget != null}
        onClose={() => setDetailTarget(null)}
        width={860}
      >
        {detailTarget && (
          <>
            <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
              <Descriptions.Item label="编码">{detailTarget.dim_code}</Descriptions.Item>
              <Descriptions.Item label="名称">{detailTarget.name}</Descriptions.Item>
              <Descriptions.Item label="业务域">{domainMap.get(detailTarget.domain) ?? detailTarget.domain}</Descriptions.Item>
              <Descriptions.Item label="SCD 类型">{detailTarget.type}</Descriptions.Item>
              <Descriptions.Item label="责任人">{ownerName(detailTarget.owner_id)}</Descriptions.Item>
              <Descriptions.Item label="状态">
                <Tag color={STATUS_COLOR[detailTarget.status]}>{STATUS_LABEL[detailTarget.status] ?? detailTarget.status}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="描述">{detailTarget.description || <span className="muted">—</span>}</Descriptions.Item>
              <Descriptions.Item label="创建时间">{detailTarget.created_at || "—"}</Descriptions.Item>
              <Descriptions.Item label="更新时间">{detailTarget.updated_at || "—"}</Descriptions.Item>
            </Descriptions>

            <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>绑定指标（{detailMetrics.length}）</div>
            <Table
              dataSource={detailMetrics}
              rowKey="metric_id"
              size="small"
              loading={detailLoading}
              pagination={false}
              locale={{ emptyText: "暂无绑定指标" }}
              columns={[
                { title: "指标编码", dataIndex: "metric_code", key: "code", render: (v: string) => <span className="mono">{v}</span> },
                { title: "指标名称", dataIndex: "metric_name", key: "name" },
                { title: "角色", dataIndex: "role", key: "role", render: (v: string) => ROLE_LABEL[v] ?? v },
                { title: "默认成员", dataIndex: "default_member", key: "dm", render: (v: string | null) => v ?? <span className="muted">—</span> },
                { title: "指标状态", dataIndex: "metric_status", key: "status", width: 100, render: (s: string) => <Tag color={STATUS_COLOR[s]}>{STATUS_LABEL[s] ?? s}</Tag> },
              ]}
            />

            <div className="muted" style={{ fontSize: 12, margin: "16px 0 8px" }}>成员（{detailMembers.length}）</div>
            <Table
              dataSource={detailMembers}
              rowKey="member_code"
              size="small"
              pagination={false}
              locale={{ emptyText: "暂无成员" }}
              columns={[
                { title: "成员编码", dataIndex: "member_code", key: "code", render: (v: string) => <span className="mono">{v}</span> },
                { title: "名称", dataIndex: "member_name", key: "name" },
                { title: "路径", dataIndex: "path", key: "path", render: (v: string | null) => v && <span className="mono">{v}</span> },
              ]}
            />

            <div className="muted" style={{ fontSize: 12, margin: "16px 0 8px" }}>维度映射（{detailMappings.length}）</div>
            <Table
              dataSource={detailMappings}
              rowKey="id"
              size="small"
              pagination={false}
              locale={{ emptyText: "暂无相关映射" }}
              columns={[
                { title: "源维度", dataIndex: "source_dim_code", key: "src", render: (v: string) => <span className="mono">{v}</span> },
                { title: "目标维度", dataIndex: "target_dim_code", key: "tgt", render: (v: string) => <span className="mono">{v}</span> },
                { title: "映射类型", dataIndex: "mapping_type", key: "type", render: (v: string) => <Tag color={v === "EQUIVALENT" ? "success" : "warning"}>{v === "EQUIVALENT" ? "等价" : "部分"}</Tag> },
                { title: "表达式", dataIndex: "expression", key: "expr", render: (v: string | null) => v ? <span className="mono">{v}</span> : <span className="muted">—</span> },
              ]}
            />
          </>
        )}
      </Drawer>
    </div>
  );
}

// 层级路径实时预览：选择父级/输入编码时自动推算将生成的路径（提交时交由后端兜底）
function PathPreview({
  form,
  members,
}: {
  form: ReturnType<typeof Form.useForm>[0];
  members: DimensionMember[];
}) {
  const parentCode = Form.useWatch("parent_code", form);
  const memberCode = Form.useWatch("member_code", form);
  let path: string;
  if (parentCode) {
    const parent = members.find((m) => m.member_code === parentCode);
    const base = parent?.path ?? `/${parentCode}`;
    path = memberCode ? `${base}/${memberCode}` : `${base}/{member_code}`;
  } else if (memberCode) {
    path = `/${memberCode}`;
  } else {
    path = "/{member_code}";
  }
  return (
    <div className="muted" style={{ fontSize: 12 }}>
      层级路径将自动生成：<code className="mono">{path}</code>
    </div>
  );
}

// 成员树节点：平铺成员按 parent_code 组装出层级后带 children 子集
type MemberTreeNode = DimensionMember & { children: MemberTreeNode[] };

// 平铺成员 → 树：根 = parent_code 为 null 或父级不存在的成员；同级按 path 排序保持稳定
function buildMemberTree(members: DimensionMember[]): MemberTreeNode[] {
  const byCode = new Map<string, MemberTreeNode>();
  for (const m of members) byCode.set(m.member_code, { ...m, children: [] });
  const roots: MemberTreeNode[] = [];
  for (const m of members) {
    const node = byCode.get(m.member_code)!;
    const parent = m.parent_code ? byCode.get(m.parent_code) : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  const sortByPath = (arr: MemberTreeNode[]) => arr.sort((a, b) => (a.path ?? a.member_code).localeCompare(b.path ?? b.member_code));
  sortByPath(roots);
  for (const node of byCode.values()) sortByPath(node.children);
  return roots;
}

function MembersTab() {
  const [dims, setDims] = useState<Dimension[]>([]);
  const [dimCode, setDimCode] = useState<string | undefined>(undefined);
  const [members, setMembers] = useState<DimensionMember[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();
  // 编辑态：复用新增布局，打开时预填当前成员值
  const [editTarget, setEditTarget] = useState<DimensionMember | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [editSaving, setEditSaving] = useState(false);
  const [editForm] = Form.useForm();

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

  async function reload() {
    if (!dimCode) return;
    setLoading(true);
    listDimensionMembers(dimCode).then((r) => setMembers(r.items)).finally(() => setLoading(false));
  }

  async function handleCreate(values: Record<string, unknown>) {
    if (!dimCode) return;
    try {
      await createDimensionMember({
        dim_code: dimCode,
        member_code: values.member_code ? String(values.member_code) : undefined,
        member_name: String(values.member_name),
        parent_code: values.parent_code ? String(values.parent_code) : null,
        // path 留空，由后端按父级路径自动推测
      });
      message.success("成员已创建");
      setModalOpen(false);
      form.resetFields();
      reload();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "创建失败");
    }
  }

  function openEdit(m: DimensionMember) {
    setEditTarget(m);
    setEditOpen(true);
    editForm.setFieldsValue({
      member_name: m.member_name,
      parent_code: m.parent_code ?? undefined,
      status: m.status,
    });
  }

  async function handleEdit(values: Record<string, unknown>) {
    if (!dimCode || !editTarget) return;
    setEditSaving(true);
    try {
      await updateDimensionMember({
        dim_code: dimCode,
        member_code: editTarget.member_code,
        member_name: values.member_name ? String(values.member_name) : undefined,
        // 空串表示置为根成员（取消父级），后端据此重算 path
        parent_code: values.parent_code ? String(values.parent_code) : "",
        status: values.status ? String(values.status) : undefined,
      });
      message.success("成员已更新");
      setEditOpen(false);
      editForm.resetFields();
      reload();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "更新失败");
    } finally {
      setEditSaving(false);
    }
  }

  async function handleDeleteMember(m: DimensionMember) {
    if (!dimCode) return;
    try {
      await deleteDimensionMember(dimCode, m.member_code);
      message.success("成员已删除");
      reload();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "删除失败");
    }
  }

  // 成员下拉选项（父级选择框）：展示路径 + 名称，便于识别层级
  function memberOptions(excludeCode?: string) {
    return members
      .filter((m) => m.member_code !== excludeCode)
      .map((m) => ({
        value: m.member_code,
        label: m.path ? `${m.path}（${m.member_name}）` : `${m.member_code} · ${m.member_name}`,
      }));
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
        dataSource={buildMemberTree(members)}
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
          {
            title: "操作",
            key: "actions",
            width: 160,
            render: (_: unknown, m: DimensionMember) => (
              <Space size={4} wrap>
                <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(m)}>编辑</Button>
                <Popconfirm
                  title="删除该成员？"
                  description="若存在子成员将级联删除整个子树"
                  okText="删除"
                  okButtonProps={{ danger: true }}
                  trigger="click"
                  onConfirm={() => handleDeleteMember(m)}
                >
                  <Button size="small" danger icon={<DeleteOutlined />}>删除</Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />

      <Modal title="新增维度成员" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="创建">
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="member_code" label="成员编码" extra={<span className="mono" style={{ color: "#0E7C86" }}>留空则由系统自动生成</span>}>
            <Input className="mono" placeholder="留空自动生成" />
          </Form.Item>
          <Form.Item name="member_name" label="成员名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="parent_code" label="父级编码" extra={<span className="muted" style={{ fontSize: 12 }}>留空则为根成员</span>}>
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="选择父级成员（可留空作为根）"
              notFoundContent={members.length === 0 ? "当前维度暂无成员，该成员将作为根" : "无匹配成员"}
              options={memberOptions()}
            />
          </Form.Item>
          <PathPreview form={form} members={members} />
        </Form>
      </Modal>

      <Modal
        title={editTarget ? `编辑成员：${editTarget.member_code}` : "编辑成员"}
        open={editOpen}
        onCancel={() => {
          setEditOpen(false);
          editForm.resetFields();
        }}
        onOk={() => editForm.submit()}
        okText="保存"
        confirmLoading={editSaving}
      >
        <Form form={editForm} layout="vertical" onFinish={handleEdit} style={{ marginTop: 8 }}>
          <Form.Item name="member_name" label="成员名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="parent_code" label="父级编码" extra={<span className="muted" style={{ fontSize: 12 }}>清空则置为根成员，层级路径自动重算</span>}>
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="选择父级成员"
              notFoundContent={members.length === 0 ? "当前维度暂无成员" : "无匹配成员"}
              options={memberOptions(editTarget?.member_code)}
            />
          </Form.Item>
          <Form.Item name="status" label="状态">
            <Select
              options={[
                { value: "DRAFT", label: "草稿" },
                { value: "PUBLISHED", label: "已发布" },
                { value: "DEPRECATED", label: "已废弃" },
              ]}
            />
          </Form.Item>
          <PathPreview form={editForm} members={members} />
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
  // 维度下拉候选（源/目标维度选项框）
  const [dims, setDims] = useState<Dimension[]>([]);
  // 编辑态：复用新建布局，打开时预填当前映射值
  const [editTarget, setEditTarget] = useState<DimensionMapping | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [editSaving, setEditSaving] = useState(false);
  const [editForm] = Form.useForm();

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
    listDimensions().then((r) => setDims(r.items)).catch(() => {});
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

  function openEditMapping(m: DimensionMapping) {
    setEditTarget(m);
    setEditOpen(true);
    editForm.setFieldsValue({
      mapping_type: m.mapping_type,
      expression: m.expression ?? undefined,
    });
  }

  async function handleEditMapping(values: Record<string, unknown>) {
    if (!editTarget) return;
    setEditSaving(true);
    try {
      await updateDimensionMapping(editTarget.id, {
        mapping_type: values.mapping_type ? String(values.mapping_type) : undefined,
        expression: values.expression ? String(values.expression) : null,
      });
      message.success("映射已更新");
      setEditOpen(false);
      editForm.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "更新失败");
    } finally {
      setEditSaving(false);
    }
  }

  async function handleDeleteMapping(m: DimensionMapping) {
    try {
      await deleteDimensionMapping(m.id);
      message.success("映射已删除");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "删除失败");
    }
  }

  const columns = [
    { title: "源维度", dataIndex: "source_dim_code", key: "source", render: (v: string) => <span className="mono">{v}</span> },
    { title: "目标维度", dataIndex: "target_dim_code", key: "target", render: (v: string) => <span className="mono">{v}</span> },
    { title: "映射类型", dataIndex: "mapping_type", key: "type", width: 130, render: (v: string) => <Tag color={v === "EQUIVALENT" ? "success" : "warning"}>{v === "EQUIVALENT" ? "等价" : "部分"}</Tag> },
    { title: "表达式", dataIndex: "expression", key: "expr", render: (v: string | null) => v ? <span className="mono">{v}</span> : <span className="muted">—</span> },
    {
      title: "操作",
      key: "actions",
      width: 150,
      render: (_: unknown, m: DimensionMapping) => (
        <Space size={4} wrap>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEditMapping(m)}>编辑</Button>
          <Popconfirm
            title="删除该映射？"
            okText="删除"
            okButtonProps={{ danger: true }}
            trigger="click"
            onConfirm={() => handleDeleteMapping(m)}
          >
            <Button size="small" danger icon={<DeleteOutlined />}>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "flex-end" }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>新建映射</Button>
      </div>
      <Table dataSource={items} columns={columns} rowKey="id" loading={loading} pagination={false} locale={{ emptyText: "暂无维度映射" }} />

      <Modal title="新建维度映射" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="创建">
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="source_dim_code" label="源维度" rules={[{ required: true }]}>
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择源维度"
              notFoundContent={dims.length === 0 ? "暂无维度，请先创建" : "无匹配维度"}
              options={dims.map((d) => ({ value: d.dim_code, label: `${d.dim_code} · ${d.name}` }))}
            />
          </Form.Item>
          <Form.Item name="target_dim_code" label="目标维度" rules={[{ required: true }]}>
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择目标维度"
              notFoundContent={dims.length === 0 ? "暂无维度，请先创建" : "无匹配维度"}
              options={dims.map((d) => ({ value: d.dim_code, label: `${d.dim_code} · ${d.name}` }))}
            />
          </Form.Item>
          <Form.Item name="mapping_type" label="映射类型">
            <Select options={[{ value: "EQUIVALENT", label: "等价" }, { value: "PARTIAL", label: "部分" }]} />
          </Form.Item>
          <Form.Item name="expression" label="映射表达式">
            <Input.TextArea rows={2} className="mono" placeholder="如 CASE WHEN ..." />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={editTarget ? `编辑维度映射：${editTarget.source_dim_code} → ${editTarget.target_dim_code}` : "编辑维度映射"}
        open={editOpen}
        onCancel={() => {
          setEditOpen(false);
          editForm.resetFields();
        }}
        onOk={() => editForm.submit()}
        okText="保存"
        confirmLoading={editSaving}
      >
        <Form form={editForm} layout="vertical" onFinish={handleEditMapping} style={{ marginTop: 8 }}>
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
    {
      title: "指标",
      dataIndex: "metric_id",
      key: "metric",
      width: 200,
      render: (v: number, r: Reconciliation) =>
        r.metric_code ? (
          <span className="mono">{r.metric_code}{r.metric_name ? ` · ${r.metric_name}` : ""}</span>
        ) : (
          <span className="mono">#{v}</span>
        ),
    },
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
