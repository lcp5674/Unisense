import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { BrowserRouter } from "react-router-dom";
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
};

function renderDashboard() {
  return render(
    <BrowserRouter>
      <Dashboard />
    </BrowserRouter>,
  );
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
      expect(screen.getByText("100")).toBeInTheDocument();
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
});
