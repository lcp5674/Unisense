import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { UserManagement } from "../pages/UserManagement";

vi.mock("../api", () => {
  class UnisenseApiError extends Error {
    code: string;
    traceId: string;
    status: number;
    constructor(message: string, code: string, status: number, traceId: string) {
      super(message);
      this.name = "UnisenseApiError";
      this.code = code;
      this.status = status;
      this.traceId = traceId;
    }
  }
  return {
    fetchCurrentUser: vi.fn(),
    listAdminUsers: vi.fn(),
    createUser: vi.fn(),
    updateUser: vi.fn(),
    setUserStatus: vi.fn(),
    resetUserPassword: vi.fn(),
    UnisenseApiError,
  };
});

import {
  fetchCurrentUser,
  listAdminUsers,
  createUser,
  updateUser,
  setUserStatus,
  resetUserPassword,
} from "../api";

const mockMe = vi.mocked(fetchCurrentUser);
const mockList = vi.mocked(listAdminUsers);
const mockCreate = vi.mocked(createUser);
const mockUpdate = vi.mocked(updateUser);
const mockStatus = vi.mocked(setUserStatus);
const mockReset = vi.mocked(resetUserPassword);

const ADMIN = {
  id: 1,
  username: "admin",
  display_name: "平台管理员",
  role: "platform_admin",
  domain: null,
  org_id: 1,
};

const USERS = {
  total: 2,
  page: 1,
  page_size: 20,
  items: [
    { id: 1, username: "admin", email: "admin@example.com", display_name: "平台管理员", role: "platform_admin", domain: null, status: "active", last_login_at: "2026-08-14T10:00:00", created_at: "2026-08-01T10:00:00" },
    { id: 2, username: "alice", email: "alice@example.com", display_name: "爱丽丝", role: "viewer", domain: "finance", status: "disabled", last_login_at: null, created_at: "2026-08-02T10:00:00" },
  ],
};

describe("UserManagement 用户管理", () => {
  beforeEach(() => {
    mockMe.mockReset();
    mockList.mockReset();
    mockCreate.mockReset();
    mockUpdate.mockReset();
    mockStatus.mockReset();
    mockReset.mockReset();
  });

  it("platform_admin：渲染用户列表与全部管理操作", async () => {
    mockMe.mockResolvedValue(ADMIN);
    mockList.mockResolvedValue(USERS);
    render(<UserManagement />);

    expect(await screen.findByText("alice")).toBeTruthy();
    expect(screen.getByText("爱丽丝")).toBeTruthy();
    expect(screen.getByText("alice@example.com")).toBeTruthy();
    expect(screen.getAllByText("平台管理员").length).toBeGreaterThan(0); // 页面标题 + 表格行
    expect(screen.getByText("禁用")).toBeTruthy(); // 状态 Tag
    expect(screen.getByText("创建用户")).toBeTruthy();
    // 管理操作按钮（编辑 / 重置密码 / 启用）
    expect(screen.getAllByText("编 辑").length).toBeGreaterThan(0);
    expect(screen.getAllByText("重置密码").length).toBeGreaterThan(0);
    expect(screen.getAllByText("启 用").length).toBeGreaterThan(0);
  });

  it("viewer：只读视图，无管理操作与创建按钮", async () => {
    mockMe.mockResolvedValue({ ...ADMIN, role: "viewer" });
    mockList.mockResolvedValue(USERS);
    render(<UserManagement />);

    expect(await screen.findByText("alice")).toBeTruthy();
    expect(screen.queryByText("创建用户")).toBeNull();
    expect(screen.queryByText("重置密码")).toBeNull();
    expect(screen.getByText(/当前账号为只读视图/)).toBeTruthy();
  });

  it("创建用户：调用 createUser 并刷新列表", async () => {
    mockMe.mockResolvedValue(ADMIN);
    mockList.mockResolvedValue(USERS);
    mockCreate.mockResolvedValue(USERS.items[1]);
    render(<UserManagement />);
    await screen.findByText("alice");

    fireEvent.click(screen.getByText("创建用户"));
    fireEvent.change(screen.getByPlaceholderText("如 zhangsan"), { target: { value: "bob" } });
    fireEvent.change(screen.getByPlaceholderText("name@example.com"), { target: { value: "bob@example.com" } });
    fireEvent.change(screen.getByPlaceholderText("如 张三"), { target: { value: "鲍勃" } });
    fireEvent.change(screen.getByPlaceholderText("至少 8 位"), { target: { value: "secret123" } });

    fireEvent.click(screen.getByText("创 建"));
    await waitFor(() => expect(mockCreate).toHaveBeenCalled());
    expect(mockCreate.mock.calls[0][0]).toMatchObject({
      username: "bob",
      email: "bob@example.com",
      display_name: "鲍勃",
      role: "viewer",
    });
  });

  it("禁用用户：确认后调用 setUserStatus(disabled)", async () => {
    mockMe.mockResolvedValue(ADMIN);
    mockList.mockResolvedValue(USERS);
    mockStatus.mockResolvedValue({ ...USERS.items[0], status: "disabled" });
    render(<UserManagement />);
    await screen.findByText("alice");

    fireEvent.click(screen.getAllByText("禁 用")[0]);
    const confirmBtn = await screen.findByText("确 认");
    fireEvent.click(confirmBtn);
    await waitFor(() => {
      expect(mockStatus).toHaveBeenCalledWith(1, "disabled");
    });
  });

  it("重置密码：填写新密码后调用 resetUserPassword", async () => {
    mockMe.mockResolvedValue(ADMIN);
    mockList.mockResolvedValue(USERS);
    mockReset.mockResolvedValue({ user_id: 2, ok: true });
    render(<UserManagement />);
    await screen.findByText("alice");

    fireEvent.click(screen.getAllByText("重置密码")[1]); // alice（id=2）行
    const input = await screen.findByPlaceholderText("至少 8 位");
    fireEvent.change(input, { target: { value: "newsecret123" } });
    fireEvent.click(screen.getByText("重 置"));
    await waitFor(() => expect(mockReset).toHaveBeenCalledWith(2, "newsecret123"));
  });
});
