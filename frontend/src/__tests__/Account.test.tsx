import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { App as AntApp } from "antd";
import { Account, accessibleMenuGroups } from "../pages/Account";

vi.mock("../api", () => {
  class UnisenseApiError extends Error {
    code: string;
    status: number;
    codeZh: string;
    constructor(message: string, code: string, status: number, codeZh: string) {
      super(message);
      this.name = "UnisenseApiError";
      this.code = code;
      this.status = status;
      this.codeZh = codeZh;
    }
  }
  return {
    fetchCurrentUser: vi.fn(),
    fetchMyPermissions: vi.fn(),
    changePassword: vi.fn(),
    setupMy2fa: vi.fn(),
    confirmMy2fa: vi.fn(),
    disableMy2fa: vi.fn(),
    listMetrics: vi.fn(),
    listFavorites: vi.fn(),
    listNotifications: vi.fn(),
    fetchUnreadCount: vi.fn(),
    listDataSources: vi.fn(),
    UnisenseApiError,
  };
});

import {
  fetchCurrentUser,
  fetchMyPermissions,
  changePassword,
  setupMy2fa,
  confirmMy2fa,
  disableMy2fa,
  listMetrics,
  listFavorites,
  listNotifications,
  fetchUnreadCount,
  listDataSources,
} from "../api";

const mockMe = vi.mocked(fetchCurrentUser);
const mockPerms = vi.mocked(fetchMyPermissions);
const mockChange = vi.mocked(changePassword);
const mockListMetrics = vi.mocked(listMetrics);
const mockFavorites = vi.mocked(listFavorites);
const mockNotifications = vi.mocked(listNotifications);
const mockUnread = vi.mocked(fetchUnreadCount);
const mockSources = vi.mocked(listDataSources);

const ME = {
  id: 1,
  username: "admin",
  display_name: "管理员",
  role: "platform_admin",
  domain: "finance",
  domain_name: "财务域",
  org_id: 1,
  org_name: "默认组织",
};

const SNAP = {
  user_id: 1,
  role: "platform_admin",
  roles: ["platform_admin"],
  home_domain: "finance",
  allowed_actions: ["read", "write", "approve", "export", "review"],
  ui_actions: [
    "dashboard:view",
    "catalog:view",
    "metric:create",
    "assetmap:view",
    "query:view",
    // 无 data-sources:view / users:view 等 → 对应菜单不展示
    "user:disable",
    "custom:probe",
  ],
  granted_domains: ["finance"],
  metric_whitelist: [],
  row_level_restricted: false,
  grants: [
    {
      id: 1,
      user_id: 1,
      role_id: null,
      domain: "sales",
      metric_whitelist: null,
      grant_type: "READ",
      status: "ACTIVE",
      row_level: false,
      expires_at: null,
      granted_by: null,
      reason: null,
    },
  ],
  expiring_soon: [],
};

function renderPage() {
  return render(
    <AntApp>
      <MemoryRouter>
        <Account />
      </MemoryRouter>
    </AntApp>,
  );
}

describe("Account 个人中心", () => {
  beforeEach(() => {
    mockMe.mockReset();
    mockPerms.mockReset();
    mockChange.mockReset();
    mockListMetrics.mockReset();
    mockFavorites.mockReset();
    mockNotifications.mockReset();
    mockUnread.mockReset();
    mockSources.mockReset();
    mockMe.mockResolvedValue(ME as never);
    mockPerms.mockResolvedValue(SNAP as never);
    mockChange.mockResolvedValue({ ok: true });
    mockListMetrics.mockResolvedValue({ items: [], total: 12, page: 1, page_size: 1 } as never);
    mockFavorites.mockResolvedValue([] as never);
    mockNotifications.mockResolvedValue({ items: [], total: 3, page: 1, page_size: 1 } as never);
    mockUnread.mockResolvedValue(5 as never);
    mockSources.mockResolvedValue({ items: [], total: 7, page: 1, page_size: 1 } as never);
  });

  it("渲染个人概览横幅：头像首字 / 显示名 / 角色 / 组织域", async () => {
    renderPage();
    expect((await screen.findAllByText("管理员")).length).toBeGreaterThan(0);
    expect(screen.getByText("@admin")).toBeTruthy();
    expect(screen.getByText("平台管理员")).toBeTruthy();
    expect(screen.getByText("🏛 默认组织")).toBeTruthy();
    expect(screen.getByText("· 财务域")).toBeTruthy();
  });

  it("渲染我的工作台：个人数据快照（负责指标/收藏/待办/未读/负责数据源）", async () => {
    renderPage();
    // 工作台标题与标签
    expect(await screen.findByText("我的工作台")).toBeTruthy();
    expect(screen.getByText("我负责的指标")).toBeTruthy();
    expect(screen.getByText("我的收藏")).toBeTruthy();
    expect(screen.getByText("我的待办")).toBeTruthy();
    expect(screen.getByText("未读通知")).toBeTruthy();
    expect(screen.getByText("我负责的数据源")).toBeTruthy();
    // 数量（来自 owner_id 过滤统计 / 收藏长度 / todo_only 通知 / 未读 / owner 过滤数据源）
    expect(screen.getByText("12")).toBeTruthy();
    expect(screen.getByText("3")).toBeTruthy();
    expect(screen.getByText("5")).toBeTruthy();
    expect(screen.getByText("7")).toBeTruthy();
    // 按负责人过滤调用（单条分页取 total）
    expect(mockListMetrics).toHaveBeenCalledWith({ owner_id: 1, page: 1, page_size: 1 });
    expect(mockNotifications).toHaveBeenCalledWith({ todo_only: true, page: 1, page_size: 1 });
    expect(mockSources).toHaveBeenCalledWith({ owner_id: 1, page: 1, page_size: 1 });
  });

  it("我的权限：可访问功能模块与侧边栏一致（有权限显示、无权限隐藏），资源级动作保留", async () => {
    renderPage();
    await screen.findByText("@admin");
    // 可访问功能模块：有 view 权限点的菜单展示
    expect(screen.getByText("可访问功能模块")).toBeTruthy();
    expect(screen.getByText("总览仪表")).toBeTruthy();
    expect(screen.getByText("指标目录")).toBeTruthy();
    expect(screen.getByText("注册指标")).toBeTruthy();
    expect(screen.getByText("资产地图")).toBeTruthy();
    expect(screen.getByText("查询工作台")).toBeTruthy();
    // 无权限的菜单不展示（数据源管理 / 用户管理等）
    expect(screen.queryByText("数据源管理")).toBeNull();
    expect(screen.queryByText("用户管理")).toBeNull();
    expect(screen.queryByText("权限治理")).toBeNull();
    // 按钮级权限点不再展示（中文 label 与英文编码都不出现）
    expect(screen.queryByText("创建指标")).toBeNull();
    expect(screen.queryByText("user:disable")).toBeNull();
    expect(screen.queryByText("custom:probe")).toBeNull();
    expect(screen.queryByText("其他")).toBeNull();
    // 资源级动作保留
    expect(screen.getByText("读取")).toBeTruthy();
    expect(screen.getByText("审批")).toBeTruthy();
    // 我的授权
    expect(screen.getByText("sales")).toBeTruthy();
    expect(screen.getByText("只读")).toBeTruthy();
  });

  it("accessibleMenuGroups 纯函数：与 ROUTE_PERM 同源判定菜单模块", () => {
    // 有 catalog:view / metric:create / assetmap:view
    const groups = accessibleMenuGroups(["catalog:view", "metric:create", "assetmap:view"]);
    const labels = groups.flatMap((g) => g.children);
    expect(labels).toContain("指标目录");
    expect(labels).toContain("注册指标");
    expect(labels).toContain("资产地图");
    // 指标运营分析/SQL 解析评测映射 metric:create → 可见
    expect(labels).toContain("指标运营分析");
    expect(labels).toContain("SQL 解析评测");
    // 无 data-sources:view → 数据源管理不出现
    expect(labels).not.toContain("数据源管理");
    expect(labels).not.toContain("用户管理");
    // 无任何权限 → 所有菜单均被权限点过滤（含 API 文档 system:docs，不再默认放行）
    expect(accessibleMenuGroups([]).flatMap((g) => g.children)).toEqual([]);
    // 快照未加载（undefined）→ 同样不显示权限菜单
    expect(accessibleMenuGroups(undefined).flatMap((g) => g.children)).toEqual([]);
  });

  it("修改密码：校验两次一致并调用 changePassword", async () => {
    renderPage();
    await screen.findByText("@admin");
    fireEvent.click(screen.getByRole("button", { name: /修改密码/ }));

    fireEvent.change(screen.getByPlaceholderText("当前密码"), { target: { value: "old123456" } });
    fireEvent.change(screen.getByPlaceholderText("至少 8 位，含大小写/数字/特殊字符中至少 3 类"), {
      target: { value: "NewPass123!" },
    });
    fireEvent.change(screen.getByPlaceholderText("再次输入新密码"), {
      target: { value: "NewPass123!" },
    });
    fireEvent.click(screen.getByRole("button", { name: /确认修改/ }));

    await waitFor(() =>
      expect(mockChange).toHaveBeenCalledWith({
        current_password: "old123456",
        new_password: "NewPass123!",
      }),
    );
  });

  it("双因子认证卡片：未开启时显示开启按钮，点击弹出设置弹窗", async () => {
    renderPage();
    await screen.findByText("@admin");
    // 卡片标题与状态
    expect(screen.getByText("双因子认证")).toBeTruthy();
    expect(screen.getByText("未开启")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /开启双因子认证/ }));
    // 弹窗要求输入当前密码（防会话劫持）
    expect(screen.getByPlaceholderText("当前密码")).toBeTruthy();
  });
});
