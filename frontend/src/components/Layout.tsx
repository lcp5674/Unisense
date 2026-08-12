import { useEffect, useMemo, useRef, useState } from "react";
import { Outlet, useNavigate, useLocation } from "react-router-dom";
import { Layout as AntLayout, Menu, Button, Avatar, Dropdown, Badge, Input, Tooltip, theme } from "antd";
import {
  AppstoreOutlined,
  PlusCircleOutlined,
  AuditOutlined,
  CheckSquareOutlined,
  ApartmentOutlined,
  HeartOutlined,
  DashboardOutlined,
  LogoutOutlined,
  UserOutlined,
  BellOutlined,
  SearchOutlined,
  DeploymentUnitOutlined,
  ExperimentOutlined,
  PartitionOutlined,
  BookOutlined,
  SafetyCertificateOutlined,
  FileSearchOutlined,
  ConsoleSqlOutlined,
  KeyOutlined,
  RobotOutlined,
  LineChartOutlined,
  CloudServerOutlined,
  DatabaseOutlined,
  FileTextOutlined,
  GlobalOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
} from "@ant-design/icons";
import type { CurrentUser } from "../types";
import { clearToken, fetchPreferences, listNotifications, setPreference } from "../api";

const { Header, Sider, Content } = AntLayout;

// 侧边栏折叠状态：按用户持久化（服务端 user_preference 为准 + 本地 per-user 缓存加速，跨用户隔离）
const SIDER_UI_KEY = "ui"; // user_preference.preference_key
const SIDER_FIELD = "sider_collapsed"; // ui.value 内的字段

function siderStorageKey(userId: number): string {
  return `unisense.sider.collapsed.${userId}`;
}

// 分组导航：覆盖后端全部功能域
const NAV_GROUPS: Array<{ label: string; children: Array<{ key: string; label: string; icon: React.ReactNode }> }> = [
  {
    label: "工作台",
    children: [
      { key: "/dashboard", label: "总览仪表", icon: <DashboardOutlined /> },
      { key: "/todo", label: "待办中心", icon: <CheckSquareOutlined /> },
      { key: "/notifications", label: "通知中心", icon: <BellOutlined /> },
    ],
  },
  {
    label: "指标资产",
    children: [
      { key: "/catalog", label: "指标目录", icon: <AppstoreOutlined /> },
      { key: "/templates", label: "指标模板", icon: <FileTextOutlined /> },
      { key: "/create", label: "注册指标", icon: <PlusCircleOutlined /> },
      { key: "/metrics/review", label: "指标审批", icon: <AuditOutlined /> },
      { key: "/favorites", label: "我的收藏", icon: <HeartOutlined /> },
      { key: "/assetmap", label: "资产地图", icon: <GlobalOutlined /> },
    ],
  },
  {
    label: "血缘与影响",
    children: [
      { key: "/lineage", label: "血缘视图", icon: <ApartmentOutlined /> },
    ],
  },
  {
    label: "治理合规",
    children: [
      { key: "/review", label: "冲突仲裁", icon: <DeploymentUnitOutlined /> },
      { key: "/quality", label: "质量中心", icon: <ExperimentOutlined /> },
      { key: "/dimensions", label: "维度管理", icon: <PartitionOutlined /> },
      { key: "/glossary", label: "术语表", icon: <BookOutlined /> },
      { key: "/governance", label: "权限治理", icon: <SafetyCertificateOutlined /> },
      { key: "/audit", label: "审计日志", icon: <FileSearchOutlined /> },
    ],
  },
  {
    label: "消费接入",
    children: [
      { key: "/query", label: "查询工作台", icon: <ConsoleSqlOutlined /> },
      { key: "/api-clients", label: "API 客户端", icon: <KeyOutlined /> },
    ],
  },
  {
    label: "智能与可观测",
    children: [
      { key: "/ai", label: "AI 助手", icon: <RobotOutlined /> },
      { key: "/observability", label: "可观测中心", icon: <LineChartOutlined /> },
    ],
  },
  {
    label: "数据采集",
    children: [
      { key: "/data-sources", label: "数据源管理", icon: <CloudServerOutlined /> },
      { key: "/catalogs", label: "采集目录", icon: <DatabaseOutlined /> },
    ],
  },
];

const ALL_NAV_KEYS = NAV_GROUPS.flatMap((g) => g.children.map((c) => c.key));

export function Layout({ user }: { user: CurrentUser }) {
  // 折叠状态从「该用户的本地缓存」恢复（跨用户隔离）；隐私模式等异常场景回退为展开
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(siderStorageKey(user.id)) === "1";
    } catch {
      return false;
    }
  });
  const [searchKw, setSearchKw] = useState("");
  const [notifCount, setNotifCount] = useState(0);
  const navigate = useNavigate();
  const location = useLocation();
  const { token } = theme.useToken();
  const saveTimer = useRef<number | null>(null);
  // 用户手动切换过折叠后，避免迟到的服务端响应覆盖用户意图
  const userToggled = useRef(false);

  // 用户切换/首次挂载：先取该用户本地缓存（避免闪烁），再以服务端偏好为准并回写缓存
  useEffect(() => {
    userToggled.current = false;
    let cancelled = false;
    try {
      const stored = localStorage.getItem(siderStorageKey(user.id));
      if (stored !== null) setCollapsed(stored === "1");
    } catch {
      /* 隐私模式等场景忽略 */
    }
    fetchPreferences()
      .then((prefs) => {
        if (cancelled || userToggled.current) return;
        const ui = (prefs[SIDER_UI_KEY] ?? {}) as Record<string, unknown>;
        if (typeof ui[SIDER_FIELD] === "boolean") {
          const serverCollapsed = ui[SIDER_FIELD] as boolean;
          setCollapsed(serverCollapsed);
          try {
            localStorage.setItem(siderStorageKey(user.id), serverCollapsed ? "1" : "0");
          } catch {
            /* 忽略 */
          }
        }
      })
      .catch(() => {
        /* 服务端不可达时保留本地偏好 */
      });
    return () => {
      cancelled = true;
    };
  }, [user.id]);

  function handleCollapse(value: boolean) {
    userToggled.current = true;
    setCollapsed(value);
    try {
      localStorage.setItem(siderStorageKey(user.id), value ? "1" : "0");
    } catch {
      /* 隐私模式等场景忽略持久化失败 */
    }
    // 服务端持久化（防抖，避免快速连点产生冗余写）
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      setPreference(SIDER_UI_KEY, { [SIDER_FIELD]: value }).catch(() => {
        /* 离线/失败时保留本地偏好，下次挂载以服务端为准 */
      });
    }, 400);
  }

  // 选中项：最长前缀匹配
  const selectedKey = useMemo(() => {
    const path = location.pathname;
    const matched = ALL_NAV_KEYS.filter((k) => path === k || path.startsWith(`${k}/`));
    if (matched.length === 0) return "";
    return matched.sort((a, b) => b.length - a.length)[0];
  }, [location.pathname]);

  useEffect(() => {
    let cancelled = false;
    listNotifications()
      .then((res) => {
        if (!cancelled) setNotifCount(res.items.filter((n) => n.status !== "SENT").length);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [location.pathname]);

  function handleSearch() {
    if (!searchKw.trim()) return;
    navigate(`/catalog?kw=${encodeURIComponent(searchKw.trim())}`);
  }

  const userMenuItems = [
    {
      key: "profile",
      label: `${user.display_name}（${user.role}${user.domain ? ` · ${user.domain}` : ""}）`,
      disabled: true,
    },
    { type: "divider" as const },
    {
      key: "logout",
      label: "退出登录",
      icon: <LogoutOutlined />,
      danger: true,
    },
  ];

  function handleUserMenu({ key }: { key: string }) {
    if (key === "logout") {
      clearToken();
      window.location.reload();
    }
  }

  const menuItems = NAV_GROUPS.map((g) => ({
    type: "group" as const,
    label: g.label,
    children: g.children.map((c) => ({ key: c.key, icon: c.icon, label: c.label })),
  }));

  return (
    <AntLayout style={{ minHeight: "100vh" }}>
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={handleCollapse}
        trigger={null}
        width={232}
        theme="dark"
        style={{ borderRight: "1px solid rgba(255,255,255,0.06)" }}
      >
        <div
          style={{
            height: 56,
            display: "flex",
            alignItems: "center",
            justifyContent: collapsed ? "center" : "flex-start",
            gap: 12,
            padding: collapsed ? 0 : "0 20px",
            borderBottom: "1px solid rgba(255,255,255,0.08)",
          }}
        >
          <div className="brand-mark" style={{ width: 32, height: 32, borderRadius: 9, fontSize: 15 }}>
            U
          </div>
          {!collapsed && (
            <div>
              <div className="brand-name" style={{ color: "#fff", fontSize: 15 }}>
                Unisense
              </div>
              <div className="brand-sub" style={{ fontSize: 10, color: "rgba(235,240,247,0.5)" }}>
                Metric Semantics Hub
              </div>
            </div>
          )}
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={selectedKey ? [selectedKey] : []}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
          style={{ borderInlineEnd: "none", paddingBottom: 24 }}
        />
      </Sider>

      <AntLayout>
        <Header
          style={{
            padding: "0 24px",
            background: token.colorBgContainer,
            display: "flex",
            alignItems: "center",
            gap: 16,
            boxShadow: "0 1px 4px rgba(12,22,38,0.06)",
            position: "sticky",
            top: 0,
            zIndex: 10,
          }}
        >
          <div
            className="header-brand"
            style={{
              flex: "0 0 auto",
              display: "flex",
              alignItems: "center",
              gap: 10,
              minWidth: collapsed ? "auto" : 200,
            }}
          >
            <Tooltip title={collapsed ? "展开侧边栏" : "收起侧边栏"}>
              <Button
                type="text"
                aria-label={collapsed ? "展开侧边栏" : "收起侧边栏"}
                icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
                onClick={() => handleCollapse(!collapsed)}
                style={{ color: token.colorTextSecondary, fontSize: 16 }}
              />
            </Tooltip>
            {!collapsed && (
              <>
                <div className="brand-mark" style={{ width: 30, height: 30, borderRadius: 8, fontSize: 14 }}>
                  U
                </div>
                <div>
                  <div className="brand-name" style={{ fontSize: 15 }}>Unisense</div>
                </div>
              </>
            )}
          </div>

          <div className="header-search">
            <Input
              prefix={<SearchOutlined style={{ color: token.colorTextSecondary }} />}
              placeholder="搜索指标名 / 编码，回车直达目录"
              value={searchKw}
              onChange={(e) => setSearchKw(e.target.value)}
              onPressEnter={handleSearch}
              allowClear
            />
          </div>

          <div style={{ flex: 1 }} />

          <Tooltip title="通知中心">
            <Badge count={notifCount} size="small" offset={[-2, 2]}>
              <Button
                type="text"
                icon={<BellOutlined style={{ fontSize: 16 }} />}
                onClick={() => navigate("/notifications")}
              />
            </Badge>
          </Tooltip>

          <Dropdown menu={{ items: userMenuItems, onClick: handleUserMenu }} placement="bottomRight">
            <Button type="text" style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <Avatar size="small" style={{ background: token.colorPrimary }} icon={<UserOutlined />} />
              <span>{user.display_name}</span>
            </Button>
          </Dropdown>
        </Header>

        <Content style={{ padding: 24, overflow: "auto" }} className="app-content">
          <Outlet />
        </Content>
      </AntLayout>
    </AntLayout>
  );
}
