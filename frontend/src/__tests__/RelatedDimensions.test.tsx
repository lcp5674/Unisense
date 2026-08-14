import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { RelatedDimensions } from "../pages/metric/RelatedDimensions";

vi.mock("../api", () => ({
  listMetricDimensions: vi.fn(),
}));

import { listMetricDimensions } from "../api";

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listMetricDimensions).mockResolvedValue({
    items: [
      { id: 1, metric_id: 7, dim_code: "sales_channel", role: "PARTITION", default_member: "all" },
      { id: 2, metric_id: 7, dim_code: "sales_region", role: "FILTER", default_member: null },
    ],
    total: 2,
  });
});

describe("RelatedDimensions（指标详情-关联维度）", () => {
  it("挂载即调用 listMetricDimensions 并渲染绑定维度/角色/默认成员", async () => {
    render(
      <MemoryRouter>
        <RelatedDimensions metricId={7} />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(listMetricDimensions).toHaveBeenCalledWith(7);
    });
    expect(await screen.findByText("sales_channel")).toBeInTheDocument();
    expect(screen.getByText("PARTITION 分区")).toBeInTheDocument();
    expect(screen.getByText("sales_region")).toBeInTheDocument();
    expect(screen.getByText("FILTER 过滤")).toBeInTheDocument();
    // 默认成员有值显示编码，无值显示占位
    expect(screen.getByText("all")).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("加载失败时展示降级文案而非崩溃", async () => {
    vi.mocked(listMetricDimensions).mockRejectedValue(new Error("boom"));
    render(
      <MemoryRouter>
        <RelatedDimensions metricId={7} />
      </MemoryRouter>,
    );
    expect(await screen.findByText(/该指标暂未绑定维度/)).toBeInTheDocument();
  });
});
