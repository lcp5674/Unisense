import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { MetricReview } from "../pages/MetricReview";
import type { MetricResponse } from "../types";

vi.mock("../api", () => {
  class UnisenseApiError extends Error {
    code: string;
    traceId: string;
    status: number;
    detail?: Record<string, unknown> | null;
    constructor(message: string, code: string, status: number, traceId: string, detail?: Record<string, unknown> | null) {
      super(message);
      this.name = "UnisenseApiError";
      this.code = code;
      this.status = status;
      this.traceId = traceId;
      this.detail = detail;
    }
    get codeZh(): string {
      return this.code;
    }
  }
  return {
    listMetrics: vi.fn(),
    reviewMetric: vi.fn(),
    fetchCurrentUser: vi.fn(),
    listUsers: vi.fn(),
    listDomainTree: vi.fn(),
    batchApproveMetrics: vi.fn(),
    batchRejectMetrics: vi.fn(),
    UnisenseApiError,
  };
});

import { fetchCurrentUser, listDomainTree, listMetrics, listUsers } from "../api";
const mockedList = vi.mocked(listMetrics);

const metric: MetricResponse = {
  id: 1,
  metric_code: "sales_gmv_day",
  name: "日销售额",
  domain: "finance",
  type: "atomic",
  granularity: "day",
  unit: "元",
  currency: null,
  aggregation: "SUM",
  time_semantics: "event",
  freshness: "T+1",
  sla: null,
  dw_layer: "DWS",
  metric_tier: "T1",
  serving_mode: "api",
  additivity: "additive",
  non_additive_dimensions: null,
  definition_json: {},
  version: 1,
  row_version: 0,
  status: "REVIEW",
  owner_id: 1,
  backup_owner_id: null,
  approver_id: null,
  submitted_by: null,
  pii_flag: false,
  compliance_reviewed: true,
  effective_version: null,
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
};

function renderReview() {
  return render(
    <MemoryRouter initialEntries={["/metrics/review"]}>
      <MetricReview />
    </MemoryRouter>,
  );
}

describe("MetricReview 指标审批", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedList.mockResolvedValue({ items: [metric], total: 1, page: 1, page_size: 100 });
    // 审批页需当前用户身份 + 用户映射（默认平台管理员，可评审任意指标）
    vi.mocked(fetchCurrentUser).mockResolvedValue({
      id: 1,
      username: "admin",
      display_name: "管理员",
      role: "platform_admin",
      domain: null,
      org_id: 1,
    });
    vi.mocked(listUsers).mockResolvedValue([]);
    vi.mocked(listDomainTree).mockResolvedValue([]);
  });

  it("加载并展示待审核指标", async () => {
    renderReview();
    await screen.findByText("sales_gmv_day");
    // 分页参数（默认第 1 页、每页 20 条）
    expect(mockedList).toHaveBeenCalledWith({
      status: "REVIEW",
      page: 1,
      page_size: 20,
      sort_by: "updated_at",
      sort_order: "asc",
    });
  });

  it("提供统一的返回按钮（返回上一入口）", async () => {
    renderReview();
    await screen.findByText("sales_gmv_day");
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/dashboard", "/metrics/review"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/metrics/review" element={<MetricReview />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("sales_gmv_day");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("dashboard-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    render(
      <MemoryRouter initialEntries={["/metrics/review"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/metrics/review" element={<MetricReview />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("sales_gmv_day");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await waitFor(() => expect(screen.getByText("dashboard-page")).toBeInTheDocument());
  });

  it("PII 待复核指标：禁用『通过』并展示待复核标签（对齐后端 COMPLIANCE_BLOCKED 拦截）", async () => {
    const piiPending: MetricResponse = {
      ...metric,
      metric_code: "pii_metric_day",
      pii_flag: true,
      compliance_reviewed: false,
    };
    mockedList.mockResolvedValue({ items: [piiPending], total: 1, page: 1, page_size: 100 });
    renderReview();
    await screen.findByText("pii_metric_day");
    // 「通过」按钮应禁用
    const approveBtn = screen.getAllByRole("button", { name: /通\s*过/ })[0];
    expect((approveBtn as HTMLButtonElement).disabled).toBe(true);
    // PII 待复核标签可见（PII 列，与指标目录一致）
    expect(screen.getAllByText(/PII 待复核/).length).toBeGreaterThan(0);
  });

  it("「我审过的」视图：按 approver_id 查询、无操作按钮、无批量按钮", async () => {
    const reviewed: MetricResponse = {
      ...metric,
      status: "PUBLISHED",
      approver_id: 1,
    };
    mockedList.mockResolvedValue({ items: [reviewed], total: 1, page: 1, page_size: 20 });
    renderReview();
    // 切换到「我审过的」
    fireEvent.click(await screen.findByRole("radio", { name: /我审过的/ }));
    await screen.findByText("sales_gmv_day");
    // 携带 approver_id 查询
    expect(mockedList).toHaveBeenCalledWith(
      expect.objectContaining({ approver_id: 1 }),
    );
    // 无「通过」操作、无批量通过按钮
    expect(screen.queryAllByRole("button", { name: /通\s*过/ }).length).toBe(0);
    // 「已处理」标记可见
    expect(screen.getByText("已处理")).toBeTruthy();
  });

  it("深页空结果自动回退上一页（审批后列表缩短不致空页）", async () => {
    // 第 1 页有数据、共 200 条（多页可分页）；第 2 页返回空（total>0 → 触发回退到第 1 页重查）
    mockedList.mockImplementation(async (params) => {
      if (params.page === 2) return { items: [], total: 1, page: 2, page_size: params.page_size };
      return { items: [metric], total: 200, page: 1, page_size: params.page_size };
    });
    renderReview();
    await screen.findByText("sales_gmv_day");
    // 翻到第 2 页 → 第 2 页为空（total>0）→ 自动回退第 1 页重查
    fireEvent.click(document.querySelector(".ant-pagination-item-2") as HTMLElement);
    await waitFor(() => {
      const pages = mockedList.mock.calls.map((c) => c[0]?.page);
      expect(pages).toContain(2); // 发起过第 2 页查询
      expect(pages.filter((p) => p === 1).length).toBeGreaterThanOrEqual(2); // 回退后再次查第 1 页
    });
    // 回退后列表仍显示数据（非空页）
    expect(await screen.findByText("sales_gmv_day")).toBeTruthy();
  });
});
