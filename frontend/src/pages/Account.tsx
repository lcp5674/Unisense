/**
 * 个人中心（方案 C 的核心承载页，用户视角）：我的工作台 / 我的账号 / 我的权限 / 我的授权 / 修改密码。
 *
 * 账号类通知（user.* / org.* / grant.* / pii.*）的深链目标——用户本人视角，
 * 不再指向管理员管理列表页。数据来源：``GET /auth/me``（含 org_name/domain_name）
 * + ``GET /me/permissions``（allowed_actions / ui_actions / grants / expiring_soon）。
 *
 * 权限展示（用户视角）：
 *  - 「可访问功能模块」与左侧菜单（NAV_GROUPS + ROUTE_PERM）同源判定——用户看到的是
 *    自己在侧边栏真实可见的模块，所见即所得，不再展示按钮级权限点（粒度远细于菜单，
 *    且大量按钮动作在菜单上不可见，易造成「权限与菜单不一致」的困惑）。
 *  - 「资源级动作」为 PDP 数据权限（读/写/审批/导出/复核），保留展示。
 *  - 按钮级权限点（ui_actions）不做面向用户展示（由管理员在权限治理中配置）。
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
  Statistic,
  Table,
  Tag,
  Tooltip,
} from "antd";
import {
  ApiOutlined,
  AppstoreOutlined,
  BellOutlined,
  CheckSquareOutlined,
  DatabaseOutlined,
  HeartOutlined,
  KeyOutlined,
  SafetyCertificateOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import {
  changePassword,
  fetchCurrentUser,
  fetchMyPermissions,
  fetchUnreadCount,
  listDataSources,
  listFavorites,
  listMetrics,
  listNotifications,
  UnisenseApiError,
} from "../api";
import type { CurrentUser, GrantResponse, PermissionSnapshot } from "../types";
import { NAV_GROUPS } from "../components/Layout";
import { ROUTE_PERM } from "../hooks/usePermission";
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

// 资源级动作中文名 / 配色
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

/** 我的工作台快捷入口清单（数量来自后端过滤统计，点击跳转对应页面） */
const WORK_ITEMS = [
  { key: "metrics", label: "我负责的指标", icon: <AppstoreOutlined />, path: "/catalog", color: "#2563eb" },
  { key: "favorites", label: "我的收藏", icon: <HeartOutlined />, path: "/favorites", color: "#ec4899" },
  { key: "todos", label: "我的待办", icon: <CheckSquareOutlined />, path: "/todo", color: "#f59e0b" },
  { key: "unread", label: "未读通知", icon: <BellOutlined />, path: "/notifications", color: "#10b981" },
  { key: "sources", label: "我负责的数据源", icon: <DatabaseOutlined />, path: "/data-sources", color: "#8b5cf6" },
] as const;

function grantStatusLabel(status: string): { text: string; color: string } {
  if (status === "ACTIVE") return { text: "生效", color: "success" };
  if (status === "EXPIRED") return { text: "已过期", color: "default" };
  return { text: "已回收", color: "error" };
}

/**
 * 可访问功能模块：与侧边栏菜单（NAV_GROUPS + ROUTE_PERM）同源判定。
 * 只返回用户在侧边栏真实可见的模块分组（过滤掉无权限菜单项、空组隐藏），
 * 保证「个人中心看到的 = 左侧菜单看到的」。
 */
export function accessibleMenuGroups(uiActions: string[] | undefined) {
  const perms = new Set(uiActions ?? []);
  return NAV_GROUPS.map((g) => ({
    label: g.label,
    children: g.children
      .filter((c) => {
        // 审批中心聚合三个审批/仲裁入口：任一相关权限点即放行（与 Layout 菜单一致）
        if (c.key === "/approval") {
          return ["metric:review", "master-data:review", "review:view"].some((p) => perms.has(p));
        }
        const perm = ROUTE_PERM[c.key];
        return perm ? perms.has(perm) : true;
      })
      .map((c) => c.label),
  })).filter((g) => g.children.length > 0);
}

export function Account() {
  const { message } = AntApp.useApp();
  const navigate = useNavigate();
  const [me, setMe] = useState<CurrentUser | null>(null);
  const [snap, setSnap] = useState<PermissionSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [work, setWork] = useState<Record<string, number | undefined>>({});
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

  // 我的工作台：个人数据快照（按负责人过滤统计，单条分页取 total，失败兜底「—」）
  useEffect(() => {
    if (!me?.id) return;
    let alive = true;
    Promise.all([
      listMetrics({ owner_id: me.id, page: 1, page_size: 1 }).then((r) => r.total).catch(() => undefined),
      listFavorites().then((f) => f.length).catch(() => undefined),
      listNotifications({ todo_only: true, page: 1, page_size: 1 }).then((r) => r.total).catch(() => undefined),
      fetchUnreadCount().catch(() => undefined),
      listDataSources({ owner_id: me.id, page: 1, page_size: 1 }).then((r) => r.total).catch(() => undefined),
    ]).then(([metrics, favorites, todos, unread, sources]) => {
      if (alive) setWork({ metrics, favorites, todos, unread, sources });
    });
    return () => {
      alive = false;
    };
  }, [me?.id]);

  const menuGroups = useMemo(() => accessibleMenuGroups(snap?.ui_actions), [snap?.ui_actions]);

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

  const displayName = me?.display_name || me?.username || "用户";
  const roles = snap?.roles?.length ? snap.roles : me?.role ? [me.role] : [];

  return (
    <div style={{ width: "100%" }}>
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

      {/* ============ 我的工作台（用户个人数据快照 + 快捷入口） ============ */}
      <Card
        title={
          <Space size={8}>
            <AppstoreOutlined style={{ color: "#2563eb" }} />
            我的工作台
          </Space>
        }
        style={{ marginBottom: 16 }}
      >
        <Row gutter={[16, 16]}>
          {WORK_ITEMS.map((item) => (
            <Col xs={12} md={8} xl={4} key={item.key}>
              <Card
                hoverable
                onClick={() => navigate(item.path)}
                styles={{ body: { padding: "16px 20px" } }}
                style={{ borderColor: "#e5e7eb" }}
              >
                <Space align="start" size={12}>
                  <span style={{ fontSize: 22, color: item.color }}>{item.icon}</span>
                  <div>
                    <Statistic
                      value={work[item.key] ?? "—"}
                      valueStyle={{ fontSize: 22, fontWeight: 600, color: "#1f2937", lineHeight: 1.2 }}
                    />
                    <div style={{ color: "#6b7280", fontSize: 13 }}>{item.label}</div>
                  </div>
                </Space>
              </Card>
            </Col>
          ))}
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
        <Descriptions column={{ xs: 1, sm: 2, xl: 4 }} size="middle">
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

      {/* ============ 我的权限（模块级 + 资源级，用户视角） ============ */}
      <Card
        title={
          <Space size={8}>
            <SafetyCertificateOutlined style={{ color: "#2563eb" }} />
            我的权限
          </Space>
        }
        style={{ marginBottom: 16 }}
      >
        {/* 可访问功能模块：与左侧菜单一致 */}
        <div style={{ marginBottom: 8 }}>
          <Space size={8} style={{ marginBottom: 8 }}>
            <SafetyCertificateOutlined style={{ color: "#8c8c8c" }} />
            <span style={{ color: "#595959", fontWeight: 500 }}>可访问功能模块</span>
            <Tooltip title="与左侧菜单一致——你当前账号能使用的功能模块；未列出的模块你无权访问（不在菜单中显示）">
              <Tag style={{ cursor: "help", color: "#8c8c8c", borderColor: "#d9d9d9" }}>?</Tag>
            </Tooltip>
          </Space>
        </div>
        {menuGroups.length === 0 ? (
          <Tag>无</Tag>
        ) : (
          <Row gutter={[24, 16]}>
            {menuGroups.map((g) => (
              <Col xs={24} md={12} xl={8} key={g.label}>
                <div style={{ color: "#8c8c8c", fontSize: 12, marginBottom: 6 }}>{g.label}</div>
                <Space wrap size={[6, 4]}>
                  {g.children.map((label) => (
                    <Tag key={label} color="blue" style={{ padding: "2px 10px", borderColor: "#91caff" }}>
                      {label}
                    </Tag>
                  ))}
                </Space>
              </Col>
            ))}
          </Row>
        )}

        <Divider style={{ margin: "16px 0" }} />

        {/* 资源级动作（PDP 数据权限） */}
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
