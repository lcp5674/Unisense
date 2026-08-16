import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { Governance } from "../pages/Governance";
import { PermissionProvider } from "../hooks/usePermission";

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
    fetchMyPermissions: vi.fn(),
    listGrants: vi.fn(),
    createGrant: vi.fn(),
    revokeGrant: vi.fn(),
    batchGrant: vi.fn(),
    listRolePermissions: vi.fn(),
    setRolePermissions: vi.fn(),
    resetRolePermissions: vi.fn(),
    deleteRole: vi.fn(),
    createRole: vi.fn(),
    listActionRegistry: vi.fn(),
    checkPermission: vi.fn(),
    piiReviewAction: vi.fn(),
    classificationRescan: vi.fn(),
    requestErasure: vi.fn(),
    listUsers: vi.fn(),
    listDomainTree: vi.fn(),
    listMetrics: vi.fn(),
    UnisenseApiError,
  };
});

import {
  fetchMyPermissions,
  listGrants,
  listRolePermissions,
  setRolePermissions,
  resetRolePermissions,
  listActionRegistry,
  deleteRole,
  createRole,
  batchGrant,
  listUsers,
  listDomainTree,
  listMetrics,
} from "../api";

const mockPerms = vi.mocked(fetchMyPermissions);
const mockGrants = vi.mocked(listGrants);
const mockRoles = vi.mocked(listRolePermissions);
const mockSetRoles = vi.mocked(setRolePermissions);
const mockResetRoles = vi.mocked(resetRolePermissions);
const mockDeleteRole = vi.mocked(deleteRole);
const mockCreateRole = vi.mocked(createRole);
const mockActionRegistry = vi.mocked(listActionRegistry);
const mockBatch = vi.mocked(batchGrant);
const mockUsers = vi.mocked(listUsers);
const mockDomains = vi.mocked(listDomainTree);
const mockMetrics = vi.mocked(listMetrics);

const ACTION_REGISTRY = [
  { action: "catalog:view", module: "指标", label: "查看指标目录", description: "访问指标目录" },
  { action: "metric:create", module: "指标", label: "创建指标", description: "新增指标" },
  { action: "user:disable", module: "账号", label: "启停用户", description: "启用/禁用用户" },
];

const ROLE_PERMISSIONS = [
  {
    role: "viewer",
    default_actions: ["read"],
    custom_actions: ["read"],
    effective_actions: ["read"],
    ui_default_actions: ["catalog:view", "dashboard:view"],
    ui_custom_actions: null,
    ui_effective_actions: ["catalog:view", "dashboard:view"],
    protected: false,
    is_custom: false,
  },
  {
    role: "platform_admin",
    default_actions: ["read", "write", "approve", "export", "review"],
    custom_actions: null,
    effective_actions: ["read", "write", "approve", "export", "review"],
    ui_default_actions: ["*"],
    ui_custom_actions: null,
    ui_effective_actions: ["*"],
    protected: true,
    is_custom: false,
  },
];

const USERS = [
  { id: 1, username: "alice", display_name: "爱丽丝", role: "viewer", domain: "finance", status: "active" },
  { id: 2, username: "bob", display_name: "鲍勃", role: "analyst", domain: null, status: "active" },
];

async function clickTab(name: string) {
  const tab = await screen.findByRole("tab", { name });
  await userEvent.click(tab);
}

/** 渲染 Governance（GrantsTab 用 useNavigate，须包 Router）。 */
function renderGov() {
  return render(
    <MemoryRouter>
      <Governance />
    </MemoryRouter>,
  );
}

describe("Governance 权限治理", () => {
  beforeEach(() => {
    mockPerms.mockReset();
    mockGrants.mockReset();
    mockRoles.mockReset();
    mockSetRoles.mockReset();
    mockResetRoles.mockReset();
    mockDeleteRole.mockReset();
    mockCreateRole.mockReset();
    mockActionRegistry.mockReset();
    mockBatch.mockReset();
    mockUsers.mockReset();
    mockDomains.mockReset();
    mockMetrics.mockReset();
    mockPerms.mockResolvedValue({
      user_id: 1,
      role: "platform_admin",
      home_domain: null,
      allowed_actions: ["read", "write", "approve", "export", "review"],
      ui_actions: ["*"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    mockGrants.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 });
    mockRoles.mockResolvedValue(ROLE_PERMISSIONS);
    mockActionRegistry.mockResolvedValue(ACTION_REGISTRY);
    mockDeleteRole.mockResolvedValue({ role: "x", deleted: true });
    mockCreateRole.mockResolvedValue({ id: 99, name: "x", description: null });
    mockUsers.mockResolvedValue(USERS);
    mockDomains.mockResolvedValue([]);
    mockMetrics.mockResolvedValue({ total: 0, page: 1, page_size: 1000, items: [] });
  });

  it("角色管理：渲染权限点矩阵，platform_admin 受保护不可编辑", async () => {
    renderGov();
    await clickTab("角色管理");

    // viewer 行：默认仅 read
    expect(await screen.findByText("只读用户")).toBeTruthy();
    // platform_admin 受保护提示（Alert 文案 + 行内 muted 均有）
    expect(screen.getAllByText(/受保护角色/).length).toBeGreaterThan(0);

    // viewer 的 read 勾选状态（Checkbox 由 antd 渲染，存在即可）
    const checkboxes = document.querySelectorAll(".ant-checkbox-input");
    expect(checkboxes.length).toBeGreaterThan(0);

    // platform_admin 行复选框应 disabled
    const disabled = document.querySelectorAll(".ant-checkbox-input:disabled");
    expect(disabled.length).toBeGreaterThan(0);
  });

  it("角色管理：勾选 viewer.write 后保存，调用 setRolePermissions", async () => {
    mockSetRoles.mockResolvedValue({
      role: "viewer",
      default_actions: ["read"],
      custom_actions: ["read", "write"],
      effective_actions: ["read", "write"],
      ui_default_actions: ["catalog:view"],
      ui_custom_actions: null,
      ui_effective_actions: ["catalog:view"],
      protected: false,
      is_custom: false,
    });
    renderGov();
    await clickTab("角色管理");

    await screen.findByText("只读用户");
    // 勾选 viewer 行的 write 复选框：选择所有未禁用复选框中的第二个（write）
    const enabledBoxes = Array.from(
      document.querySelectorAll<HTMLInputElement>(".ant-checkbox-input:not(:disabled)"),
    );
    // viewer 行有 5 个复选框（read/write/approve/export/review），第 2 个即 write
    const writeBox = enabledBoxes.find((_box, idx) => idx === 1);
    fireEvent.click(writeBox!);

    // 保存按钮启用并点击（platform_admin 行的保存禁用，选启用的 viewer 行）
    const saveBtn = screen
      .getAllByRole("button", { name: /保\s*存/ })
      .find((b) => !(b as HTMLButtonElement).disabled);
    await userEvent.click(saveBtn!);

    await waitFor(() => {
      expect(mockSetRoles).toHaveBeenCalledWith("viewer", expect.arrayContaining(["write", "read"]));
    });
  });

  it("角色管理：恢复默认调用 resetRolePermissions", async () => {
    mockResetRoles.mockResolvedValue({
      role: "viewer",
      default_actions: ["read"],
      custom_actions: null,
      effective_actions: ["read"],
      ui_default_actions: ["catalog:view"],
      ui_custom_actions: null,
      ui_effective_actions: ["catalog:view"],
      protected: false,
      is_custom: false,
    });
    renderGov();
    await clickTab("角色管理");

    await screen.findByText("只读用户");
    const resetBtn = screen
      .getAllByRole("button", { name: /恢复默认/ })
      .find((b) => !(b as HTMLButtonElement).disabled);
    await userEvent.click(resetBtn!);

    await waitFor(() => {
      expect(mockResetRoles).toHaveBeenCalledWith("viewer");
    });
  });

  it("批量授权：选用户预览后确认，先 dry-run 再正式执行", async () => {
    mockBatch.mockResolvedValue({
      dry_run: true,
      operation: "grant",
      affected_users: 2,
      affected_metrics: 0,
      succeeded: 2,
      failed: 0,
      items: [
        { user_id: 1, domain: "finance", action: "grant", ok: true, detail: "将新建授权" },
        { user_id: 2, domain: "finance", action: "grant", ok: true, detail: "将新建授权" },
      ],
    });
    renderGov();
    await clickTab("授权管理");

    await screen.findByText("新建授权");
    await userEvent.click(screen.getByRole("button", { name: /批量授权/ }));

    // 打开多选用户下拉并选择 alice、bob（antd multiple 点击后保持打开）
    // 精确选择 Modal 内的多选选择器（避免命中工具栏状态筛选 Select）
    await waitFor(() => {
      expect(document.querySelector(".ant-select-multiple .ant-select-selector")).toBeTruthy();
    });
    const userSelect = document.querySelector(".ant-select-multiple .ant-select-selector");
    fireEvent.mouseDown(userSelect!);
    await waitFor(() => {
      const dropdown = document.querySelector(
        ".ant-select-dropdown:not(.ant-select-dropdown-hidden)",
      );
      expect(dropdown?.querySelector('.ant-select-item-option[title*="alice"]')).toBeTruthy();
    });
    for (const name of ["alice", "bob"]) {
      const dropdown = document.querySelector(
        ".ant-select-dropdown:not(.ant-select-dropdown-hidden)",
      );
      const opt = dropdown?.querySelector<HTMLElement>(
        `.ant-select-item-option[title*="${name}"]`,
      );
      fireEvent.click(opt!);
    }

    // 点击预览影响 → dry-run
    await userEvent.click(screen.getByRole("button", { name: /预览影响/ }));
    await waitFor(() => {
      expect(mockBatch).toHaveBeenCalledWith(
        expect.any(Array),
        "grant",
        true,
      );
    });
    // 预览结果出现，确认按钮启用
    expect(await screen.findByText(/预览：影响 2 个用户/)).toBeTruthy();

    // 确认授权 → 正式执行（dryRun=false）
    mockBatch.mockResolvedValue({
      dry_run: false,
      operation: "grant",
      affected_users: 2,
      affected_metrics: 0,
      succeeded: 2,
      failed: 0,
      items: [
        { user_id: 1, domain: "finance", action: "grant", ok: true, detail: "grant#1" },
        { user_id: 2, domain: "finance", action: "grant", ok: true, detail: "grant#2" },
      ],
    });
    await userEvent.click(screen.getByRole("button", { name: /确认授权/ }));

    await waitFor(() => {
      const calls = mockBatch.mock.calls.filter((c) => c[2] === false);
      expect(calls.length).toBeGreaterThan(0);
    });
  });

  it("PII 复核：敏感度选项不含 UNKNOWN（降级标记不可人工赋值，与其他页 NEEDS_REVIEW 终态对齐）", async () => {
    renderGov();
    await clickTab("PII 复核");

    await userEvent.click(screen.getByRole("button", { name: /PII 人工复核/ }));
    await screen.findByText("敏感度");

    // 打开敏感度下拉：仅真实级别 PUBLIC/INTERNAL/CONFIDENTIAL/PII，无 UNKNOWN
    fireEvent.mouseDown(screen.getByLabelText("敏感度"));
    await waitFor(() => {
      const dropdown = document.querySelector(".ant-select-dropdown:not(.ant-select-dropdown-hidden)");
      expect(dropdown).toBeTruthy();
    });
    const options = Array.from(
      document.querySelectorAll<HTMLElement>(".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option"),
    ).map((o) => o.getAttribute("title"));

    expect(options).toContain("公开");
    expect(options).toContain("内部");
    expect(options).toContain("机密");
    expect(options).toContain("PII 敏感");
    expect(options).not.toContain("未知");
    expect(options).not.toContain("UNKNOWN");
  });

  it("角色管理：新建自定义角色 → 配置按钮级权限点 → 删除", async () => {
    mockCreateRole.mockResolvedValue({ id: 99, name: "data_analyst", description: null });
    const withCustom = [
      ...ROLE_PERMISSIONS,
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
    ];
    // 首次加载返回内置角色；创建后刷新返回含自定义角色（delete 刷新复用 withCustom）
    mockRoles.mockReset();
    mockRoles.mockResolvedValueOnce(ROLE_PERMISSIONS).mockResolvedValue(withCustom);
    mockSetRoles.mockResolvedValue({
      role: "data_analyst",
      default_actions: [],
      custom_actions: null,
      effective_actions: [],
      ui_default_actions: [],
      ui_custom_actions: ["metric:create"],
      ui_effective_actions: ["metric:create"],
      protected: false,
      is_custom: true,
    });

    renderGov();
    await clickTab("角色管理");
    await screen.findByText("只读用户");

    // 新建自定义角色
    await userEvent.click(screen.getByRole("button", { name: /新建自定义角色/ }));
    await userEvent.type(screen.getByLabelText("角色名"), "data_analyst");
    await userEvent.click(screen.getByRole("button", { name: /创\s*建/ }));
    await waitFor(() =>
      expect(mockCreateRole).toHaveBeenCalledWith({
        name: "data_analyst",
        description: null,
        is_custom: true,
      }),
    );
    expect(await screen.findByText("data_analyst")).toBeTruthy();
    expect(screen.getAllByText("自定义").length).toBeGreaterThan(0);

    // 打开 data_analyst 行的「配置」→ 按钮级权限点弹窗 → 勾选「创建指标」→ 保存
    const configBtns = screen.getAllByRole("button", { name: /配\s*置/ });
    await userEvent.click(configBtns[configBtns.length - 1]);
    expect(await screen.findByText("创建指标")).toBeTruthy();
    const wrapper = Array.from(
      document.querySelectorAll<HTMLElement>(".ant-checkbox-wrapper"),
    ).find((el) => el.textContent?.includes("创建指标"));
    const cb = wrapper?.querySelector("input.ant-checkbox-input");
    fireEvent.click(cb!);
    const saveBtn = screen
      .getAllByRole("button", { name: /保\s*存/ })
      .find((b) => !(b as HTMLButtonElement).disabled);
    await userEvent.click(saveBtn!);
    await waitFor(() =>
      expect(mockSetRoles).toHaveBeenCalledWith(
        "data_analyst",
        expect.arrayContaining(["metric:create"]),
      ),
    );

    // 删除自定义角色（Popconfirm 确认：antd 默认 locale 下确定按钮为 primary）
    const delBtn = screen
      .getAllByRole("button", { name: /删\s*除/ })
      .find((b) => !(b as HTMLButtonElement).disabled);
    await userEvent.click(delBtn!);
    await waitFor(() => {
      const okBtn = document.querySelector(
        ".ant-popconfirm-buttons .ant-btn-primary",
      ) as HTMLElement | null;
      expect(okBtn).toBeTruthy();
      if (okBtn) fireEvent.click(okBtn);
    });
    await waitFor(() => expect(mockDeleteRole).toHaveBeenCalledWith("data_analyst"));
  });

  it("授权管理：用户列显示用户名、指标白名单为选项框、可在页内给该用户授权", async () => {
    mockGrants.mockResolvedValue({
      total: 1,
      page: 1,
      page_size: 20,
      items: [
        {
          id: 11,
          user_id: 1,
          role_id: null,
          domain: "finance",
          metric_whitelist: ["sales_gmv"],
          grant_type: "READ",
          row_level: false,
          status: "ACTIVE",
          expires_at: null,
          granted_by: null,
          reason: null,
        },
      ],
    });
    mockMetrics.mockResolvedValue({
      total: 1,
      page: 1,
      page_size: 100,
      items: [
        { metric_code: "sales_gmv", name: "销售 GMV" } as never,
      ],
    });

    renderGov();
    await clickTab("授权管理");
    await screen.findByText(/爱丽丝/); // 用户列显示用户名（user_id → username 映射，display_name 带括号）

    // 操作列有「给该用户授权」按钮（在授权页内直接授权，不再跳转用户管理全局页）
    const grantBtn = screen
      .getAllByRole("button", { name: /给该用户授权/ })
      .find((b) => !(b as HTMLButtonElement).disabled);
    expect(grantBtn).toBeTruthy();
    // 指标白名单列展示
    expect(screen.getByText("sales_gmv")).toBeTruthy();

    // 点击「给该用户授权」→ 打开批量授权弹窗并预填该用户（user_ids=[1]）
    await userEvent.click(grantBtn!);
    expect(await screen.findByText("批量授权（同一参数应用到多个用户）")).toBeInTheDocument();
    expect(screen.getByText("alice（爱丽丝）")).toBeTruthy();
    // 批量弹窗内指标白名单为多选选项框（不再手动逗号输入；此时仅批量弹窗打开，placeholder 唯一）
    expect(await screen.findByText("搜索并选择指标编码")).toBeTruthy();
    expect(screen.queryByPlaceholderText("指标白名单（逗号分隔）")).toBeNull();
  });
});

describe("Governance Tab 级权限过滤", () => {
  function renderWithPerms(ui_actions: string[]) {
    mockPerms.mockResolvedValue({
      user_id: 1,
      role: "custom",
      home_domain: null,
      allowed_actions: ["read"],
      ui_actions,
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    return render(
      <MemoryRouter>
        <PermissionProvider user={{ id: 1, username: "u", display_name: "U", role: "custom", domain: null, org_id: 1 }}>
          <Governance />
        </PermissionProvider>
      </MemoryRouter>,
    );
  }

  it("无管理权限点时只显示我的权限/权限检查", async () => {
    renderWithPerms(["governance:view"]);
    await waitFor(() => expect(screen.getByRole("tab", { name: "我的权限" })).toBeInTheDocument());
    expect(screen.queryByRole("tab", { name: "授权管理" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "角色管理" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "PII 复核" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "数据擦除" })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "权限检查" })).toBeInTheDocument();
  });

  it("有授权权限点时显示授权管理 Tab", async () => {
    renderWithPerms(["governance:view", "grant:create"]);
    await waitFor(() => expect(screen.getByRole("tab", { name: "我的权限" })).toBeInTheDocument());
    expect(screen.getByRole("tab", { name: "授权管理" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "角色管理" })).not.toBeInTheDocument();
  });

  it("合规官角色显示 PII 复核 Tab", async () => {
    renderWithPerms(["governance:view", "pii:review"]);
    await waitFor(() => expect(screen.getByRole("tab", { name: "我的权限" })).toBeInTheDocument());
    expect(screen.getByRole("tab", { name: "PII 复核" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "授权管理" })).not.toBeInTheDocument();
  });

  it("有擦除权限点时显示数据擦除 Tab", async () => {
    renderWithPerms(["governance:view", "erasure:execute"]);
    await waitFor(() => expect(screen.getByRole("tab", { name: "我的权限" })).toBeInTheDocument());
    expect(screen.getByRole("tab", { name: "数据擦除" })).toBeInTheDocument();
  });
});
