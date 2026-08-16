import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, message, Tabs, Space, Drawer, Descriptions, Popconfirm, Divider } from "antd";
import { DeleteOutlined, EditOutlined, PlusOutlined, SendOutlined, ArrowLeftOutlined, HeartOutlined, DatabaseOutlined } from "@ant-design/icons";
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
  listFavorites,
  addFavorite,
  removeFavorite,
  listDataSources,
  previewColumnValues,
  fetchCurrentUser,
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
  DataSource,
} from "../types";
import { formatCnTime } from "../utils/timeCn";
import { usePersistentPageSize } from "../hooks/usePersistentPageSize";
import { usePermission } from "../hooks/usePermission";

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
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { can } = usePermission();
  // 主表每页条数（持久化，用户可自定义）
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.dimensions.pageSize", 20);
  // URL 直达参数（?kw=）作为初始筛选，避免「先查全量再过滤」的竞态覆盖
  const urlKw = searchParams.get("kw") ?? "";
  // 生命周期状态下钻（?status=，总览仪表「维度」资产卡片）作为初始筛选
  const urlStatus = searchParams.get("status") ?? "";
  // 责任人（Owner）下钻（?owner_id=，总览仪表 Owner 责任分布）
  const urlOwnerId = searchParams.get("owner_id");
  const [items, setItems] = useState<Dimension[]>([]);
  const [keyword, setKeyword] = useState(urlKw);
  const [status, setStatus] = useState(urlStatus);
  const [ownerId, setOwnerId] = useState<number | undefined>(
    urlOwnerId && /^\d+$/.test(urlOwnerId) ? Number(urlOwnerId) : undefined,
  );
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  // 维度收藏（C 层多资产收藏：DIMENSION）
  const [favCodes, setFavCodes] = useState<Set<string>>(new Set());
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

  // 响应 URL 责任人参数变化（Owner 责任分布二次下钻）；ownerId 在 load 依赖中自动重查
  useEffect(() => {
    if (urlOwnerId && /^\d+$/.test(urlOwnerId) && Number(urlOwnerId) !== ownerId) {
      setOwnerId(Number(urlOwnerId));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlOwnerId]);

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
    // page_size 取后端上限（semantic MetricQuery le=100），避免 422
    listMetrics({ page_size: 100 })
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
    // 当前用户维度收藏（DIMENSION）供行内收藏按钮判断
    listFavorites()
      .then((favs) =>
        setFavCodes(
          new Set(favs.filter((f) => f.asset_type === "DIMENSION").map((f) => f.asset_id)),
        ),
      )
      .catch(() => {});
  }, []);

  // 维度收藏切换（行内心形）
  async function toggleFavorite(d: Dimension) {
    const fav = favCodes.has(d.dim_code);
    try {
      if (fav) {
        await removeFavorite("DIMENSION", d.dim_code);
        setFavCodes((prev) => {
          const next = new Set(prev);
          next.delete(d.dim_code);
          return next;
        });
        message.success("已取消收藏");
      } else {
        await addFavorite("DIMENSION", d.dim_code);
        setFavCodes((prev) => new Set(prev).add(d.dim_code));
        message.success("已收藏");
      }
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "收藏操作失败",
      );
    }
  }

  async function load() {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      const res = await listDimensions({
        keyword: keyword || undefined,
        status: status || undefined,
        owner_id: ownerId,
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
  }, [keyword, status, ownerId]);

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
        dim_code: fresh.dim_code,
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
      // 编码仅 DRAFT 状态可改（后端强校验）；非 DRAFT 时不传编码，避免误改
      const canEditCode = editTarget.status === "DRAFT";
      await updateDimension(editTarget.dim_code, {
        ...(canEditCode && values.dim_code ? { dim_code: String(values.dim_code) } : {}),
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
            <Button
              size="small"
              type="link"
              icon={<HeartOutlined style={{ color: favCodes.has(d.dim_code) ? "#eb2f96" : undefined }} />}
              onClick={() => toggleFavorite(d)}
            >
              {favCodes.has(d.dim_code) ? "已收藏" : "收藏"}
            </Button>
            <Button size="small" onClick={() => openDetail(d)}>详情</Button>
            {can("dimension:edit") && <Button size="small" onClick={() => openEdit(d)}>编辑</Button>}
            {can("dimension:edit") && (
              <Button
                size="small"
                onClick={async () => {
                  bindForm.resetFields();
                  setBindTarget(d);
                  // 打开时重新加载指标候选（确保与指标目录一致，带状态标签可区分）
                  try {
                    const r = await listMetrics({ page_size: 100 });
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
            )}
            {d.status !== "PUBLISHED" && can("dimension:edit") && <Button size="small" type="primary" onClick={() => handlePublish(d)}>发布</Button>}
            {can("dimension:deprecate") && <Button size="small" danger onClick={() => handleDeprecate(d)}>废弃</Button>}
          </Space>
        ) : (
          <Space size={4} wrap>
            <Button
              size="small"
              type="link"
              icon={<HeartOutlined style={{ color: favCodes.has(d.dim_code) ? "#eb2f96" : undefined }} />}
              onClick={() => toggleFavorite(d)}
            >
              {favCodes.has(d.dim_code) ? "已收藏" : "收藏"}
            </Button>
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
        {can("dimension:create") && (
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>新建维度</Button>
        )}
      </Space>
      <Table
        dataSource={items}
        columns={columns}
        rowKey="dim_code"
        loading={loading}
        pagination={{ pageSize, showSizeChanger: true, onShowSizeChange }}
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
          <Form.Item
            name="dim_code"
            label="维度编码"
            extra={
              editTarget?.status === "DRAFT" ? (
                <span style={{ color: "#0E7C86" }}>草稿状态可修改；已发布/已废弃禁止</span>
              ) : (
                <span className="muted">已发布/已废弃维度编码不可修改</span>
              )
            }
            rules={[
              { required: true, message: "请输入维度编码" },
              { pattern: /^[a-z][a-z0-9_]*$/, message: "仅小写字母/数字/下划线，且不以数字开头" },
            ]}
          >
            <Input className="mono" disabled={editTarget?.status !== "DRAFT"} />
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
              <Descriptions.Item label="创建时间">{detailTarget.created_at ? formatCnTime(detailTarget.created_at) : "—"}</Descriptions.Item>
              <Descriptions.Item label="更新时间">{detailTarget.updated_at ? formatCnTime(detailTarget.updated_at) : "—"}</Descriptions.Item>
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
                {
                  title: "指标编码",
                  dataIndex: "metric_code",
                  key: "code",
                  render: (v: string) => (
                    <a className="mono" onClick={() => navigate(`/detail/${encodeURIComponent(v)}`)}>{v}</a>
                  ),
                },
                {
                  title: "指标名称",
                  dataIndex: "metric_name",
                  key: "name",
                  render: (v: string, r: { metric_code: string }) => (
                    <a onClick={() => navigate(`/detail/${encodeURIComponent(r.metric_code)}`)}>{v ?? "—"}</a>
                  ),
                },
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
  const { can } = usePermission();
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
  // 从表自动获取枚举值：数据源列表 + 弹窗 + 预览结果
  const [dataSources, setDataSources] = useState<DataSource[]>([]);
  const [autoOpen, setAutoOpen] = useState(false);
  const [autoLoading, setAutoLoading] = useState(false);
  const [autoForm] = Form.useForm();
  const [previewValues, setPreviewValues] = useState<string[]>([]);
  const [previewTruncated, setPreviewTruncated] = useState(false);
  const [importing, setImporting] = useState(false);

  useEffect(() => {
    listDimensions().then((r) => setDims(r.items)).catch(() => {});
    listDataSources({ page_size: 100 })
      .then((r) => setDataSources(r.items))
      .catch(() => setDataSources([]));
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

  // 拉取预览：根据所选数据源/表/列，调后端获取去重枚举值
  async function handlePreview(values: Record<string, unknown>) {
    if (!dimCode) return;
    setAutoLoading(true);
    setPreviewValues([]);
    setPreviewTruncated(false);
    try {
      const r = await previewColumnValues({
        source_id: String(values.source_id),
        table: String(values.table),
        column: String(values.column),
        limit: 200,
      });
      setPreviewValues(r.values);
      setPreviewTruncated(r.truncated);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "拉取枚举值失败");
    } finally {
      setAutoLoading(false);
    }
  }

  // 导入预览值为维度成员（member_code = 枚举值本身，member_name = 枚举值）
  async function handleImportValues() {
    if (!dimCode || previewValues.length === 0) return;
    setImporting(true);
    let ok = 0;
    let failed = 0;
    try {
      for (const v of previewValues) {
        try {
          await createDimensionMember({
            dim_code: dimCode,
            member_code: v,
            member_name: v,
          });
          ok += 1;
        } catch {
          failed += 1;
        }
      }
      message.success(`已导入 ${ok} 个维度值${failed > 0 ? `，跳过 ${failed} 个（已存在或失败）` : ""}`);
      setAutoOpen(false);
      autoForm.resetFields();
      setPreviewValues([]);
      reload();
    } finally {
      setImporting(false);
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
      {/* 维度值说明：区分「维度的取值」与「系统用户账号」，避免概念混淆 */}
      <div
        style={{
          marginBottom: 12,
          padding: "8px 12px",
          background: "var(--bg-elevated, #fafafa)",
          border: "1px solid var(--line-soft, #eef1f5)",
          borderRadius: 6,
          fontSize: 13,
          color: "var(--text-2)",
        }}
      >
        维度值 = 该维度允许的<b>业务取值集合</b>（如「渠道」维度的值：线上 / 线下 / 小程序），
        用于指标按此维度分组/过滤时校验合法性。这里管理的<b>不是系统用户账号</b>，
        而是维度自身的枚举取值，可手动新增或从数据源表列自动导入。
        <br />
        维度值需与指标口径声明的维度保持一致——指标在维度管理绑定后，消费查询即按此维度校验过滤。
      </div>
      <Space style={{ marginBottom: 12 }}>
        <Select
          placeholder="选择维度"
          style={{ width: 260 }}
          value={dimCode}
          onChange={setDimCode}
          options={dims.map((d) => ({ value: d.dim_code, label: `${d.dim_code} · ${d.name}` }))}
        />
        {can("dimension:create") && (
          <Button icon={<PlusOutlined />} disabled={!dimCode} onClick={() => setModalOpen(true)}>新增值</Button>
        )}
        {can("dimension:create") && (
          <Button
            icon={<DatabaseOutlined />}
            disabled={!dimCode}
            onClick={() => {
              autoForm.resetFields();
              setPreviewValues([]);
              setPreviewTruncated(false);
              setAutoOpen(true);
            }}
          >
            从表自动获取
          </Button>
        )}
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
                {can("dimension:edit") && (
                  <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(m)}>编辑</Button>
                )}
                {can("dimension:edit") && (
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
                )}
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

      {/* 从表自动获取枚举值：选数据源→表→列，拉取去重值预览后批量导入为维度值 */}
      <Modal
        title={`从表自动获取维度值 → ${dimCode ?? ""}`}
        open={autoOpen}
        onCancel={() => {
          setAutoOpen(false);
          autoForm.resetFields();
          setPreviewValues([]);
        }}
        width={640}
        footer={null}
      >
        <Form
          form={autoForm}
          layout="vertical"
          onFinish={handlePreview}
          style={{ marginTop: 8 }}
          initialValues={{ limit: 200 }}
        >
          <Form.Item
            label="数据源"
            name="source_id"
            rules={[{ required: true, message: "请选择数据源" }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择数据源（须已注册）"
              options={dataSources.map((s) => ({
                value: s.source_id,
                label: `${s.name}（${s.source_id}）`,
              }))}
            />
          </Form.Item>
          <Form.Item
            label="表名"
            name="table"
            extra={<span className="muted" style={{ fontSize: 12 }}>可带库前缀，如 dwd.sales</span>}
            rules={[
              { required: true, message: "请输入表名" },
              { pattern: /^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*$/, message: "表名不合法" },
            ]}
          >
            <Input className="mono" placeholder="如 dwd.sales" />
          </Form.Item>
          <Form.Item
            label="列名"
            name="column"
            rules={[
              { required: true, message: "请输入列名" },
              { pattern: /^[a-zA-Z_][a-zA-Z0-9_]*$/, message: "列名不合法" },
            ]}
          >
            <Input className="mono" placeholder="如 channel" />
          </Form.Item>
          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={autoLoading} icon={<DatabaseOutlined />}>
                拉取去重值
              </Button>
              <span className="muted" style={{ fontSize: 12 }}>
                将执行 <code className="mono">SELECT DISTINCT</code> 读取该列全部取值
              </span>
            </Space>
          </Form.Item>
        </Form>

        {previewValues.length > 0 && (
          <div>
            <Divider style={{ margin: "12px 0" }} />
            <div style={{ marginBottom: 8 }}>
              <span className="muted">已获取 {previewValues.length} 个去重值</span>
              {previewTruncated && (
                <Tag color="orange" style={{ marginLeft: 8 }}>结果已达上限，可能不完整</Tag>
              )}
            </div>
            <div
              style={{
                maxHeight: 200,
                overflow: "auto",
                border: "1px solid var(--line, #e3e7ee)",
                borderRadius: 6,
                padding: 8,
                marginBottom: 12,
              }}
            >
              {previewValues.map((v) => (
                <Tag key={v} className="mono" style={{ marginBottom: 4 }}>
                  {v}
                </Tag>
              ))}
            </div>
            {can("dimension:create") && (
              <Button
                type="primary"
                loading={importing}
                onClick={handleImportValues}
                icon={<PlusOutlined />}
              >
                导入全部为维度值
              </Button>
            )}
          </div>
        )}
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
  const { can } = usePermission();
  const [items, setItems] = useState<DimensionMapping[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();
  // 映射表每页条数（持久化，用户可自定义）
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.mappings.pageSize", 20);
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
          {can("dimension:mapping") && (
            <Button size="small" icon={<EditOutlined />} onClick={() => openEditMapping(m)}>编辑</Button>
          )}
          {can("dimension:mapping") && (
            <Popconfirm
              title="删除该映射？"
              okText="删除"
              okButtonProps={{ danger: true }}
              trigger="click"
              onConfirm={() => handleDeleteMapping(m)}
            >
              <Button size="small" danger icon={<DeleteOutlined />}>删除</Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div>
      {/* 维度映射说明 + 示例引导：解释映射解决什么问题、如何用 */}
      <div
        style={{
          marginBottom: 12,
          padding: "10px 12px",
          background: "var(--bg-elevated, #fafafa)",
          border: "1px solid var(--line-soft, #eef1f5)",
          borderRadius: 6,
          fontSize: 13,
          color: "var(--text-2)",
          lineHeight: 1.7,
        }}
      >
        <b>维度映射</b>表达不同系统间<b>维度取值的对应关系</b>——同一业务概念在不同系统里编码不同，
        指标跨系统对账时需要知道它们等价。
        <br />
        <span className="muted">
          示例：业务库维度 <code className="mono">channel</code>（取值 app / web） ↔ 数仓维度{" "}
          <code className="mono">渠道</code>（取值 APP / PC）。
          创建一条 <Tag color="success">等价</Tag> 映射（source=channel, target=渠道），即可让指标在
          「渠道」维度上正确对账。
        </span>
      </div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "flex-end" }}>
        {can("dimension:mapping") && (
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>新建映射</Button>
        )}
      </div>
      <Table dataSource={items} columns={columns} rowKey="id" loading={loading} pagination={{ pageSize, showSizeChanger: true, onShowSizeChange }} locale={{ emptyText: "暂无维度映射" }} />

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
          <Form.Item
            name="mapping_type"
            label="映射类型"
            extra={<span className="muted" style={{ fontSize: 12 }}>等价 = 源/目标取值一一对应（如 app↔APP）；部分 = 存在一对多或需表达式换算</span>}
          >
            <Select options={[{ value: "EQUIVALENT", label: "等价" }, { value: "PARTIAL", label: "部分" }]} />
          </Form.Item>
          <Form.Item
            name="expression"
            label="映射表达式"
            extra={<span className="muted" style={{ fontSize: 12 }}>支持键值对（app=APP）或 SQL 片段（CASE WHEN ...）</span>}
          >
            <Input.TextArea rows={2} className="mono" placeholder="如 app=APP;web=PC" />
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
          <Form.Item
            name="mapping_type"
            label="映射类型"
            extra={<span className="muted" style={{ fontSize: 12 }}>等价 = 源/目标取值一一对应（如 app↔APP）；部分 = 存在一对多或需表达式换算</span>}
          >
            <Select options={[{ value: "EQUIVALENT", label: "等价" }, { value: "PARTIAL", label: "部分" }]} />
          </Form.Item>
          <Form.Item
            name="expression"
            label="映射表达式"
            extra={<span className="muted" style={{ fontSize: 12 }}>支持键值对（app=APP）或 SQL 片段（CASE WHEN ...）</span>}
          >
            <Input.TextArea rows={2} className="mono" placeholder="如 app=APP;web=PC" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

function ReconciliationsTab() {
  const { can } = usePermission();
  const [items, setItems] = useState<Reconciliation[]>([]);
  const [metrics, setMetrics] = useState<MetricResponse[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();
  // 复核需治理角色（对齐后端 _GOV_DEPS = domain_admin/platform_admin）；
  // 提交对账用 dimension:reconcile（metric_owner 等可提交，但不能复核他人对账）
  const [isGov, setIsGov] = useState(false);
  // 对账表每页条数（持久化，用户可自定义）
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.reconciliations.pageSize", 20);

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
    fetchCurrentUser()
      .then((u) => setIsGov(u.role === "domain_admin" || u.role === "platform_admin"))
      .catch(() => {});
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
            <Button size="small" type="primary" disabled={!isGov} onClick={() => handleReview(r, "APPROVED")}>通过</Button>
            <Button size="small" danger disabled={!isGov} onClick={() => handleReview(r, "REJECTED")}>驳回</Button>
          </Space>
        ) : null,
    },
  ];

  return (
    <div>
      {/* 对账用途说明：对比语义端与应用端口径是否一致，保证数据可信 */}
      <div
        style={{
          marginBottom: 12,
          padding: "10px 12px",
          background: "var(--bg-elevated, #fafafa)",
          border: "1px solid var(--line-soft, #eef1f5)",
          borderRadius: 6,
          fontSize: 13,
          color: "var(--text-2)",
          lineHeight: 1.7,
        }}
      >
        <b>口径对账</b>用于校验<b>同一指标在"语义端"与"业务端"计算口径是否一致</b>——
        防止指标定义与业务实际执行发生漂移（如语义端口径改了、业务端还是旧的）。
        <br />
        <span className="muted">
          提交后由治理人员在「待复核」中通过（口径一致）或驳回（存在漂移需修正）。
          状态含义：<Tag color="warning">待复核</Tag>等待治理确认 ·{" "}
          <Tag color="success">已通过</Tag>口径一致 · <Tag color="error">已驳回</Tag>存在漂移。
        </span>
      </div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "flex-end" }}>
        {can("dimension:reconcile") && (
          <Button type="primary" icon={<SendOutlined />} onClick={() => setModalOpen(true)}>提交对账</Button>
        )}
      </div>
      <Table dataSource={items} columns={columns} rowKey="id" loading={loading} pagination={{ pageSize, showSizeChanger: true, onShowSizeChange }} locale={{ emptyText: "暂无对账记录" }} />

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
  const navigate = useNavigate();

  // 统一返回上一入口：优先回退浏览器历史（总览资产卡片/全局搜索等入口），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  const tabItems = [
    { key: "dims", label: "维度列表", children: <DimensionsTab /> },
    {
      key: "members",
      label: "维度值管理",
      children: <MembersTab />,
    },
    { key: "mappings", label: "维度映射", children: <MappingsTab /> },
    { key: "reconcile", label: "对账记录", children: <ReconciliationsTab /> },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
            返回
          </Button>
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
