import { useState } from "react";
import { Outlet, useNavigate, useLocation } from "react-router-dom";
import { Layout as AntLayout, Menu, Breadcrumb, Button, Avatar, Dropdown } from "antd";
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
} from "@ant-design/icons";
import type { CurrentUser } from "../types";
import { clearToken } from "../api";

const { Header, Sider, Content } = AntLayout;

const NAV_ITEMS = [
  { key: "/catalog", label: "指标目录", icon: <AppstoreOutlined /> },
  { key: "/create", label: "注册指标", icon: <PlusCircleOutlined /> },
  { key: "/review", label: "审核工作台", icon: <AuditOutlined /> },
  { key: "/todo", label: "待办中心", icon: <CheckSquareOutlined /> },
  { key: "/lineage", label: "血缘视图", icon: <ApartmentOutlined /> },
  { key: "/favorites", label: "我的收藏", icon: <HeartOutlined /> },
  { key: "/dashboard", label: "治理驾驶舱", icon: <DashboardOutlined /> },
];

const BREADCRUMB_MAP: Record<string, string> = {
  catalog: "指标目录",
  create: "注册指标",
  review: "审核工作台",
  todo: "待办中心",
  lineage: "血缘视图",
  favorites: "我的收藏",
  dashboard: "治理驾驶舱",
  detail: "指标详情",
  guide: "消费指南",
};

export function Layout({ user }: { user: CurrentUser }) {
  const [collapsed, setCollapsed] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();

  const selectedKey = "/" + location.pathname.split("/").filter(Boolean)[0];

  const pathSegments = location.pathname.split("/").filter(Boolean);
  const breadcrumbItems = [
    { title: "Unisense" },
    ...pathSegments.map((seg) => ({
      title: BREADCRUMB_MAP[seg] || seg,
    })),
  ];

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

  function handleMenuClick({ key }: { key: string }) {
    if (key === "logout") {
      clearToken();
      window.location.reload();
    }
  }

  function handleNav({ key }: { key: string }) {
    navigate(key);
  }

  return (
    <AntLayout style={{ minHeight: "100vh" }}>
      <Sider collapsible collapsed={collapsed} onCollapse={setCollapsed}>
        <div
          style={{
            height: 32,
            margin: 16,
            color: "#fff",
            fontWeight: 700,
            fontSize: collapsed ? 14 : 18,
            textAlign: "center",
            lineHeight: "32px",
          }}
        >
          {collapsed ? "U" : "Unisense"}
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selectedKey]}
          items={NAV_ITEMS.map((item) => ({
            key: item.key,
            icon: item.icon,
            label: item.label,
          }))}
          onClick={handleNav}
        />
      </Sider>
      <AntLayout>
        <Header
          style={{
            padding: "0 24px",
            background: "#fff",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            boxShadow: "0 1px 4px rgba(0,0,0,0.08)",
          }}
        >
          <Breadcrumb items={breadcrumbItems} />
          <Dropdown menu={{ items: userMenuItems, onClick: handleMenuClick }} placement="bottomRight">
            <Button type="text" icon={<Avatar size="small" icon={<UserOutlined />} />}>
              {user.display_name}
            </Button>
          </Dropdown>
        </Header>
        <Content style={{ margin: 24, padding: 24, background: "#fff", borderRadius: 8, minHeight: 280 }}>
          <Outlet />
        </Content>
      </AntLayout>
    </AntLayout>
  );
}
