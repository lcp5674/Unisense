import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  message,
} from "antd";
import {
  CopyOutlined,
  LockOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  StopOutlined,
  TeamOutlined,
} from "@ant-design/icons";
import {
  UnisenseApiError,
  batchSetUserStatus,
  createUser,
  fetchCurrentUser,
  listAdminUsers,
  listDomainTree,
  listOrganizations,
  resetUserPassword,
  setUserStatus,
  updateUser,
} from "../api";
import type {
  AdminUser,
  CurrentUser,
  SubjectDomainTreeNode,
  UserBatchStatusResult,
  UserCreateRequest,
  UserUpdateRequest,
} from "../types";
import { formatCnTime } from "../utils/timeCn";

const ROLE_LABEL: Record<string, string> = {
  platform_admin: "平台管理员",
  domain_admin: "域管理员",
  metric_owner: "指标负责人",
  reviewer: "评审员",
  compliance_officer: "合规官",
  analyst: "分析师",
  viewer: "只读用户",
};

const ROLE_OPTIONS = Object.entries(ROLE_LABEL).map(([value, label]) => ({ value, label }));

// 主题域树 → 扁平化下拉选项（保留层级缩进，与数据源页「业务域」下拉同款实现）
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

// 强随机密码：crypto.getRandomValues，保证大小写/数字/符号各至少 1 个（后端要求 ≥8 位）
function generateStrongPassword(length = 16): string {
  const upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
  const lower = "abcdefghijklmnopqrstuvwxyz";
  const digits = "0123456789";
  const symbols = "!@#$%^&*()-_=+[]{};:,.?";
  const all = upper + lower + digits + symbols;
  const chars: string[] = [];
  for (const pool of [upper, lower, digits, symbols]) {
    const buf = new Uint32Array(1);
    crypto.getRandomValues(buf);
    chars.push(pool[buf[0] % pool.length]);
  }
  const rest = new Uint32Array(Math.max(0, length - chars.length));
  crypto.getRandomValues(rest);
  for (const v of rest) chars.push(all[v % all.length]);
  const shuffle = new Uint32Array(chars.length);
  crypto.getRandomValues(shuffle);
  for (let i = chars.length - 1; i > 0; i--) {
    const j = shuffle[i] % (i + 1);
    [chars[i], chars[j]] = [chars[j], chars[i]];
  }
  return chars.join("");
}

const STATUS_LABEL: Record<string, { text: string; color: string }> = {
  active: { text: "启用", color: "success" },
  disabled: { text: "禁用", color: "error" },
  deleted: { text: "已删除", color: "default" },
};

export function UserManagement() {
  const [me, setMe] = useState<CurrentUser | null>(null);
  const [items, setItems] = useState<AdminUser[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [role, setRole] = useState("");
  const [status, setStatus] = useState("");
  const [keyword, setKeyword] = useState("");
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<AdminUser | null>(null);
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null);
  const [saving, setSaving] = useState(false);
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();
  const [resetForm] = Form.useForm();
  const [domainOptions, setDomainOptions] = useState<Array<{ value: string; label: string }>>([]);
  const [orgOptions, setOrgOptions] = useState<Array<{ value: number; label: string }>>([]);
  const [generatedPassword, setGeneratedPassword] = useState("");
  const [createdResult, setCreatedResult] = useState<{ username: string; password: string } | null>(
    null,
  );
  // 批量启用/停用：多选行 + 请求进行中标记
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [batchLoading, setBatchLoading] = useState(false);

  const canManage = me?.role === "platform_admin";

  async function load() {
    setLoading(true);
    try {
      const res = await listAdminUsers({
        role: role || undefined,
        status: status || undefined,
        keyword: keyword || undefined,
        page,
        page_size: pageSize,
      });
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchCurrentUser().then(setMe).catch(() => setMe(null));
  }, []);

  useEffect(() => {
    // 所属域下拉：仅展示启用中的主题域（与数据源页「业务域」下拉同源）
    listDomainTree("active")
      .then((tree) => setDomainOptions(flattenDomains(tree)))
      .catch(() => setDomainOptions([]));
  }, []);

  useEffect(() => {
    // 组织下拉：仅展示启用中的组织（多租户；停用组织不可新建用户）
    listOrganizations({ page: 1, page_size: 200 })
      .then((res) =>
        setOrgOptions(
          res.items
            .filter((o) => o.status === "active")
            .map((o) => ({ value: o.id, label: `${o.name}（${o.code}）` })),
        ),
      )
      .catch(() => setOrgOptions([]));
  }, []);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize, role, status]);

  async function handleCreate(values: Record<string, unknown>) {
    setSaving(true);
    try {
      const payload: UserCreateRequest = {
        username: String(values.username),
        email: String(values.email),
        display_name: String(values.display_name),
        role: String(values.role ?? "viewer"),
        domain: values.domain ? String(values.domain) : null,
        org_id: values.org_id ? Number(values.org_id) : undefined,
        password: String(values.password),
      };
      await createUser(payload);
      message.success("用户已创建");
      setCreateOpen(false);
      createForm.resetFields();
      setCreatedResult({ username: String(values.username), password: String(values.password) });
      setPage(1);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "创建失败");
    } finally {
      setSaving(false);
    }
  }

  async function handleUpdate(values: Record<string, unknown>) {
    if (!editTarget) return;
    setSaving(true);
    try {
      const payload: UserUpdateRequest = {
        display_name: String(values.display_name),
        email: String(values.email),
        role: String(values.role),
        domain: values.domain ? String(values.domain) : null,
      };
      await updateUser(editTarget.id, payload);
      message.success("用户已更新");
      setEditTarget(null);
      editForm.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "更新失败");
    } finally {
      setSaving(false);
    }
  }

  async function handleToggleStatus(u: AdminUser) {
    const next = u.status === "active" ? "disabled" : "active";
    try {
      await setUserStatus(u.id, next);
      message.success(next === "active" ? "已启用" : "已禁用");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    }
  }

  async function handleBatchToggle(enabled: boolean) {
    if (selectedRowKeys.length === 0) return;
    const ids = selectedRowKeys.map(Number);
    setBatchLoading(true);
    try {
      const result: UserBatchStatusResult = await batchSetUserStatus(
        ids,
        enabled ? "active" : "disabled",
      );
      const action = enabled ? "启用" : "停用";
      if (result.failed.length > 0) {
        const names = result.failed
          .map((f) => `${f.username ?? f.user_id}（${f.message ?? f.error_code ?? "失败"}）`)
          .join("、");
        message.warning(
          `${action}完成 ${result.succeeded.length} 个，失败 ${result.failed.length} 个：${names}`,
        );
      } else {
        message.success(`${action}成功 ${result.succeeded.length} 个用户`);
      }
      setSelectedRowKeys([]);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量操作失败");
    } finally {
      setBatchLoading(false);
    }
  }

  async function handleReset(values: Record<string, unknown>) {
    if (!resetTarget) return;
    setSaving(true);
    try {
      await resetUserPassword(resetTarget.id, String(values.new_password));
      message.success(`已重置「${resetTarget.username}」的密码`);
      setResetTarget(null);
      resetForm.resetFields();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "重置失败");
    } finally {
      setSaving(false);
    }
  }

  function openEdit(u: AdminUser) {
    setEditTarget(u);
    editForm.setFieldsValue({
      display_name: u.display_name,
      email: u.email,
      role: u.role,
      domain: u.domain ?? undefined,
    });
  }

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 60 },
    {
      title: "用户名",
      dataIndex: "username",
      key: "username",
      render: (v: string) => <span className="mono">{v}</span>,
    },
    { title: "显示名", dataIndex: "display_name", key: "display_name" },
    {
      title: "邮箱",
      dataIndex: "email",
      key: "email",
      render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span>,
    },
    {
      title: "角色",
      dataIndex: "role",
      key: "role",
      render: (v: string) => <Tag>{ROLE_LABEL[v] ?? v}</Tag>,
    },
    {
      title: "域",
      dataIndex: "domain",
      key: "domain",
      render: (v: string | null) => v ?? <span className="muted">—</span>,
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 90,
      render: (v: string) => (
        <Tag color={STATUS_LABEL[v]?.color ?? "default"}>{STATUS_LABEL[v]?.text ?? v}</Tag>
      ),
    },
    {
      title: "最后登录",
      dataIndex: "last_login_at",
      key: "last_login",
      width: 160,
      render: (v: string | null) =>
        v ? <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> : <span className="muted">—</span>,
    },
    {
      title: "操作",
      key: "actions",
      width: 220,
      render: (_: unknown, u: AdminUser) =>
        canManage ? (
          <Space size={4}>
            <Button size="small" onClick={() => openEdit(u)}>编辑</Button>
            <Button size="small" icon={<LockOutlined />} onClick={() => { setResetTarget(u); resetForm.resetFields(); }}>
              重置密码
            </Button>
            <Popconfirm
              title={u.status === "active" ? `禁用用户 ${u.username}？` : `启用用户 ${u.username}？`}
              description={u.status === "active" ? "禁用后该用户将无法登录" : undefined}
              okText="确认"
              cancelText="取消"
              onConfirm={() => handleToggleStatus(u)}
            >
              <Button size="small" danger={u.status === "active"}>{u.status === "active" ? "禁用" : "启用"}</Button>
            </Popconfirm>
          </Space>
        ) : null,
    },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">系统管理 / Users & Accounts</div>
          <h2>用户管理</h2>
          <p>账号生命周期管理：创建、编辑、启用/禁用与重置密码——仅平台管理员可操作，全程审计。</p>
        </div>
      </div>

      {!canManage && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="当前账号为只读视图：用户管理操作仅平台管理员可执行。"
        />
      )}

      <Card styles={{ body: { paddingTop: 8 } }}>
        <Space style={{ marginBottom: 12 }} wrap>
          <Select
            allowClear
            placeholder="全部角色"
            style={{ width: 150 }}
            value={role || undefined}
            onChange={(v) => { setRole(v || ""); setPage(1); }}
            options={ROLE_OPTIONS}
          />
          <Select
            allowClear
            placeholder="全部状态"
            style={{ width: 130 }}
            value={status || undefined}
            onChange={(v) => { setStatus(v || ""); setPage(1); }}
            options={[
              { value: "active", label: "启用" },
              { value: "disabled", label: "禁用" },
              { value: "deleted", label: "已删除" },
            ]}
          />
          <Input.Search
            allowClear
            placeholder="搜索用户名 / 显示名 / 邮箱"
            style={{ width: 240 }}
            onSearch={(v) => { setKeyword(v); setPage(1); }}
          />
          {canManage && (
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => {
                const pwd = generateStrongPassword();
                setGeneratedPassword(pwd);
                setCreateOpen(true);
                createForm.resetFields();
                createForm.setFieldsValue({ password: pwd });
              }}
            >
              创建用户
            </Button>
          )}
          {canManage && (
            <>
              {selectedRowKeys.length > 0 && (
                <span style={{ color: "rgba(0,0,0,0.45)", fontSize: 13 }}>
                  已选 {selectedRowKeys.length} 个用户
                </span>
              )}
              <Button
                icon={<PlayCircleOutlined />}
                onClick={() => handleBatchToggle(true)}
                disabled={selectedRowKeys.length === 0 || batchLoading}
              >
                批量启用
              </Button>
              <Popconfirm
                title="批量停用"
                description={`确定停用选中的 ${selectedRowKeys.length} 个用户？停用后这些账号将无法登录，可随时再次启用。`}
                okText="确认停用"
                onConfirm={() => handleBatchToggle(false)}
                disabled={selectedRowKeys.length === 0 || batchLoading}
              >
                <Button icon={<StopOutlined />} disabled={selectedRowKeys.length === 0 || batchLoading}>
                  批量停用
                </Button>
              </Popconfirm>
            </>
          )}
          <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
        </Space>
        <Table
          dataSource={items}
          columns={columns}
          rowKey="id"
          loading={loading}
          rowSelection={canManage ? { selectedRowKeys, onChange: setSelectedRowKeys } : undefined}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: [10, 20, 50, 100],
            onChange: (p, ps) => { setPage(p); setPageSize(ps); },
            showTotal: (t) => `共 ${t} 个用户`,
          }}
          locale={{ emptyText: "暂无用户" }}
        />
      </Card>

      {/* 创建用户 */}
      <Modal
        title={<Space><TeamOutlined />创建用户</Space>}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => createForm.submit()}
        okText="创建"
        confirmLoading={saving}
      >
        <Form form={createForm} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="username" label="用户名" rules={[{ required: true, pattern: /^[A-Za-z0-9_.-]{2,64}$/, message: "2-64 位字母/数字/._-" }]}>
            <Input className="mono" placeholder="如 zhangsan" />
          </Form.Item>
          <Form.Item name="email" label="邮箱" rules={[{ required: true, type: "email", message: "请输入合法邮箱" }]}>
            <Input className="mono" placeholder="name@example.com" />
          </Form.Item>
          <Form.Item name="display_name" label="显示名称" rules={[{ required: true }]}>
            <Input placeholder="如 张三" />
          </Form.Item>
          <Space size={16} style={{ width: "100%" }}>
            <Form.Item name="role" label="角色" initialValue="viewer" rules={[{ required: true }]} style={{ width: 180 }}>
              <Select options={ROLE_OPTIONS} />
            </Form.Item>
            <Form.Item name="domain" label="所属域（可留空）" style={{ width: 240 }}>
              <Select
                allowClear
                showSearch
                optionFilterProp="label"
                placeholder="选择主题域"
                options={domainOptions}
              />
            </Form.Item>
          </Space>
          <Form.Item name="org_id" label="所属组织" extra="缺省归入当前管理员组织；停用组织不可选">
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="选择组织（缺省当前管理员组织）"
              options={orgOptions}
            />
          </Form.Item>
          <Form.Item
            name="password"
            label="初始密码"
            extra={
              <Space size={8} style={{ marginTop: 4 }}>
                <Button
                  type="link"
                  size="small"
                  style={{ padding: 0 }}
                  icon={<ReloadOutlined />}
                  onClick={() => {
                    const pwd = generateStrongPassword();
                    setGeneratedPassword(pwd);
                    createForm.setFieldsValue({ password: pwd });
                  }}
                >
                  重新生成
                </Button>
                {generatedPassword ? <span className="muted">已自动预填强密码（含大小写/数字/符号）</span> : null}
              </Space>
            }
            rules={[{ required: true, min: 8, message: "至少 8 位" }]}
          >
            <Input.Password autoComplete="new-password" placeholder="至少 8 位" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 编辑用户 */}
      <Modal
        title={`编辑用户：${editTarget?.username ?? ""}`}
        open={!!editTarget}
        onCancel={() => setEditTarget(null)}
        onOk={() => editForm.submit()}
        okText="保存"
        confirmLoading={saving}
      >
        <Form form={editForm} layout="vertical" onFinish={handleUpdate} style={{ marginTop: 8 }}>
          <Form.Item name="display_name" label="显示名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="email" label="邮箱" rules={[{ required: true, type: "email", message: "请输入合法邮箱" }]}>
            <Input className="mono" />
          </Form.Item>
          <Form.Item name="role" label="角色" rules={[{ required: true }]}>
            <Select options={ROLE_OPTIONS} />
          </Form.Item>
          <Form.Item name="domain" label="所属域（留空为无）">
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="选择主题域"
              options={domainOptions}
            />
          </Form.Item>
          <div className="muted" style={{ fontSize: 12 }}>用户名不可修改；密码请使用「重置密码」单独操作。</div>
        </Form>
      </Modal>

      {/* 重置密码 */}
      <Modal
        title={`重置密码：${resetTarget?.username ?? ""}`}
        open={!!resetTarget}
        onCancel={() => setResetTarget(null)}
        onOk={() => resetForm.submit()}
        okText="重置"
        confirmLoading={saving}
      >
        <Form form={resetForm} layout="vertical" onFinish={handleReset} style={{ marginTop: 8 }}>
          <Form.Item name="new_password" label="新密码" rules={[{ required: true, min: 8, message: "至少 8 位" }]}>
            <Input.Password autoComplete="new-password" placeholder="至少 8 位" />
          </Form.Item>
          <Alert type="warning" showIcon message="重置后将立即生效，请通知该用户使用新密码登录。" />
        </Form>
      </Modal>

      {/* 创建成功：一次性展示初始密码明文（仅内存，可复制交付，不落日志） */}
      <Modal
        title="用户创建成功"
        open={!!createdResult}
        onCancel={() => setCreatedResult(null)}
        footer={[
          <Button
            key="copy"
            type="primary"
            icon={<CopyOutlined />}
            onClick={() => {
              navigator.clipboard?.writeText(createdResult?.password ?? "");
              message.success("初始密码已复制");
            }}
          >
            复制密码
          </Button>,
          <Button key="close" onClick={() => setCreatedResult(null)}>关闭</Button>,
        ]}
      >
        <Alert
          type="success"
          showIcon
          message={`用户「${createdResult?.username ?? ""}」已创建`}
          description="初始密码仅在此展示一次，请立即安全地交给该用户；关闭后无法再次查看明文。"
        />
        <div
          className="mono"
          style={{
            marginTop: 12,
            padding: "12px 16px",
            background: "#fafafa",
            border: "1px solid #f0f0f0",
            borderRadius: 6,
            fontSize: 14,
            wordBreak: "break-all",
          }}
        >
          {createdResult?.password}
        </div>
      </Modal>
    </div>
  );
}
