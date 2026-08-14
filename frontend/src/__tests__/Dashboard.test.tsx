import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { BrowserRouter, MemoryRouter, useLocation } from "react-router-dom";
import { Dashboard } from "../pages/Dashboard";

// Mock API
vi.mock("../api", () => ({
  fetchDashboard: vi.fn(),
  fetchRecommendedMetrics: vi.fn(),
  fetchRecommendedTerms: vi.fn(),
}));

// Mock 图表库（jsdom 无 canvas 环境）
vi.mock("@ant-design/charts", () => ({
  Pie: () => <div data-testid="mock-pie" />,
  Bar: () => <div data-testid="mock-bar" />,
}));

// Mock useTracking hook（返回稳定引用，避免 effect 依赖反复触发）
const trackMock = vi.fn();
vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: trackMock }),
}));

import { fetchDashboard, fetchRecommendedMetrics, fetchRecommendedTerms } from "../api";
const mockedFetchDashboard = vi.mocked(fetchDashboard);

const mockDashboardData = {
  total: 100,
  by_status: { DRAFT: 25, EXPERIMENTAL: 3, REVIEW: 7, PUBLISHED: 60, DEPRECATED: 5 },
  by_tier: { T1: 20, T2: 50, T3: 30 },
  by_domain: { finance: 40, marketing: 30, growth: 20, risk: 10 },
  pii_count: 12,
  pii_ratio: 0.12,
  assets: {
    metric: { total: 100, by_status: { DRAFT: 25, EXPERIMENTAL: 3, REVIEW: 7, PUBLISHED: 60, DEPRECATED: 5 } },
    table: { total: 40, by_status: { PUBLIC: 5, INTERNAL: 25, CONFIDENTIAL: 6, PII: 3, NEEDS_REVIEW: 1 } },
    source: { total: 8, by_status: { healthy: 6, unhealthy: 1, unknown: 1 } },
    dimension: { total: 15, by_status: { DRAFT: 2, PUBLISHED: 12, DEPRECATED: 1 } },
    term: { total: 22, by_status: { DRAFT: 4, PUBLISHED: 17, DEPRECATED: 1 } },
    template: { total: 10, by_status: { active: 9, inactive: 1 } },
    collection_task: { total: 6, by_status: { QUEUED: 1, RUNNING: 1, COMPLETED: 3, FAILED: 1 } },
    system_dict: { total: 30, by_status: { active: 28, inactive: 2 } },
  },
};

function renderDashboard() {
  return render(
    <BrowserRouter>
      <Dashboard />
    </BrowserRouter>,
  );
}

/** 渲染 Dashboard 并捕获路由跳转结果（MemoryRouter + useLocation 探针）。 */
function renderWithLocation() {
  let captured: { pathname: string; search: string } | null = null;
  function Probe() {
    captured = useLocation();
    return null;
  }
  render(
    <MemoryRouter initialEntries={["/dashboard"]}>
      <Probe />
      <Dashboard />
    </MemoryRouter>,
  );
  return {
    location: () => captured,
  };
}

describe("Dashboard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedFetchDashboard.mockResolvedValue(mockDashboardData);
    vi.mocked(fetchRecommendedMetrics).mockResolvedValue([]);
    vi.mocked(fetchRecommendedTerms).mockResolvedValue([]);
  });

  it("shows loading state initially", () => {
    mockedFetchDashboard.mockReturnValue(new Promise(() => {})); // never resolves
    const { container } = renderDashboard();
    expect(container.querySelector(".ant-spin-spinning")).toBeTruthy();
  });

  it("renders dashboard data after successful fetch", async () => {
    renderDashboard();

    await waitFor(() => {
      // KPI 指标总数 + 指标资产卡 total 均为 100
      expect(screen.getAllByText("100").length).toBeGreaterThan(0);
    });

    expect(screen.getAllByText("60").length).toBeGreaterThan(0);
    expect(screen.getAllByText("25").length).toBeGreaterThan(0);
    expect(screen.getByText("总览仪表")).toBeInTheDocument();
  });

  it("shows error message on fetch failure", async () => {
    mockedFetchDashboard.mockRejectedValue(new Error("网络错误"));
    renderDashboard();

    await waitFor(() => {
      expect(screen.getByText(/加载失败/)).toBeInTheDocument();
    });
    expect(screen.getByText(/网络错误/)).toBeInTheDocument();
  });

  it("calls fetchDashboard on mount", () => {
    renderDashboard();
    expect(mockedFetchDashboard).toHaveBeenCalledOnce();
  });

  it("资产总览：渲染全部 8 类资产卡片与计数", async () => {
    renderDashboard();
    await waitFor(() => expect(screen.getByText("资产总览")).toBeInTheDocument());

    // 8 类资产名称
    for (const label of ["指标", "数据表", "数据源", "维度", "术语", "指标模板", "采集任务", "数据字典"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    // 各资产总数（asset-card 内的 ac-total）
    for (const total of ["40", "8", "15", "22", "10", "30"]) {
      expect(screen.getByText(total)).toBeInTheDocument();
    }
    // 数据表 INTERNAL=25（与指标 DRAFT=25 重复出现，用 getAllByText）
    expect(screen.getAllByText("25").length).toBeGreaterThan(0);
    // 数据源 healthy=6 / 采集任务 total=6
    expect(screen.getAllByText("6").length).toBeGreaterThan(0);
  });

  it("资产卡片下钻：点击资产名跳转对应目录（无状态）", async () => {
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("资产总览")).toBeInTheDocument());

    fireEvent.click(screen.getByText("数据表"));
    expect(probe.location()?.pathname).toBe("/catalogs");
  });

  it("资产卡片下钻：点击状态段跳转对应目录并携带状态参数", async () => {
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("资产总览")).toBeInTheDocument());

    // 数据表 → PII 敏感段
    fireEvent.click(screen.getByText("PII"));
    expect(probe.location()?.pathname).toBe("/catalogs");
    expect(probe.location()?.search).toContain("sensitivity=PII");
  });

  it("资产卡片下钻：采集任务状态段携带状态参数", async () => {
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("资产总览")).toBeInTheDocument());

    // 采集任务 → RUNNING 段（「采集中」标签唯一）
    fireEvent.click(screen.getByText("采集中"));
    expect(probe.location()?.pathname).toBe("/collection-tasks");
    expect(probe.location()?.search).toContain("status=RUNNING");
  });
});
