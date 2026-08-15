import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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
  UnisenseApiError,
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

  it("仲裁为权威口径时展示绿色「权威口径」Tag（TD §12.4）", async () => {
    mockedGetMetric.mockResolvedValue({
      ...metric,
      arbitration_mark: {
        status: "canonical",
        conflict_id: "CF-ABC",
        decision: "merge",
        ruled_at: "2026-08-15T04:00:00Z",
        opposite_code: "sales_gmv_d",
      },
    });
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    const tag = await screen.findByText("权威口径");
    expect(tag).toBeInTheDocument();
    // 悬停 Tooltip 展示裁决明细（决策中文 + 落败方）
    fireEvent.mouseEnter(tag);
    expect(await screen.findByText(/合并口径/)).toBeInTheDocument();
    expect(screen.getByText(/落败方 sales_gmv_d/)).toBeInTheDocument();
  });

  it("仲裁保留差异时展示蓝色「已裁定共存」Tag", async () => {
    mockedGetMetric.mockResolvedValue({
      ...metric,
      arbitration_mark: {
        status: "coexist",
        conflict_id: "CF-DEF",
        decision: "keep_diff",
        ruled_at: "2026-08-15T04:00:00Z",
        opposite_code: "sales_gmv_d",
      },
    });
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    const tag = await screen.findByText("已裁定共存");
    expect(tag).toBeInTheDocument();
    fireEvent.mouseEnter(tag);
    expect(await screen.findByText(/保留差异/)).toBeInTheDocument();
  });

  it("仲裁作废指标（METRIC_ARCHIVED）直访时展示友好引导页并可跳转权威指标", async () => {
    const err = Object.assign(new UnisenseApiError("指标已因口径裁决作废: sales_e2e_conflictb_day"), {
      code: "METRIC_ARCHIVED",
      codeZh: "该指标已因口径裁决作废，请查看权威指标",
      detail: {
        metric_code: "sales_e2e_conflictb_day",
        successor_code: "sales_e2e_conflicta_day",
        arbitration_mark: {
          status: "defeated",
          conflict_id: "CF-ABC",
          decision: "merge",
          ruled_at: "2026-08-15T04:00:00Z",
          opposite_code: "sales_e2e_conflicta_day",
        },
      },
    });
    mockedGetMetric.mockRejectedValue(err);
    renderDetail({ pathname: "/detail/sales_e2e_conflictb_day" });

    // 友好引导而非裸「指标不存在」
    expect(await screen.findByText("该指标已因口径裁决作废")).toBeInTheDocument();
    expect(screen.queryByText("指标不存在")).not.toBeInTheDocument();
    // 展示原指标 + 裁决信息
    expect(screen.getByText(/原指标：/)).toBeInTheDocument();
    expect(screen.getAllByText("sales_e2e_conflictb_day").length).toBeGreaterThan(0);
    expect(screen.getByText(/相关冲突：CF-ABC/)).toBeInTheDocument();
    // 权威指标跳转按钮 → 点击后以新 code 重新拉取详情
    const jump = await screen.findByRole("button", { name: /sales_e2e_conflicta_day/ });
    fireEvent.click(jump);
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalledWith("sales_e2e_conflicta_day"));
  });
});
