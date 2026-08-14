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
    listDomainTree: vi.fn(),
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
  listDomainTree,
  createUser,
  updateUser,
  setUserStatus,
  resetUserPassword,
} from "../api";

const mockMe = vi.mocked(fetchCurrentUser);
const mockList = vi.mocked(listAdminUsers);
const mockDomains = vi.mocked(listDomainTree);
const mockCreate = vi.mocked(createUser);
const mockUpdate = vi.mocked(updateUser);
const mockStatus = vi.mocked(setUserStatus);
const mockReset = vi.mocked(resetUserPassword);

/** 在可见 antd 下拉中点击指定选项：虚拟列表渲染同名包裹节点，须点 .ant-select-item-option 本体才触发选中。 */
async function clickSelectOption(title: string) {
  await waitFor(() => {
    const dropdown = document.querySelector(
      ".ant-select-dropdown:not(.ant-select-dropdown-hidden)",
    ) as HTMLElement | null;
    const option = dropdown?.querySelector(
      `.ant-select-item-option[title="${title}"]`,
    ) as HTMLElement | null;
    expect(option).toBeTruthy();
    if (option) fireEvent.click(option);
  });
}

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
    mockDomains.mockReset();
    mockCreate.mockReset();
    mockUpdate.mockReset();
    mockStatus.mockReset();
    mockReset.mockReset();
    mockDomains.mockResolvedValue([]);
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

  it("创建用户：手动输入密码、选择主题域，创建成功后一次性展示明文", async () => {
    mockMe.mockResolvedValue(ADMIN);
    mockList.mockResolvedValue(USERS);
    mockDomains.mockResolvedValue([
      { id: 1, code: "finance", name: "财务域", level: 1, parent_id: null, sort_order: 1, status: "active", metric_count: 0, children: [] },
    ]);
    mockCreate.mockResolvedValue(USERS.items[1]);
    render(<UserManagement />);
    await screen.findByText("alice");

    fireEvent.click(screen.getByText("创建用户"));
    fireEvent.change(screen.getByPlaceholderText("如 zhangsan"), { target: { value: "bob" } });
    fireEvent.change(screen.getByPlaceholderText("name@example.com"), { target: { value: "bob@example.com" } });
    fireEvent.change(screen.getByPlaceholderText("如 张三"), { target: { value: "鲍勃" } });
    fireEvent.change(screen.getByPlaceholderText("至少 8 位"), { target: { value: "secret123" } });

    // 所属域下拉：从主题域列表选择（复用 listDomainTree(active)）
    fireEvent.mouseDown(screen.getByText("选择主题域"));
    await clickSelectOption("财务域（finance）");

    fireEvent.click(screen.getByText("创 建"));
    await waitFor(() => expect(mockCreate).toHaveBeenCalled());
    expect(mockCreate.mock.calls[0][0]).toMatchObject({
      username: "bob",
      email: "bob@example.com",
      display_name: "鲍勃",
      role: "viewer",
      domain: "finance",
      password: "secret123",
    });

    // 创建成功后一次性展示明文密码（可复制交付）
    expect(await screen.findByText("用户创建成功")).toBeTruthy();
    expect(screen.getByText("secret123")).toBeTruthy();
    expect(screen.getByText(/仅在此展示一次/)).toBeTruthy();
  });

  it("创建用户：打开弹窗自动预填强随机密码，可直接提交", async () => {
    mockMe.mockResolvedValue(ADMIN);
    mockList.mockResolvedValue(USERS);
    mockCreate.mockResolvedValue(USERS.items[1]);
    render(<UserManagement />);
    await screen.findByText("alice");

    fireEvent.click(screen.getByText("创建用户"));
    fireEvent.change(screen.getByPlaceholderText("如 zhangsan"), { target: { value: "bob" } });
    fireEvent.change(screen.getByPlaceholderText("name@example.com"), { target: { value: "bob@example.com" } });
    fireEvent.change(screen.getByPlaceholderText("如 张三"), { target: { value: "鲍勃" } });

    // 密码框已自动预填强密码
    expect(screen.getByText(/已自动预填强密码/)).toBeTruthy();

    fireEvent.click(screen.getByText("创 建"));
    await waitFor(() => expect(mockCreate).toHaveBeenCalled());
    const payload = mockCreate.mock.calls[0][0];
    // 满足后端 ≥8 位且含大小写/数字/符号的强密码要求
    expect(payload.password).toMatch(/^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z0-9]).{8,}$/);

    // 创建成功后展示该明文
    expect(await screen.findByText("用户创建成功")).toBeTruthy();
    expect(screen.getByText(payload.password)).toBeTruthy();
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

  it("切换每页条数后按新 page_size 重新请求（不固化为 20 条/页）", async () => {
    mockMe.mockResolvedValue(ADMIN);
    mockList.mockResolvedValue(USERS);
    const { container } = render(<UserManagement />);
    await screen.findByText("alice");

    // 初始请求固定 page_size=20
    await waitFor(() => {
      expect(mockList).toHaveBeenCalledWith(expect.objectContaining({ page_size: 20 }));
    });

    // 打开每页条数选择器并选择「50」
    const sizeChanger = container.querySelector(".ant-pagination-options .ant-select-selector");
    expect(sizeChanger).toBeTruthy();
    fireEvent.mouseDown(sizeChanger!);
    const option = await screen.findByRole("option", { name: /50/ });
    fireEvent.click(option);

    // 重新请求携带新的 page_size
    await waitFor(() => {
      const calls = mockList.mock.calls;
      const lastCall = calls.length > 0 ? calls[calls.length - 1][0] : undefined;
      expect(lastCall?.page_size).toBe(50);
    });
  });
});
