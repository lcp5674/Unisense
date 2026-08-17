import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QualityCenter } from "../pages/QualityCenter";
import type { MetricResponse, QualityBenchmark, QualityEvent } from "../types";

vi.mock("../api", () => ({
  listQualityRules: vi.fn(),
  createQualityRule: vi.fn(),
  updateQualityRule: vi.fn(),
  deleteQualityRule: vi.fn(),
  listQualityEvents: vi.fn(),
  qualityEventAck: vi.fn(),
  qualityEventResolve: vi.fn(),
  qualityEventClose: vi.fn(),
  qualityEventDetect: vi.fn(),
  qualityEventConfirmRepair: vi.fn(),
  listBenchmarks: vi.fn(),
  importBenchmark: vi.fn(),
  bindBenchmark: vi.fn(),
  listReconciliationRecords: vi.fn(),
  runReconciliation: vi.fn(),
  confirmReconciliation: vi.fn(),
  listMetrics: vi.fn(),
}));

import {
  listQualityRules,
  listQualityEvents,
  listBenchmarks,
  qualityEventDetect,
  qualityEventConfirmRepair,
  bindBenchmark,
  listMetrics,
  listReconciliationRecords,
} from "../api";
const mockedListRules = vi.mocked(listQualityRules);
const mockedListEvents = vi.mocked(listQualityEvents);
const mockedListBenchmarks = vi.mocked(listBenchmarks);
const mockedDetect = vi.mocked(qualityEventDetect);
const mockedRepair = vi.mocked(qualityEventConfirmRepair);
const mockedBind = vi.mocked(bindBenchmark);
const mockedListMetrics = vi.mocked(listMetrics);
const mockedListReconciliations = vi.mocked(listReconciliationRecords);

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
  status: "PUBLISHED",
  owner_id: 1,
  backup_owner_id: null,
  approver_id: null,
  submitted_by: null,
  pii_flag: false,
  compliance_reviewed: true,
  term_id: null,
  effective_version: null,
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

const event: QualityEvent = {
  id: 10,
  metric_id: 1,
  level: "P1",
  rule_type: "ACCURACY",
  obs_value: 999,
  threshold: 100,
  status: "OPEN",
  created_at: "2026-08-10T10:00:00",
  ack_note: null,
  ack_by: null,
  ack_at: null,
  resolved_by: null,
  resolved_at: null,
  closed_by: null,
  closed_at: null,
  repair_suggestion: null,
};

const benchmark: QualityBenchmark = {
  id: 5,
  source_id: "finance_report",
  metric_code: "sales_gmv_day",
  bench_date: "2026-08-01",
  dims: null,
  bench_value: 1000000,
  provider: "财务部",
  tolerance_pct: 5,
  imported_by: 1,
  created_at: "2026-08-01T00:00:00",
};

function renderQuality() {
  return render(
    <MemoryRouter initialEntries={["/quality"]}>
      <QualityCenter />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedListRules.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 });
  mockedListEvents.mockResolvedValue({ items: [event], total: 1, page: 1, page_size: 20 });
  mockedListBenchmarks.mockResolvedValue({ items: [benchmark], total: 1, page: 1, page_size: 20 });
  mockedListMetrics.mockResolvedValue({ items: [metric], total: 1, page: 1, page_size: 100 });
  mockedListReconciliations.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 });
});

describe("QualityCenter 质量中心", () => {
  it("质量事件页展示事件列表与手动检测入口", async () => {
    renderQuality();
    fireEvent.click(screen.getByText("质量事件"));
    await waitFor(() => expect(screen.getByText("10")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /手动检测/ })).toBeInTheDocument();
    // 修复确认仅 OPEN 状态提供
    expect(screen.getByRole("button", { name: /修复确认/ })).toBeInTheDocument();
  });

  it("手动检测：命中返回事件时提示成功并刷新", async () => {
    mockedDetect.mockResolvedValue(event);
    renderQuality();
    fireEvent.click(screen.getByText("质量事件"));
    await waitFor(() => expect(screen.getByRole("button", { name: /手动检测/ })).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /手动检测/ }));
    await waitFor(() => expect(screen.getByText(/手动触发质量检测/)).toBeInTheDocument());

    // 选择指标 + 规则类型 + 输入观测值后提交（antd Select 在 selector 上 mouseDown 打开下拉）
    fireEvent.mouseDown(screen.getByText("选择指标"));
    await waitFor(() => expect(screen.getByText(/sales_gmv_day/)).toBeInTheDocument());
    fireEvent.click(screen.getByText(/sales_gmv_day/));

    fireEvent.mouseDown(screen.getByText("选择规则类型"));
    await waitFor(() =>
      expect(document.querySelectorAll(".ant-select-item-option[title='准确性']").length).toBeGreaterThan(0),
    );
    fireEvent.click(document.querySelector(".ant-select-item-option[title='准确性']") as HTMLElement);

    const obsInput = screen.getByLabelText("观测值").closest(".ant-input-number")?.querySelector("input") as HTMLInputElement;
    fireEvent.change(obsInput, { target: { value: "999" } });

    const detectDialog = screen.getByRole("dialog") as HTMLElement;
    fireEvent.click(within(detectDialog).getByRole("button", { name: /检\s*测/ }));
    await waitFor(() =>
      expect(mockedDetect).toHaveBeenCalledWith({
        metric_id: 1,
        rule_type: "ACCURACY",
        obs_value: 999,
        rule_mode: null,
      }),
    );
  });

  it("修复确认调用 qualityEventConfirmRepair", async () => {
    mockedRepair.mockResolvedValue(event);
    renderQuality();
    fireEvent.click(screen.getByText("质量事件"));
    await waitFor(() => expect(screen.getByRole("button", { name: /修复确认/ })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /修复确认/ }));
    await waitFor(() => expect(mockedRepair).toHaveBeenCalledWith(10));
  });

  it("基准库页提供绑定入口，提交调用 bindBenchmark", async () => {
    mockedBind.mockResolvedValue(benchmark);
    renderQuality();
    fireEvent.click(screen.getByText("基准库"));
    await waitFor(() => expect(screen.getByText("finance_report")).toBeInTheDocument());
    const row = screen.getByText("finance_report").closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: /绑定/ }));

    await waitFor(() => expect(screen.getByText(/绑定基准 #5/)).toBeInTheDocument());
    const dialog = screen.getByRole("dialog") as HTMLElement;
    const codeInput = within(dialog).getByLabelText("目标指标编码").closest("input") as HTMLInputElement;
    fireEvent.change(codeInput, { target: { value: "sales_gmv_d" } });
    fireEvent.click(within(dialog).getByRole("button", { name: /绑\s*定/ }));
    await waitFor(() =>
      expect(mockedBind).toHaveBeenCalledWith(5, {
        metric_code: "sales_gmv_d",
        tolerance_pct: null,
      }),
    );
  });

  it("基准对账：OK 状态渲染中文「正常」而非原始英文（跨服务枚举对齐）", async () => {
    mockedListReconciliations.mockResolvedValue({
      items: [
        {
          id: 1,
          benchmark_id: 5,
          metric_code: "sales_gmv_day",
          metric_value: 1000000,
          bench_value: 1000000,
          diff_pct: 0.5,
          window: "2026-07",
          status: "OK",
          owner_note: null,
          decision: null,
          confirmed_by: null,
          checked_at: null,
          created_at: "2026-08-01T00:00:00",
        },
      ],
      total: 1,
      page: 1,
      page_size: 50,
    });
    renderQuality();
    fireEvent.click(screen.getByText("基准对账"));
    await screen.findByText("正常");
    // 不应出现原始英文 OK 状态
    expect(screen.queryByText("OK")).toBeNull();
  });

  it("基准对账：APPROVED（维度对账）状态也有中文映射「已通过」", async () => {
    mockedListReconciliations.mockResolvedValue({
      items: [
        {
          id: 2,
          benchmark_id: 5,
          metric_code: "sales_gmv_day",
          metric_value: 1000000,
          bench_value: 990000,
          diff_pct: 1.0,
          window: "2026-07",
          status: "APPROVED",
          owner_note: null,
          decision: "reasonable",
          confirmed_by: 1,
          checked_at: "2026-08-01T00:00:00",
          created_at: "2026-08-01T00:00:00",
        },
      ],
      total: 1,
      page: 1,
      page_size: 50,
    });
    renderQuality();
    fireEvent.click(screen.getByText("基准对账"));
    await screen.findByText("已通过");
    expect(screen.queryByText("APPROVED")).toBeNull();
  });

  it("提供统一的返回按钮（返回上一入口）", async () => {
    renderQuality();
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/lineage", "/quality"]}>
        <Routes>
          <Route path="/lineage" element={<div>lineage-page</div>} />
          <Route path="/quality" element={<QualityCenter />} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("lineage-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    render(
      <MemoryRouter initialEntries={["/quality"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/quality" element={<QualityCenter />} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("dashboard-page");
  });
});
