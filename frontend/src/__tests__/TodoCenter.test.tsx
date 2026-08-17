import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { TodoCenter } from "../pages/TodoCenter";
import type { MetricListResponse, ConflictListResponse, QualityEvent } from "../types";

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
  }
  return {
    listConflicts: vi.fn(),
    listMetrics: vi.fn(),
    listQualityEvents: vi.fn(),
    fetchCurrentUser: vi.fn(),
    UnisenseApiError,
  };
});

vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: vi.fn() }),
}));

import { fetchCurrentUser, listConflicts, listMetrics, listQualityEvents } from "../api";

const mockedConflicts = vi.mocked(listConflicts);
const mockedMetrics = vi.mocked(listMetrics);
const mockedQuality = vi.mocked(listQualityEvents);
const mockedCurrentUser = vi.mocked(fetchCurrentUser);

function PathSpy() {
  const loc = useLocation();
  return <div data-testid="path">{loc.pathname}</div>;
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/todo"]}>
      <Routes>
        <Route path="/todo" element={<TodoCenter />} />
        <Route path="*" element={<PathSpy />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedCurrentUser.mockResolvedValue({
    id: 1,
    username: "alice",
    display_name: "Alice",
    role: "domain_admin",
    domain: "finance",
    org_id: 1,
  });
  mockedConflicts.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 });
  mockedQuality.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 });
  mockedMetrics.mockImplementation((params) => {
    if (params?.status === "REVIEW") {
      return Promise.resolve({
        items: [{ id: 1, metric_code: "GMV_REV", name: "待审核指标", domain: "finance", status: "REVIEW" }],
        total: 1,
        page: 1,
        page_size: 50,
      } as MetricListResponse);
    }
    if (params?.status === "DATA_SOURCE_DROPPED") {
      return Promise.resolve({
        items: [
          {
            id: 3,
            metric_code: "GMV_DSD",
            name: "源下线指标",
            domain: "finance",
            status: "DATA_SOURCE_DROPPED",
          },
        ],
        total: 1,
        page: 1,
        page_size: 50,
      } as MetricListResponse);
    }
    return Promise.resolve({
      items: [{ id: 2, metric_code: "GMV_DRAFT", name: "草稿指标", domain: "finance", status: "DRAFT" }],
      total: 1,
      page: 1,
      page_size: 50,
    } as MetricListResponse);
  });
});

describe("待办中心 - 聚合与跳转", () => {
  it("聚合四类待办：冲突/草稿/待审核/质量告警", async () => {
    mockedConflicts.mockResolvedValue({
      items: [
        {
          conflict_id: "C-1",
          candidate_metric_code: "A",
          existing_metric_code: "B",
          type: "same_name_diff_def",
          status: "OPEN",
        } as unknown as ConflictListResponse["items"][number],
      ],
      total: 1,
      page: 1,
      page_size: 50,
    } as ConflictListResponse);
    mockedQuality.mockResolvedValue({
      items: [{ id: 7, metric_id: 3, level: "P1", rule_type: "COMPLETENESS", status: "OPEN" } as QualityEvent],
      total: 1,
      page: 1,
      page_size: 50,
    } as { items: QualityEvent[]; total: number; page: number; page_size: number });

    renderPage();

    await screen.findByText(/冲突待仲裁/);
    await screen.findByText(/草稿待完善/);
    await screen.findByText(/待审核指标/);
    await screen.findByText(/质量告警待处理/);
  });

  it("展示各类待办数量汇总", async () => {
    mockedConflicts.mockResolvedValue({
      items: [
        { conflict_id: "C-1", candidate_metric_code: "A", existing_metric_code: "B", type: "same_name_diff_def", status: "OPEN" } as unknown as ConflictListResponse["items"][number],
        { conflict_id: "C-2", candidate_metric_code: "C", existing_metric_code: "D", type: "pii", status: "OPEN" } as unknown as ConflictListResponse["items"][number],
      ],
      total: 2,
      page: 1,
      page_size: 50,
    } as ConflictListResponse);
    mockedQuality.mockResolvedValue({
      items: [{ id: 7, metric_id: 3, level: "P1", rule_type: "COMPLETENESS", status: "OPEN" } as QualityEvent],
      total: 1,
      page: 1,
      page_size: 50,
    } as { items: QualityEvent[]; total: number; page: number; page_size: number });

    renderPage();

    await waitFor(() => expect(screen.getByTestId("todo-count-conflict").textContent).toContain("2"));
    expect(screen.getByTestId("todo-count-draft").textContent).toContain("1");
    expect(screen.getByTestId("todo-count-review").textContent).toContain("1");
    expect(screen.getByTestId("todo-count-quality").textContent).toContain("1");
  });

  it("冲突项点「去仲裁」跳转冲突仲裁页", async () => {
    mockedConflicts.mockResolvedValue({
      items: [
        { conflict_id: "C-1", candidate_metric_code: "A", existing_metric_code: "B", type: "same_name_diff_def", status: "OPEN" } as unknown as ConflictListResponse["items"][number],
      ],
      total: 1,
      page: 1,
      page_size: 50,
    } as ConflictListResponse);

    renderPage();
    await screen.findByText(/冲突待仲裁/);
    fireEvent.click(screen.getByRole("button", { name: /去仲裁/ }));
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/review"));
  });

  it("草稿项点「查看」跳转指标详情", async () => {
    renderPage();
    await screen.findByText(/草稿待完善/);
    fireEvent.click(screen.getByRole("button", { name: /查看/ }));
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/detail/GMV_DRAFT"));
  });

  it("待审核项点「去审核」跳转指标审批页", async () => {
    renderPage();
    await screen.findByText(/待审核指标/);
    fireEvent.click(screen.getByRole("button", { name: /去审核/ }));
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/metrics/review"));
  });

  it("质量告警项点「去处理」跳转质量中心", async () => {
    mockedQuality.mockResolvedValue({
      items: [{ id: 7, metric_id: 3, level: "P1", rule_type: "COMPLETENESS", status: "OPEN" } as QualityEvent],
      total: 1,
      page: 1,
      page_size: 50,
    } as { items: QualityEvent[]; total: number; page: number; page_size: number });

    renderPage();
    await screen.findByText(/质量告警待处理/);
    fireEvent.click(screen.getByRole("button", { name: /去处理/ }));
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/quality"));
  });

  it("点击草稿行（item body）触发整行跳转指标详情", async () => {
    renderPage();
    await screen.findByText(/草稿待完善/);
    const item = await screen.findByTestId("todo-item-draft");
    fireEvent.click(item);
    await waitFor(() =>
      expect(screen.getByTestId("path").textContent).toBe("/detail/GMV_DRAFT"),
    );
  });

  it("点击冲突行（item body）触发整行跳转冲突仲裁页", async () => {
    mockedConflicts.mockResolvedValue({
      items: [
        { conflict_id: "C-1", candidate_metric_code: "A", existing_metric_code: "B", type: "same_name_diff_def", status: "OPEN" } as unknown as ConflictListResponse["items"][number],
      ],
      total: 1,
      page: 1,
      page_size: 50,
    } as ConflictListResponse);

    renderPage();
    await screen.findByText(/冲突待仲裁/);
    const item = await screen.findByTestId("todo-item-conflict");
    fireEvent.click(item);
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/review"));
  });

  it("展示数据源下线待办并按当前用户 Owner 维度查询", async () => {
    renderPage();
    await screen.findByText(/源下线指标/);
    expect(mockedCurrentUser).toHaveBeenCalled();
    // DSD 查询带当前用户 owner_id
    const dsdCall = mockedMetrics.mock.calls.find(
      (params) => params?.[0]?.status === "DATA_SOURCE_DROPPED",
    );
    expect(dsdCall?.[0]?.owner_id).toBe(1);
    expect(screen.getByTestId("todo-count-dsd").textContent).toContain("1");
  });

  it("数据源下线项点「去恢复」跳转指标详情", async () => {
    renderPage();
    await screen.findByText(/源下线指标/);
    fireEvent.click(screen.getByRole("button", { name: /去恢复/ }));
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/detail/GMV_DSD"));
  });
});
