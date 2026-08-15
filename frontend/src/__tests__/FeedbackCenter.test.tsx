import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { FeedbackCenter } from "../pages/FeedbackCenter";
import type { Feedback } from "../types";

vi.mock("../api", () => ({
  listFeedback: vi.fn(),
  updateFeedbackStatus: vi.fn(),
  submitFeedback: vi.fn(),
  submitNps: vi.fn(),
  fetchNpsStats: vi.fn(),
  listUsers: vi.fn(),
  getMetric: vi.fn(),
  UnisenseApiError: class extends Error {},
}));

import { listFeedback, updateFeedbackStatus, listUsers, getMetric } from "../api";
const mockedList = vi.mocked(listFeedback);
const mockedUpdate = vi.mocked(updateFeedbackStatus);
const mockedUsers = vi.mocked(listUsers);
const mockedGetMetric = vi.mocked(getMetric);

const feedbacks: Feedback[] = [
  {
    id: 1,
    user_id: 7,
    target_type: "metric",
    target_id: "sales_gmv",
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
  mockedList.mockResolvedValue({
    items: feedbacks,
    total: feedbacks.length,
    page: 1,
    page_size: 20,
  } as never);
  mockedUpdate.mockResolvedValue(feedbacks[0] as never);
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

  it("对象已失效（指标不存在）的反馈展示「已失效」标记而非裸编码", async () => {
    mockedGetMetric.mockRejectedValue(new Error("404"));
    render(<MemoryRouter><FeedbackCenter /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("已失效")).toBeInTheDocument());
    // 仍保留编码，但不再显示为可点击的指标链接
    expect(screen.getByText("sales_gmv")).toBeInTheDocument();
  });
});