import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { FeedbackCenter } from "../pages/FeedbackCenter";
import type { Feedback } from "../types";

vi.mock("../api", () => ({
  listFeedback: vi.fn(),
  updateFeedbackStatus: vi.fn(),
}));

import { listFeedback, updateFeedbackStatus } from "../api";
const mockedList = vi.mocked(listFeedback);
const mockedUpdate = vi.mocked(updateFeedbackStatus);

const feedbacks: Feedback[] = [
  { id: 1, user_id: 7, target_type: "metric", target_id: "sales_gmv", rating: 4, comment: "口径很清楚", created_at: "2026-08-10T10:00:00" },
  { id: 2, user_id: 9, target_type: "dashboard", target_id: null, rating: null, comment: "希望增加导出", created_at: "2026-08-11T09:00:00" },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockedList.mockResolvedValue({ items: feedbacks, total: feedbacks.length });
  mockedUpdate.mockResolvedValue(feedbacks[0]);
});

describe("FeedbackCenter 用户反馈", () => {
  it("加载并渲染反馈列表", async () => {
    render(<FeedbackCenter />);
    await waitFor(() => expect(screen.getByText("sales_gmv")).toBeInTheDocument());
    expect(screen.getByText("口径很清楚")).toBeInTheDocument();
    expect(screen.getByText("希望增加导出")).toBeInTheDocument();
    // 时间列改为中文描述 + 上海时区，原始 ISO 串不应再直出
    expect(screen.queryByText("2026-08-10T10:00:00")).not.toBeInTheDocument();
    expect(screen.getAllByText(/前|昨天|月\d+日/).length).toBeGreaterThan(0);
  });

  it("反馈行提供跟进/采纳/驳回处理按钮", async () => {
    render(<FeedbackCenter />);
    await waitFor(() => expect(screen.getByText("sales_gmv")).toBeInTheDocument());
    const row = screen.getByText("sales_gmv").closest("tr") as HTMLElement;
    // antd Button 会在两字中文间插入空格（"跟 进"），用正则匹配
    expect(within(row).getByText(/跟\s*进/)).toBeInTheDocument();
    expect(within(row).getByText(/采\s*纳/)).toBeInTheDocument();
    expect(within(row).getByText(/驳\s*回/)).toBeInTheDocument();
  });

  it("点击采纳调用 updateFeedbackStatus 并刷新列表", async () => {
    render(<FeedbackCenter />);
    await waitFor(() => expect(screen.getByText("sales_gmv")).toBeInTheDocument());
    const row = screen.getByText("sales_gmv").closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByText(/采\s*纳/));
    await waitFor(() => expect(mockedUpdate).toHaveBeenCalledWith(1, "adopted", "前台处理"));
    expect(mockedList).toHaveBeenCalledTimes(2); // 初始 + 处理后刷新
  });
});
