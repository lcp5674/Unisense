import { useEffect, useMemo, useState } from "react";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, message, Tabs, Space, Alert, Descriptions, Checkbox, Popconfirm, Tooltip } from "antd";
import { PlusOutlined, SafetyCertificateOutlined, ExperimentOutlined, SearchOutlined, AuditOutlined, DeleteOutlined, SettingOutlined, TeamOutlined } from "@ant-design/icons";
import {
  fetchMyPermissions,
  listGrants,
  createGrant,
  revokeGrant,
  batchGrant,
  listRolePermissions,
  setRolePermissions,
  resetRolePermissions,
  deleteRole,
  listActionRegistry,
  createRole,
  checkPermission,
  piiReviewAction,
  classificationRescan,
  requestErasure,
  listUsers,
  listDomainTree,
  listMetrics,
  listRoleOptions,
  listDictItems,
  UnisenseApiError,
} from "../api";
import type { ActionRegistryItem, GrantBatchResult, GrantCreate, GrantResponse, PermissionSnapshot, RoleOption, RolePermissionItem, SubjectDomainTreeNode, UserBrief } from "../types";
import { formatCnTime } from "../utils/timeCn";
import { usePermission } from "../hooks/usePermission";
import { UserPermModal, groupRegistry, categoryOf, UI_CATEGORIES } from "../components/governance/UserPermModal";
import type { UiCategory } from "../components/governance/UserPermModal";
import { SidebarPermPanel } from "../components/governance/SidebarPermPanel";

// 主题域树 → 扁平化下拉选项（保留层级缩进，与用户管理/数据源页「业务域」下拉同款实现）
function flattenDomains(
  nodes: SubjectDomainTreeNode[],
  depth = 0,
  out: Array<{ value: string; label: string }> = [],
): Array<{ value: string; label: string }> {
  for (const n of nodes) {
    const indent = depth > 0 ? `${"　".repeat(depth)}` : "";
    out.push({ value: n.code, label: `${indent}${n.name}（${n.code}）` });
    if (n.children?.length) flattenDomains(n.children, depth + 1, out);
  }
  return out;
}

// ---- 可拖拽列宽（无额外依赖）：表头单元格渲染拖拽手柄，mouse 事件调整列宽 ----
interface ResizeHandleProps extends React.HTMLAttributes<HTMLTableHeaderCellElement> {
  onResize?: (width: number) => void;
  width?: number;
}

function ResizableTitle(props: ResizeHandleProps) {
  const { onResize, width, ...restProps } = props;
  if (!width) return <th {...restProps} />;
  return (
    <th
      {...restProps}
      style={{ ...(restProps.style ?? {}), position: "relative" }}
    >
      {restProps.children}
      <div
        onMouseDown={(e) => {
          e.preventDefault();
          const startX = e.clientX;
          const startW = width;
          const onMove = (ev: MouseEvent) => {
            const next = Math.max(60, startW + ev.clientX - startX);
            onResize?.(Math.round(next));
          };
          const onUp = () => {
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", onUp);
          };
          document.addEventListener("mousemove", onMove);
          document.addEventListener("mouseup", onUp);
        }}
        style={{
          position: "absolute",
          right: 0,
          top: 0,
          bottom: 0,
          width: 8,
          cursor: "col-resize",
          userSelect: "none",
          touchAction: "none",
        }}
      />
    </th>
  );
}

interface ResizableColumn {
  key: string;
  width?: number;
}

/** 为列集附加可拖拽能力：返回 [新列, 表格 components 配置]。宽度变更由调用方 state 持有。 */
function useResizableColumns<T extends ResizableColumn>(
  base: T[],
  widths: Record<string, number>,
  setWidths: (updater: (prev: Record<string, number>) => Record<string, number>) => void,
): [T[], { header: { cell: typeof ResizableTitle } }] {
  const cols = base.map((c) => {
    const key = c.key;
    const width = widths[key] ?? c.width;
    return {
      ...c,
      width,
      onHeaderCell: (col: ResizableColumn) => ({
        width: widths[col.key] ?? col.width,
        onResize: (w: number) => setWidths((prev) => ({ ...prev, [col.key]: w })),
      }),
    };
  });
  return [cols as T[], { header: { cell: ResizableTitle } }];
}

const GRANT_TYPE_LABEL: Record<string, string> = {
  READ: "只读",
  WRITE: "写",
  READ_WRITE: "读写",
};

const GRANT_STATUS_LABEL: Record<string, string> = {
  ACTIVE: "生效",
  EXPIRED: "已过期",
  REVOKED: "已回收",
};

const ACTION_LABEL: Record<string, string> = {
  read: "读取",
  write: "写入",
  approve: "审批",
  export: "导出",
  review: "复核",
};

const SENSITIVITY_LABEL: Record<string, string> = {
  PUBLIC: "公开",
  INTERNAL: "内部",
  CONFIDENTIAL: "机密",
  PII: "PII 敏感",
  // db_catalog.sensitivity_level 终态（对齐 Catalogs/AssetMap/Dashboard）
  NEEDS_REVIEW: "待复核",
  // 兼容旧数据/降级标记展示（classification 表仅存，不可人工赋值）
  UNKNOWN: "未知",
};

const MASK_POLICY_LABEL: Record<string, string> = {
  none: "无",
  mask: "掩码",
  hash: "哈希",
  deny: "拒绝访问",
};

const DECISION_LABEL: Record<string, string> = {
  APPROVE: "通过",
  REJECT: "拒绝",
};

// PII 复核可选字段：优先从字典接口拉取（pii_field_type，2026-08-28 字典化——
// 后端新增 PII 规则时前端无需发版）；拉取失败/空时回退以下硬编码兜底。
const FALLBACK_PII_FIELD_OPTIONS = [
  { value: "user_phone", label: "手机号" },
  { value: "id_card", label: "身份证号" },
  { value: "email", label: "邮箱" },
  { value: "bank_card", label: "银行卡号" },
  { value: "real_name", label: "真实姓名" },
  { value: "address", label: "住址" },
  { value: "passport", label: "护照号" },
  { value: "gps", label: "定位/GPS" },
].map((o) => ({ value: o.value, label: `${o.value}（${o.label}）` }));

function PermissionsTab() {
  const [snap, setSnap] = useState<PermissionSnapshot | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchMyPermissions()
      .then(setSnap)
      .catch(() => setSnap(null))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return null;
  if (!snap) return <Alert type="error" message="无法加载权限快照" showIcon />;

  return (
    <Card title="我的权限快照">
      <Descriptions column={2} bordered size="small">
        <Descriptions.Item label="用户">{snap.user_id}</Descriptions.Item>
        <Descriptions.Item label="角色">{snap.role}</Descriptions.Item>
        <Descriptions.Item label="归属域">{snap.home_domain ?? "全局"}</Descriptions.Item>
        <Descriptions.Item label="行级受限">{snap.row_level_restricted ? "是" : "否"}</Descriptions.Item>
        <Descriptions.Item label="可用操作" span={2}>
          <Space wrap>
            {snap.allowed_actions.map((a) => <Tag key={a}>{ACTION_LABEL[a] ?? a}</Tag>)}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="授权域" span={2}>
          <Space wrap>
            {snap.granted_domains.length ? snap.granted_domains.map((d) => <Tag key={d}>{d}</Tag>) : <span className="muted">无</span>}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="指标白名单" span={2}>
          <span className="mono" style={{ fontSize: 12 }}>{snap.metric_whitelist.join(", ") || "无限制"}</span>
        </Descriptions.Item>
      </Descriptions>
      {snap.expiring_soon.length > 0 && (
        <Alert type="warning" showIcon style={{ marginTop: 12 }} message={`${snap.expiring_soon.length} 个授权即将到期`} />
      )}
    </Card>
  );
}

function GrantsTab() {
  const { can } = usePermission();
  const [items, setItems] = useState<GrantResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [status, setStatus] = useState("");
  const [users, setUsers] = useState<UserBrief[]>([]);
  const [domainOptions, setDomainOptions] = useState<Array<{ value: string; label: string }>>([]);
  // 指标白名单选项（供授权弹窗选项框选择，替代手动逗号输入）
  const [metricOptions, setMetricOptions] = useState<Array<{ value: string; label: string }>>([]);
  // 角色行选项（grants.role_id → 角色名，授权弹窗下拉与列表展示）
  const [roleOptions, setRoleOptions] = useState<RoleOption[]>([]);
  // 可拖拽列宽（用户可手动左右调整）
  const [colWidths, setColWidths] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();
  // 批量授权（接线后端 POST /grants/batch，dry-run 预览后确认）
  const [batchModalOpen, setBatchModalOpen] = useState(false);
  const [batchPreview, setBatchPreview] = useState<GrantBatchResult | null>(null);
  const [batchLoading, setBatchLoading] = useState(false);
  const [batchForm] = Form.useForm();
  // 授权表格行选择（勾选后可在页内直接为该用户/多用户授权，不跳转用户管理）
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  // 「按用户授权」矩阵（给指定用户直挂按钮权限点）
  const [permUserId, setPermUserId] = useState<number | null>(null);
  // 「管理用户」→ 在页内打开预填该用户的批量授权弹窗
  function openGrantForUser(userId: number) {
    batchForm.setFieldsValue({ user_ids: [userId] });
    setBatchPreview(null);
    setBatchModalOpen(true);
  }
  // 「批量授权」→ 预填表格勾选用户（若有），否则清空让用户自行选择
  function openBatchForSelection() {
    const keys = selectedRowKeys.map(Number);
    batchForm.setFieldsValue({ user_ids: keys.length ? keys : [] });
    setBatchPreview(null);
    setBatchModalOpen(true);
  }

  useEffect(() => {
    listUsers()
      .then(setUsers)
      .catch(() => setUsers([]));
    listDomainTree("active")
      .then((tree) => setDomainOptions(flattenDomains(tree)))
      .catch(() => setDomainOptions([]));
    // 指标白名单选项：拉取已发布指标（编码 + 名称，供搜索选择）
    listMetrics({ status: "PUBLISHED", page: 1, page_size: 100 })
      .then((res) =>
        setMetricOptions(
          res.items.map((m) => ({ value: m.metric_code, label: `${m.metric_code}（${m.name}）` })),
        ),
      )
      .catch(() => setMetricOptions([]));
    // 角色行选项（grants.role_id → 角色名，授权弹窗与列表展示）
    listRoleOptions()
      .then(setRoleOptions)
      .catch(() => setRoleOptions([]));
  }, []);

  // user_id → 用户名/显示名 映射（授权记录归属可读）
  const userMap = useMemo(() => {
    const m = new Map<number, UserBrief>();
    for (const u of users) m.set(u.id, u);
    return m;
  }, [users]);

  // role_id → 角色名 映射（授权列表「角色」列显示名称而非裸数字）
  const roleMap = useMemo(() => {
    const m = new Map<number, RoleOption>();
    for (const r of roleOptions) m.set(r.id, r);
    return m;
  }, [roleOptions]);

  async function load() {
    setLoading(true);
    try {
      const res = await listGrants({ status, page, page_size: pageSize });
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
  }, [page, pageSize, status]);

  async function handleCreate(values: Record<string, unknown>) {
    try {
      await createGrant({
        user_id: Number(values.user_id),
        role_id: values.role_id ? Number(values.role_id) : null,
        domain: values.domain ? String(values.domain) : null,
        metric_whitelist:
          Array.isArray(values.metric_whitelist) && values.metric_whitelist.length
            ? values.metric_whitelist.map(String)
            : null,
        grant_type: String(values.grant_type ?? "READ"),
        row_level: Boolean(values.row_level),
        reason: values.reason ? String(values.reason) : null,
      });
      message.success("授权已创建");
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "创建失败");
    }
  }

  async function handleRevoke(g: GrantResponse) {
    try {
      await revokeGrant(g.id, "前台手动回收");
      message.success("已回收");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "回收失败");
    }
  }

  // 批量授权：将同一授权参数应用到多个用户，生成 GrantCreate 列表
  function buildBatchItems(values: Record<string, unknown>): GrantCreate[] {
    const users = Array.isArray(values.user_ids) ? values.user_ids.map(Number) : [];
    const whitelist =
      Array.isArray(values.metric_whitelist) && values.metric_whitelist.length
        ? values.metric_whitelist.map(String)
        : null;
    return users.map((uid) => ({
      user_id: uid,
      role_id: null,
      domain: values.domain ? String(values.domain) : null,
      metric_whitelist: whitelist,
      grant_type: String(values.grant_type ?? "READ"),
      row_level: Boolean(values.row_level),
      reason: values.reason ? String(values.reason) : null,
    }));
  }

  async function handleBatchPreview() {
    const values = await batchForm.validateFields().catch(() => null);
    if (!values) return;
    if (!Array.isArray(values.user_ids) || values.user_ids.length === 0) {
      message.warning("请选择至少一个用户");
      return;
    }
    setBatchLoading(true);
    try {
      const res = await batchGrant(buildBatchItems(values), "grant", true);
      setBatchPreview(res);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "预览失败");
    } finally {
      setBatchLoading(false);
    }
  }

  async function handleBatchConfirm() {
    const values = await batchForm.validateFields().catch(() => null);
    if (!values) return;
    setBatchLoading(true);
    try {
      const res = await batchGrant(buildBatchItems(values), "grant", false);
      message.success(`批量授权完成：成功 ${res.succeeded} · 失败 ${res.failed}`);
      setBatchModalOpen(false);
      setBatchPreview(null);
      batchForm.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量授权失败");
    } finally {
      setBatchLoading(false);
    }
  }

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 70 },
    {
      title: "用户",
      dataIndex: "user_id",
      key: "user",
      width: 200,
      render: (v: number) => {
        const u = userMap.get(v);
        return u ? (
          <Space size={4}>
            <span>{u.username}</span>
            <span className="muted" style={{ fontSize: 12 }}>（{u.display_name}）</span>
          </Space>
        ) : (
          <span className="mono">#{v}</span>
        );
      },
    },
    { title: "角色", dataIndex: "role_id", key: "role", width: 120, render: (v: number | null) => {
      if (!v) return <Tag>角色挂起</Tag>;
      const r = roleMap.get(v);
      return r ? <span>{ROLE_LABEL[r.name] ?? r.name}{r.is_custom ? <Tag style={{ marginLeft: 4 }} color="blue">自定义</Tag> : null}</span> : <span className="mono">#{v}</span>;
    } },
    { title: "域", dataIndex: "domain", key: "domain", width: 120, render: (v: string | null) => v ?? <span className="muted">全部</span> },
    { title: "授权类型", dataIndex: "grant_type", key: "type", width: 110, render: (v: string) => <Tag color={v === "READ_WRITE" ? "warning" : v === "WRITE" ? "orange" : "default"}>{GRANT_TYPE_LABEL[v] ?? v}</Tag> },
    { title: "行级", dataIndex: "row_level", key: "row", width: 70, render: (v: boolean) => (v ? "是" : "否") },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (v: string) => <Tag color={v === "ACTIVE" ? "success" : v === "EXPIRED" ? "default" : "error"}>{GRANT_STATUS_LABEL[v] ?? v}</Tag>,
    },
    { title: "到期", dataIndex: "expires_at", key: "expires", width: 160, render: (v: string | null) => (v ? <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> : <span className="muted">长期</span>) },
    {
      title: "指标白名单",
      dataIndex: "metric_whitelist",
      key: "whitelist",
      width: 160,
      render: (v: string[] | null) =>
        v && v.length ? (
          <span className="mono" style={{ fontSize: 12 }}>{v.join(", ")}</span>
        ) : (
          <span className="muted">全部</span>
        ),
    },
    {
      title: "操作",
      key: "actions",
      width: 150,
      render: (_: unknown, g: GrantResponse) => (
        <Space size={4}>
          {can("grant:create") && <Button size="small" icon={<TeamOutlined />} onClick={() => openGrantForUser(g.user_id)}>给该用户授权</Button>}
          {can("role:edit") && <Button size="small" icon={<SettingOutlined />} onClick={() => setPermUserId(g.user_id)}>按钮权限</Button>}
          {g.status === "ACTIVE" && can("grant:revoke") ? <Button size="small" danger onClick={() => handleRevoke(g)}>回收</Button> : null}
        </Space>
      ),
    },
  ];

  // 可拖拽列宽（手动左右调整列宽，间距自适应）
  const [resizableColumns, resizableComponents] = useResizableColumns(columns, colWidths, setColWidths);

  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <Select
          allowClear
          placeholder="全部状态"
          style={{ width: 140 }}
          value={status || undefined}
          onChange={(v) => { setStatus(v || ""); setPage(1); }}
          options={[{ value: "ACTIVE", label: "生效" }, { value: "EXPIRED", label: "过期" }, { value: "REVOKED", label: "已回收" }]}
        />
        {can("grant:create") && <Button type="primary" icon={<PlusOutlined />} onClick={() => { setModalOpen(true); }}>新建授权</Button>}
        {can("grant:create") && <Button icon={<AuditOutlined />} onClick={openBatchForSelection}>批量授权{selectedRowKeys.length ? `（${selectedRowKeys.length}）` : ""}</Button>}
      </Space>
      <Table
        dataSource={items}
        columns={resizableColumns}
        components={resizableComponents}
        rowKey="id"
        rowSelection={{
          selectedRowKeys,
          onChange: setSelectedRowKeys,
          preserveSelectedRowKeys: true,
        }}
        loading={loading}
        scroll={{ x: 1100 }}
        pagination={{ current: page, pageSize, total, showSizeChanger: true, pageSizeOptions: [10, 20, 50, 100], onChange: (p, ps) => { setPage(p); setPageSize(ps); }, showTotal: (t) => `共 ${t} 条` }}
        locale={{ emptyText: "暂无授权记录" }}
      />

      <Modal title="新建授权" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="授权">
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Space size={16} style={{ width: "100%" }}>
            <Form.Item name="user_id" label="用户" rules={[{ required: true, message: "请选择用户" }]}>
              <Select
                showSearch
                optionFilterProp="label"
                style={{ width: 220 }}
                placeholder="按用户名 / 显示名搜索"
                options={users.map((u) => ({
                  value: u.id,
                  label: `${u.username}（${u.display_name}）`,
                }))}
              />
            </Form.Item>
            <Form.Item name="role_id" label="角色（可留空）" extra="从既有角色中选择（含自定义角色）">
              <Select
                allowClear
                showSearch
                optionFilterProp="label"
                placeholder="选择角色"
                style={{ width: 220 }}
                options={roleOptions.map((r) => ({
                  value: r.id,
                  label: `${ROLE_LABEL[r.name] ?? r.name}${r.is_custom ? "（自定义）" : ""}`,
                }))}
              />
            </Form.Item>
          </Space>
          <Space size={16} style={{ width: "100%" }}>
            <Form.Item name="grant_type" label="授权类型" initialValue="READ">
              <Select style={{ width: 160 }} options={[{ value: "READ", label: "只读" }, { value: "WRITE", label: "写" }, { value: "READ_WRITE", label: "读写" }]} />
            </Form.Item>
            <Form.Item name="row_level" label="行级受限">
              <Select style={{ width: 160 }} options={[{ value: false, label: "否" }, { value: true, label: "是" }]} />
            </Form.Item>
          </Space>
          <Form.Item name="domain" label="授权域（留空为全部）">
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="选择主题域"
              options={domainOptions}
            />
          </Form.Item>
          <Form.Item
            name="metric_whitelist"
            label="指标白名单（可多选，留空=不限制）"
            extra="从已发布指标中选择，替代手动输入编码"
          >
            <Select
              mode="multiple"
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="搜索并选择指标编码"
              options={metricOptions}
              tokenSeparators={[","]}
            />
          </Form.Item>
          <Form.Item name="reason" label="授权原因">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="批量授权（同一参数应用到多个用户）"
        open={batchModalOpen}
        onCancel={() => { setBatchModalOpen(false); setBatchPreview(null); }}
        width={680}
        footer={[
          <Button key="cancel" onClick={() => { setBatchModalOpen(false); setBatchPreview(null); }}>取消</Button>,
          <Button key="preview" icon={<SearchOutlined />} loading={batchLoading} onClick={handleBatchPreview}>预览影响</Button>,
          <Button key="confirm" type="primary" disabled={!batchPreview} loading={batchLoading} onClick={handleBatchConfirm}>确认授权</Button>,
        ]}
      >
        <Form form={batchForm} layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item name="user_ids" label="授权用户（可多选）" rules={[{ required: true, message: "请选择至少一个用户" }]}>
            <Select
              mode="multiple"
              showSearch
              optionFilterProp="label"
              placeholder="按用户名 / 显示名搜索"
              options={users.map((u) => ({
                value: u.id,
                label: `${u.username}（${u.display_name}）`,
              }))}
            />
          </Form.Item>
          <Space size={16} style={{ width: "100%" }}>
            <Form.Item name="grant_type" label="授权类型" initialValue="READ">
              <Select style={{ width: 160 }} options={[{ value: "READ", label: "只读" }, { value: "WRITE", label: "写" }, { value: "READ_WRITE", label: "读写" }]} />
            </Form.Item>
            <Form.Item name="row_level" label="行级受限">
              <Select style={{ width: 160 }} options={[{ value: false, label: "否" }, { value: true, label: "是" }]} />
            </Form.Item>
          </Space>
          <Form.Item name="domain" label="授权域（留空为全部）">
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="选择主题域"
              options={domainOptions}
            />
          </Form.Item>
          <Form.Item
            name="metric_whitelist"
            label="指标白名单（可多选，留空=不限制）"
            extra="从已发布指标中选择，替代手动输入编码"
          >
            <Select
              mode="multiple"
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="搜索并选择指标编码"
              options={metricOptions}
              tokenSeparators={[","]}
            />
          </Form.Item>
          <Form.Item name="reason" label="授权原因">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
        {batchPreview && (
          <Alert
            type="info"
            showIcon
            style={{ marginTop: 8 }}
            message={`预览：影响 ${batchPreview.affected_users} 个用户 · ${batchPreview.affected_metrics} 个指标 · 可执行 ${batchPreview.succeeded} · 不可执行 ${batchPreview.failed}`}
            description={
              <div style={{ maxHeight: 160, overflow: "auto" }}>
                {batchPreview.items.map((it) => (
                  <div key={`${it.user_id}-${it.domain ?? "all"}`} style={{ fontSize: 12, marginBottom: 4 }}>
                    <Tag color={it.ok ? "success" : "error"}>#{it.user_id}</Tag>
                    <span className="mono">{it.detail}</span>
                  </div>
                ))}
              </div>
            }
          />
        )}
      </Modal>
      {permUserId !== null && (
        <UserPermModal
          userId={permUserId}
          userName={(() => {
            const u = userMap.get(permUserId);
            return u ? `${u.username}（${u.display_name}）` : `用户 #${permUserId}`;
          })()}
          open
          onClose={() => setPermUserId(null)}
        />
      )}
    </div>
  );
}

const ROLE_LABEL: Record<string, string> = {
  platform_admin: "平台管理员",
  domain_admin: "域管理员",
  metric_owner: "指标负责人",
  reviewer: "评审员",
  compliance_officer: "合规官",
  analyst: "分析师（存量兼容）",
  viewer: "只读用户",
};

const ACTION_ORDER = ["read", "write", "approve", "export", "review"];

// 业务只读 view 集合（「常用组合」预设的公共基础）：数据消费侧页面，不含账号/治理/审计等管理视图
const VIEW_BASE = [
  "dashboard:view",
  "todo:view",
  "notifications:view",
  "favorites:view",
  "catalog:view",
  "compare:view",
  "templates:view",
  "assetmap:view",
  "lineage:view",
  "quality:view",
  "review:view",
  "query:view",
  "ai:view",
  "dimensions:view",
  "glossary:view",
  "data-sources:view",
  "catalogs:view",
  "collection-tasks:view",
  "collection-history:view",
  "feedback:view",
  "guide:view",
];

/** 角色「常用组合」预设：一键勾选常用权限点组合（可在此基础上微调再保存）。
 *
 * 引用注册表真实权限点；应用时仅合并注册表中存在的点（防未来权限点删除导致后端 422）。
 * 供批量建角色的授权者快速套用，避免从 90+ 按钮逐个勾选。
 */
const ROLE_PRESETS: Array<{ key: string; name: string; description: string; actions: string[] }> = [
  {
    key: "readonly_analyst",
    name: "只读分析师",
    description: "数据消费：浏览指标/资产/血缘/质量/维度/术语/查询并可执行查询，无任何写权限",
    actions: [...VIEW_BASE, "query:execute"],
  },
  {
    key: "metric_ops",
    name: "指标运营",
    description: "在只读基础上可创建/编辑/评审/废弃指标、批量导入、实例化模板、AI 推断描述",
    actions: [
      ...VIEW_BASE,
      "query:execute",
      "metric:create",
      "metric:edit",
      "metric:deprecate",
      "metric:export",
      "metric:review",
      "metric:import",
      "metric:infer-description",
      "template:instantiate",
      "template:assign-owner",
    ],
  },
  {
    key: "domain_admin",
    name: "域管理员",
    description:
      "业务全写：数据源/采集/资产/血缘/质量/维度/术语/用户组织/域授权/主题域字典的管理能力（含评审、导出、LLM 推断）",
    actions: [
      ...VIEW_BASE,
      "query:execute",
      "ai:nl2sql",
      "metric:create",
      "metric:edit",
      "metric:approve",
      "metric:deprecate",
      "metric:export",
      "metric:review",
      "metric:emergency-publish",
      "metric:import",
      "metric:infer-description",
      "template:instantiate",
      "template:assign-owner",
      "assetmap:edit",
      "assetmap:export",
      "lineage:write",
      "lineage:manage-edge",
      "quality:run-check",
      "quality:config-rule",
      "review:arbitrate",
      "review:escalate",
      "review:close",
      "review:reopen",
      "dimension:create",
      "dimension:edit",
      "dimension:deprecate",
      "dimension:mapping",
      "dimension:reconcile",
      "glossary:infer",
      "glossary:create",
      "glossary:edit",
      "glossary:deprecate",
      "data-source:create",
      "data-source:edit",
      "data-source:delete",
      "data-source:test-connection",
      "data-source:collect",
      "catalog:deprecate",
      "catalog:edit-description",
      "catalog:infer-description",
      "user:create",
      "user:edit",
      "user:disable",
      "user:reset-password",
      "user:batch-status",
      "org:create",
      "org:edit",
      "org:disable",
      "grant:create",
      "grant:revoke",
      "grant:export",
      "role:create",
      "role:edit",
      "role:delete",
      "domain:create",
      "dict:create",
      "governance:view",
      "users:view",
      "organizations:view",
      "domains:view",
      "dicts:view",
      "audit:view",
      "audit:export",
    ],
  },
  {
    key: "compliance",
    name: "合规复核员",
    description: "PII 合规复核/校验、分级重扫、被遗忘权执行、审计查看导出 + 业务只读",
    actions: [
      ...VIEW_BASE,
      "governance:view",
      "audit:view",
      "audit:export",
      "pii:review",
      "pii:validate",
      "classification:rescan",
      "erasure:execute",
    ],
  },
];

function RolesTab() {
  const { can } = usePermission();
  const [items, setItems] = useState<RolePermissionItem[]>([]);
  const [registry, setRegistry] = useState<ActionRegistryItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [savingRole, setSavingRole] = useState<string | null>(null);
  // 草稿：role → 勾选中的资源动作集合（read/write/approve/export/review，未编辑无草稿）
  const [draft, setDraft] = useState<Record<string, string[]>>({});
  // UI 权限点草稿：role → 勾选中的按钮级权限点集合
  const [uiDraft, setUiDraft] = useState<Record<string, string[]>>({});
  const [createOpen, setCreateOpen] = useState(false);
  const [uiConfigRole, setUiConfigRole] = useState<string | null>(null);
  const [createForm] = Form.useForm<{ name: string; description?: string }>();

  useEffect(() => {
    setLoading(true);
    Promise.all([listRolePermissions(), listActionRegistry()])
      .then(([roleItems, reg]) => {
        setItems(roleItems);
        setRegistry(reg);
      })
      .catch((err) => {
        message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
      })
      .finally(() => setLoading(false));
  }, []);

  function checkedActions(role: string): string[] {
    return draft[role] ?? items.find((i) => i.role === role)?.effective_actions ?? [];
  }

  function checkedUiActions(role: string): string[] {
    return uiDraft[role] ?? items.find((i) => i.role === role)?.ui_effective_actions ?? [];
  }

  // 「常用组合」一键套用：将预设权限点合并进当前勾选（仅应用注册表中真实存在的点，防删点后 422）
  function applyPreset(role: string, preset: (typeof ROLE_PRESETS)[number]) {
    const valid = new Set(registry.map((r) => r.action));
    const current = new Set(checkedUiActions(role));
    for (const a of preset.actions) if (valid.has(a)) current.add(a);
    setUiDraft((prev) => ({ ...prev, [role]: [...current] }));
    message.info(`已套用「${preset.name}」：合并 ${[...current].filter((a) => preset.actions.includes(a)).length} 项权限点，可在此基础上微调后保存`);
  }

  // 提交时合并资源动作 + UI 权限点（后端 role_permission 整表替换，二者须一并写入）
  function combinedActions(role: string): string[] {
    return [...new Set([...checkedActions(role), ...checkedUiActions(role)])];
  }

  async function refreshItems() {
    try {
      setItems(await listRolePermissions());
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "刷新失败");
    }
  }

  function clearDrafts(role: string) {
    setDraft((prev) => {
      const next = { ...prev };
      delete next[role];
      return next;
    });
    setUiDraft((prev) => {
      const next = { ...prev };
      delete next[role];
      return next;
    });
  }

  async function handleSave(role: string) {
    setSavingRole(role);
    try {
      const updated = await setRolePermissions(role, combinedActions(role));
      message.success(`已更新「${ROLE_LABEL[role] ?? role}」的权限点`);
      clearDrafts(role);
      setItems((prev) =>
        prev.map((i) =>
          i.role === role ? { ...updated, protected: i.protected, is_custom: i.is_custom } : i,
        ),
      );
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "保存失败");
    } finally {
      setSavingRole(null);
    }
  }

  async function handleReset(role: string) {
    setSavingRole(role);
    try {
      const updated = await resetRolePermissions(role);
      message.success(`已恢复「${ROLE_LABEL[role] ?? role}」默认权限点`);
      clearDrafts(role);
      setItems((prev) =>
        prev.map((i) =>
          i.role === role ? { ...updated, protected: i.protected, is_custom: i.is_custom } : i,
        ),
      );
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "重置失败");
    } finally {
      setSavingRole(null);
    }
  }

  async function handleDelete(role: string) {
    try {
      await deleteRole(role);
      message.success(`已删除自定义角色「${role}」`);
      await refreshItems();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "删除失败");
    }
  }

  async function handleCreateCustom() {
    const values = await createForm.validateFields().catch(() => null);
    if (!values) return;
    try {
      await createRole({ name: values.name, description: values.description ?? null, is_custom: true });
      message.success(`已创建自定义角色「${values.name}」，请勾选权限点后保存`);
      createForm.resetFields();
      setCreateOpen(false);
      await refreshItems();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "创建失败");
    }
  }

  const columns = [
    {
      title: "角色",
      dataIndex: "role",
      key: "role",
      width: 220,
      render: (v: string, r: RolePermissionItem) => (
        <Space direction="vertical" size={2}>
          <Space size={4}>
            <Tag color={v === "platform_admin" ? "gold" : undefined}>{ROLE_LABEL[v] ?? v}</Tag>
            {r.is_custom && <Tag color="blue">自定义</Tag>}
            {r.protected && <Tag color="red">受保护</Tag>}
          </Space>
          <span className="muted" style={{ fontSize: 12 }}>
            {r.protected
              ? "受保护角色 · 不可配置"
              : r.is_custom
                ? `已配置 UI 权限点 ${r.ui_effective_actions.length} 个`
                : `默认资源动作：${r.default_actions.map((a) => ACTION_LABEL[a] ?? a).join("、") || "无"}`}
          </span>
        </Space>
      ),
    },
    {
      title: "本域权限点（资源级）",
      key: "actions",
      render: (_: unknown, r: RolePermissionItem) => (
        <Checkbox.Group
          disabled={r.protected}
          value={checkedActions(r.role)}
          onChange={(vals) => setDraft((prev) => ({ ...prev, [r.role]: [...vals].sort() }))}
        >
          <Space wrap>
            {ACTION_ORDER.map((a) => (
              <Checkbox key={a} value={a} onClick={(e) => e.stopPropagation()}>
                {ACTION_LABEL[a] ?? a}
              </Checkbox>
            ))}
          </Space>
        </Checkbox.Group>
      ),
    },
    {
      title: "按钮级权限点",
      key: "ui",
      width: 260,
      render: (_: unknown, r: RolePermissionItem) => {
        const uis = checkedUiActions(r.role);
        return (
          <Space direction="vertical" size={4}>
            <Space size={4} wrap>
              {uis.length === 0 ? (
                <span className="muted">未配置</span>
              ) : (
                uis.slice(0, 4).map((a) => <Tag key={a} style={{ maxWidth: 150 }}>{a}</Tag>)
              )}
              {uis.length > 4 && <Tag>+{uis.length - 4}</Tag>}
            </Space>
            <Button
              size="small"
              icon={<SettingOutlined />}
              disabled={r.protected || !can("role:edit")}
              onClick={() => setUiConfigRole(r.role)}
            >
              配置
            </Button>
          </Space>
        );
      },
    },
    {
      title: "操作",
      key: "ops",
      width: 230,
      render: (_: unknown, r: RolePermissionItem) => {
        const dirty = draft[r.role] !== undefined || uiDraft[r.role] !== undefined;
        return (
          <Space>
            <Button
              type="primary"
              size="small"
              disabled={r.protected || !dirty || !can("role:edit")}
              loading={savingRole === r.role}
              onClick={() => handleSave(r.role)}
            >
              保存
            </Button>
            <Button
              size="small"
              disabled={r.protected || (!dirty && r.custom_actions === null && r.ui_custom_actions === null) || !can("role:edit")}
              onClick={() => handleReset(r.role)}
            >
              恢复默认
            </Button>
            <Popconfirm
              title={`删除自定义角色「${r.role}」？`}
              description="该角色的权限点配置将被清除，占用该角色的用户需先改派。"
              okText="删除"
              okButtonProps={{ danger: true }}
              onConfirm={() => handleDelete(r.role)}
            >
              <Button size="small" danger icon={<DeleteOutlined />} disabled={!r.is_custom || !can("role:delete")}>
                删除
              </Button>
            </Popconfirm>
          </Space>
        );
      },
    },
  ];

  const grouped = groupRegistry(registry);
  // 按钮级配置弹窗的搜索词与动作类型分类（先模块后类型，帮助授权者快速定位按钮）
  const [uiSearch, setUiSearch] = useState("");
  const [uiCategory, setUiCategory] = useState<UiCategory>("all");
  // 配置弹窗视图：buttons=按钮权限点矩阵 / sidebar=侧边栏入口授权
  const [uiTab, setUiTab] = useState<"buttons" | "sidebar">("buttons");
  const uiGrouped = useMemo(() => {
    const kw = uiSearch.trim().toLowerCase();
    return grouped
      .map((g) => ({
        module: g.module,
        items: g.items.filter((it) => {
          if (uiCategory !== "all" && categoryOf(it.action) !== uiCategory) return false;
          if (
            kw &&
            !it.label.toLowerCase().includes(kw) &&
            !it.action.toLowerCase().includes(kw)
          ) {
            return false;
          }
          return true;
        }),
      }))
      .filter((g) => g.items.length > 0);
  }, [grouped, uiSearch, uiCategory]);

  return (
    <div>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="细粒度权限管控：按模块/按钮勾选权限点并保存即生效（前端 usePermission 实时读取；后端写接口仍按内置角色强制，二者不互相替代）。platform_admin 为受保护角色（跨域运维直通，不可配置）；可新建自定义角色并为其分配按钮级权限点。"
      />
      <Space style={{ marginBottom: 12 }}>
        {can("role:create") && (
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
            新建自定义角色
          </Button>
        )}
      </Space>
      <Table
        dataSource={items}
        columns={columns}
        rowKey="role"
        loading={loading}
        pagination={false}
        locale={{ emptyText: "暂无角色配置" }}
        size="small"
      />

      {/* 新建自定义角色 */}
      <Modal
        title="新建自定义角色"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={handleCreateCustom}
        okText="创建"
      >
        <Form form={createForm} layout="vertical">
          <Form.Item
            name="name"
            label="角色名"
            rules={[
              { required: true, message: "请输入角色名" },
              {
                pattern: /^[a-z][a-z0-9_]{1,31}$/,
                message: "小写字母开头，含小写字母/数字/下划线，2-32 位",
              },
            ]}
          >
            <Input placeholder="如 data_analyst" autoFocus />
          </Form.Item>
          <Form.Item name="description" label="角色说明">
            <Input.TextArea rows={2} placeholder="说明该角色的职责范围" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 按钮级权限点可视化配置 */}
      <Modal
        title={`配置「${ROLE_LABEL[uiConfigRole ?? ""] ?? uiConfigRole}」按钮级权限点`}
        open={uiConfigRole !== null}
        onCancel={() => setUiConfigRole(null)}
        onOk={() => {
          if (!can("role:edit")) {
            message.warning("无角色编辑权限（role:edit）");
            return;
          }
          if (uiConfigRole) void handleSave(uiConfigRole);
        }}
        okText="保存"
        width={780}
        okButtonProps={{ loading: uiConfigRole !== null && savingRole === uiConfigRole }}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="按模块勾选按钮级权限点（菜单显隐 / 页面按钮 / 组件）。可搜索按钮名/权限点并按类型筛选，快速定位；保存后写入 role_permission 覆盖表并即时生效；资源级本域权限点在上一列配置，二者保存时一并提交。"
        />
        <Tabs
          activeKey={uiTab}
          onChange={(k) => setUiTab(k as "buttons" | "sidebar")}
          items={[
            {
              key: "buttons",
              label: "按钮权限点",
              children: (
                <>
                  <div style={{ marginBottom: 12 }}>
                    <span className="muted" style={{ fontSize: 12, marginRight: 8 }}>
                      常用组合（一键套用后仍可微调）：
                    </span>
                    <Space wrap>
                      {ROLE_PRESETS.map((p) => (
                        <Tooltip key={p.key} title={p.description}>
                          <Button size="small" onClick={() => uiConfigRole && applyPreset(uiConfigRole, p)}>
                            {p.name}
                          </Button>
                        </Tooltip>
                      ))}
                    </Space>
                  </div>
                  <Space direction="vertical" style={{ width: "100%", marginBottom: 12 }} size={8}>
                    <Input.Search
                      placeholder="搜索按钮名称或权限点（如：导出 / audit:export）"
                      allowClear
                      value={uiSearch}
                      onChange={(e) => setUiSearch(e.target.value)}
                    />
                    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                      <Tabs
                        size="small"
                        activeKey={uiCategory}
                        onChange={(k) => setUiCategory(k as UiCategory)}
                        items={UI_CATEGORIES.map((c) => ({ key: c.key, label: c.label }))}
                        tabBarStyle={{ marginBottom: 0, flex: 1 }}
                      />
                      <span className="muted" style={{ whiteSpace: "nowrap", fontSize: 12 }}>
                        已勾选 {uiConfigRole ? checkedUiActions(uiConfigRole).length : 0} 项
                      </span>
                    </div>
                  </Space>
                  <div style={{ maxHeight: 420, overflow: "auto" }}>
                    {uiGrouped.length === 0 ? (
                      <div className="muted" style={{ textAlign: "center", padding: 24 }}>
                        无匹配的权限点
                      </div>
                    ) : (
                      uiGrouped.map((g) => (
                        <div key={g.module} style={{ marginBottom: 14 }}>
                          <div style={{ fontWeight: 600, marginBottom: 6 }}>{g.module}</div>
                          <Checkbox.Group
                            value={uiConfigRole ? checkedUiActions(uiConfigRole) : []}
                            onChange={(vals) => {
                              if (uiConfigRole) setUiDraft((prev) => ({ ...prev, [uiConfigRole]: [...vals] as string[] }));
                            }}
                          >
                            <Space wrap>
                              {g.items.map((it) => (
                                <Tooltip key={it.action} title={`${it.description}（${it.action}）`}>
                                  <Checkbox value={it.action}>{it.label}</Checkbox>
                                </Tooltip>
                              ))}
                            </Space>
                          </Checkbox.Group>
                        </div>
                      ))
                    )}
                  </div>
                </>
              ),
            },
            {
              key: "sidebar",
              label: "侧边栏入口",
              children: (
                <div style={{ maxHeight: 420, overflow: "auto" }}>
                  <SidebarPermPanel
                    checked={uiConfigRole ? checkedUiActions(uiConfigRole) : []}
                    onChange={(next) =>
                      uiConfigRole && setUiDraft((prev) => ({ ...prev, [uiConfigRole]: next }))
                    }
                  />
                </div>
              ),
            },
          ]}
        />
      </Modal>
    </div>
  );
}

function PiiReviewTab() {
  const { can } = usePermission();
  const [modalOpen, setModalOpen] = useState(false);
  const [rescanLoading, setRescanLoading] = useState(false);
  const [form] = Form.useForm();
  // 指标编码选项（PII 复核目标从已发布指标选择，替代手动输入）
  const [metricOptions, setMetricOptions] = useState<Array<{ value: string; label: string }>>([]);
  // PII 字段选项（字典驱动，回退硬编码）
  const [piiFieldOptions, setPiiFieldOptions] =
    useState<Array<{ value: string; label: string }>>(FALLBACK_PII_FIELD_OPTIONS);

  useEffect(() => {
    listMetrics({ status: "PUBLISHED", page: 1, page_size: 100 })
      .then((res) =>
        setMetricOptions(
          res.items.map((m) => ({ value: m.metric_code, label: `${m.metric_code}（${m.name}）` })),
        ),
      )
      .catch(() => setMetricOptions([]));
    listDictItems("pii_field_type")
      .then((items) => {
        if (items.length > 0) {
          setPiiFieldOptions(
            items.map((i) => ({ value: i.code, label: `${i.code}（${i.label}）` })),
          );
        }
      })
      .catch(() => {
        /* 字典拉取失败保留硬编码兜底 */
      });
  }, []);

  async function handleReview(values: Record<string, unknown>) {
    try {
      const res = await piiReviewAction({
        metric_code: String(values.metric_code),
        decision: String(values.decision) as "APPROVE" | "REJECT",
        sensitivity_level: String(values.sensitivity_level ?? "PII"),
        masking_policy: values.masking_policy ? String(values.masking_policy) : null,
        pii_columns:
          Array.isArray(values.pii_columns) && values.pii_columns.length
            ? values.pii_columns.map(String)
            : null,
        comment: String(values.comment),
      });
      message.success(`复核完成：${DECISION_LABEL[res.decision] ?? res.decision}`);
      setModalOpen(false);
      form.resetFields();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "复核失败");
    }
  }

  async function handleRescan() {
    setRescanLoading(true);
    try {
      const res = await classificationRescan({ limit: 200 });
      const r = res as { scanned: number; changed: number; pii_found: number; degraded: number };
      message.success(`重扫完成：扫描 ${r.scanned} · 变更 ${r.changed} · PII ${r.pii_found} · 降级 ${r.degraded}`);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "重扫失败");
    } finally {
      setRescanLoading(false);
    }
  }

  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        {can("pii:review") && <Button type="primary" icon={<SafetyCertificateOutlined />} onClick={() => setModalOpen(true)}>PII 人工复核</Button>}
        {can("classification:rescan") && <Button icon={<ExperimentOutlined />} loading={rescanLoading} onClick={handleRescan}>敏感度分类重扫</Button>}
      </Space>
      <Alert type="warning" showIcon message="PII 复核与分类重扫仅 compliance_officer / platform_admin 可执行；复核结果写入治理审计。" />

      <Modal title="PII 人工复核" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="提交复核">
        <Form form={form} layout="vertical" onFinish={handleReview} style={{ marginTop: 8 }}>
          <Form.Item name="metric_code" label="指标" rules={[{ required: true, message: "请选择指标" }]}>
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="从已发布指标选择（搜索编码/名称）"
              options={metricOptions}
            />
          </Form.Item>
          <Form.Item name="decision" label="决定" rules={[{ required: true }]} initialValue="APPROVE">
            <Select options={[{ value: "APPROVE", label: "通过（合规复核通过）" }, { value: "REJECT", label: "拒绝（退回）" }]} />
          </Form.Item>
          <Form.Item name="sensitivity_level" label="敏感度" initialValue="PII">
            <Select options={["PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII"].map((v) => ({ value: v, label: SENSITIVITY_LABEL[v] ?? v }))} />
          </Form.Item>
          <Form.Item name="masking_policy" label="脱敏策略">
            <Select allowClear options={[{ value: "none", label: "无" }, { value: "mask", label: "掩码" }, { value: "hash", label: "哈希" }, { value: "deny", label: "拒绝访问" }]} />
          </Form.Item>
          <Form.Item name="pii_columns" label="PII 字段（可多选，留空=沿用识别结果）">
            <Select
              mode="multiple"
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="搜索并选择敏感字段"
              options={piiFieldOptions}
              tokenSeparators={[","]}
            />
          </Form.Item>
          <Form.Item name="comment" label="复核意见" rules={[{ required: true, min: 1 }]}>
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

function CheckTab() {
  const [result, setResult] = useState<{ allow: boolean; reason: string; masking: string; restricted: boolean } | null>(null);
  const [users, setUsers] = useState<UserBrief[]>([]);
  const [domainOptions, setDomainOptions] = useState<Array<{ value: string; label: string }>>([]);
  const [metricOptions, setMetricOptions] = useState<Array<{ value: string; label: string }>>([]);
  const [form] = Form.useForm();

  useEffect(() => {
    listUsers()
      .then(setUsers)
      .catch(() => setUsers([]));
    listDomainTree("active")
      .then((tree) => setDomainOptions(flattenDomains(tree)))
      .catch(() => setDomainOptions([]));
    listMetrics({ status: "PUBLISHED", page: 1, page_size: 100 })
      .then((res) =>
        setMetricOptions(
          res.items.map((m) => ({ value: m.metric_code, label: `${m.metric_code}（${m.name}）` })),
        ),
      )
      .catch(() => setMetricOptions([]));
  }, []);

  async function handleCheck(values: Record<string, unknown>) {
    try {
      const res = await checkPermission({
        user_id: Number(values.user_id),
        action: String(values.action),
        domain: values.domain ? String(values.domain) : null,
        metric_code: values.metric_code ? String(values.metric_code) : null,
      });
      setResult({ allow: res.allow, reason: res.reason, masking: res.masking, restricted: res.restricted });
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "检查失败");
    }
  }

  return (
    <Card title="权限即时检查">
      <Form form={form} layout="inline" onFinish={handleCheck} style={{ rowGap: 12 }}>
        <Form.Item name="user_id" label="用户" rules={[{ required: true, message: "请选择用户" }]}>
          <Select
            showSearch
            optionFilterProp="label"
            style={{ width: 220 }}
            placeholder="按用户名 / 显示名搜索"
            options={users.map((u) => ({
              value: u.id,
              label: `${u.username}（${u.display_name}）`,
            }))}
          />
        </Form.Item>
        <Form.Item name="action" label="动作" rules={[{ required: true }]}>
          <Select style={{ width: 130 }} options={["read", "write", "approve", "export", "review"].map((v) => ({ value: v, label: ACTION_LABEL[v] ?? v }))} />
        </Form.Item>
        <Form.Item name="domain" label="域">
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            style={{ width: 160 }}
            placeholder="全部域"
            options={domainOptions}
          />
        </Form.Item>
        <Form.Item name="metric_code" label="指标">
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            style={{ width: 220 }}
            placeholder="不限（从已发布指标选择）"
            options={metricOptions}
          />
        </Form.Item>
        <Form.Item>
          <Button type="primary" htmlType="submit" icon={<SearchOutlined />}>检查</Button>
        </Form.Item>
      </Form>
      {result && (
        <Alert
          type={result.allow ? "success" : "error"}
          showIcon
          style={{ marginTop: 12 }}
          message={result.allow ? "允许执行" : "拒绝执行"}
          description={`${result.reason}${result.restricted ? "（行级受限）" : ""}${result.masking && result.masking !== "none" ? ` · 脱敏策略：${MASK_POLICY_LABEL[result.masking] ?? result.masking}` : ""}`}
        />
      )}
    </Card>
  );
}

function ErasureTab() {
  const [modalOpen, setModalOpen] = useState(false);
  const [users, setUsers] = useState<UserBrief[]>([]);
  const [form] = Form.useForm();

  useEffect(() => {
    // 数据主体从用户列表选择（替代手动输入用户 ID）
    listUsers()
      .then(setUsers)
      .catch(() => setUsers([]));
  }, []);

  async function handleCreate(values: Record<string, unknown>) {
    try {
      const res = await requestErasure({
        subject_user_id: Number(values.subject_user_id),
        reason: values.reason ? String(values.reason) : null,
      });
      message.success(`擦除请求已受理：影响 ${res.affected_rows} 行`);
      setModalOpen(false);
      form.resetFields();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "请求失败");
    }
  }

  return (
    <div>
      <Alert type="error" showIcon style={{ marginBottom: 12 }} message="数据擦除（被遗忘权）——仅 compliance_officer / platform_admin 可发起，操作不可逆且全程审计。" />
      <Button type="primary" danger icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>发起擦除请求</Button>
      <Modal title="发起数据擦除" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="提交">
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="subject_user_id" label="数据主体用户" rules={[{ required: true, message: "请选择数据主体用户" }]}>
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="按用户名 / 显示名搜索（对该用户全部个人数据执行擦除）"
              options={users.map((u) => ({
                value: u.id,
                label: `${u.username}（${u.display_name}）`,
              }))}
            />
          </Form.Item>
          <Form.Item name="reason" label="擦除原因">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

export function Governance() {
  // 组件级权限点过滤：每个 Tab 挂载「需具备任一权限点」才可见（方案 C：按组件管控）。
  // 我的权限/权限检查对全部治理页访问者开放；管理/合规 Tab 按对应功能权限点收敛。
  const { can } = usePermission();
  const rawTabs: Array<{
    key: string;
    label: string;
    children: React.ReactNode;
    perm?: string[];
  }> = [
    { key: "perms", label: "我的权限", children: <PermissionsTab /> },
    { key: "grants", label: "授权管理", children: <GrantsTab />, perm: ["grant:create", "grant:revoke", "grant:export"] },
    { key: "roles", label: "角色管理", children: <RolesTab />, perm: ["role:create", "role:edit", "role:delete"] },
    { key: "pii", label: "PII 复核", children: <PiiReviewTab />, perm: ["pii:review", "pii:validate", "classification:rescan"] },
    { key: "check", label: "权限检查", children: <CheckTab /> },
    { key: "erasure", label: "数据擦除", children: <ErasureTab />, perm: ["erasure:execute"] },
  ];
  const tabItems = rawTabs
    .filter((t) => !t.perm || t.perm.some((p) => can(p)))
    .map(({ perm: _perm, ...rest }) => rest);

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Governance / RBAC & Compliance</div>
          <h2>权限治理</h2>
          <p>角色、授权、PII 复核与数据擦除——治理闭环，全量审计。</p>
        </div>
      </div>
      <Card styles={{ body: { paddingTop: 8 } }}>
        <Tabs items={tabItems} />
      </Card>
    </div>
  );
}
