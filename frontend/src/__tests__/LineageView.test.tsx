import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { LineageView } from "../pages/LineageView";
import * as api from "../api";
import type { LineageGraphData } from "../types";

const { graphMock } = vi.hoisted(() => ({
  graphMock: {
    on: vi.fn(),
    render: vi.fn().mockResolvedValue(undefined),
    destroy: vi.fn(),
    getNodeData: vi.fn<(id?: string) => Array<{ id: string; data?: Record<string, unknown> }> | undefined>(
      () => [],
    ),
    getNeighborNodesData: vi.fn(() => []),
    setElementState: vi.fn(),
    focusElement: vi.fn(),
  },
}));
vi.mock("@antv/g6", () => ({
  Graph: vi.fn(() => graphMock),
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, lineageGraph: vi.fn() };
});

const graphData: LineageGraphData = {
  nodes: [
    { id: "metric:revenue", type: "metric", label: "营收", domain: "finance" },
    { id: "table:orders", type: "table", label: "订单表", domain: "sales", pii: true },
  ],
  edges: [{ source: "metric:revenue", target: "table:orders", type: "DERIVED_FROM" }],
};

let currentPath = "";
function PathProbe() {
  const location = useLocation();
  currentPath = location.pathname + location.search;
  return null;
}

async function renderLineage() {
  render(
    <MemoryRouter initialEntries={["/lineage"]}>
      <LineageView />
      <PathProbe />
    </MemoryRouter>,
  );
}

describe("LineageView 血缘图谱 Tab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.lineageGraph).mockResolvedValue(graphData);
    // AssetGraph 点击回调调 graph.getNodeData(id)?.data，需按 id 返回对应节点
    graphMock.getNodeData.mockImplementation((id?: string) => {
      const found = graphData.nodes.find((n) => n.id === String(id));
      return found ? ({ id: found.id, data: found } as never) : undefined;
    });
  });

  it("默认选中血缘图谱并加载全量图谱数据", async () => {
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalledWith({ limit: 2000 }));
    await waitFor(() => {
      expect(screen.getByText(/共 2 节点 · 1 条血缘边/)).toBeInTheDocument();
    });
  });

  it("点击指标节点跳转指标详情", async () => {
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    const clickHandler = graphMock.on.mock.calls.find(([evt]) => evt === "node:click")?.[1] as
      | ((evt: unknown) => void)
      | undefined;
    expect(clickHandler).toBeDefined();
    await act(async () => {
      clickHandler?.({ target: { id: "metric:revenue" } });
    });
    expect(currentPath).toBe("/detail/revenue");
  });

  it("点击表节点跳转采集目录并携带关键词", async () => {
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    const clickHandler = graphMock.on.mock.calls.find(([evt]) => evt === "node:click")?.[1] as
      | ((evt: unknown) => void)
      | undefined;
    await act(async () => {
      clickHandler?.({ target: { id: "table:orders" } });
    });
    expect(currentPath).toBe("/catalog?kw=orders");
  });

  it("图谱为空时显示引导提示", async () => {
    vi.mocked(api.lineageGraph).mockResolvedValue({ nodes: [], edges: [] });
    renderLineage();
    await waitFor(() => {
      expect(screen.getByText(/暂无血缘图谱数据/)).toBeInTheDocument();
    });
  });
});
