import { useEffect, useMemo, useRef, useState } from "react";
import { Outlet, useNavigate, useLocation } from "react-router-dom";
import { Layout as AntLayout, Menu, Button, Avatar, Dropdown, Badge, Input, Tooltip, theme, AutoComplete, Spin } from "antd";
import {
  AppstoreOutlined,
  PlusCircleOutlined,
  SwapOutlined,
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
  MessageOutlined,
  LineChartOutlined,
  CloudServerOutlined,
  DatabaseOutlined,
  ScheduleOutlined,
  FileTextOutlined,
  FundOutlined,
  GlobalOutlined,
  TagsOutlined,
  SettingOutlined,
  TeamOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
} from "@ant-design/icons";
import type { CurrentUser, GlobalSearchItem, GlobalSearchType } from "../types";
import {
  clearAuthTokens,
  fetchGlobalSearch,
  fetchPreferences,
  listNotifications,
  setPreference,
} from "../api";
import { navigateToSearchItem } from "../utils/searchNavigate";

const ROLE_LABEL: Record<string, string> = {
  platform_admin: "平台管理员",
  domain_admin: "域管理员",
  metric_owner: "指标负责人",
  reviewer: "评审员",
  compliance_officer: "合规官",
  analyst: "分析师",
  viewer: "只读用户",
};

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
      { key: "/favorites", label: "我的收藏", icon: <HeartOutlined /> },
    ],
  },
  {
    label: "数据采集",
    children: [
      { key: "/data-sources", label: "数据源", icon: <CloudServerOutlined /> },
      { key: "/catalogs", label: "采集目录", icon: <DatabaseOutlined /> },
      { key: "/collection-tasks", label: "采集任务", icon: <ScheduleOutlined /> },
      { key: "/collection-history", label: "采集记录", icon: <AuditOutlined /> },
    ],
  },
  {
    label: "指标资产",
    children: [
      { key: "/catalog", label: "指标目录", icon: <AppstoreOutlined /> },
      { key: "/compare", label: "指标对比", icon: <SwapOutlined /> },
      { key: "/templates", label: "指标模板", icon: <FileTextOutlined /> },
      { key: "/create", label: "注册指标", icon: <PlusCircleOutlined /> },
      { key: "/metrics/review", label: "指标审批", icon: <AuditOutlined /> },
      { key: "/domains", label: "主题域管理", icon: <ApartmentOutlined /> },
      { key: "/dimensions", label: "维度管理", icon: <PartitionOutlined /> },
      { key: "/glossary", label: "术语表", icon: <BookOutlined /> },
    ],
  },
  {
    label: "资产洞察",
    children: [
      { key: "/assetmap", label: "资产地图", icon: <GlobalOutlined /> },
      { key: "/lineage", label: "血缘视图", icon: <ApartmentOutlined /> },
    ],
  },
  {
    label: "治理合规",
    children: [
      { key: "/review", label: "冲突仲裁", icon: <DeploymentUnitOutlined /> },
      { key: "/quality", label: "质量中心", icon: <ExperimentOutlined /> },
    ],
  },
  {
    label: "消费接入",
    children: [
      { key: "/query", label: "查询工作台", icon: <ConsoleSqlOutlined /> },
      { key: "/ai", label: "AI 助手", icon: <RobotOutlined /> },
      { key: "/api-clients", label: "API 客户端", icon: <KeyOutlined /> },
    ],
  },
  {
    label: "运营中心",
    children: [
      { key: "/observability", label: "可观测中心", icon: <LineChartOutlined /> },
      { key: "/feedback", label: "用户反馈", icon: <MessageOutlined /> },
      { key: "/tracking-stats", label: "埋点统计", icon: <FundOutlined /> },
    ],
  },
  {
    label: "系统管理",
    children: [
      { key: "/users", label: "用户管理", icon: <TeamOutlined /> },
      { key: "/governance", label: "权限治理", icon: <SafetyCertificateOutlined /> },
      { key: "/audit", label: "审计日志", icon: <FileSearchOutlined /> },
      { key: "/dicts", label: "数据字典", icon: <TagsOutlined /> },
      { key: "/system-config", label: "系统配置", icon: <SettingOutlined /> },
    ],
  },
];

const ALL_NAV_KEYS = NAV_GROUPS.flatMap((g) => g.children.map((c) => c.key));

// 全局搜索类型 → 中文标签（顶栏下拉分组标题）
const SEARCH_TYPE_LABEL: Record<GlobalSearchType, string> = {
  metric: "指标",
  dimension: "维度",
  term: "术语",
  template: "模板",
  data_source: "数据源",
  catalog: "采集目录",
  field: "字段",
  subject_domain: "主题域",
};

// 顶栏下拉分组展示顺序
const SEARCH_TYPE_ORDER: GlobalSearchType[] = [
  "metric",
  "dimension",
  "term",
  "template",
  "data_source",
  "catalog",
  "field",
  "subject_domain",
];

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
  // 顶栏实时搜索：防抖聚合下拉的选项与加载态
  const [searchOptions, setSearchOptions] = useState<
    Array<{ value: string; label: React.ReactNode; groupLabel?: string; item: GlobalSearchItem }>
  >([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [notifCount, setNotifCount] = useState(0);
  const navigate = useNavigate();
  const location = useLocation();
  const { token } = theme.useToken();
  const saveTimer = useRef<number | null>(null);
  const searchTimer = useRef<number | null>(null);
  // 用户手动切换过折叠后，避免迟到的服务端响应覆盖用户意图
  const userToggled = useRef(false);
  // 内容区滚动容器引用，路由切换时自动回顶
  const contentRef = useRef<HTMLDivElement>(null);

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

  // 路由切换时自动将内容区滚动到顶部，避免切换页面后仍停留在上次滚动位置
  useEffect(() => {
    contentRef.current?.scrollTo({ top: 0, behavior: "smooth" });
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

  // 全局搜索跳转：按类型路由到对应详情/列表页（列表页支持 ?kw= 定位）
  function handleGoToItem(item: GlobalSearchItem) {
    navigateToSearchItem(navigate, item, searchKw.trim());
  }

  // 拉取聚合搜索并按类型分组组装下拉选项
  function runGlobalSearch(kw: string) {
    const trimmed = kw.trim();
    if (!trimmed) {
      setSearchOptions([]);
      return;
    }
    setSearchLoading(true);
    fetchGlobalSearch(trimmed, 5)
      .then((res) => {
        const opts: Array<{ value: string; label: React.ReactNode; groupLabel?: string; item: GlobalSearchItem }> = [];
        for (const type of SEARCH_TYPE_ORDER) {
          const items = res.groups[type] ?? [];
          if (items.length === 0) continue;
          for (const it of items) {
            opts.push({
              value: `${type}:${it.code}`,
              groupLabel: SEARCH_TYPE_LABEL[type],
              label: (
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "baseline" }}>
                  <span className="mono" style={{ fontSize: 13 }}>{it.code}</span>
                  <span style={{ color: "rgba(0,0,0,0.45)", fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {it.name}
                  </span>
                </div>
              ),
              item: it,
            });
          }
        }
        setSearchOptions(opts);
      })
      .catch(() => {
        // 搜索失败静默降级：下拉置空，不阻塞后续导航
        setSearchOptions([]);
      })
      .finally(() => setSearchLoading(false));
  }

  // 输入变化：300ms 防抖触发聚合搜索
  function handleSearchInput(value: string) {
    setSearchKw(value);
    if (searchTimer.current) window.clearTimeout(searchTimer.current);
    if (!value.trim()) {
      setSearchOptions([]);
      return;
    }
    searchTimer.current = window.setTimeout(() => runGlobalSearch(value), 300);
  }

  function handleSearchSelect(_value: string, option: { item?: GlobalSearchItem }) {
    if (option?.item) handleGoToItem(option.item);
    setSearchOptions([]);
  }

  // 回车兜底：跳转全局搜索页查看全部结果
  function handleSearchEnter() {
    if (!searchKw.trim()) return;
    navigate(`/search?q=${encodeURIComponent(searchKw.trim())}`);
    setSearchOptions([]);
  }

  // 组件卸载清理搜索防抖定时器
  useEffect(() => {
    return () => {
      if (searchTimer.current) window.clearTimeout(searchTimer.current);
    };
  }, []);

  const userMenuItems = [
    {
      key: "profile",
      label: `${user.display_name}（${ROLE_LABEL[user.role] ?? user.role}${user.domain ? ` · ${user.domain}` : ""}）`,
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
      clearAuthTokens();
      window.location.reload();
    }
  }

  const menuItems = NAV_GROUPS.map((g) => ({
    type: "group" as const,
    label: g.label,
    children: g.children.map((c) => ({ key: c.key, icon: c.icon, label: c.label })),
  }));

  return (
    <AntLayout style={{ height: "100vh" }}>
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
          onClick={({ key }) => {
            navigate(key);
            // 点击导航立即回顶（即使已停留在当前路由），避免内容停留在上次滚动位置
            contentRef.current?.scrollTo({ top: 0, behavior: "smooth" });
          }}
          style={{ borderInlineEnd: "none", paddingBottom: 24, height: "calc(100vh - 56px)", overflowY: "auto" }}
        />
      </Sider>

      <AntLayout style={{ height: "100%", overflow: "hidden" }}>
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
            <AutoComplete
              value={searchKw}
              options={searchOptions}
              onChange={handleSearchInput}
              onSelect={handleSearchSelect}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleSearchEnter();
              }}
              allowClear
              popupMatchSelectWidth={400}
              notFoundContent={
                searchLoading ? (
                  <Spin size="small" />
                ) : searchKw.trim() ? (
                  "未找到匹配结果"
                ) : (
                  "输入关键词搜索指标 / 维度 / 术语 / 表字段…"
                )
              }
            >
              <Input
                prefix={<SearchOutlined style={{ color: token.colorTextSecondary }} />}
                suffix={searchLoading ? <Spin size="small" /> : null}
                placeholder="搜索指标 / 维度 / 术语 / 模板 / 数据源 / 表字段"
                aria-label="全局搜索"
              />
            </AutoComplete>
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

        <Content ref={contentRef} style={{ padding: 24, overflow: "auto" }} className="app-content">
          <Outlet />
        </Content>
      </AntLayout>
    </AntLayout>
  );
}
