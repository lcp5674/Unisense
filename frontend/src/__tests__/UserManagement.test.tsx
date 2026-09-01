import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { UserManagement } from "../pages/UserManagement";
import { PermissionProvider } from "../hooks/usePermission";

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
    fetchMyPermissions: vi.fn(),
    listAdminUsers: vi.fn(),
    listDomainTree: vi.fn(),
    listOrganizations: vi.fn(),
    listRolePermissions: vi.fn(),
    createUser: vi.fn(),
    updateUser: vi.fn(),
    setUserStatus: vi.fn(),
    batchSetUserStatus: vi.fn(),
    resetUserPassword: vi.fn(),
    getUserPermissions: vi.fn(),
    setUserPermissions: vi.fn(),
    listActionRegistry: vi.fn(),
    UnisenseApiError,
  };
});

import {
  fetchCurrentUser,
  fetchMyPermissions,
  listAdminUsers,
  listDomainTree,
  listOrganizations,
  listRolePermissions,
  createUser,
  updateUser,
  setUserStatus,
  batchSetUserStatus,
  resetUserPassword,
  getUserPermissions,
  setUserPermissions,
  listActionRegistry,
} from "../api";

const mockMe = vi.mocked(fetchCurrentUser);
const mockPerms = vi.mocked(fetchMyPermissions);
const mockList = vi.mocked(listAdminUsers);
const mockDomains = vi.mocked(listDomainTree);
const mockOrgs = vi.mocked(listOrganizations);
const mockRoles = vi.mocked(listRolePermissions);
const mockCreate = vi.mocked(createUser);
const mockUpdate = vi.mocked(updateUser);
const mockStatus = vi.mocked(setUserStatus);
const mockBatchStatus = vi.mocked(batchSetUserStatus);
const mockReset = vi.mocked(resetUserPassword);
const mockGetUserPerm = vi.mocked(getUserPermissions);
const mockSetUserPerm = vi.mocked(setUserPermissions);
const mockActionRegistry = vi.mocked(listActionRegistry);

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

/** 渲染并注入权限上下文（viewer 基线：无任何 user:* 管理权限点）。 */
function renderViewer() {
  mockPerms.mockResolvedValue({
    user_id: 2,
    role: "viewer",
    home_domain: null,
    allowed_actions: ["read"],
    ui_actions: ["dashboard:view", "catalog:view", "quality:view", "guide:view"],
    granted_domains: [],
    metric_whitelist: [],
    row_level_restricted: false,
    grants: [],
    expiring_soon: [],
  } as never);
  return render(
    <PermissionProvider user={{ ...ADMIN, role: "viewer" } as never}>
      <UserManagement />
    </PermissionProvider>,
  );
}

const USERS = {
  total: 2,
  page: 1,
  page_size: 20,
  items: [
    { id: 1, username: "admin", email: "admin@example.com", display_name: "平台管理员", role: "platform_admin", roles: ["platform_admin"], domain: null, org_id: 1, org_name: "默认团队", status: "active", last_login_at: "2026-08-14T10:00:00", created_at: "2026-08-01T10:00:00" },
    { id: 2, username: "alice", email: "alice@example.com", display_name: "爱丽丝", role: "viewer", roles: ["viewer"], domain: "finance", org_id: 1, org_name: "默认团队", status: "disabled", last_login_at: null, created_at: "2026-08-02T10:00:00" },
  ],
};

describe("UserManagement 用户管理", () => {
  beforeEach(() => {
    mockMe.mockReset();
    mockList.mockReset();
    mockDomains.mockReset();
    mockOrgs.mockReset();
    mockRoles.mockReset();
    mockCreate.mockReset();
    mockUpdate.mockReset();
    mockStatus.mockReset();
    mockBatchStatus.mockReset();
    mockReset.mockReset();
    mockGetUserPerm.mockReset();
    mockSetUserPerm.mockReset();
    mockActionRegistry.mockReset();
    mockDomains.mockResolvedValue([]);
    mockOrgs.mockResolvedValue({ total: 0, page: 1, page_size: 200, items: [] });
    mockRoles.mockResolvedValue([]);
    mockGetUserPerm.mockResolvedValue({
      user_id: 1,
      role: "platform_admin",
      role_actions: ["*"],
      direct_actions: [],
      deny_actions: [],
      effective_actions: ["*"],
    });
    mockSetUserPerm.mockResolvedValue({
      user_id: 1,
      role: "platform_admin",
      role_actions: ["*"],
      direct_actions: [],
      deny_actions: [],
      effective_actions: ["*"],
    });
    mockActionRegistry.mockResolvedValue([
      { action: "metric:create", module: "指标", label: "创建指标", description: "新增指标" },
    ]);
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
    // 管理操作按钮（编辑为主操作；重置密码/启用/禁用收进「更多」下拉）
    expect(screen.getAllByRole("button", { name: /编\s*辑/ }).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/更\s*多/).length).toBeGreaterThan(0);
  });

  it("viewer：只读视图，无管理操作与创建按钮", async () => {
    mockMe.mockResolvedValue({ ...ADMIN, role: "viewer" });
    mockList.mockResolvedValue(USERS);
    renderViewer();

    expect(await screen.findByText("alice")).toBeTruthy();
    expect(screen.queryByText("创建用户")).toBeNull();
    expect(screen.queryByText("重置密码")).toBeNull();
    expect(screen.getByText(/当前账号为只读视图/)).toBeTruthy();
  });

  it("创建用户：选择所属团队（域自动继承），创建成功后一次性展示明文", async () => {
    mockMe.mockResolvedValue(ADMIN);
    mockList.mockResolvedValue(USERS);
    mockOrgs.mockResolvedValue({
      total: 1,
      page: 1,
      page_size: 200,
      items: [
        { id: 1, name: "默认团队", code: "default", status: "active", domain: "finance", user_count: 2, created_at: "2026-08-01T10:00:00" },
      ],
    });
    mockCreate.mockResolvedValue(USERS.items[1]);
    render(<UserManagement />);
    await screen.findByText("alice");

    fireEvent.click(screen.getByText("创建用户"));
    fireEvent.change(screen.getByPlaceholderText("如 zhangsan"), { target: { value: "bob" } });
    fireEvent.change(screen.getByPlaceholderText("name@example.com"), { target: { value: "bob@example.com" } });
    fireEvent.change(screen.getByPlaceholderText("如 张三"), { target: { value: "鲍勃" } });
    fireEvent.change(screen.getByPlaceholderText("至少 8 位"), { target: { value: "secret123" } });

    // 所属团队下拉：选择团队（业务域自动继承，不再单独选择域）
    fireEvent.mouseDown(screen.getByText("选择所属团队（业务域自动继承）"));
    await clickSelectOption("默认团队（default） · 域：finance");
    // 选项已选中（下拉项 + 回显可能同时存在）+ 继承域提示出现（Form.useWatch 异步重渲染）
    expect((await screen.findAllByText("默认团队（default） · 域：finance")).length).toBeGreaterThan(0);
    expect(await screen.findByText(/绑定业务域/)).toBeTruthy();

    fireEvent.click(screen.getByText("创 建"));
    await waitFor(() => expect(mockCreate).toHaveBeenCalled());
    expect(mockCreate.mock.calls[0][0]).toMatchObject({
      username: "bob",
      email: "bob@example.com",
      display_name: "鲍勃",
      role: "viewer",
      org_id: 1,
      password: "secret123",
    });

    // 创建成功后一次性展示明文密码（可复制交付）
    expect(await screen.findByText("用户创建成功")).toBeTruthy();
    expect(screen.getByText("secret123")).toBeTruthy();
    expect(screen.getByText(/仅在此展示一次/)).toBeTruthy();
  });

  it("创建用户：打开弹窗自动预填强随机密码，可直接提交", async () => {
    mockMe.mockResolvedValue(ADMIN);
    mockList.mockResolvedValue(USERS);    mockCreate.mockResolvedValue(USERS.items[1]);
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

    // 禁用收进「更多」下拉：展开 admin 行（active）→ 点「禁用」菜单项 → Modal.confirm 确认
    fireEvent.click(screen.getAllByText(/更\s*多/)[0]);
    const toggleItem = (await screen.findAllByRole("menuitem")).find((el) => el.textContent?.trim() === "禁用");
    expect(toggleItem).toBeTruthy();
    fireEvent.click(toggleItem as HTMLElement);
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

    // 重置密码收进「更多」下拉：展开 alice 行（第二行）→ 点「重置密码」菜单项
    fireEvent.click(screen.getAllByText(/更\s*多/)[1]);
    const resetItem = (await screen.findAllByRole("menuitem")).find((el) => el.textContent?.trim() === "重置密码");
    expect(resetItem).toBeTruthy();
    fireEvent.click(resetItem as HTMLElement);
    const input = await screen.findByPlaceholderText("至少 8 位");
    fireEvent.change(input, { target: { value: "newsecret123" } });
    fireEvent.click(screen.getByText("重 置"));
    await waitFor(() => expect(mockReset).toHaveBeenCalledWith(2, "newsecret123"));
  });

  it("重置密码：可自动生成随机强密码，重置成功后一次性展示明文", async () => {
    mockMe.mockResolvedValue(ADMIN);
    mockList.mockResolvedValue(USERS);
    mockReset.mockResolvedValue({ user_id: 2, ok: true });
    render(<UserManagement />);
    await screen.findByText("alice");

    // 重置密码收进「更多」下拉：展开 alice 行（第二行）→ 点「重置密码」菜单项
    fireEvent.click(screen.getAllByText(/更\s*多/)[1]);
    const resetItem = (await screen.findAllByRole("menuitem")).find((el) => el.textContent?.trim() === "重置密码");
    expect(resetItem).toBeTruthy();
    fireEvent.click(resetItem as HTMLElement);
    fireEvent.click(await screen.findByText("生成随机密码"));

    const input = await screen.findByPlaceholderText("至少 8 位");
    const val = (input as HTMLInputElement).value;
    // 已自动预填强密码（含大小写/数字/符号）
    expect(val).toMatch(/^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z0-9]).{8,}$/);

    fireEvent.click(screen.getByText("重 置"));
    await waitFor(() => expect(mockReset).toHaveBeenCalledWith(2, val));

    // 重置成功后一次性展示明文密码（可复制交付，不落日志）
    expect(await screen.findByText("密码重置成功")).toBeTruthy();
    expect(screen.getByText(val)).toBeTruthy();
    expect(screen.getByText(/仅在此展示一次/)).toBeTruthy();
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

  it("批量停用：勾选多行后确认，调用 batchSetUserStatus(disabled) 并提示成功", async () => {
    const user = userEvent.setup();
    mockMe.mockResolvedValue(ADMIN);
    mockList.mockResolvedValue(USERS);
    mockBatchStatus.mockResolvedValue({
      succeeded: [
        { user_id: 1, username: "admin", ok: true, error_code: null, message: "已停用" },
        { user_id: 2, username: "alice", ok: true, error_code: null, message: "已停用" },
      ],
      failed: [],
    });
    render(<UserManagement />);
    await screen.findByText("alice");

    // 表头全选（rowSelection 选择所有行）
    const checkboxes = screen.getAllByRole("checkbox");
    await user.click(checkboxes[0]);

    expect(screen.getByText("已选 2 个用户")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: /批量停用/ }));
    await user.click(await screen.findByText("确认停用"));

    await waitFor(() => {
      expect(mockBatchStatus).toHaveBeenCalledWith([1, 2], "disabled");
    });
    expect(await screen.findByText("停用成功 2 个用户")).toBeTruthy();
  });

  it("批量启用：勾选多行后调用 batchSetUserStatus(active)，部分失败逐项提示", async () => {
    const user = userEvent.setup();
    mockMe.mockResolvedValue(ADMIN);
    mockList.mockResolvedValue(USERS);
    mockBatchStatus.mockResolvedValue({
      succeeded: [
        { user_id: 1, username: "admin", ok: true, error_code: null, message: "已启用" },
      ],
      failed: [
        { user_id: 2, username: "alice", ok: false, error_code: "USER_NOT_FOUND", message: "用户不存在" },
      ],
    });
    render(<UserManagement />);
    await screen.findByText("alice");

    const checkboxes = screen.getAllByRole("checkbox");
    await user.click(checkboxes[0]);
    await user.click(screen.getByRole("button", { name: /批量启用/ }));

    await waitFor(() => {
      expect(mockBatchStatus).toHaveBeenCalledWith([1, 2], "active");
    });
    expect(await screen.findByText(/启用完成 1 个，失败 1 个/)).toBeTruthy();
  });

  it("viewer：只读视图无批量操作按钮与复选框", async () => {
    mockMe.mockResolvedValue({ ...ADMIN, role: "viewer" });
    mockList.mockResolvedValue(USERS);
    renderViewer();
    await screen.findByText("alice");

    expect(screen.queryByText("批量停用")).toBeNull();
    expect(screen.queryByText("批量启用")).toBeNull();
    // 无 rowSelection 复选框列
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("创建用户：角色下拉含自定义角色（自定义后缀）", async () => {
    mockMe.mockResolvedValue(ADMIN);
    mockList.mockResolvedValue(USERS);
    mockRoles.mockResolvedValue([
      {
        role: "data_analyst",
        default_actions: [],
        custom_actions: null,
        effective_actions: [],
        ui_default_actions: [],
        ui_custom_actions: null,
        ui_effective_actions: [],
        protected: false,
        is_custom: true,
      },
    ]);
    render(<UserManagement />);
    await screen.findByText("alice");

    fireEvent.click(screen.getByText("创建用户"));
    // 打开角色下拉（多选）：内置角色 + 自定义角色（带「自定义」后缀）
    fireEvent.mouseDown(screen.getByLabelText("角色（可多选）"));
    await clickSelectOption("data_analyst（自定义）");
    // 选中后表单角色值为自定义角色（下拉项与选中项均含该文本，用 getAllByText 判定）
    await waitFor(() =>
      expect(screen.getAllByText("data_analyst（自定义）").length).toBeGreaterThan(0),
    );
  });

  it("操作列「授权」：直达该用户的按钮权限矩阵（角色已含只读 + 直挂可勾选）", async () => {
    mockMe.mockResolvedValue(ADMIN);
    mockList.mockResolvedValue(USERS);
    render(<UserManagement />);
    await screen.findByText("alice");

    // 点第一行（admin）的「授权」按钮 → 打开按用户授权矩阵
    fireEvent.click(screen.getAllByText(/授\s*权/)[0]);
    await screen.findByText(/按用户授权：admin/);
    // 拉取该用户权限 + 动作点注册表
    expect(mockGetUserPerm).toHaveBeenCalledWith(1);
    expect(mockActionRegistry).toHaveBeenCalled();

    // 关闭矩阵（antd 默认 locale 下取消按钮为 Cancel，用右上角关闭图标）
    const closeIcon = document.querySelector(".ant-modal-close") as HTMLElement | null;
    expect(closeIcon).toBeTruthy();
    if (closeIcon) fireEvent.click(closeIcon);
    await waitFor(() => expect(screen.queryByText(/按用户授权：admin/)).toBeNull());
  });
});
