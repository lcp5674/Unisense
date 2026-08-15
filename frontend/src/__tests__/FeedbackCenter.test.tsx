import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { FeedbackCenter } from "../pages/FeedbackCenter";
import type { Feedback } from "../types";

vi.mock("../api", () => ({
  listFeedback: vi.fn(),
  updateFeedbackStatus: vi.fn(),
  submitFeedback: vi.fn(),
  submitNps: vi.fn(),
  fetchNpsStats: vi.fn(),
  UnisenseApiError: class extends Error {},
}));

import { listFeedback, updateFeedbackStatus } from "../api";
const mockedList = vi.mocked(listFeedback);
const mockedUpdate = vi.mocked(updateFeedbackStatus);

const feedbacks: Feedback[] = [
  {
    id: 1,
    user_id: 7,
    target_type: "metric",
    target_id: "sales_gmv",
    rating: 4,
    comment: "口径很清楚",
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
});

describe("FeedbackCenter 用户反馈", () => {
  it("加载并渲染反馈列表，状态/处理人/处理时间中文展示", async () => {
    render(<FeedbackCenter />);
    await waitFor(() => expect(screen.getByText("sales_gmv")).toBeInTheDocument());
    expect(screen.getByText("口径很清楚")).toBeInTheDocument();
    expect(screen.getByText("希望增加导出")).toBeInTheDocument();
    // 状态列：待处理 + 已采纳
    expect(screen.getByText("待处理")).toBeInTheDocument();
    expect(screen.getByText("已采纳")).toBeInTheDocument();
    // 处理人列
    expect(screen.getByText("4")).toBeInTheDocument();
    // 原始 ISO 串不应直出
    expect(screen.queryByText("2026-08-10T10:00:00")).not.toBeInTheDocument();
    expect(screen.getAllByText(/前|昨天|月\d+日/).length).toBeGreaterThan(0);
  });

  it("反馈行提供跟进/采纳/驳回处理按钮", async () => {
    render(<FeedbackCenter />);
    await waitFor(() => expect(screen.getByText("sales_gmv")).toBeInTheDocument());
    const row = screen.getByText("sales_gmv").closest("tr") as HTMLElement;
    expect(within(row).getByText(/跟\s*进/)).toBeInTheDocument();
    expect(within(row).getByText(/采\s*纳/)).toBeInTheDocument();
    expect(within(row).getByText(/驳\s*回/)).toBeInTheDocument();
  });

  it("点击采纳打开处理弹窗，输入处理说明后调用 updateFeedbackStatus(id, status, note)", async () => {
    render(<FeedbackCenter />);
    await waitFor(() => expect(screen.getByText("sales_gmv")).toBeInTheDocument());
    const row = screen.getByText("sales_gmv").closest("tr") as HTMLElement;
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
    render(<FeedbackCenter />);
    await waitFor(() => expect(screen.getByText("sales_gmv")).toBeInTheDocument());
    const row = screen.getByText("sales_gmv").closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByText(/驳\s*回/));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByText(/确\s*认\s*处\s*理/));
    await waitFor(() => expect(mockedUpdate).toHaveBeenCalledWith(1, "rejected", null));
  });

  it("按类型筛选：切换下拉后按 target_type 调用 listFeedback", async () => {
    render(<FeedbackCenter />);
    await waitFor(() => expect(screen.getByText("sales_gmv")).toBeInTheDocument());
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
});