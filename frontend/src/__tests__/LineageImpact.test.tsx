import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { LineageImpact } from "../pages/metric/LineageImpact";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    lineageImpact: vi.fn(),
  };
});

import { lineageImpact } from "../api";

const mockedImpact = vi.mocked(lineageImpact);

describe("LineageImpact", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedImpact.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 });
  });

  it("查询时以 metric:{code} 前缀节点调用（对齐血缘图节点约定，避免裸 code 查空）", async () => {
    render(<LineageImpact metricCode="sales_e2e_gmv_day" />);
    await waitFor(() => {
      expect(mockedImpact).toHaveBeenCalledWith(
        expect.objectContaining({ node: "metric:sales_e2e_gmv_day", direction: "downstream" }),
      );
    });
  });

  it("切换上游方向时同样携带 metric: 前缀", async () => {
    const { container } = render(<LineageImpact metricCode="sales_e2e_gmv_day" />);
    await waitFor(() => expect(mockedImpact).toHaveBeenCalled());
    // 点击「上游依赖」分段
    const upstreamSeg = Array.from(container.querySelectorAll(".ant-segmented-item")).find(
      (el) => el.textContent?.includes("上游"),
    );
    (upstreamSeg as HTMLElement | undefined)?.click();
    await waitFor(() => {
      expect(mockedImpact).toHaveBeenLastCalledWith(
        expect.objectContaining({ node: "metric:sales_e2e_gmv_day", direction: "upstream" }),
      );
    });
  });
});
