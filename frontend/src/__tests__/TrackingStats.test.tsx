import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TrackingStats } from "../pages/TrackingStats";

// Mock API
vi.mock("../api", () => ({
  fetchTrackingStats: vi.fn(),
}));

// Mock 图表（jsdom 中 Bar 真实渲染依赖 canvas 测量，统一替换为占位节点）
vi.mock("@ant-design/charts", () => ({
  Bar: ({ data, yField }: any) => (
    <div data-testid="mock-bar" data-rows={data?.length} data-yfield={yField} />
  ),
}));

import { fetchTrackingStats } from "../api";
const mockedFetchStats = vi.mocked(fetchTrackingStats);

const mockStats = {
  stats: [
    { group_key: "metric_view", event_count: 120, unique_actors: 30 },
    { group_key: "consume_query", event_count: 80, unique_actors: 20 },
  ],
};

function renderPage() {
  return render(<TrackingStats />);
}

describe("TrackingStats", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedFetchStats.mockResolvedValue(mockStats);
  });

  it("shows loading state initially", () => {
    mockedFetchStats.mockReturnValue(new Promise(() => {}));
    const { container } = renderPage();
    expect(container.querySelector(".ant-spin-spinning")).toBeTruthy();
  });

  it("loads stats on mount with default group_by=event_type", async () => {
    renderPage();
    await waitFor(() => {
      expect(mockedFetchStats).toHaveBeenCalledWith(
        expect.objectContaining({ group_by: "event_type" }),
      );
    });
  });

  it("renders stat cards and table after success", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("事件总数")).toBeInTheDocument();
    });
    // 事件总数 = 120 + 80
    expect(screen.getByText("200")).toBeInTheDocument();
    // 去重用户数 = 30 + 20
    expect(screen.getByText("50")).toBeInTheDocument();
    // 表格行渲染（事件类型英文值转中文标签）
    expect(screen.getByText("指标查看")).toBeInTheDocument();
    expect(screen.getByText("消费查询")).toBeInTheDocument();
  });

  it("shows empty state when no stats", async () => {
    mockedFetchStats.mockResolvedValue({ stats: [] });
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/暂无埋点统计数据/)).toBeInTheDocument();
    });
  });

  it("shows error alert on fetch failure", async () => {
    mockedFetchStats.mockRejectedValue(new Error("无权限"));
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("加载失败")).toBeInTheDocument();
    });
    expect(screen.getByText("无权限")).toBeInTheDocument();
  });

  it("refetches when group_by changes", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(mockedFetchStats).toHaveBeenCalled());

    // 打开分组下拉并选择「操作用户」
    await user.click(screen.getByText("事件类型"));
    await user.click(await screen.findByText("操作用户"));

    await waitFor(() => {
      expect(mockedFetchStats).toHaveBeenLastCalledWith(
        expect.objectContaining({ group_by: "actor_id" }),
      );
    });
  });

  it("applies filters when clicking 查询 button", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(mockedFetchStats).toHaveBeenCalled());

    await user.type(screen.getByPlaceholderText("如 metric_view"), "metric_view");
    await user.click(screen.getByRole("button", { name: /查\s*询/ }));

    await waitFor(() => {
      expect(mockedFetchStats).toHaveBeenLastCalledWith(
        expect.objectContaining({ event_type: "metric_view" }),
      );
    });
  });
});
