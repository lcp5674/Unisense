import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { MetricDetail } from "../pages/MetricDetail";
import type { MetricHealth, MetricResponse } from "../types";

vi.mock("../api", () => ({
  getMetric: vi.fn(),
  listVersions: vi.fn(),
  fetchCurrentUser: vi.fn(),
  listFavorites: vi.fn(),
  getMetricHealth: vi.fn(),
  listUsers: vi.fn(),
  listSubscriptions: vi.fn(),
  fetchRelatedMetrics: vi.fn(),
  addFavorite: vi.fn(),
  removeFavorite: vi.fn(),
  approveMetric: vi.fn(),
  deprecateMetric: vi.fn(),
  emergencyPublishMetric: vi.fn(),
  piiReview: vi.fn(),
  promoteMetric: vi.fn(),
  rollbackMetric: vi.fn(),
  submitReview: vi.fn(),
  upsertSubscription: vi.fn(),
  // 详情页子组件依赖
  listQualityEvents: vi.fn().mockResolvedValue({ items: [] }),
  listQualityRules: vi.fn().mockResolvedValue({ items: [] }),
  listSnapshots: vi.fn().mockResolvedValue([]),
  qualityEventAck: vi.fn(),
  qualityEventResolve: vi.fn(),
  qualityEventClose: vi.fn(),
  confirmMetricVersion: vi.fn(),
  extendMetricVersion: vi.fn(),
  rejectMetricVersion: vi.fn(),
  lineageImpact: vi.fn().mockResolvedValue([]),
  listAudit: vi.fn().mockResolvedValue({ items: [] }),
  listMetricDimensions: vi.fn().mockResolvedValue([]),
  UnisenseApiError: class extends Error {},
}));
const trackMock = vi.fn();
vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: trackMock }),
}));

import {
  getMetric,
  listVersions,
  fetchCurrentUser,
  listFavorites,
  getMetricHealth,
  listUsers,
  listSubscriptions,
  fetchRelatedMetrics,
} from "../api";
const mockedGetMetric = vi.mocked(getMetric);
const mockedListVersions = vi.mocked(listVersions);
const mockedCurrentUser = vi.mocked(fetchCurrentUser);
const mockedFavorites = vi.mocked(listFavorites);
const mockedHealth = vi.mocked(getMetricHealth);
const mockedUsers = vi.mocked(listUsers);
const mockedSubs = vi.mocked(listSubscriptions);
const mockedRelated = vi.mocked(fetchRelatedMetrics);

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
  definition_json: {
    expression: "sum(gmv)",
    definition: "当日支付成功订单的成交总额",
    sql: "SELECT SUM(order_amount) AS gmv, dt FROM dwd_order_di GROUP BY dt",
    source_tables: ["dwd_order_di"],
    dependencies: ["user_base_cnt_d"],
    source_fields: ["gmv"],
  },
  version: 2,
  row_version: 1,
  status: "PUBLISHED",
  owner_id: 1,
  backup_owner_id: 2,
  approver_id: 3,
  submitted_by: 1,
  pii_flag: true,
  compliance_reviewed: true,
  effective_version: 2,
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
  updated_at: "2026-08-02T00:00:00",
};

function renderDetail(initialEntry: { pathname: string; state?: { from?: string } }) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/detail/:code" element={<MetricDetail />} />
        <Route path="/catalog" element={<div>catalog-page</div>} />
        <Route path="/dashboard" element={<div>dashboard-page</div>} />
        <Route path="/todo" element={<div>todo-page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("MetricDetail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGetMetric.mockResolvedValue(metric);
    mockedListVersions.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({
      id: 1,
      username: "zhangsan",
      display_name: "张三",
      role: "metric_owner",
      domain: "sales",
      org_id: 1,
    });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
  });

  it("提供统一的返回按钮，从指标目录进入（历史栈有上一页）时回退到指标目录", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/catalog", "/detail/sales_gmv_sum_d"]}>
        <Routes>
          <Route path="/detail/:code" element={<MetricDetail />} />
          <Route path="/catalog" element={<div>catalog-page</div>} />
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
        </Routes>
      </MemoryRouter>,
    );
    const btn = await screen.findByRole("button", { name: /返\s*回/ });
    fireEvent.click(btn);
    await screen.findByText("catalog-page");
    lengthSpy.mockRestore();
  });

  it("URL 直达无上一页时点击返回兜底跳转总览仪表", async () => {
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    const btn = await screen.findByRole("button", { name: /返\s*回/ });
    fireEvent.click(btn);
    await screen.findByText("dashboard-page");
  });

  it("从总览仪表「为你推荐」进入时，返回按钮为统一文案并精确返回仪表盘", async () => {
    renderDetail({ pathname: "/detail/sales_gmv_sum_d", state: { from: "dashboard" } });
    // 来源感知影响跳转目标（仪表盘），文案统一为"返回"（与其他页面一致）
    const btn = await screen.findByRole("button", { name: /返\s*回/ });
    expect(btn.textContent).not.toMatch(/←/);
    fireEvent.click(btn);
    await screen.findByText("dashboard-page");
  });

  it("从待办中心进入时，返回按钮为统一文案并精确返回 /todo", async () => {
    renderDetail({ pathname: "/detail/sales_gmv_sum_d", state: { from: "todo" } });
    // 来源感知影响跳转目标（待办中心），文案统一为"返回"
    const btn = await screen.findByRole("button", { name: /返\s*回/ });
    expect(btn.textContent).not.toMatch(/←/);
    fireEvent.click(btn);
    await screen.findByText("todo-page");
  });
});
