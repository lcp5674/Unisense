import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, waitFor, screen, fireEvent } from "@testing-library/react";
import { LineageImpact } from "../pages/metric/LineageImpact";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    lineageImpact: vi.fn(),
    lineageImpactPreview: vi.fn(),
  };
});

// G6 图谱渲染 mock（对齐 AssetGraph.test.tsx 模式）：图实例复用，真实数据经 setData 传入
const { graphMock } = vi.hoisted(() => ({
  graphMock: {
    destroyed: false,
    on: vi.fn(),
    render: vi.fn().mockResolvedValue(undefined),
    destroy: vi.fn(),
    setData: vi.fn(),
    getNodeData: vi.fn<() => Array<{ id: string; data?: Record<string, unknown> }>>(() => []),
    getNeighborNodesData: vi.fn(() => []),
    setElementState: vi.fn(),
    focusElement: vi.fn(),
    getZoom: vi.fn(() => 1),
  },
}));
vi.mock("@antv/g6", () => ({
  Graph: vi.fn(() => graphMock),
}));

import { Graph } from "@antv/g6";
import { lineageImpact, lineageImpactPreview } from "../api";
import type { AssetGraphEdge, AssetGraphNode } from "../components/assetmap/AssetGraph";

const mockedImpact = vi.mocked(lineageImpact);
const mockedPreview = vi.mocked(lineageImpactPreview);

function lastGraphData(): {
  nodes: Array<{ id: string; data?: AssetGraphNode }>;
  edges: AssetGraphEdge[];
} {
  const dataCalls = graphMock.setData.mock.calls as Array<
    [{ nodes: Array<{ id: string; data?: AssetGraphNode }>; edges: AssetGraphEdge[] }]
  >;
  if (dataCalls.length > 0) return dataCalls[dataCalls.length - 1][0];
  const ctorCalls = vi.mocked(Graph).mock.calls;
  return ctorCalls[ctorCalls.length - 1][0].data as {
    nodes: Array<{ id: string; data?: AssetGraphNode }>;
    edges: AssetGraphEdge[];
  };
}

describe("LineageImpact", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedImpact.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 });
    vi.mocked(graphMock.render).mockImplementation(() => Promise.resolve(undefined));
  });

  it("查询时以 metric:{code} 前缀节点调用（对齐血缘图节点约定，避免裸 code 查空）", async () => {
    render(<LineageImpact metricCode="outp_e2e_fee_day" />);
    await waitFor(() => {
      expect(mockedImpact).toHaveBeenCalledWith(
        expect.objectContaining({ node: "metric:outp_e2e_fee_day", direction: "downstream" }),
      );
    });
  });

  it("切换上游方向时同样携带 metric: 前缀", async () => {
    const { container } = render(<LineageImpact metricCode="outp_e2e_fee_day" />);
    await waitFor(() => expect(mockedImpact).toHaveBeenCalled());
    // 点击「上游依赖」分段
    const upstreamSeg = Array.from(container.querySelectorAll(".ant-segmented-item")).find(
      (el) => el.textContent?.includes("上游"),
    );
    (upstreamSeg as HTMLElement | undefined)?.click();
    await waitFor(() => {
      expect(mockedImpact).toHaveBeenLastCalledWith(
        expect.objectContaining({ node: "metric:outp_e2e_fee_day", direction: "upstream" }),
      );
    });
  });

  it("有血缘数据时以图谱展示（AssetGraph 渲染节点/边，边明细折叠为辅）", async () => {
    mockedImpact.mockResolvedValue({
      items: [
        {
          id: 1,
          source_node: "table:ods_orders",
          target_node: "metric:outp_e2e_fee_day",
          edge_type: "METRIC_DERIVES",
          granularity: "day",
          confidence: 0.9,
          provenance: "sqlglot",
        },
        {
          id: 2,
          source_node: "metric:outp_e2e_fee_day",
          target_node: "table:ads_sales_gmv",
          edge_type: "METRIC_DERIVES",
          granularity: "day",
          confidence: 0.95,
          provenance: "sqlglot",
        },
      ],
      total: 2,
      page: 1,
      page_size: 50,
      nodes: [
        { id: "metric:outp_e2e_fee_day", type: "metric", label: "outp_e2e_fee_day", domain: "outpatient" },
        { id: "table:ods_orders", type: "table", label: "ods_orders", entity_id: 3 },
        { id: "table:ads_sales_gmv", type: "table", label: "ads_sales_gmv", entity_id: 5 },
      ],
    });
    render(<LineageImpact metricCode="outp_e2e_fee_day" />);
    // 图谱渲染：G6 Graph 实例化 + 数据含 3 业务节点 2 边（表→指标→表）；
    // lanes 泳道模式会附加 __lane_*__ 隐藏锚点节点，断言时过滤
    await waitFor(() => expect(Graph).toHaveBeenCalled());
    const data = lastGraphData();
    const realNodes = data.nodes.filter((n) => !(n.data as AssetGraphNode | undefined)?.anchor);
    expect(realNodes).toHaveLength(3);
    expect(realNodes.map((n) => n.id).sort()).toEqual([
      "metric:outp_e2e_fee_day",
      "table:ads_sales_gmv",
      "table:ods_orders",
    ]);
    expect(data.edges.filter((e) => !(e.data as { anchorEdge?: boolean } | undefined)?.anchorEdge)).toHaveLength(2);
    // 节点元数据已合并（table 节点带 entity_id，供下钻）
    const tableNode = data.nodes.find((n) => n.id === "table:ads_sales_gmv");
    expect((tableNode?.data as AssetGraphNode | undefined)?.entity_id).toBe(5);
    // 边明细折叠面板存在
    expect(screen.getByText(/边明细/)).toBeTruthy();
  });

  it("无血缘数据时展示空态而非图谱", async () => {
    render(<LineageImpact metricCode="outp_e2e_fee_day" />);
    await waitFor(() => expect(mockedImpact).toHaveBeenCalled());
    await waitFor(() => {
      expect(Graph).not.toHaveBeenCalled();
      expect(screen.getByText(/暂无血缘关系/)).toBeTruthy();
    });
  });

  it("切换到「双向」方向时携带 direction=both", async () => {
    const { container } = render(<LineageImpact metricCode="outp_e2e_fee_day" />);
    await waitFor(() => expect(mockedImpact).toHaveBeenCalled());
    const bothSeg = Array.from(container.querySelectorAll(".ant-segmented-item")).find(
      (el) => el.textContent?.includes("双向"),
    );
    (bothSeg as HTMLElement | undefined)?.click();
    await waitFor(() => {
      expect(mockedImpact).toHaveBeenLastCalledWith(
        expect.objectContaining({ node: "metric:outp_e2e_fee_day", direction: "both" }),
      );
    });
  });

  it("调节跳数（max_hops）后重新查询携带新跳数", async () => {
    const { container } = render(<LineageImpact metricCode="outp_e2e_fee_day" />);
    await waitFor(() =>
      expect(mockedImpact).toHaveBeenLastCalledWith(expect.objectContaining({ max_hops: 5 })),
    );
    // 打开跳数下拉选择「3 跳」（等 load 完成后 loading=false，Select 可交互）
    const hopSel = container.querySelector(".ant-select .ant-select-selector") as HTMLElement;
    fireEvent.mouseDown(hopSel);
    await waitFor(() => {
      const opt = Array.from(document.querySelectorAll(".ant-select-item-option")).find(
        (el) => el.textContent?.includes("3 跳"),
      );
      expect(opt).toBeTruthy();
      (opt as HTMLElement).click();
    });
    await waitFor(() =>
      expect(mockedImpact).toHaveBeenLastCalledWith(expect.objectContaining({ max_hops: 3 })),
    );
  });

  it("点击「变更影响预览」调用 lineageImpactPreview 并展示风险摘要", async () => {
    mockedPreview.mockResolvedValue({
      affected_metrics: [{ metric_code: "outp_e2e_avgfee_day", change_type: "schema_drift" }],
      affected_tables: ["dwd_sales_detail"],
      affected_consumers: ["看板A", "报表B"],
      risk_level: "high",
    } as any);
    render(<LineageImpact metricCode="outp_e2e_fee_day" />);
    // 等首次 load 完成（loading=false，按钮可点）
    await waitFor(() => expect(mockedImpact).toHaveBeenCalled());
    fireEvent.click(screen.getByText("变更影响预览"));
    await waitFor(() => {
      expect(mockedPreview).toHaveBeenCalledWith("outp_e2e_fee_day", "schema_drift");
      expect(screen.getByText("变更影响预览（what-if）")).toBeTruthy();
      expect(screen.getByText(/受影响指标 1/)).toBeTruthy();
      expect(screen.getByText(/风险等级 高/)).toBeTruthy();
    });
  });
});
