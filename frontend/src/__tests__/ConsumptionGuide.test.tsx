import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ConsumptionGuide } from "../pages/ConsumptionGuide";

// Mock API
vi.mock("../api", () => ({
  fetchConsumptionGuide: vi.fn(),
}));

// Mock useTracking hook
vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: vi.fn() }),
}));

import { fetchConsumptionGuide } from "../api";
const mockedFetchGuide = vi.mocked(fetchConsumptionGuide);

const mockGuideData = {
  metric_code: "finance_revenue_sum_d",
  definition: "财务域收入汇总指标",
  calculation_logic: "SUM(revenue) GROUP BY date",
  dimensions: [
    { name: "date", description: "统计日期", type: "PARTITION" },
  ],
  usage_examples: [
    { title: "按日查询", sql: "SELECT * FROM finance_revenue WHERE date = '2026-01-01'", description: "按日查询收入" },
  ],
  related_metrics: ["finance_cost_sum_d"],
  faq: [{ question: "是否含税？", answer: "不含税" }],
};

function renderGuide(metricCode = "finance_revenue_sum_d") {
  return render(
    <MemoryRouter initialEntries={[`/guide/${metricCode}`]}>
      <ConsumptionGuide />
    </MemoryRouter>,
  );
}

describe("ConsumptionGuide", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows loading state initially", () => {
    mockedFetchGuide.mockReturnValue(new Promise(() => {}));
    renderGuide();
    expect(screen.getByText(/加载消费指南/)).toBeInTheDocument();
  });

  it("renders guide data after successful fetch", async () => {
    mockedFetchGuide.mockResolvedValue(mockGuideData);
    renderGuide();

    await waitFor(() => {
      expect(screen.getByText("finance_revenue_sum_d")).toBeInTheDocument();
    });
  });

  it("shows error message on fetch failure", async () => {
    mockedFetchGuide.mockRejectedValue(new Error("指标不存在"));
    renderGuide();

    await waitFor(() => {
      expect(screen.getByText(/加载失败/)).toBeInTheDocument();
    });
  });

  it("renders tabs for definition, calculation, dimensions, examples, related, faq", async () => {
    mockedFetchGuide.mockResolvedValue(mockGuideData);
    renderGuide();

    await waitFor(() => {
      expect(screen.getByText("口径定义")).toBeInTheDocument();
    });

    expect(screen.getByText("计算逻辑")).toBeInTheDocument();
    expect(screen.getByText("维度说明")).toBeInTheDocument();
    expect(screen.getByText("使用示例")).toBeInTheDocument();
    expect(screen.getByText("关联指标")).toBeInTheDocument();
    expect(screen.getByText("FAQ")).toBeInTheDocument();
  });
});
