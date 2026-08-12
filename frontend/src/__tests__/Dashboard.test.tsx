import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { BrowserRouter } from "react-router-dom";
import { Dashboard } from "../pages/Dashboard";

// Mock API
vi.mock("../api", () => ({
  fetchDashboard: vi.fn(),
}));

// Mock useTracking hook
vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: vi.fn() }),
}));

import { fetchDashboard } from "../api";
const mockedFetchDashboard = vi.mocked(fetchDashboard);

const mockDashboardData = {
  total_metrics: 100,
  published_count: 60,
  draft_count: 25,
  deprecated_count: 5,
  conflict_count: 3,
  review_pending_count: 7,
  avg_review_hours: 4.5,
  pii_metric_count: 12,
  quality_anomaly_count: 2,
  top_domains: [
    { domain: "finance", count: 40 },
    { domain: "marketing", count: 30 },
  ],
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
  });

  it("shows loading state initially", () => {
    mockedFetchDashboard.mockReturnValue(new Promise(() => {})); // never resolves
    renderDashboard();
    expect(screen.getByText(/加载驾驶舱数据/)).toBeInTheDocument();
  });

  it("renders dashboard data after successful fetch", async () => {
    mockedFetchDashboard.mockResolvedValue(mockDashboardData);
    renderDashboard();

    await waitFor(() => {
      expect(screen.getByText("100")).toBeInTheDocument();
    });

    expect(screen.getByText("60")).toBeInTheDocument();
    expect(screen.getByText("25")).toBeInTheDocument();
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
    mockedFetchDashboard.mockResolvedValue(mockDashboardData);
    renderDashboard();
    expect(mockedFetchDashboard).toHaveBeenCalledOnce();
  });
});
