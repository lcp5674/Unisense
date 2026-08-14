import { useEffect, useState } from "react";
import { Card, Table, Tag, Button, Modal, Form, Input, InputNumber, Select, message, Tabs, Space, Alert, Descriptions } from "antd";
import { PlusOutlined, SafetyCertificateOutlined, ExperimentOutlined, SearchOutlined } from "@ant-design/icons";
import {
  fetchMyPermissions,
  listGrants,
  createGrant,
  revokeGrant,
  createRole,
  checkPermission,
  piiReviewAction,
  classificationRescan,
  requestErasure,
  listUsers,
  UnisenseApiError,
} from "../api";
import type { GrantResponse, PermissionSnapshot, UserBrief } from "../types";

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
  const [items, setItems] = useState<GrantResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const [users, setUsers] = useState<UserBrief[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();

  useEffect(() => {
    listUsers()
      .then(setUsers)
      .catch(() => setUsers([]));
  }, []);

  async function load() {
    setLoading(true);
    try {
      const res = await listGrants({ status, page, page_size: 20 });
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
      await createGrant({
        user_id: Number(values.user_id),
        role_id: values.role_id ? Number(values.role_id) : null,
        domain: values.domain ? String(values.domain) : null,
        metric_whitelist: values.metric_whitelist ? String(values.metric_whitelist).split(",").map((s) => s.trim()).filter(Boolean) : null,
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

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 70 },
    { title: "用户", dataIndex: "user_id", key: "user", width: 80 },
    { title: "角色", dataIndex: "role_id", key: "role", width: 90, render: (v: number | null) => v ? <span className="mono">#{v}</span> : <Tag>角色挂起</Tag> },
    { title: "域", dataIndex: "domain", key: "domain", render: (v: string | null) => v ?? <span className="muted">全部</span> },
    { title: "授权类型", dataIndex: "grant_type", key: "type", width: 110, render: (v: string) => <Tag color={v === "READ_WRITE" ? "warning" : v === "WRITE" ? "orange" : "default"}>{GRANT_TYPE_LABEL[v] ?? v}</Tag> },
    { title: "行级", dataIndex: "row_level", key: "row", width: 70, render: (v: boolean) => (v ? "是" : "否") },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (v: string) => <Tag color={v === "ACTIVE" ? "success" : v === "EXPIRED" ? "default" : "error"}>{GRANT_STATUS_LABEL[v] ?? v}</Tag>,
    },
    { title: "到期", dataIndex: "expires_at", key: "expires", width: 160, render: (v: string | null) => v ?? <span className="muted">长期</span> },
    {
      title: "操作",
      key: "actions",
      width: 90,
      render: (_: unknown, g: GrantResponse) => (g.status === "ACTIVE" ? <Button size="small" danger onClick={() => handleRevoke(g)}>回收</Button> : null),
    },
  ];

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
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>新建授权</Button>
      </Space>
      <Table
        dataSource={items}
        columns={columns}
        rowKey="id"
        loading={loading}
        pagination={{ current: page, pageSize: 20, total, onChange: setPage, showTotal: (t) => `共 ${t} 条` }}
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
            <Form.Item name="role_id" label="角色 ID（可留空）">
              <InputNumber min={1} style={{ width: 140 }} />
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
          <Form.Item name="domain" label="授权域">
            <Input placeholder="如 finance（留空为全部）" />
          </Form.Item>
          <Form.Item name="metric_whitelist" label="指标白名单（逗号分隔）">
            <Input className="mono" />
          </Form.Item>
          <Form.Item name="reason" label="授权原因">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

function RolesTab() {
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();

  async function handleCreate(values: Record<string, unknown>) {
    try {
      await createRole({ name: String(values.name), description: values.description ? String(values.description) : null });
      message.success("角色已创建（幂等）");
      setModalOpen(false);
      form.resetFields();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "创建失败");
    }
  }

  return (
    <div>
      <Alert type="info" showIcon style={{ marginBottom: 12 }} message="角色为内置枚举（platform_admin / domain_admin / metric_owner / reviewer / compliance_officer / viewer），创建角色用于授予绑定。" />
      <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>创建角色</Button>
      <Modal title="创建角色" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="创建">
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="name" label="角色名" rules={[{ required: true }]}>
            <Select
              options={[
                { value: "platform_admin", label: "platform_admin 平台管理员" },
                { value: "domain_admin", label: "domain_admin 域管理员" },
                { value: "metric_owner", label: "metric_owner 指标负责人" },
                { value: "reviewer", label: "reviewer 评审员" },
                { value: "compliance_officer", label: "compliance_officer 合规官" },
                { value: "viewer", label: "viewer 只读" },
              ]}
            />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

function PiiReviewTab() {
  const [modalOpen, setModalOpen] = useState(false);
  const [rescanLoading, setRescanLoading] = useState(false);
  const [form] = Form.useForm();

  async function handleReview(values: Record<string, unknown>) {
    try {
      const res = await piiReviewAction({
        metric_code: String(values.metric_code),
        decision: String(values.decision) as "APPROVE" | "REJECT",
        sensitivity_level: String(values.sensitivity_level ?? "PII"),
        masking_policy: values.masking_policy ? String(values.masking_policy) : null,
        pii_columns: values.pii_columns ? String(values.pii_columns).split(",").map((s) => s.trim()).filter(Boolean) : null,
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
        <Button type="primary" icon={<SafetyCertificateOutlined />} onClick={() => setModalOpen(true)}>PII 人工复核</Button>
        <Button icon={<ExperimentOutlined />} loading={rescanLoading} onClick={handleRescan}>敏感度分类重扫</Button>
      </Space>
      <Alert type="warning" showIcon message="PII 复核与分类重扫仅 compliance_officer / platform_admin 可执行；复核结果写入治理审计。" />

      <Modal title="PII 人工复核" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="提交复核">
        <Form form={form} layout="vertical" onFinish={handleReview} style={{ marginTop: 8 }}>
          <Form.Item name="metric_code" label="指标编码" rules={[{ required: true }]}>
            <Input className="mono" />
          </Form.Item>
          <Form.Item name="decision" label="决定" rules={[{ required: true }]} initialValue="APPROVE">
            <Select options={[{ value: "APPROVE", label: "通过（合规复核通过）" }, { value: "REJECT", label: "拒绝（退回）" }]} />
          </Form.Item>
          <Form.Item name="sensitivity_level" label="敏感度" initialValue="PII">
            <Select options={["PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", "UNKNOWN"].map((v) => ({ value: v, label: SENSITIVITY_LABEL[v] ?? v }))} />
          </Form.Item>
          <Form.Item name="masking_policy" label="脱敏策略">
            <Select allowClear options={[{ value: "none", label: "无" }, { value: "mask", label: "掩码" }, { value: "hash", label: "哈希" }, { value: "deny", label: "拒绝访问" }]} />
          </Form.Item>
          <Form.Item name="pii_columns" label="PII 字段（逗号分隔）">
            <Input className="mono" placeholder="如 user_phone, id_card" />
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
  const [form] = Form.useForm();

  useEffect(() => {
    listUsers()
      .then(setUsers)
      .catch(() => setUsers([]));
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
          <Input style={{ width: 140 }} />
        </Form.Item>
        <Form.Item name="metric_code" label="指标">
          <Input className="mono" style={{ width: 180 }} />
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
  const [form] = Form.useForm();

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
          <Form.Item name="subject_user_id" label="数据主体用户 ID" rules={[{ required: true }]}>
            <InputNumber min={1} style={{ width: 200 }} />
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
  const tabItems = [
    { key: "perms", label: "我的权限", children: <PermissionsTab /> },
    { key: "grants", label: "授权管理", children: <GrantsTab /> },
    { key: "roles", label: "角色管理", children: <RolesTab /> },
    { key: "pii", label: "PII 复核", children: <PiiReviewTab /> },
    { key: "check", label: "权限检查", children: <CheckTab /> },
    { key: "erasure", label: "数据擦除", children: <ErasureTab /> },
  ];

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
