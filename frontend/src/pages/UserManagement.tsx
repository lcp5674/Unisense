import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Dropdown,
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
  DownOutlined,
  LockOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
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
  listRolePermissions,
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
import { usePermission } from "../hooks/usePermission";
import { UserPermModal } from "../components/governance/UserPermModal";

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

/** 复制文本到剪贴板（异步安全）。
 *
 * 优先 navigator.clipboard（仅 HTTPS/localhost 安全上下文可用）；生产内网常为
 * http://IP 非安全上下文，clipboard API 不存在 → 回退临时 textarea + execCommand。
 * 返回是否成功，供调用方决定提示文案（避免「谎报已复制」）。
 */
async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // 权限拒绝等 → 落到 execCommand 兜底
    }
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.top = "-9999px";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  document.body.removeChild(ta);
  return ok;
}

const STATUS_LABEL: Record<string, { text: string; color: string }> = {
  active: { text: "启用", color: "success" },
  disabled: { text: "禁用", color: "error" },
  deleted: { text: "已删除", color: "default" },
};

export function UserManagement() {
  // 当前用户信息（fetchCurrentUser）：权限判断已迁移到 usePermission，此处仅保留拉取副作用
  const [, setMe] = useState<CurrentUser | null>(null);
  const [items, setItems] = useState<AdminUser[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [role, setRole] = useState<string[]>([]);
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
  // 团队下拉（方案 B：所属域由团队自动继承，不再单独选择）；带 domain 供「继承域」提示
  const [orgOptions, setOrgOptions] = useState<Array<{ value: number; label: string; domain: string | null }>>([]);
  // 业务域下拉（active 主题域；用户可显式指定，留空=继承所属团队域）
  const [domainOptions, setDomainOptions] = useState<Array<{ value: string; label: string }>>([]);
  // 自定义角色（方案 A：后端 GET /roles 返回 is_custom 标记；创建/编辑用户角色下拉合并展示）
  const [customRoles, setCustomRoles] = useState<string[]>([]);
  const [generatedPassword, setGeneratedPassword] = useState("");
  const [createdResult, setCreatedResult] = useState<{ username: string; password: string } | null>(
    null,
  );
  // 重置密码成功：一次性展示明文（同创建用户，便于安全交付）
  const [resetResult, setResetResult] = useState<{ username: string; password: string } | null>(
    null,
  );
  // 批量启用/停用：多选行 + 请求进行中标记
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [batchLoading, setBatchLoading] = useState(false);
  // 「按用户授权」直挂按钮权限矩阵目标用户（复用 Governance 授权矩阵，用户管理直达）
  const [permUser, setPermUser] = useState<AdminUser | null>(null);

  // 监听团队选择（方案 B：所属域由团队自动继承，提示所选团队绑定的域）
  const createOrgId = Form.useWatch("org_id", createForm);
  const editOrgId = Form.useWatch("org_id", editForm);
  const inheritedDomainOf = (orgId: number | undefined): string | null =>
    orgOptions.find((o) => o.value === orgId)?.domain ?? null;

  const { can } = usePermission();
  const canManage =
    can("user:create") ||
    can("user:edit") ||
    can("user:disable") ||
    can("user:batch-status") ||
    can("user:reset-password");

  async function load() {
    setLoading(true);
    try {
      const res = await listAdminUsers({
        role: role.length ? role : undefined,
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
    // 团队下拉：仅展示启用中的团队（停用团队不可新建/换入用户）；domain 用于「继承域」提示
    listOrganizations({ page: 1, page_size: 200 })
      .then((res) =>
        setOrgOptions(
          res.items
            .filter((o) => o.status === "active")
            .map((o) => ({
              value: o.id,
              label: `${o.name}（${o.code}）${o.domain ? ` · 域：${o.domain}` : ""}`,
              domain: o.domain,
            })),
        ),
      )
      .catch(() => setOrgOptions([]));
  }, []);

  useEffect(() => {
    // 业务域下拉：仅 active 主题域（树形拍平；停用域不可指定，防指定失效域）
    listDomainTree("active")
      .then((tree) => {
        const flat: Array<{ value: string; label: string }> = [];
        const walk = (nodes: SubjectDomainTreeNode[]) => {
          for (const n of nodes) {
            flat.push({ value: n.code, label: `${n.name}（${n.code}）` });
            if (n.children?.length) walk(n.children);
          }
        };
        walk(tree);
        setDomainOptions(flat);
      })
      .catch(() => setDomainOptions([]));
  }, []);

  useEffect(() => {
    // 自定义角色下拉：GET /roles 返回全部角色（内置 + 自定义，is_custom 标记）
    listRolePermissions()
      .then((items) => setCustomRoles(items.filter((i) => i.is_custom).map((i) => i.role)))
      .catch(() => setCustomRoles([]));
  }, []);

  // 角色下拉 = 内置七角色 + 自定义角色（带「自定义」后缀，便于区分）
  const roleOptions = useMemo(
    () => [
      ...ROLE_OPTIONS,
      ...customRoles.map((r) => ({ value: r, label: `${r}（自定义）` })),
    ],
    [customRoles],
  );

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize, role, status]);

  async function handleCreate(values: Record<string, unknown>) {
    setSaving(true);
    try {
      // 方案 A 多角色：角色字段为多选数组；主角色取首个（后端按权限优先级重算）。
      const roleList =
        Array.isArray(values.roles) && values.roles.length > 0
          ? (values.roles as string[])
          : [String(values.role ?? "viewer")];
      const payload: UserCreateRequest = {
        username: String(values.username),
        email: String(values.email),
        display_name: String(values.display_name),
        role: roleList[0],
        roles: roleList,
        org_id: values.org_id ? Number(values.org_id) : undefined,
        domains:
          Array.isArray(values.domains) && values.domains.length > 0
            ? (values.domains as string[]).map(String)
            : undefined,
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
      // 方案 A 多角色：多选数组；主角色取首个（后端按权限优先级重算）。
      const roleList =
        Array.isArray(values.roles) && values.roles.length > 0
          ? (values.roles as string[])
          : [String(values.role)];
      const payload: UserUpdateRequest = {
        display_name: String(values.display_name),
        email: String(values.email),
        role: roleList[0],
        roles: roleList,
        org_id: values.org_id ? Number(values.org_id) : undefined,
        domains:
          Array.isArray(values.domains) && values.domains.length > 0
            ? (values.domains as string[]).map(String)
            : undefined,
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

  // 「更多」下拉中的停用/启用二次确认
  function confirmToggleStatus(u: AdminUser) {
    const disabling = u.status === "active";
    Modal.confirm({
      title: disabling ? `禁用用户 ${u.username}？` : `启用用户 ${u.username}？`,
      content: disabling ? "禁用后该用户将无法登录。" : "启用后该用户恢复登录。",
      okText: "确认",
      cancelText: "取消",
      okButtonProps: disabling ? { danger: true } : undefined,
      onOk: () => handleToggleStatus(u),
    });
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
      // 重置成功：一次性展示明文密码（仅内存，可复制交付，不落日志）
      setResetResult({
        username: resetTarget.username,
        password: String(values.new_password),
      });
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
      roles: u.roles?.length ? u.roles : [u.role],
      org_id: u.org_id ?? undefined,
      domains:
        u.domains?.length
          ? u.domains
          : u.domain
            ? [u.domain]
            : undefined,
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
      dataIndex: "roles",
      key: "roles",
      // 方案 A 多角色：全部角色渲染为多个 Tag，主角色（user.role）标蓝突出。
      render: (_: unknown, u: AdminUser) => {
        const roles = u.roles?.length ? u.roles : [u.role];
        return (
          <>
            {roles.map((r) => (
              <Tag key={r} color={r === u.role ? "blue" : undefined}>
                {ROLE_LABEL[r] ?? r}
              </Tag>
            ))}
          </>
        );
      },
    },
    {
      title: "所属团队",
      dataIndex: "org_name",
      key: "org_name",
      render: (v: string | null) => v ?? <span className="muted">—</span>,
    },
    {
      title: "业务域",
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
      width: 200,
      render: (_: unknown, u: AdminUser) =>
        canManage ? (
          <Space size={8} wrap>
            {can("user:edit") && (
              <Button size="small" type="link" onClick={() => openEdit(u)}>编辑</Button>
            )}
            {can("user:edit") && (
              <Button size="small" type="link" icon={<SafetyCertificateOutlined />} onClick={() => setPermUser(u)}>
                授权
              </Button>
            )}
            <Dropdown
              trigger={["click"]}
              menu={{
                items: [
                  ...(can("user:reset-password")
                    ? [{ key: "reset", icon: <LockOutlined />, label: "重置密码" }]
                    : []),
                  ...(can("user:disable")
                    ? [
                        { type: "divider" as const },
                        {
                          key: "toggle",
                          icon: u.status === "active" ? <StopOutlined /> : <PlayCircleOutlined />,
                          label: u.status === "active" ? "禁用" : "启用",
                          danger: u.status === "active",
                        },
                      ]
                    : []),
                ],
                onClick: ({ key }) => {
                  if (key === "reset") {
                    setResetTarget(u);
                    resetForm.resetFields();
                  } else if (key === "toggle") confirmToggleStatus(u);
                },
              }}
            >
              <Button size="small">
                更多 <DownOutlined />
              </Button>
            </Dropdown>
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
          <Select showSearch
            mode="multiple"
            allowClear
            placeholder="全部角色（可多选）"
            style={{ minWidth: 200 }}
            value={role}
            onChange={(v: string[]) => { setRole(v); setPage(1); }}
            options={roleOptions}
            maxTagCount="responsive"
          />
          <Select showSearch
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
          {can("user:create") && (
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
          {can("user:batch-status") && (
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
          <div style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
            <Form.Item
              name="roles"
              label="角色（可多选）"
              initialValue={["viewer"]}
              rules={[{ required: true, message: "至少选择一个角色" }]}
              style={{ flex: 1, minWidth: 0 }}
              extra="主角色自动取权限最高者（如同时选择域管理员+评审员，主角色为域管理员）"
            >
              <Select showSearch
                mode="multiple"
                options={roleOptions}
                placeholder="选择一个或多个角色"
                maxTagCount="responsive"
              />
            </Form.Item>
            <Form.Item name="org_id" label="所属团队" style={{ flex: 1, minWidth: 0 }} extra={undefined}>
              <Select
                allowClear
                showSearch
                optionFilterProp="label"
                placeholder="选择所属团队（业务域自动继承）"
                options={orgOptions}
              />
            </Form.Item>
          </div>
          {createOrgId ? (
            <div className="muted" style={{ fontSize: 12, marginBottom: 12 }}>
              {inheritedDomainOf(createOrgId)
                ? <>该团队绑定业务域「<span className="mono">{inheritedDomainOf(createOrgId)}</span>」，新成员未显式指定时将自动继承该域。</>
                : <>该团队未绑定业务域，新成员未显式指定时业务域为空 = 不限域（数据范围不限制，可按角色查看/治理全部业务域数据）。</>}
            </div>
          ) : null}
          <Form.Item
            name="domains"
            label="业务域"
            extra="可空；留空自动继承所属团队业务域，选择则与团队域取并集（权限域 = 团队继承 ∪ 显式指定）"
          >
            <Select
              mode="multiple"
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="留空继承团队域；可多选，与团队域取并集"
              options={domainOptions}
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
          <Form.Item
            name="roles"
            label="角色（可多选）"
            rules={[{ required: true, message: "至少选择一个角色" }]}
            extra="主角色自动取权限最高者；移除「平台管理员」将失去平台级管理权"
          >
            <Select showSearch
              mode="multiple"
              options={roleOptions}
              placeholder="选择一个或多个角色"
              maxTagCount="responsive"
            />
          </Form.Item>
          <Form.Item
            name="org_id"
            label="所属团队"
            extra={
              editOrgId ? (
                inheritedDomainOf(editOrgId)
                  ? <>该团队绑定业务域「<span className="mono">{inheritedDomainOf(editOrgId)}</span>」，保存后该用户域将自动切换继承。</>
                  : <>该团队未绑定业务域，保存后该用户业务域为空 = 不限域（数据范围不限制，可按角色查看/治理全部业务域数据）。</>
              ) : undefined
            }
          >
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="选择所属团队（业务域自动继承）"
              options={orgOptions}
            />
          </Form.Item>
          <Form.Item
            name="domains"
            label="业务域"
            extra={
              editOrgId
                ? inheritedDomainOf(editOrgId)
                  ? <>留空自动继承该团队业务域「<span className="mono">{inheritedDomainOf(editOrgId)}</span>」；选择则与团队域取并集（权限域 = 团队继承 ∪ 显式指定）。</>
                  : <>该团队未绑定业务域；留空则业务域为空 = 不限域（数据范围不限制），选择则显式指定。</>
                : "可空；留空自动继承所属团队业务域，选择则与团队域取并集"
            }
          >
            <Select
              mode="multiple"
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="留空继承团队域；可多选，与团队域取并集"
              options={domainOptions}
            />
          </Form.Item>
          <div className="muted" style={{ fontSize: 12 }}>用户名不可修改；业务域留空继承所属团队、多选与团队域取并集；密码请使用「重置密码」单独操作。</div>
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
          <Form.Item
            name="new_password"
            label="新密码"
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
                    resetForm.setFieldsValue({ new_password: pwd });
                  }}
                >
                  生成随机密码
                </Button>
                {generatedPassword ? <span className="muted">已自动预填强密码（含大小写/数字/符号）</span> : null}
              </Space>
            }
            rules={[{ required: true, min: 8, message: "至少 8 位" }]}
          >
            <Input.Password autoComplete="new-password" placeholder="至少 8 位" />
          </Form.Item>
          <Alert type="warning" showIcon message="重置后将立即生效并强制该用户首登改密；新密码仅展示一次，请安全交付。" />
        </Form>
      </Modal>

      {/* 重置成功：一次性展示新密码明文（仅内存，可复制交付，不落日志） */}
      <Modal
        title="密码重置成功"
        open={!!resetResult}
        onCancel={() => setResetResult(null)}
        footer={[
          <Button
            key="copy"
            type="primary"
            icon={<CopyOutlined />}
            onClick={async () => {
              const ok = await copyText(resetResult?.password ?? "");
              if (ok) message.success("新密码已复制");
              else message.error("复制失败：当前为非安全上下文，请手动选中上方密码复制");
            }}
          >
            复制密码
          </Button>,
          <Button key="close" onClick={() => setResetResult(null)}>关闭</Button>,
        ]}
      >
        <Alert
          type="success"
          showIcon
          message={`用户「${resetResult?.username ?? ""}」的密码已重置`}
          description="新密码仅在此展示一次，请立即安全地交给该用户；关闭后无法再次查看明文。该用户首次登录将被要求修改密码。"
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
          {resetResult?.password}
        </div>
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
            onClick={async () => {
              const ok = await copyText(createdResult?.password ?? "");
              if (ok) message.success("初始密码已复制");
              else message.error("复制失败：当前为非安全上下文，请手动选中上方密码复制");
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

      {/* 按用户授权：直挂按钮权限矩阵（复用 Governance 授权矩阵，用户管理直达） */}
      {permUser && (
        <UserPermModal
          userId={permUser.id}
          userName={permUser.username}
          open
          onClose={() => setPermUser(null)}
        />
      )}
    </div>
  );
}
