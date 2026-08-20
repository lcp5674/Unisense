import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, waitFor, screen } from "@testing-library/react";
import { LineageImpact } from "../pages/metric/LineageImpact";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    lineageImpact: vi.fn(),
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
import { lineageImpact } from "../api";
import type { AssetGraphNode } from "../components/assetmap/AssetGraph";

const mockedImpact = vi.mocked(lineageImpact);

function lastGraphData(): {
  nodes: Array<{ id: string; data?: AssetGraphNode }>;
  edges: Array<{ source: string; target: string; type: string }>;
} {
  const dataCalls = graphMock.setData.mock.calls as Array<
    [{ nodes: Array<{ id: string; data?: AssetGraphNode }>; edges: Array<{ source: string; target: string; type: string }> }]
  >;
  if (dataCalls.length > 0) return dataCalls[dataCalls.length - 1][0];
  const ctorCalls = vi.mocked(Graph).mock.calls;
  return ctorCalls[ctorCalls.length - 1][0].data as {
    nodes: Array<{ id: string; data?: AssetGraphNode }>;
    edges: Array<{ source: string; target: string; type: string }>;
  };
}

describe("LineageImpact", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedImpact.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 });
    vi.mocked(graphMock.render).mockImplementation(() => Promise.resolve(undefined));
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

  it("有血缘数据时以图谱展示（AssetGraph 渲染节点/边，边明细折叠为辅）", async () => {
    mockedImpact.mockResolvedValue({
      items: [
        {
          id: 1,
          source_node: "table:ods_orders",
          target_node: "metric:sales_e2e_gmv_day",
          edge_type: "METRIC_DERIVES",
          granularity: "day",
          confidence: 0.9,
          provenance: "sqlglot",
        },
        {
          id: 2,
          source_node: "metric:sales_e2e_gmv_day",
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
        { id: "metric:sales_e2e_gmv_day", type: "metric", label: "sales_e2e_gmv_day", domain: "sales" },
        { id: "table:ods_orders", type: "table", label: "ods_orders", entity_id: 3 },
        { id: "table:ads_sales_gmv", type: "table", label: "ads_sales_gmv", entity_id: 5 },
      ],
    });
    render(<LineageImpact metricCode="sales_e2e_gmv_day" />);
    // 图谱渲染：G6 Graph 实例化 + 数据含 3 业务节点 2 边（表→指标→表）；
    // lanes 泳道模式会附加 __lane_*__ 隐藏锚点节点，断言时过滤
    await waitFor(() => expect(Graph).toHaveBeenCalled());
    const data = lastGraphData();
    const realNodes = data.nodes.filter((n) => !(n.data as AssetGraphNode | undefined)?.anchor);
    expect(realNodes).toHaveLength(3);
    expect(realNodes.map((n) => n.id).sort()).toEqual([
      "metric:sales_e2e_gmv_day",
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
    render(<LineageImpact metricCode="sales_e2e_gmv_day" />);
    await waitFor(() => expect(mockedImpact).toHaveBeenCalled());
    await waitFor(() => {
      expect(Graph).not.toHaveBeenCalled();
      expect(screen.getByText(/暂无血缘关系/)).toBeTruthy();
    });
  });
});
