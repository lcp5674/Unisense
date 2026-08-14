import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConsumptionGuide } from "../pages/ConsumptionGuide";

// Mock API
vi.mock("../api", () => ({
  fetchConsumptionGuide: vi.fn(),
  getMetric: vi.fn(),
}));

// Mock useTracking hook（返回稳定引用，避免 effect 依赖反复触发）
const trackMock = vi.fn();
vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: trackMock }),
}));

import { fetchConsumptionGuide, getMetric } from "../api";
const mockedFetchGuide = vi.mocked(fetchConsumptionGuide);

const mockGuideData = {
  metric_code: "finance_revenue_sum_d",
  name: "财务域收入汇总",
  domain: "finance",
  type: "atomic",
  granularity: "day",
  unit: "元",
  aggregation: "SUM",
  time_semantics: "PERIOD",
  serving_mode: "BATCH_ONLY",
  recommended_usage: ["适用 finance 域 day 粒度分析", "聚合方式为 SUM，可以跨维度聚合"],
  cautions: ["该指标包含 PII 数据，使用时需遵守数据合规要求"],
  related_metrics: ["finance_cost_sum_d"],
};

function renderGuide(metricCode = "finance_revenue_sum_d") {
  return render(
    <MemoryRouter initialEntries={[`/guide/${metricCode}`]}>
      <Routes>
        <Route path="/guide/:metricCode" element={<ConsumptionGuide />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("ConsumptionGuide", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getMetric).mockResolvedValue({
      id: 1,
      metric_code: "finance_revenue_sum_d",
      name: "财务域收入汇总",
      domain: "finance",
      type: "atomic",
      granularity: "day",
      unit: "元",
      currency: null,
      aggregation: "SUM",
      time_semantics: "PERIOD",
      freshness: "T1",
      dw_layer: "DWS",
      sla: null,
      metric_tier: "T1",
      serving_mode: "BATCH_ONLY",
      additivity: "ADDITIVE",
      non_additive_dimensions: null,
      definition_json: { expr: "sum(amount)" },
      version: 1,
      row_version: 1,
      status: "PUBLISHED",
      owner_id: 1,
      backup_owner_id: null,
      approver_id: null,
      submitted_by: null,
      pii_flag: true,
      compliance_reviewed: true,
      effective_version: 1,
      consumption_guide: null,
      successor_code: null,
      deprecated_at: null,
      sunset_until: null,
      emergency_publish: false,
      emergency_reason: null,
      gray_tenant_ids: null,
      pending_conflict: false,
      pending_conflict_detail: null,
  pending_version: false,
      created_at: "2026-08-01T00:00:00",
      updated_at: "2026-08-01T00:00:00",
    });
  });

  it("shows loading state initially", () => {
    mockedFetchGuide.mockReturnValue(new Promise(() => {}));
    const { container } = renderGuide();
    expect(container.querySelector(".ant-spin-spinning")).toBeTruthy();
  });

  it("renders guide data after successful fetch", async () => {
    mockedFetchGuide.mockResolvedValue(mockGuideData);
    renderGuide();

    await waitFor(() => {
      expect(screen.getAllByText("finance_revenue_sum_d").length).toBeGreaterThan(0);
    });
  });

  it("shows error message on fetch failure", async () => {
    mockedFetchGuide.mockRejectedValue(new Error("指标不存在"));
    renderGuide();

    await waitFor(() => {
      expect(screen.getByText(/加载失败/)).toBeInTheDocument();
    });
  });

  it("renders recommended usage, cautions and related metrics sections", async () => {
    mockedFetchGuide.mockResolvedValue(mockGuideData);
    renderGuide();

    await waitFor(() => {
      expect(screen.getByText("推荐使用方式")).toBeInTheDocument();
    });

    expect(screen.getByText("注意事项")).toBeInTheDocument();
    expect(screen.getByText("关联指标")).toBeInTheDocument();
  });

  it("提供统一的返回按钮（返回上一入口）", async () => {
    mockedFetchGuide.mockResolvedValue(mockGuideData);
    renderGuide();
    await waitFor(() => {
      expect(screen.getAllByText("finance_revenue_sum_d").length).toBeGreaterThan(0);
    });
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    mockedFetchGuide.mockResolvedValue(mockGuideData);
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/detail/finance_revenue_sum_d", "/guide/finance_revenue_sum_d"]}>
        <Routes>
          <Route path="/detail/:code" element={<div>detail-page</div>} />
          <Route path="/guide/:metricCode" element={<ConsumptionGuide />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getAllByText("finance_revenue_sum_d").length).toBeGreaterThan(0);
    });
    const backBtn = screen.getByRole("button", { name: /返\s*回/ });
    backBtn.click();
    await screen.findByText("detail-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    mockedFetchGuide.mockResolvedValue(mockGuideData);
    render(
      <MemoryRouter initialEntries={["/guide/finance_revenue_sum_d"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/guide/:metricCode" element={<ConsumptionGuide />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getAllByText("finance_revenue_sum_d").length).toBeGreaterThan(0);
    });
    screen.getByRole("button", { name: /返\s*回/ }).click();
    await screen.findByText("dashboard-page");
  });
});
