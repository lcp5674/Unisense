import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  AssetGraph,
  type AssetGraphNode,
  type AssetGraphEdge,
} from "../components/assetmap/AssetGraph";

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
  },
}));
vi.mock("@antv/g6", () => ({
  Graph: vi.fn(() => graphMock),
}));
import { Graph } from "@antv/g6";

const nodes: AssetGraphNode[] = [
  { id: "metric:revenue", label: "finance_revenue_sum_d", type: "metric", domain: "finance" },
  { id: "table:orders", label: "ods_orders", type: "table", domain: "sales", pii: true },
  { id: "field:orders.id", label: "orders.id", type: "field" },
];
const edges: AssetGraphEdge[] = [
  { source: "metric:revenue", target: "table:orders", type: "DERIVED_FROM" },
];

// 图实例复用后，真实数据通过 setData 传入（构造时 data 为空）；回退读取构造参数
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

describe("AssetGraph 交互", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("按类型筛选后仅保留所选类型的节点", async () => {
    const user = userEvent.setup();
    render(<AssetGraph nodes={nodes} edges={edges} height={300} />);
    await waitFor(() => expect(Graph).toHaveBeenCalled());

    // 默认渲染全部类型
    expect(lastGraphData().nodes).toHaveLength(3);

    // 通过类型多选 Select 筛选「指标」（antd Select 需在 selector 上 mouseDown 打开）
    const select = screen.getByTestId("asset-graph-type-filter");
    fireEvent.mouseDown(select.querySelector(".ant-select-selector") as Element);
    await user.click(await screen.findByText("指标"));
    await user.keyboard("{Escape}");

    await waitFor(() => {
      const data = lastGraphData();
      expect(data.nodes.length).toBeGreaterThan(0);
      // G6 data.nodes 元素为 { id, data }，type 在 data 内
      expect(
        data.nodes.every((n) => (n.data as AssetGraphNode | undefined)?.type === "metric"),
      ).toBe(true);
    });
  });

  it("搜索匹配节点时高亮并聚焦首个匹配", async () => {
    const user = userEvent.setup();
    // 模拟渲染后的节点集合（含 data 属性）
    graphMock.getNodeData.mockReturnValue([
      { id: "metric:revenue", data: nodes[0] },
      { id: "table:orders", data: nodes[1] },
    ]);
    render(<AssetGraph nodes={nodes} edges={edges} height={300} />);
    await waitFor(() => expect(Graph).toHaveBeenCalled());

    await user.type(screen.getByTestId("asset-graph-search"), "revenue");

    await waitFor(() => {
      // 匹配节点进入 active 状态，其余 inactive
      expect(graphMock.setElementState).toHaveBeenCalledWith("metric:revenue", "active");
      expect(graphMock.setElementState).toHaveBeenCalledWith("table:orders", "inactive");
      expect(graphMock.focusElement).toHaveBeenCalledWith("metric:revenue");
    });
  });

  it("清空搜索后恢复全部节点状态", async () => {
    const user = userEvent.setup();
    graphMock.getNodeData.mockReturnValue([{ id: "metric:revenue", data: nodes[0] }]);
    render(<AssetGraph nodes={nodes} edges={edges} height={300} />);
    await waitFor(() => expect(Graph).toHaveBeenCalled());

    await user.type(screen.getByTestId("asset-graph-search"), "x");
    await user.clear(screen.getByTestId("asset-graph-search"));

    await waitFor(() => {
      expect(graphMock.setElementState).toHaveBeenCalledWith("metric:revenue", []);
    });
  });

  it("showFields=false 时剔除字段节点（血缘总览降噪）", async () => {
    render(<AssetGraph nodes={nodes} edges={edges} height={300} showFields={false} />);
    await waitFor(() => expect(Graph).toHaveBeenCalled());
    await waitFor(() => {
      const data = lastGraphData();
      expect(data.nodes).toHaveLength(2);
      expect(
        data.nodes.every((n) => (n.data as AssetGraphNode | undefined)?.type !== "field"),
      ).toBe(true);
    });
  });
});
