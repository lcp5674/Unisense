/**
 * 个人中心（方案 C 的核心承载页）：我的概览 / 我的账号 / 我的权限 / 我的授权 / 修改密码。
 *
 * 账号类通知（user.* / org.* / grant.* / pii.*）的深链目标——用户本人视角，
 * 不再指向管理员管理列表页。数据来源：``GET /auth/me``（含 org_name/domain_name）
 * + ``GET /me/permissions``（ui_actions / ui_action_meta / grants / expiring_soon）。
 *
 * 权限展示：按钮级权限点（``ui_actions``）经后端 ``ui_action_meta``（单一事实来源 =
 * 后端 UI_ACTION_REGISTRY）按模块分组渲染中文名；未知自定义动作降级显示编码。
 */

import { useEffect, useMemo, useState } from "react";
import {
  App as AntApp,
  Alert,
  Avatar,
  Button,
  Card,
  Col,
  Descriptions,
  Divider,
  Form,
  Input,
  Modal,
  Row,
  Space,
  Table,
  Tag,
  Tooltip,
} from "antd";
import {
  ApiOutlined,
  KeyOutlined,
  SafetyCertificateOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { changePassword, fetchCurrentUser, fetchMyPermissions, UnisenseApiError } from "../api";
import type { CurrentUser, GrantResponse, PermissionSnapshot, UiActionMeta } from "../types";
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

const ACTION_COLOR: Record<string, string> = {
  read: "blue",
  write: "green",
  approve: "orange",
  export: "purple",
  review: "cyan",
};

const GRANT_TYPE_LABEL: Record<string, string> = {
  READ: "只读",
  WRITE: "写",
  READ_WRITE: "读写",
};

// 权限点模块 → 徽标配色（未列出的模块用默认灰）
const MODULE_COLOR: Record<string, string> = {
  总览: "blue",
  指标: "geekblue",
  资产地图: "cyan",
  质量中心: "orange",
  分析: "purple",
  采集: "green",
  治理: "magenta",
  系统: "red",
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

/** 未知/自定义权限点的降级元数据（后端未注册的动作，保留编码可见性） */
function fallbackMeta(action: string): UiActionMeta {
  return { action, module: "其他", label: action, description: "自定义权限点" };
}

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

  // 按钮级权限点按模块分组（模块排序：注册表顺序保持中文自然序，「其他」最后）
  const permGroups = useMemo(() => {
    const byMeta = new Map((snap?.ui_action_meta ?? []).map((m) => [m.action, m]));
    const map = new Map<string, UiActionMeta[]>();
    for (const a of snap?.ui_actions ?? []) {
      const meta = byMeta.get(a) ?? fallbackMeta(a);
      const arr = map.get(meta.module) ?? [];
      arr.push(meta);
      map.set(meta.module, arr);
    }
    return [...map.entries()].sort(([m1], [m2]) => {
      if (m1 === "其他") return 1;
      if (m2 === "其他") return -1;
      return m1.localeCompare(m2, "zh-CN");
    });
  }, [snap]);

  if (loading) return null;

  const displayName = me?.display_name || me?.username || "用户";
  const roles = snap?.roles?.length ? snap.roles : me?.role ? [me.role] : [];

  return (
    <div style={{ maxWidth: 1100 }}>
      {/* ============ 顶部个人概览横幅 ============ */}
      <Card
        style={{
          marginBottom: 16,
          background: "linear-gradient(135deg, #1e3a8a 0%, #2563eb 60%, #3b82f6 100%)",
          border: "none",
          boxShadow: "0 4px 16px rgba(37, 99, 235, 0.25)",
        }}
        styles={{ body: { padding: "20px 24px" } }}
      >
        <Row align="middle" gutter={20} wrap>
          <Col>
            <Avatar
              size={68}
              style={{
                background: "rgba(255,255,255,0.18)",
                color: "#fff",
                fontSize: 30,
                fontWeight: 600,
              }}
            >
              {displayName.slice(0, 1).toUpperCase()}
            </Avatar>
          </Col>
          <Col flex="auto">
            <div style={{ fontSize: 24, fontWeight: 600, color: "#fff", lineHeight: 1.4 }}>
              {displayName}
              <span style={{ marginLeft: 10, fontSize: 14, fontWeight: 400, color: "rgba(255,255,255,0.75)" }}>
                @{me?.username}
              </span>
            </div>
            <Space wrap size={[6, 4]} style={{ marginTop: 8 }}>
              {roles.map((r) => (
                <Tag key={r} color="rgba(255,255,255,0.25)" style={{ color: "#fff", borderColor: "rgba(255,255,255,0.35)" }}>
                  {ROLE_LABEL[r] ?? r}
                </Tag>
              ))}
              {me?.org_name && <span style={{ color: "rgba(255,255,255,0.85)" }}>🏛 {me.org_name}</span>}
              {me?.domain_name && <span style={{ color: "rgba(255,255,255,0.85)" }}>· {me.domain_name}</span>}
              {snap?.row_level_restricted && (
                <Tag color="rgba(250,173,20,0.9)" style={{ borderColor: "transparent" }}>行级受限</Tag>
              )}
            </Space>
          </Col>
          <Col>
            <Button icon={<KeyOutlined />} onClick={() => setPwdOpen(true)}>
              修改密码
            </Button>
          </Col>
        </Row>
      </Card>

      {/* ============ 我的账号 ============ */}
      <Card
        title={
          <Space size={8}>
            <UserOutlined style={{ color: "#2563eb" }} />
            我的账号
          </Space>
        }
        style={{ marginBottom: 16 }}
      >
        <Descriptions column={2} size="middle">
          <Descriptions.Item label="用户名">{me?.username}</Descriptions.Item>
          <Descriptions.Item label="显示名称">{me?.display_name}</Descriptions.Item>
          <Descriptions.Item label="所属组织">
            {me?.org_name ? `${me.org_name}（${me.org_id}）` : `组织 ${me?.org_id ?? "-"}`}
          </Descriptions.Item>
          <Descriptions.Item label="所属主题域">
            {me?.domain ? (me.domain_name ? `${me.domain_name}（${me.domain}）` : me.domain) : "全局"}
          </Descriptions.Item>
          <Descriptions.Item label="行级受限">{snap?.row_level_restricted ? "是" : "否"}</Descriptions.Item>
          <Descriptions.Item label="已授权数据域">
            {snap?.granted_domains?.length ? snap.granted_domains.join("、") : "无"}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      {/* ============ 我的权限 ============ */}
      <Card
        title={
          <Space size={8}>
            <SafetyCertificateOutlined style={{ color: "#2563eb" }} />
            我的权限
          </Space>
        }
        style={{ marginBottom: 16 }}
      >
        {/* 资源级动作 */}
        <div style={{ marginBottom: 8 }}>
          <Space size={8} style={{ marginBottom: 8 }}>
            <ApiOutlined style={{ color: "#8c8c8c" }} />
            <span style={{ color: "#595959", fontWeight: 500 }}>资源级动作</span>
            <Tooltip title="对指标资源的读 / 写 / 审批 / 导出 / 复核权限（PDP 数据权限判定）">
              <Tag style={{ cursor: "help", color: "#8c8c8c", borderColor: "#d9d9d9" }}>?</Tag>
            </Tooltip>
          </Space>
          <Space wrap size={[6, 4]}>
            {(snap?.allowed_actions ?? []).map((a) => (
              <Tag key={a} color={ACTION_COLOR[a] ?? "default"}>{ACTION_LABEL[a] ?? a}</Tag>
            ))}
            {(snap?.allowed_actions ?? []).length === 0 && <Tag>无</Tag>}
          </Space>
        </div>

        <Divider style={{ margin: "16px 0" }} />

        {/* 按钮级权限点（按模块分组，中文展示） */}
        <div style={{ marginBottom: 8 }}>
          <Space size={8} style={{ marginBottom: 4 }}>
            <SafetyCertificateOutlined style={{ color: "#8c8c8c" }} />
            <span style={{ color: "#595959", fontWeight: 500 }}>按钮级权限点</span>
            <Tooltip title="按模块分组的功能按钮权限点（路由 / 菜单 / 页面 / 按钮级管控）；悬停单个权限点可查看说明">
              <Tag style={{ cursor: "help", color: "#8c8c8c", borderColor: "#d9d9d9" }}>?</Tag>
            </Tooltip>
          </Space>
        </div>
        {permGroups.length === 0 ? (
          <Tag>无</Tag>
        ) : (
          <Row gutter={[0, 16]}>
            {permGroups.map(([module, metas]) => (
              <Col span={24} key={module}>
                <Space size={8} style={{ marginBottom: 8 }}>
                  <Tag color={MODULE_COLOR[module] ?? "default"} style={{ minWidth: 56, textAlign: "center" }}>
                    {module}
                  </Tag>
                  <span style={{ color: "#8c8c8c", fontSize: 12 }}>{metas.length} 项</span>
                </Space>
                <Space wrap size={[6, 4]}>
                  {metas.map((m) => (
                    <Tooltip key={m.action} title={`${m.description}（${m.action}）`}>
                      <Tag
                        style={{
                          cursor: "default",
                          padding: "2px 10px",
                          background: m.module === "其他" ? "#fafafa" : undefined,
                          borderColor: "#d9d9d9",
                        }}
                      >
                        {m.label}
                      </Tag>
                    </Tooltip>
                  ))}
                </Space>
              </Col>
            ))}
          </Row>
        )}
      </Card>

      {/* ============ 我的授权 ============ */}
      <Card
        title={
          <Space size={8}>
            <SafetyCertificateOutlined style={{ color: "#2563eb" }} />
            我的授权
          </Space>
        }
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

      {/* ============ 修改密码 ============ */}
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
