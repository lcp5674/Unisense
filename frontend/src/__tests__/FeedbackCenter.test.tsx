import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { FeedbackCenter } from "../pages/FeedbackCenter";
import { PermissionProvider } from "../hooks/usePermission";
import type { Feedback } from "../types";

vi.mock("../api", () => ({
  listFeedback: vi.fn(),
  updateFeedbackStatus: vi.fn(),
  clarifyFeedback: vi.fn(),
  submitFeedback: vi.fn(),
  submitNps: vi.fn(),
  fetchNpsStats: vi.fn(),
  listUsers: vi.fn(),
  getMetric: vi.fn(),
  fetchMyPermissions: vi.fn(),
  fetchCurrentUser: vi.fn(),
  UnisenseApiError: class extends Error {},
}));

import { listFeedback, updateFeedbackStatus, clarifyFeedback, listUsers, getMetric, submitFeedback, fetchMyPermissions, fetchCurrentUser } from "../api";
const mockedList = vi.mocked(listFeedback);
const mockedUpdate = vi.mocked(updateFeedbackStatus);
const mockedUsers = vi.mocked(listUsers);
const mockedGetMetric = vi.mocked(getMetric);
const mockedSubmit = vi.mocked(submitFeedback);
const mockedPerms = vi.mocked(fetchMyPermissions);

const feedbacks: Feedback[] = [
  {
    id: 1,
    user_id: 7,
    target_type: "metric",
    target_id: "sales_gmv",
    target_name: "销售GMV",
    rating: 4,
    comment: "口径很清楚",
    category: "praise",
    priority: "low",
    source_url: "/catalog",
    nps_score: null,
    status: "pending",
    resolution_note: null,
    resolver_id: null,
    resolved_at: null,
    created_at: "2026-08-10T10:00:00",
  },
  {
    id: 2,
    user_id: 9,
    target_type: "dashboard",
    target_id: null,
    rating: null,
    comment: "希望增加导出",
    category: "feature",
    priority: "high",
    source_url: null,
    nps_score: null,
    status: "adopted",
    resolution_note: "已排期下版本",
    resolver_id: 4,
    resolved_at: "2026-08-11T02:00:00",
    created_at: "2026-08-11T01:00:00",
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchCurrentUser).mockResolvedValue({
    id: 7,
    username: "alice",
    display_name: "Alice",
    role: "platform_admin",
    domain: null,
    org_id: 1,
  } as never);
  mockedList.mockResolvedValue({
    items: feedbacks,
    total: feedbacks.length,
    page: 1,
    page_size: 20,
  } as never);
  mockedUpdate.mockResolvedValue(feedbacks[0] as never);
  mockedSubmit.mockResolvedValue({
    id: 99,
    user_id: 3,
    target_type: "metric",
    target_id: null,
    rating: null,
    comment: "新反馈",
    nps_score: null,
    status: "pending",
    resolution_note: null,
    resolver_id: null,
    resolved_at: null,
    created_at: "2026-08-16T00:00:00",
  } as never);
  // 用户名单：id=7→爱丽丝、id=4→审核员；id=9 无 display_name 回落 username
  mockedUsers.mockResolvedValue([
    { id: 7, username: "alice", display_name: "爱丽丝", role: "analyst", domain: null, status: "active" },
    { id: 9, username: "bob", display_name: "", role: "viewer", domain: null, status: "active" },
    { id: 4, username: "reviewer1", display_name: "审核员", role: "reviewer", domain: null, status: "active" },
  ] as never);
  // 指标对象解析：sales_gmv → 销售GMV
  mockedGetMetric.mockResolvedValue({
    metric_code: "sales_gmv",
    name: "销售GMV",
  } as never);
  // 权限快照默认全放行（fail-open 语义）；gate 测试单独覆盖为受限集合
  mockedPerms.mockResolvedValue({
    user_id: 1, role: "platform_admin", home_domain: null,
    allowed_actions: ["read", "write", "approve", "export", "review"],
    ui_actions: ["*"], granted_domains: [], metric_whitelist: [],
    row_level_restricted: false, grants: [], expiring_soon: [],
  } as never);
});

describe("FeedbackCenter 用户反馈", () => {
  it("加载并渲染反馈列表，状态/处理人/处理时间中文展示", async () => {
    render(<MemoryRouter><FeedbackCenter /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/销售GMV/)).toBeInTheDocument());
    expect(screen.getByText("口径很清楚")).toBeInTheDocument();
    expect(screen.getByText("希望增加导出")).toBeInTheDocument();
    // 用户列：ID → 用户名（爱丽丝 / bob 回落 username）
    expect(screen.getByText("爱丽丝")).toBeInTheDocument();
    expect(screen.getByText("bob")).toBeInTheDocument();
    // 状态列：待处理 + 已采纳
    expect(screen.getByText("待处理")).toBeInTheDocument();
    expect(screen.getByText("已采纳")).toBeInTheDocument();
    // 处理人列：数字 ID → 用户名
    expect(screen.getByText("审核员")).toBeInTheDocument();
    expect(screen.queryByText("4")).not.toBeInTheDocument();
    // 分类/优先级列：业务术语展示（表扬/功能需求、低/高）
    expect(screen.getByText("表扬")).toBeInTheDocument();
    expect(screen.getByText("功能需求")).toBeInTheDocument();
    expect(screen.getByText("低")).toBeInTheDocument();
    expect(screen.getByText("高")).toBeInTheDocument();
    // 处理时效列：feedback 2（01:00→02:00）显示「1 小时」
    expect(screen.getByText("1 小时")).toBeInTheDocument();
    // 原始 ISO 串不应直出
    expect(screen.queryByText("2026-08-10T10:00:00")).not.toBeInTheDocument();
    expect(screen.getAllByText(/前|昨天|月\d+日/).length).toBeGreaterThan(0);
    // 对象名称由服务端 target_name 提供，前端不再逐条 getMetric 探测（消除 404 噪音）
    expect(mockedGetMetric).not.toHaveBeenCalled();
  });

  it("反馈行提供跟进/采纳/驳回处理按钮", async () => {
    render(<MemoryRouter><FeedbackCenter /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/销售GMV/)).toBeInTheDocument());
    const row = screen.getByText(/销售GMV/).closest("tr") as HTMLElement;
    expect(within(row).getByText(/跟\s*进/)).toBeInTheDocument();
    expect(within(row).getByText(/采\s*纳/)).toBeInTheDocument();
    expect(within(row).getByText(/驳\s*回/)).toBeInTheDocument();
  });

  it("点击采纳打开处理弹窗，输入处理说明后调用 updateFeedbackStatus(id, status, note)", async () => {
    render(<MemoryRouter><FeedbackCenter /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/销售GMV/)).toBeInTheDocument());
    const row = screen.getByText(/销售GMV/).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByText(/采\s*纳/));

    // 弹窗出现，含反馈内容与处理说明输入框
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/口径很清楚/)).toBeInTheDocument();
    const noteArea = within(dialog).getByPlaceholderText(/处理说明/) as HTMLTextAreaElement;
    fireEvent.change(noteArea, { target: { value: "已转产品跟进" } });

    fireEvent.click(within(dialog).getByText(/确\s*认\s*处\s*理/));
    await waitFor(() => expect(mockedUpdate).toHaveBeenCalledWith(1, "adopted", "已转产品跟进"));
    // 更新后刷新列表
    expect(mockedList).toHaveBeenCalledTimes(2);
  });

  it("驳回时不传说明则调用 updateFeedbackStatus(id, rejected, null)", async () => {
    render(<MemoryRouter><FeedbackCenter /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/销售GMV/)).toBeInTheDocument());
    const row = screen.getByText(/销售GMV/).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByText(/驳\s*回/));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByText(/确\s*认\s*处\s*理/));
    await waitFor(() => expect(mockedUpdate).toHaveBeenCalledWith(1, "rejected", null));
  });

  it("按类型筛选：切换下拉后按 target_type 调用 listFeedback", async () => {
    render(<MemoryRouter><FeedbackCenter /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/销售GMV/)).toBeInTheDocument());
    fireEvent.mouseDown(screen.getByText("全部类型"));
    const opt = await screen.findByTitle("指标");
    fireEvent.click(opt);

    await waitFor(() =>
      expect(mockedList).toHaveBeenLastCalledWith({
        target_type: "metric",
        status: undefined,
        page: 1,
        page_size: 20,
      }),
    );
  });

  it("对象已失效（指标不存在）的反馈展示「已失效」标记，且不再触发指标探测请求", async () => {
    // 服务端 target_name=null 表示对象失效；前端应直显「已失效」，而非逐条探测详情接口
    const withDeadTarget = feedbacks.map((f) =>
      f.id === 1 ? { ...f, target_name: null } : f,
    );
    mockedList.mockResolvedValue({ items: withDeadTarget, total: 2, page: 1, page_size: 20 } as never);
    render(<MemoryRouter><FeedbackCenter /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("已失效")).toBeInTheDocument());
    // 仍保留编码，但不再显示为可点击的指标链接
    expect(screen.getByText("sales_gmv")).toBeInTheDocument();
    // 关键：不再逐条 getMetric 探测（此前每次加载列表都产生 404 噪音）
    expect(mockedGetMetric).not.toHaveBeenCalled();
  });

  it("处理按钮按反馈状态差异化：跟进中反馈跟进禁用，已采纳反馈不再提供处理按钮", async () => {
    const withInProgress = [
      ...feedbacks,
      { ...feedbacks[0], id: 5, status: "in_progress" },
    ];
    mockedList.mockResolvedValue({ items: withInProgress, total: 3, page: 1, page_size: 20 } as never);
    render(<MemoryRouter><FeedbackCenter /></MemoryRouter>);
    await waitFor(() => expect(screen.getAllByText(/销售GMV/).length).toBeGreaterThan(0));

    // in_progress 行（id=5）：跟进按钮禁用，采纳/驳回仍可用
    const ipRow = screen.getByText("5").closest("tr") as HTMLElement;
    expect(within(ipRow).getByRole("button", { name: /跟\s*进/ })).toBeDisabled();
    expect(within(ipRow).getByRole("button", { name: /采\s*纳/ })).not.toBeDisabled();

    // adopted 行（id=2，dashboard）：不再提供处理按钮（操作列留空）
    const adoptedRow = screen.getByText("2").closest("tr") as HTMLElement;
    expect(within(adoptedRow).queryByRole("button", { name: /采\s*纳/ })).not.toBeInTheDocument();
    expect(within(adoptedRow).queryByRole("button", { name: /驳\s*回/ })).not.toBeInTheDocument();
  });

  it("提交反馈后自动切回列表并刷新（无需手动刷新即可看到新反馈）", async () => {
    render(<MemoryRouter><FeedbackCenter /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/销售GMV/)).toBeInTheDocument());
    const submitCountBefore = mockedList.mock.calls.length;

    // 切到「提交反馈」Tab（表单 textarea 出现即切换成功）
    fireEvent.click(screen.getByRole("tab", { name: /提交反馈/ }));
    const textarea = await screen.findByPlaceholderText(/描述你的问题/) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "新增导出功能" } });
    fireEvent.click(screen.getByRole("button", { name: /提交反馈/ }));

    // 提交成功 → 切回「用户反馈」Tab 并刷新列表
    await waitFor(() => expect(mockedSubmit).toHaveBeenCalled());
    await waitFor(() => expect(mockedList.mock.calls.length).toBeGreaterThan(submitCountBefore));
  });
});

describe("FeedbackCenter 处置权限 gate", () => {
  function renderWithPerms(ui_actions: string[]) {
    mockedPerms.mockResolvedValue({
      user_id: 1, role: "custom", home_domain: null,
      allowed_actions: ["read"],
      ui_actions, granted_domains: [], metric_whitelist: [],
      row_level_restricted: false, grants: [], expiring_soon: [],
    } as never);
    return render(
      <MemoryRouter>
        <PermissionProvider user={{ id: 1, username: "u", display_name: "U", role: "custom", domain: null, org_id: 1 }}>
          <FeedbackCenter />
        </PermissionProvider>
      </MemoryRouter>,
    );
  }

  it("无 feedback:manage 时，反馈行不提供跟进/采纳/驳回按钮", async () => {
    renderWithPerms(["feedback:view"]);
    await waitFor(() => expect(screen.getByText(/销售GMV/)).toBeInTheDocument());
    const row = screen.getByText(/销售GMV/).closest("tr") as HTMLElement;
    expect(within(row).queryByText(/跟\s*进/)).not.toBeInTheDocument();
    expect(within(row).queryByText(/采\s*纳/)).not.toBeInTheDocument();
    expect(within(row).queryByText(/驳\s*回/)).not.toBeInTheDocument();
    expect(within(row).getByText("无处置权限")).toBeInTheDocument();
  });

  it("有 feedback:manage 时，反馈行提供跟进/采纳/驳回按钮", async () => {
    renderWithPerms(["feedback:view", "feedback:manage"]);
    await waitFor(() => expect(screen.getByText(/销售GMV/)).toBeInTheDocument());
    const row = screen.getByText(/销售GMV/).closest("tr") as HTMLElement;
    expect(within(row).getByText(/跟\s*进/)).toBeInTheDocument();
    expect(within(row).getByText(/采\s*纳/)).toBeInTheDocument();
    expect(within(row).getByText(/驳\s*回/)).toBeInTheDocument();
  });
});
describe("FeedbackCenter 质疑闭环", () => {
  it("clarifying 反馈由提交人本人可见「提交澄清」入口，提交后调用 clarifyFeedback", async () => {
    const clarifying: Feedback = {
      id: 5,
      user_id: 7,
      target_type: "metric",
      target_id: "sales_gmv",
      target_name: "销售GMV",
      rating: null,
      comment: "该指标口径与我的理解不一致",
      category: "question",
      priority: "medium",
      source_url: null,
      nps_score: null,
      status: "clarifying",
      resolution_note: null,
      resolver_id: 4,
      resolved_at: null,
      created_at: "2026-08-12T09:00:00",
    };
    mockedList.mockResolvedValue({
      items: [clarifying],
      total: 1,
      page: 1,
      page_size: 20,
    } as never);
    const mockedClarify = vi.mocked(clarifyFeedback);
    mockedClarify.mockResolvedValue({ ...clarifying, status: "in_progress" } as never);
    render(
      <MemoryRouter>
        <PermissionProvider user={{ id: 1, username: "u", display_name: "U", role: "custom", domain: null, org_id: 1 }}>
          <FeedbackCenter />
        </PermissionProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText(/口径与我的理解不一致/)).toBeInTheDocument());
    // 提交人本人可见「提交澄清」（行内按钮）
    const clarifyBtns = screen.getAllByRole("button", { name: "提交澄清" });
    fireEvent.click(clarifyBtns[0]);
    // 弹窗中输入澄清并提交（Modal ok 按钮与行内按钮同名，取最后一个）
    const textarea = await screen.findByPlaceholderText(/按门诊人次口径统计/);
    fireEvent.change(textarea, { target: { value: "按门诊人次口径统计（含退号）" } });
    const submitBtns = screen.getAllByRole("button", { name: "提交澄清" });
    fireEvent.click(submitBtns[submitBtns.length - 1]);
    await waitFor(() =>
      expect(mockedClarify).toHaveBeenCalledWith(5, "按门诊人次口径统计（含退号）"),
    );
  });

  it("非提交人本人不显示「提交澄清」入口（他人不可代答）", async () => {
    const clarifying: Feedback = {
      id: 6,
      user_id: 99,
      target_type: "metric",
      target_id: "sales_gmv",
      rating: null,
      comment: "口径疑问",
      category: "question",
      priority: "medium",
      source_url: null,
      nps_score: null,
      status: "clarifying",
      resolution_note: null,
      resolver_id: 4,
      resolved_at: null,
      created_at: "2026-08-12T09:00:00",
    };
    mockedList.mockResolvedValue({
      items: [clarifying],
      total: 1,
      page: 1,
      page_size: 20,
    } as never);
    render(
      <MemoryRouter>
        <PermissionProvider user={{ id: 1, username: "u", display_name: "U", role: "custom", domain: null, org_id: 1 }}>
          <FeedbackCenter />
        </PermissionProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText(/口径疑问/)).toBeInTheDocument());
    // 当前用户 id=7 ≠ 提交人 99：不显示提交澄清；管理按钮需 feedback:manage
    expect(screen.queryByRole("button", { name: /提交澄清/ })).not.toBeInTheDocument();
  });
});
