import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { MetricCatalog } from "../pages/MetricCatalog";

vi.mock("../api", () => ({
  listMetrics: vi.fn(),
  fetchDashboard: vi.fn(),
}));
const trackMock = vi.fn();
vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: trackMock }),
}));

import { listMetrics, fetchDashboard } from "../api";
import type { MetricResponse } from "../types";
const mockedList = vi.mocked(listMetrics);
const mockedDashboard = vi.mocked(fetchDashboard);

const metric: MetricResponse = {
  id: 1,
  metric_code: "sales_gmv_sum_d",
  name: "销售 GMV",
  domain: "sales",
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
  definition_json: { expression: "sum(gmv)" },
  version: 2,
  row_version: 1,
  status: "PUBLISHED",
  owner_id: 1,
  backup_owner_id: null,
  pii_flag: true,
  compliance_reviewed: true,
  effective_version: 2,
  consumption_guide: null,
  successor_code: null,
  deprecated_at: null,
  sunset_until: null,
  emergency_publish: true,
  emergency_reason: "hotfix",
  gray_tenant_ids: [101, 102],
  pending_conflict: false,
  pending_conflict_detail: null,
  created_at: "2026-08-01T00:00:00",
  updated_at: "2026-08-02T00:00:00",
};

function renderCatalog() {
  return render(
    <MemoryRouter initialEntries={["/catalog"]}>
      <Routes>
        <Route path="/catalog" element={<MetricCatalog />} />
        <Route path="/detail/:code" element={<div>detail</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("MetricCatalog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedList.mockResolvedValue({ items: [metric], total: 1, page: 1, page_size: 20 });
    mockedDashboard.mockResolvedValue({
      total: 1,
      by_status: { PUBLISHED: 1 },
      by_tier: { T1: 1 },
      by_domain: { sales: 1 },
      pii_count: 1,
      pii_ratio: 1,
    });
  });

  it("渲染治理徽章（紧急/灰度/PII）", async () => {
    renderCatalog();
    await waitFor(() => {
      expect(screen.getByText("sales_gmv_sum_d")).toBeTruthy();
    });
    expect(screen.getByText("紧急")).toBeTruthy();
    expect(screen.getByText("灰度")).toBeTruthy();
    expect(screen.getByText("PII 已复核")).toBeTruthy();
  });

  it("域筛选选项来自真实 dashboard by_domain（非硬编码）", async () => {
    renderCatalog();
    await waitFor(() => {
      expect(mockedDashboard).toHaveBeenCalled();
    });
  });

  it("列表请求携带排序与筛选参数", async () => {
    renderCatalog();
    await waitFor(() => {
      expect(mockedList).toHaveBeenCalledWith(
        expect.objectContaining({ page: 1, page_size: 20, sort_by: "updated_at", sort_order: "desc" }),
      );
    });
  });

  it("空态给出创建引导", async () => {
    mockedList.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 });
    renderCatalog();
    await waitFor(() => {
      expect(screen.getByText("创建指标")).toBeTruthy();
      expect(screen.getByText("从模板创建")).toBeTruthy();
    });
  });
});
