import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { MetricCompare } from "../pages/MetricCompare";
import type { MetricCompareResult } from "../types";

vi.mock("../api", () => ({
  compareMetrics: vi.fn(),
}));

import { compareMetrics } from "../api";
const mockedCompare = vi.mocked(compareMetrics);

const compareResult: MetricCompareResult = {
  metrics: ["sales_gmv_day", "sales_gmv_d"],
  fields: {
    granularity: { a: "day", b: "d", difference_level: "similar" },
    unit: { a: "元", b: "元", difference_level: "identical" },
    definition: { a: { expression: "sum(gmv)" }, b: { expression: "sum(amount)" }, difference_level: "different" },
    dependencies: { a: ["ods_order"], b: ["ods_order"], intersection: ["ods_order"], only_a: [], only_b: [], difference_level: "identical" },
  },
};

function renderCompare() {
  return render(
    <MemoryRouter initialEntries={["/compare?a=sales_gmv_day&b=sales_gmv_d"]}>
      <MetricCompare />
    </MemoryRouter>,
  );
}

describe("MetricCompare 指标对比", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedCompare.mockResolvedValue(compareResult);
  });

  it("加载并展示对比结果", async () => {
    renderCompare();
    await waitFor(() => expect(mockedCompare).toHaveBeenCalledWith("sales_gmv_day", "sales_gmv_d"));
    expect((await screen.findAllByText("sales_gmv_day")).length).toBeGreaterThan(0);
  });

  it("提供统一的返回按钮（返回上一入口）", async () => {
    renderCompare();
    await waitFor(() => expect(mockedCompare).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/catalog", "/compare?a=sales_gmv_day&b=sales_gmv_d"]}>
        <Routes>
          <Route path="/catalog" element={<div>catalog-page</div>} />
          <Route path="/compare" element={<MetricCompare />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(mockedCompare).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("catalog-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    render(
      <MemoryRouter initialEntries={["/compare?a=sales_gmv_day&b=sales_gmv_d"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/compare" element={<MetricCompare />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(mockedCompare).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("dashboard-page");
  });
});
