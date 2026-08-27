import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ApprovalCenter } from "../pages/ApprovalCenter";

vi.mock("../api", () => {
  class UnisenseApiError extends Error {
    code: string;
    traceId: string;
    status: number;
    detail?: Record<string, unknown> | null;
    constructor(
      message: string,
      code: string,
      status: number,
      traceId: string,
      detail?: Record<string, unknown> | null,
    ) {
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
    // 指标审批 MetricReview
    listMetrics: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 }),
    reviewMetric: vi.fn(),
    approveMetric: vi.fn(),
    batchApproveMetrics: vi.fn(),
    batchRejectMetrics: vi.fn(),
    listVersions: vi.fn(),
    // 主数据审批 MasterDataReview
    listDimensions: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 }),
    listMeasureCatalogs: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 }),
    listTerms: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 }),
    approveDimension: vi.fn(),
    rejectDimension: vi.fn(),
    approveMeasureCatalog: vi.fn(),
    rejectMeasureCatalog: vi.fn(),
    approveTerm: vi.fn(),
    rejectTerm: vi.fn(),
    // 冲突仲裁 ReviewWorkbench
    listConflicts: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 }),
    listConflictRulings: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 }),
    arbitrateConflict: vi.fn(),
    closeConflict: vi.fn(),
    compareMetrics: vi.fn(),
    escalateConflict: vi.fn(),
    reopenConflict: vi.fn(),
    // 共享
    fetchCurrentUser: vi
      .fn()
      .mockResolvedValue({
        id: 1,
        username: "tester",
        display_name: "测试评审",
        role: "reviewer",
        domain: "finance",
        must_change_password: false,
        roles: ["reviewer"],
        permissions: [],
      }),
    listUsers: vi.fn().mockResolvedValue([]),
    listDomainTree: vi.fn().mockResolvedValue([]),
    UnisenseApiError,
  };
});

vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: vi.fn() }),
}));

function renderPage(initial = "/approval") {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <ApprovalCenter />
    </MemoryRouter>,
  );
}

describe("ApprovalCenter 统一审批中心", () => {
  it("渲染三个审批 Tab，默认激活指标审批（内嵌 MetricReview）", async () => {
    renderPage();
    expect(screen.getByRole("tab", { name: "指标审批" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "主数据审批" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "冲突仲裁" })).toBeInTheDocument();
    // 默认激活指标审批：MetricReview 的「待我审」Segmented 出现
    await waitFor(() => expect(screen.getByText("待我审")).toBeInTheDocument());
  });

  it("URL ?tab=conflict 深链直达冲突仲裁（内嵌 ReviewWorkbench）", async () => {
    renderPage("/approval?tab=conflict");
    await waitFor(() => expect(screen.getByText("审核工作台（冲突仲裁）")).toBeInTheDocument());
  });

  it("切换 Tab 挂载对应工作台", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("待我审")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: "冲突仲裁" }));
    await waitFor(() => expect(screen.getByText("审核工作台（冲突仲裁）")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: "主数据审批" }));
    // antd Tabs 保留已挂载 pane：指标与主数据两个工作台同时存在（各有一个「待我审」Segmented）
    await waitFor(() => expect(screen.getAllByText("待我审").length).toBe(2));
  });
});
