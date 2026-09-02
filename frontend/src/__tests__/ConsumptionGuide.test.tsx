import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConsumptionGuide } from "../pages/ConsumptionGuide";
import type { MetricResponse } from "../types";

// Mock API
vi.mock("../api", () => ({
  fetchConsumptionGuide: vi.fn(),
  getMetric: vi.fn(),
  updateConsumptionGuide: vi.fn(),
}));

// Mock useTracking hook（返回稳定引用，避免 effect 依赖反复触发）
const trackMock = vi.fn();
vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: trackMock }),
}));

import { fetchConsumptionGuide, getMetric, updateConsumptionGuide } from "../api";
const mockedFetchGuide = vi.mocked(fetchConsumptionGuide);
const mockedUpdateGuide = vi.mocked(updateConsumptionGuide);

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

/** 默认指标 mock（beforeEach 默认注入，单测可覆盖 definition_json 等字段） */
const mockMetric: MetricResponse = {
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
  term_id: null,
  effective_version: 1,
  consumption_guide: null,
  successor_code: null,
  deprecated_at: null,
  sunset_until: null,
  emergency_publish: false,
  emergency_reason: null,
  emergency_reviewed_at: null,
  gray_tenant_ids: null,
  pending_conflict: false,
  pending_conflict_detail: null,
  pending_version: false,
  created_at: "2026-08-01T00:00:00",
  updated_at: "2026-08-01T00:00:00",
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
    vi.mocked(getMetric).mockResolvedValue({ ...mockMetric });
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

  it("口径定义分区渲染：统计要素/技术口径/来源与依赖分组展示", async () => {
    vi.mocked(getMetric).mockResolvedValue({
      ...mockMetric,
      definition_json: {
        sql: "SELECT COUNT(1) AS cnt\nFROM ods_sales",
        period: "day",
        measures: [{ name: "cnt", aggregation: "COUNT" }],
        dimensions: ["dept_id"],
        source_tables: ["ods_sales"],
      },
    });
    mockedFetchGuide.mockResolvedValue(mockGuideData);
    renderGuide();

    await waitFor(() => {
      expect(screen.getAllByText("finance_revenue_sum_d").length).toBeGreaterThan(0);
    });

    // 分区标题
    expect(screen.getByText("统计要素")).toBeInTheDocument();
    expect(screen.getByText("技术口径")).toBeInTheDocument();
    expect(screen.getByText("来源与依赖")).toBeInTheDocument();

    // SQL 代码块头部：标签 + 复制按钮（含行数）
    expect(screen.getByText("技术口径（源业务库口径）")).toBeInTheDocument();
    expect(screen.getByText(/复制 SQL（2 行）/)).toBeInTheDocument();

    // 统计要素 chip：统计周期: day（day 同时出现在基本信息粒度，故用 getAllByText）
    expect(screen.getByText("统计周期")).toBeInTheDocument();
    expect(screen.getAllByText("day").length).toBeGreaterThan(0);

    // 来源与依赖：来源表 Tag 与维度
    expect(screen.getByText("依赖表（上游）")).toBeInTheDocument();
    expect(screen.getByText("维度")).toBeInTheDocument();
    expect(screen.getByText("ods_sales")).toBeInTheDocument();
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

  it("自动生成来源徽标 + 编辑按钮（人工维护标识）", async () => {
    mockedFetchGuide.mockResolvedValue({ ...mockGuideData, guide_source: "manual", guide_updated_at: "2026-08-26T00:00:00" });
    renderGuide();
    await waitFor(() => {
      expect(screen.getByText("人工维护")).toBeInTheDocument();
    });
    // can() 在测试默认环境放行 → 编辑按钮可见
    expect(screen.getByRole("button", { name: /编辑指南/ })).toBeTruthy();
  });

  it("编辑指南：打开弹窗 → 增删行 → 保存调用 updateConsumptionGuide（乐观锁）", async () => {
    mockedFetchGuide.mockResolvedValue({ ...mockGuideData, guide_source: "auto" });
    mockedUpdateGuide.mockResolvedValue({ ...mockGuideData, guide_source: "manual" });
    renderGuide();
    await waitFor(() => {
      expect(screen.getByText("自动生成")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: /编辑指南/ }));
    await screen.findAllByText("推荐使用方式"); // 页面 Card + Modal 内 label 均命中
    // 预填当前推荐用法（2 项）+ 注意事项（1）+ 关联指标（1）= 4 个输入框
    expect(screen.getAllByRole("textbox").length).toBe(4);
    expect(screen.getByDisplayValue("适用 finance 域 day 粒度分析")).toBeInTheDocument();
    // 添加一项推荐用法（第一组的「添加一项」）→ 5 个输入框
    fireEvent.click(screen.getAllByRole("button", { name: /添加一项/ })[0]);
    expect(screen.getAllByRole("textbox").length).toBe(5);
    // 保存
    fireEvent.click(screen.getByRole("button", { name: "保 存" }));
    await waitFor(() => {
      expect(mockedUpdateGuide).toHaveBeenCalledWith(
        "finance_revenue_sum_d",
        expect.objectContaining({ recommended_usage: mockGuideData.recommended_usage, row_version: 1 }),
      );
    });
  });

  it("DEPRECATED 指标：显示废弃提示且无编辑指南按钮（废弃冻结）", async () => {
    mockedFetchGuide.mockResolvedValue({ ...mockGuideData, guide_source: "manual" });
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
      version: 2,
      row_version: 2,
      status: "DEPRECATED",
      owner_id: 1,
      backup_owner_id: null,
      approver_id: null,
      submitted_by: null,
      pii_flag: true,
      compliance_reviewed: true,
      term_id: null,
      effective_version: 1,
      consumption_guide: null,
      successor_code: null,
      deprecated_at: "2026-08-20T00:00:00",
      sunset_until: null,
      emergency_publish: false,
      emergency_reason: null,
      emergency_reviewed_at: null,
      gray_tenant_ids: null,
      pending_conflict: false,
      pending_conflict_detail: null,
      pending_version: false,
      created_at: "2026-08-01T00:00:00",
      updated_at: "2026-08-20T00:00:00",
    });
    renderGuide();
    await waitFor(() => {
      expect(screen.getAllByText(/指标已废弃/).length).toBeGreaterThan(0);
    });
    expect(screen.getByText(/不可消费，消费指南仅供审计回溯/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /编辑指南/ })).not.toBeInTheDocument();
  });
});
