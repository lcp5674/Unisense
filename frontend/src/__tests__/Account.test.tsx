import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { App as AntApp } from "antd";
import { Account } from "../pages/Account";

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
    UnisenseApiError,
  };
});

import { fetchCurrentUser, fetchMyPermissions, changePassword } from "../api";

const mockMe = vi.mocked(fetchCurrentUser);
const mockPerms = vi.mocked(fetchMyPermissions);
const mockChange = vi.mocked(changePassword);

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
  ui_actions: ["catalog:view", "metric:create", "user:disable", "custom:probe"],
  ui_action_meta: [
    { action: "catalog:view", module: "指标", label: "查看指标目录", description: "访问指标目录列表" },
    { action: "metric:create", module: "指标", label: "创建指标", description: "新增指标（含口径定义）" },
    { action: "user:disable", module: "系统", label: "停用用户", description: "停用指定用户账号" },
    // custom:probe 故意不在 meta 中 → 降级显示编码
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

describe("Account 个人中心", () => {
  beforeEach(() => {
    mockMe.mockReset();
    mockPerms.mockReset();
    mockChange.mockReset();
    mockMe.mockResolvedValue(ME as never);
    mockPerms.mockResolvedValue(SNAP as never);
    mockChange.mockResolvedValue({ ok: true });
  });

  it("渲染个人概览横幅：头像首字 / 显示名 / 角色 / 组织域", async () => {
    render(<AntApp><Account /></AntApp>);
    // 概览横幅：显示名 + 用户名 + 主角色中文 + 组织 + 域
    expect((await screen.findAllByText("管理员")).length).toBeGreaterThan(0);
    expect(screen.getByText("@admin")).toBeTruthy();
    expect(screen.getByText("平台管理员")).toBeTruthy();
    expect(screen.getByText("🏛 默认组织")).toBeTruthy();
    expect(screen.getByText("· 财务域")).toBeTruthy();
  });

  it("渲染我的账号 / 我的权限 / 我的授权", async () => {
    render(<AntApp><Account /></AntApp>);
    await screen.findByText("@admin");
    // 账号卡：组织 / 域带编码
    expect(screen.getByText("默认组织（1）")).toBeTruthy();
    expect(screen.getByText("财务域（finance）")).toBeTruthy();
    // 资源级动作中文
    expect(screen.getByText("读取")).toBeTruthy();
    expect(screen.getByText("审批")).toBeTruthy();
    // 按钮级权限点：中文 label（不再是英文编码）
    expect(screen.getByText("查看指标目录")).toBeTruthy();
    expect(screen.getByText("创建指标")).toBeTruthy();
    expect(screen.getByText("停用用户")).toBeTruthy();
    // 模块分组徽标
    expect(screen.getByText("指标")).toBeTruthy();
    expect(screen.getByText("系统")).toBeTruthy();
    // 未知自定义动作降级显示编码 + 「其他」分组
    expect(screen.getByText("custom:probe")).toBeTruthy();
    expect(screen.getByText("其他")).toBeTruthy();
    // 我的授权
    expect(screen.getByText("sales")).toBeTruthy();
    expect(screen.getByText("只读")).toBeTruthy();
  });

  it("按钮级权限点按模块分组渲染（指标组含 2 项、系统组含 1 项）", async () => {
    render(<AntApp><Account /></AntApp>);
    await screen.findByText("@admin");
    // 「指标」模块徽标旁应有「2 项」计数
    const metricBadge = screen.getByText("指标");
    const metricSection = metricBadge.closest(".ant-space")?.parentElement;
    expect(within(metricSection as HTMLElement).getByText("2 项")).toBeTruthy();
    expect(within(metricSection as HTMLElement).getByText("查看指标目录")).toBeTruthy();
    expect(within(metricSection as HTMLElement).getByText("创建指标")).toBeTruthy();
    // 「系统」模块
    const sysBadge = screen.getByText("系统");
    const sysSection = sysBadge.closest(".ant-space")?.parentElement;
    expect(within(sysSection as HTMLElement).getByText("1 项")).toBeTruthy();
    expect(within(sysSection as HTMLElement).getByText("停用用户")).toBeTruthy();
  });

  it("修改密码：校验两次一致并调用 changePassword", async () => {
    render(<AntApp><Account /></AntApp>);
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
});
