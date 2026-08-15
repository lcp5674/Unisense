import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TrackingStats } from "../pages/TrackingStats";

// Mock API
vi.mock("../api", () => ({
  fetchTrackingStats: vi.fn(),
  listUsers: vi.fn(),
}));

// Mock 图表（jsdom 中 Bar 真实渲染依赖 canvas 测量，统一替换为占位节点）
vi.mock("@ant-design/charts", () => ({
  Bar: ({ data, yField }: any) => (
    <div data-testid="mock-bar" data-rows={data?.length} data-yfield={yField} />
  ),
}));

import { fetchTrackingStats, listUsers } from "../api";
const mockedFetchStats = vi.mocked(fetchTrackingStats);
const mockedListUsers = vi.mocked(listUsers);

const mockStats = {
  stats: [
    { group_key: "metric_view", event_count: 120, unique_actors: 30 },
    { group_key: "consume_query", event_count: 80, unique_actors: 20 },
  ],
  // 全量去重用户数：两组用户有重叠，全量仅 40（≠ 各组之和 50）
  total_unique_actors: 40,
};

function renderPage() {
  return render(<TrackingStats />);
}

describe("TrackingStats", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedFetchStats.mockResolvedValue(mockStats);
    mockedListUsers.mockResolvedValue([
      { id: 3, username: "admin", display_name: "系统管理员", role: "platform_admin", domain: null, status: "active" },
      { id: 1, username: "nowner", display_name: "", role: "metric_owner", domain: "sales", status: "active" },
    ]);
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
    // 去重用户数 = 后端全量 total_unique_actors（40），而非各组相加（30+20=50）
    expect(screen.getByText("40")).toBeInTheDocument();
    expect(screen.queryByText("50")).not.toBeInTheDocument();
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

  it("applies filters when clicking 查询 button (业务标签选择事件类型)", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(mockedFetchStats).toHaveBeenCalled());

    // 事件类型筛选改为业务标签 Select：选「按钮点击」（= button_click）。
    // showSearch 虚拟列表只渲染首屏项且可见项无 role=option，复用仓库 MetricCreate 模式：
    // fireEvent.mouseDown 打开 + 点击 .ant-select-item-option[title=中文标签]
    fireEvent.mouseDown(screen.getByText("全部事件类型"));
    await waitFor(() => {
      const dropdown = document.querySelector(
        ".ant-select-dropdown:not(.ant-select-dropdown-hidden)",
      ) as HTMLElement | null;
      const option = dropdown?.querySelector(
        '.ant-select-item-option[title="按钮点击"]',
      ) as HTMLElement | null;
      expect(option).toBeTruthy();
      if (option) fireEvent.click(option);
    });
    await user.click(screen.getByRole("button", { name: /查\s*询/ }));

    await waitFor(() => {
      expect(mockedFetchStats).toHaveBeenLastCalledWith(
        expect.objectContaining({ event_type: "button_click" }),
      );
    });
  });

  it("target_type 分组展示业务标签而非技术值", async () => {
    const user = userEvent.setup();
    mockedFetchStats.mockResolvedValue({
      stats: [
        { group_key: "dashboard", event_count: 10, unique_actors: 2 },
        { group_key: "metric", event_count: 5, unique_actors: 1 },
      ],
    });
    renderPage();
    await waitFor(() => expect(mockedFetchStats).toHaveBeenCalled());

    // 切换到「目标类型」分组
    await user.click(screen.getByText("事件类型"));
    await user.click(await screen.findByText("目标类型"));

    await waitFor(() => {
      expect(mockedFetchStats).toHaveBeenLastCalledWith(
        expect.objectContaining({ group_by: "target_type" }),
      );
    });
    await waitFor(() => expect(screen.getByText("仪表盘")).toBeInTheDocument());
    expect(screen.getByText("指标")).toBeInTheDocument();
    // 技术值不应直出
    expect(screen.queryByText("dashboard")).not.toBeInTheDocument();
  });

  it("actor_id 分组展示用户名而非数字 ID（display_name 优先，缺失回落 username）", async () => {
    const user = userEvent.setup();
    mockedFetchStats.mockResolvedValue({
      stats: [
        { group_key: "3", event_count: 10, unique_actors: 1 },
        { group_key: "1", event_count: 5, unique_actors: 1 },
      ],
    });
    renderPage();
    await waitFor(() => expect(mockedFetchStats).toHaveBeenCalled());

    await user.click(screen.getByText("事件类型"));
    await user.click(await screen.findByText("操作用户"));

    await waitFor(() => {
      expect(mockedFetchStats).toHaveBeenLastCalledWith(
        expect.objectContaining({ group_by: "actor_id" }),
      );
    });
    // admin 有 display_name → 系统管理员；nowner 无 display_name → 回落 username
    await waitFor(() => expect(screen.getByText("系统管理员")).toBeInTheDocument());
    expect(screen.getByText("nowner")).toBeInTheDocument();
    // 原始数字 ID 不应直出
    expect(screen.queryByText("3")).not.toBeInTheDocument();
  });
});
