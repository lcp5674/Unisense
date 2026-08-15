import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
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
  home_domain: "finance",
  allowed_actions: ["read", "write", "approve", "export", "review"],
  ui_actions: ["catalog:view", "metric:create", "user:disable"],
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

  it("渲染我的账号 / 我的权限 / 我的授权", async () => {
    render(<AntApp><Account /></AntApp>);
    expect(await screen.findByText("管理员")).toBeTruthy();
    expect(screen.getByText("财务域（finance）")).toBeTruthy();
    expect(screen.getByText("默认组织（1）")).toBeTruthy();
    // 资源级动作 + 按钮级权限点
    expect(screen.getByText("读取")).toBeTruthy();
    expect(screen.getByText("catalog:view")).toBeTruthy();
    expect(screen.getByText("metric:create")).toBeTruthy();
    // 我的授权
    expect(screen.getByText("sales")).toBeTruthy();
    expect(screen.getByText("只读")).toBeTruthy();
  });

  it("修改密码：校验两次一致并调用 changePassword", async () => {
    render(<AntApp><Account /></AntApp>);
    await screen.findByText("管理员");
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
