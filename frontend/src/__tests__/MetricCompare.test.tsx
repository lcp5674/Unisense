import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { message } from "antd";
import { MetricCompare } from "../pages/MetricCompare";
import type { MetricCompareMatrixResult, MetricListResponse, MetricResponse } from "../types";

vi.mock("../api", () => ({
  listMetrics: vi.fn(),
  compareMetricsMatrix: vi.fn(),
}));

import { compareMetricsMatrix, listMetrics } from "../api";
const mockedMatrix = vi.mocked(compareMetricsMatrix);
const mockedList = vi.mocked(listMetrics);

const candidates: MetricResponse[] = [
  {
    metric_code: "sales_gmv_day",
    name: "每日 GMV",
    granularity: "day",
    unit: "元",
    currency: "CNY",
    aggregation: "SUM",
    time_semantics: "PERIOD",
    status: "PUBLISHED",
    version: 1,
    owner_id: 1,
    type: "atomic",
    metric_tier: "T3",
    dw_layer: "DWD",
    serving_mode: "BATCH_ONLY",
    freshness: "T1",
    additivity: "ADDITIVE",
    definition_json: {},
    created_at: "",
    updated_at: "",
  } as MetricResponse,
  {
    metric_code: "sales_gmv_d",
    name: "GMV 明细",
    granularity: "d",
    unit: "元",
    currency: "CNY",
    aggregation: "SUM",
    time_semantics: "PERIOD",
    status: "PUBLISHED",
    version: 1,
    owner_id: 1,
    type: "atomic",
    metric_tier: "T3",
    dw_layer: "DWD",
    serving_mode: "BATCH_ONLY",
    freshness: "T1",
    additivity: "ADDITIVE",
    definition_json: {},
    created_at: "",
    updated_at: "",
  } as MetricResponse,
];

const listResponse: MetricListResponse = {
  total: 2,
  page: 1,
  page_size: 100,
  items: candidates,
};

const matrixResult: MetricCompareMatrixResult = {
  metrics: ["sales_gmv_day", "sales_gmv_d"],
  fields: {
    granularity: {
      values: { sales_gmv_day: "day", sales_gmv_d: "d" },
      difference_level: "partial",
    },
    unit: {
      values: { sales_gmv_day: "元", sales_gmv_d: "元" },
      difference_level: "all_identical",
    },
    definition: {
      values: {
        sales_gmv_day: { expression: "sum(gmv)" },
        sales_gmv_d: { expression: "sum(amount)" },
      },
      difference_level: "all_different",
    },
    dependencies: {
      values: { sales_gmv_day: ["ods_order"], sales_gmv_d: ["ods_order"] },
      intersection: ["ods_order"],
      only: { sales_gmv_day: [], sales_gmv_d: [] },
      difference_level: "all_identical",
    },
  },
};

function renderCompare(entry = "/compare?codes=sales_gmv_day,sales_gmv_d") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <MetricCompare />
    </MemoryRouter>,
  );
}

describe("MetricCompare 指标对比", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedList.mockResolvedValue(listResponse);
    mockedMatrix.mockResolvedValue(matrixResult);
  });

  it("新入口 codes=a,b：加载候选并自动发起矩阵对比", async () => {
    renderCompare();
    await waitFor(() =>
      expect(mockedMatrix).toHaveBeenCalledWith(["sales_gmv_day", "sales_gmv_d"]),
    );
    // 矩阵表头展示两个指标编码
    expect(await screen.findAllByText("sales_gmv_day")).toBeTruthy();
    expect((await screen.findAllByText("sales_gmv_d")).length).toBeGreaterThan(0);
  });

  it("兼容旧入口 a=/b=（两两）", async () => {
    renderCompare("/compare?a=sales_gmv_day&b=sales_gmv_d");
    await waitFor(() =>
      expect(mockedMatrix).toHaveBeenCalledWith(["sales_gmv_day", "sales_gmv_d"]),
    );
  });

  it("无 URL 参数：显示内置选择器引导（不自动对比）", async () => {
    renderCompare("/compare");
    await waitFor(() => expect(mockedList).toHaveBeenCalled());
    expect(mockedMatrix).not.toHaveBeenCalled();
    // 引导文案（Empty 描述含「从上方选择」）
    expect(await screen.findByText(/从上方选择至少 2 个指标/)).toBeTruthy();
  });

  it("URL 直达超过 6 个：截断到前 6 个并提示，不向后端发超限请求", async () => {
    const messageSpy = vi
      .spyOn(message, "warning")
      .mockImplementation(() => undefined as never);
    renderCompare("/compare?codes=c1,c2,c3,c4,c5,c6,c7");
    await waitFor(() => {
      const calls = mockedMatrix.mock.calls.map((c) => c[0]);
      expect(calls.length).toBeGreaterThan(0);
      expect(calls.every((codes) => codes.length <= 6)).toBe(true);
    });
    // 请求只带前 6 个（截断后的 URL 二次触发也应是 6 个）
    expect(mockedMatrix).toHaveBeenCalledWith(["c1", "c2", "c3", "c4", "c5", "c6"]);
    // 友好提示而非 422
    expect(messageSpy).toHaveBeenCalledWith(expect.stringContaining("最多支持 6"));
    messageSpy.mockRestore();
  });

  it("已选满 6 个显示上限提示", async () => {
    renderCompare("/compare?codes=c1,c2,c3,c4,c5,c6");
    expect(await screen.findByText(/已达上限 6 个/)).toBeTruthy();
  });

  it("手动选择 2 个指标触发矩阵对比并更新 URL", async () => {
    const user = userEvent.setup();
    renderCompare("/compare");
    const select = await screen.findByRole("combobox");
    await user.click(select);
    await user.click(await screen.findByText("sales_gmv_day · 每日 GMV"));
    await user.click(await screen.findByText("sales_gmv_d · GMV 明细"));
    await waitFor(() =>
      expect(mockedMatrix).toHaveBeenCalledWith(["sales_gmv_day", "sales_gmv_d"]),
    );
  });

  it("提供统一的返回按钮（返回上一入口）", async () => {
    renderCompare();
    await waitFor(() => expect(mockedMatrix).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter
        initialEntries={["/catalog", "/compare?codes=sales_gmv_day,sales_gmv_d"]}
      >
        <Routes>
          <Route path="/catalog" element={<div>catalog-page</div>} />
          <Route path="/compare" element={<MetricCompare />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(mockedMatrix).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("catalog-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    render(
      <MemoryRouter initialEntries={["/compare?codes=sales_gmv_day,sales_gmv_d"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/compare" element={<MetricCompare />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(mockedMatrix).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("dashboard-page");
  });
});
