/**
 * 个人中心（方案 C 的核心承载页）：我的账号 / 我的权限 / 我的授权 / 修改密码。
 *
 * 账号类通知（user.* / org.* / grant.* / pii.*）的深链目标——用户本人视角，
 * 不再指向管理员管理列表页。数据来源：``GET /auth/me``（含 org_name/domain_name）
 * + ``GET /me/permissions``（ui_actions / grants / expiring_soon）。
 */

import { useEffect, useState } from "react";
import { Card, Descriptions, Tag, Button, Modal, Form, Input, Space, Alert, Table, App as AntApp } from "antd";
import { KeyOutlined } from "@ant-design/icons";
import { changePassword, fetchCurrentUser, fetchMyPermissions, UnisenseApiError } from "../api";
import type { CurrentUser, GrantResponse, PermissionSnapshot } from "../types";
import { formatCnTime } from "../utils/timeCn";

// 内置角色中文名（与用户管理页同源）
const ROLE_LABEL: Record<string, string> = {
  platform_admin: "平台管理员",
  domain_admin: "域管理员",
  metric_owner: "指标负责人",
  reviewer: "评审员",
  compliance_officer: "合规官",
  analyst: "分析师",
  viewer: "只读用户",
};

// 资源级动作中文名
const ACTION_LABEL: Record<string, string> = {
  read: "读取",
  write: "写入",
  approve: "审批",
  export: "导出",
  review: "复核",
};

const GRANT_TYPE_LABEL: Record<string, string> = {
  READ: "只读",
  WRITE: "写",
  READ_WRITE: "读写",
};

function grantStatusLabel(status: string): { text: string; color: string } {
  if (status === "ACTIVE") return { text: "生效", color: "success" };
  if (status === "EXPIRED") return { text: "已过期", color: "default" };
  return { text: "已回收", color: "error" };
}

const GRANT_COLUMNS = [
  { title: "类型", dataIndex: "grant_type", key: "grant_type", width: 100, render: (v: string) => GRANT_TYPE_LABEL[v] ?? v },
  { title: "主题域", dataIndex: "domain", key: "domain", render: (v: string | null) => v ?? "全局白名单" },
  {
    title: "指标白名单",
    dataIndex: "metric_whitelist",
    key: "metric_whitelist",
    render: (v: string[] | null) => (v?.length ? v.join(", ") : "按域授权"),
  },
  {
    title: "到期时间",
    dataIndex: "expires_at",
    key: "expires_at",
    width: 180,
    render: (v: string | null) => (v ? formatCnTime(v) : "永久"),
  },
  {
    title: "状态",
    dataIndex: "status",
    key: "status",
    width: 90,
    render: (v: string) => {
      const s = grantStatusLabel(v);
      return <Tag color={s.color}>{s.text}</Tag>;
    },
  },
];

export function Account() {
  const { message } = AntApp.useApp();
  const [me, setMe] = useState<CurrentUser | null>(null);
  const [snap, setSnap] = useState<PermissionSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [pwdOpen, setPwdOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [pwdForm] = Form.useForm<{ current_password: string; new_password: string; confirm: string }>();

  useEffect(() => {
    Promise.all([fetchCurrentUser(), fetchMyPermissions()])
      .then(([u, s]) => {
        setMe(u);
        setSnap(s);
      })
      .catch(() => {
        message.error("加载个人中心信息失败，请稍后重试");
      })
      .finally(() => setLoading(false));
  }, [message]);

  async function handleChangePwd() {
    const values = await pwdForm.validateFields().catch(() => null);
    if (!values) return;
    if (values.new_password !== values.confirm) {
      message.warning("两次输入的新密码不一致");
      return;
    }
    setSaving(true);
    try {
      await changePassword({
        current_password: values.current_password,
        new_password: values.new_password,
      });
      message.success("密码已修改");
      setPwdOpen(false);
      pwdForm.resetFields();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "修改失败");
    } finally {
      setSaving(false);
    }
  }

  if (loading) return null;

  return (
    <div style={{ maxWidth: 1100 }}>
      <Space style={{ width: "100%", justifyContent: "space-between", marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>个人中心</h2>
        <Button icon={<KeyOutlined />} onClick={() => setPwdOpen(true)}>
          修改密码
        </Button>
      </Space>

      {/* 我的账号 */}
      <Card title="我的账号" style={{ marginBottom: 12 }}>
        <Descriptions column={2} bordered size="small">
          <Descriptions.Item label="用户名">{me?.username}</Descriptions.Item>
          <Descriptions.Item label="显示名称">{me?.display_name}</Descriptions.Item>
          <Descriptions.Item label="角色">
            <Tag color={me?.role === "platform_admin" ? "gold" : undefined}>
              {ROLE_LABEL[me?.role ?? ""] ?? me?.role}
            </Tag>
            {me && !ROLE_LABEL[me.role] && <Tag color="blue">自定义角色</Tag>}
          </Descriptions.Item>
          <Descriptions.Item label="所属组织">
            {me?.org_name ? `${me.org_name}（${me.org_id}）` : `组织 ${me?.org_id ?? "-"}`}
          </Descriptions.Item>
          <Descriptions.Item label="所属主题域">
            {me?.domain ? (me.domain_name ? `${me.domain_name}（${me.domain}）` : me.domain) : "全局"}
          </Descriptions.Item>
          <Descriptions.Item label="行级受限">{snap?.row_level_restricted ? "是" : "否"}</Descriptions.Item>
        </Descriptions>
      </Card>

      {/* 我的权限 */}
      <Card title="我的权限" style={{ marginBottom: 12 }}>
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Space wrap>
            <span className="muted">资源级动作：</span>
            {(snap?.allowed_actions ?? []).map((a) => (
              <Tag key={a} color="blue">{ACTION_LABEL[a] ?? a}</Tag>
            ))}
            {(snap?.allowed_actions ?? []).length === 0 && <Tag>无</Tag>}
          </Space>
          <Space wrap>
            <span className="muted">按钮级权限点：</span>
            {(snap?.ui_actions ?? []).map((a) => (
              <Tag key={a}>{a}</Tag>
            ))}
            {(snap?.ui_actions ?? []).length === 0 && <Tag>无</Tag>}
          </Space>
        </Space>
      </Card>

      {/* 我的授权 */}
      <Card
        title="我的授权"
        extra={
          snap && snap.expiring_soon.length > 0 ? (
            <Alert type="warning" showIcon message={`${snap.expiring_soon.length} 个授权即将到期`} />
          ) : undefined
        }
      >
        <Table
          dataSource={snap?.grants ?? []}
          columns={GRANT_COLUMNS}
          rowKey={(r: GrantResponse) => r.id}
          pagination={false}
          size="small"
          locale={{ emptyText: "暂无跨域授权" }}
        />
      </Card>

      {/* 修改密码 */}
      <Modal
        title="修改密码"
        open={pwdOpen}
        onCancel={() => setPwdOpen(false)}
        onOk={handleChangePwd}
        okText="确认修改"
        confirmLoading={saving}
      >
        <Form form={pwdForm} layout="vertical">
          <Form.Item
            name="current_password"
            label="当前密码"
            rules={[{ required: true, message: "请输入当前密码" }]}
          >
            <Input.Password placeholder="当前密码" autoComplete="current-password" />
          </Form.Item>
          <Form.Item
            name="new_password"
            label="新密码"
            rules={[
              { required: true, message: "请输入新密码" },
              { min: 8, message: "至少 8 位" },
            ]}
          >
            <Input.Password placeholder="至少 8 位，含大小写/数字/特殊字符中至少 3 类" />
          </Form.Item>
          <Form.Item
            name="confirm"
            label="确认新密码"
            rules={[{ required: true, message: "请再次输入新密码" }]}
          >
            <Input.Password placeholder="再次输入新密码" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
