import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  AssetGraph,
  applyLanes,
  layerOf,
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
    getZoom: vi.fn(() => 1),
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
    // 每个用例有干净的 render implementation（避免上一个用例 mockReset 污染）
    vi.mocked(graphMock.render).mockImplementation(() => Promise.resolve(undefined));
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

  it("检测到真环时显示循环提示条并自动切换力导向布局", async () => {
    // A→B→C→A 三节点真环（SCC 尺寸 3 > 2）
    const cycNodes: AssetGraphNode[] = [
      { id: "table:a", label: "a", type: "table", domain: "sales" },
      { id: "table:b", label: "b", type: "table", domain: "sales" },
      { id: "table:c", label: "c", type: "table", domain: "sales" },
    ];
    const cycEdges: AssetGraphEdge[] = [
      { source: "table:a", target: "table:b", type: "DERIVED_FROM" },
      { source: "table:b", target: "table:c", type: "DERIVED_FROM" },
      { source: "table:c", target: "table:a", type: "DERIVED_FROM" },
    ];
    render(<AssetGraph nodes={cycNodes} edges={cycEdges} height={300} />);
    await waitFor(() => expect(Graph).toHaveBeenCalled());

    // 循环提示条出现，说明 3 个节点被识别为循环依赖
    const banner = await screen.findByTestId("asset-graph-cycle-banner");
    expect(banner.textContent).toContain("3");
    expect(banner.textContent).toContain("循环依赖");
    // 提示条同时解释环节点（橙色描边）与环边（红色虚线）图例语义
    expect(banner.textContent).toContain("橙色描边");
    expect(banner.textContent).toContain("红色虚线");

    // 有环时布局自动切换为力导向（dagre 对环渲染异常），环边标记为 inCycle 虚线
    const ctorCalls = vi.mocked(Graph).mock.calls;
    const ctorConfig = ctorCalls[ctorCalls.length - 1][0] as {
      layout?: { type?: string };
    };
    expect(ctorConfig.layout?.type).toBe("d3-force");
    const data = lastGraphData();
    expect(
      data.edges.every((e) => (e.data as AssetGraphEdge | undefined)?.inCycle === true),
    ).toBe(true);
  });

  it("无环的 DAG 使用分层布局", async () => {
    const dagNodes: AssetGraphNode[] = [
      { id: "table:o", label: "ods_orders", type: "table", domain: "sales" },
      { id: "metric:m", label: "gmv", type: "metric", domain: "sales" },
    ];
    const dagEdges: AssetGraphEdge[] = [
      { source: "table:o", target: "metric:m", type: "DERIVED_FROM" },
    ];
    render(<AssetGraph nodes={dagNodes} edges={dagEdges} height={300} />);
    await waitFor(() => expect(Graph).toHaveBeenCalled());

    expect(screen.queryByTestId("asset-graph-cycle-banner")).toBeNull();
    const ctorCalls = vi.mocked(Graph).mock.calls;
    const ctorConfig = ctorCalls[ctorCalls.length - 1][0] as {
      layout?: { type?: string };
    };
    expect(ctorConfig.layout?.type).toBe("antv-dagre");
  });

  it("渲染中切走再切回布局后数据不丢失——回归：force→hierarchy 图空白", async () => {
    const user = userEvent.setup();
    // 模拟真实 G6：第 1 次渲染（分层）正常 resolve；切到力导向后其 render promise
    // 永不 resolve（destroy 后在途 d3-force 仿真被中断）；切回分层后新图 render 恢复
    // resolve。修复前新图 setData 永远排队在卡住的 promise 之后导致图空白，
    // 修复后 cleanup 重置渲染链 + 递增序号作废旧在途渲染。
    let renderCalls = 0;
    vi.mocked(graphMock.render).mockImplementation(() => {
      renderCalls += 1;
      if (renderCalls === 2) return new Promise<void>(() => {});
      return Promise.resolve(undefined);
    });

    const dagNodes: AssetGraphNode[] = [
      { id: "table:o", label: "ods_orders", type: "table", domain: "sales" },
      { id: "metric:m", label: "gmv", type: "metric", domain: "sales" },
    ];
    const dagEdges: AssetGraphEdge[] = [
      { source: "table:o", target: "metric:m", type: "DERIVED_FROM" },
    ];
    render(<AssetGraph nodes={dagNodes} edges={dagEdges} height={300} />);
    await waitFor(() => expect(Graph).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(graphMock.setData).toHaveBeenCalled());

    // 切到力导向：layoutMode 变化 → 销毁重建，第 2 次 render 卡住（在途仿真被中断）
    const select = screen.getByTestId("asset-graph-layout");
    fireEvent.mouseDown(select.querySelector(".ant-select-selector") as Element);
    await user.click(await screen.findByText("力导向布局"));
    await user.keyboard("{Escape}");
    await waitFor(() => expect(Graph).toHaveBeenCalledTimes(2));

    // 切回分层布局：cleanup 必须重置渲染链 → 新图应能立即 setData 出数据（修复点）
    fireEvent.mouseDown(select.querySelector(".ant-select-selector") as Element);
    await user.click(await screen.findByText("分层布局"));
    await user.keyboard("{Escape}");
    await waitFor(() => expect(Graph).toHaveBeenCalledTimes(3));

    // 决定性断言：第 3 次构造的新图必须执行 setData（修复前永远停在 2 次）
    await waitFor(() => expect(graphMock.setData).toHaveBeenCalledTimes(3));
    const data = lastGraphData();
    expect(data.nodes.length).toBe(2);
    expect(renderCalls).toBe(3);
  });

  it("大数据量 LOD：缩放低于阈值批量切 compact 状态，放大自动恢复", async () => {
    // 模拟超过 LOD_LARGE_GRAPH(200) 的节点集 → 初始即紧凑，减轻首帧标签/柔光绘制
    const bigNodes: AssetGraphNode[] = Array.from({ length: 220 }, (_, i) => ({
      id: `table:t${i}`,
      label: `t${i}`,
      type: "table",
    }));
    const bigEdges: AssetGraphEdge[] = [];
    // 全景 fitView 后缩放 < 阈值 0.6
    vi.mocked(graphMock.getZoom).mockReturnValue(0.3);
    graphMock.getNodeData.mockReturnValue(bigNodes.map((n) => ({ id: n.id, data: n })));
    render(<AssetGraph nodes={bigNodes} edges={bigEdges} height={300} />);
    await waitFor(() => expect(Graph).toHaveBeenCalled());

    // render 后 applyLod：批量 compact（record 形式，第二参 animation=false）
    await waitFor(() => {
      expect(graphMock.setElementState).toHaveBeenCalledWith(
        expect.objectContaining({ "table:t0": "compact" }),
        false,
      );
    });

    // 滚轮放大到阈值以上 → canvas:wheel 回调触发 applyLod 恢复非 compact
    const wheelHandler = graphMock.on.mock.calls.find(
      ([name]) => name === "canvas:wheel",
    )?.[1] as (() => void) | undefined;
    expect(wheelHandler).toBeDefined();
    vi.mocked(graphMock.getZoom).mockReturnValue(1.5);
    wheelHandler?.();
    await waitFor(() => {
      expect(graphMock.setElementState).toHaveBeenCalledWith(
        expect.objectContaining({ "table:t0": [] }),
        false,
      );
    });
  });

  it("小图（节点数少）不进入 compact：标签始终显示", async () => {
    vi.mocked(graphMock.getZoom).mockReturnValue(1);
    graphMock.getNodeData.mockReturnValue(nodes.map((n) => ({ id: n.id, data: n })));
    render(<AssetGraph nodes={nodes} edges={edges} height={300} />);
    await waitFor(() => expect(Graph).toHaveBeenCalled());

    // 节点 3 个 < 200，且 zoom=1 ≥ 0.6 → applyLod 不触发 compact 状态设置
    await waitFor(() => expect(graphMock.setData).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 50));
    expect(graphMock.setElementState).not.toHaveBeenCalledWith(
      expect.objectContaining({ "metric:revenue": "compact" }),
      false,
    );
  });

  it("lanes 语义泳道：插入隐藏锚点节点与锚定边（默认关闭不插入）", async () => {
    // 默认 lanes=false：不插入锚点
    const { unmount } = render(<AssetGraph nodes={nodes} edges={edges} height={300} />);
    await waitFor(() => expect(Graph).toHaveBeenCalled());
    expect(lastGraphData().nodes).toHaveLength(3);
    expect(lastGraphData().edges).toHaveLength(1);
    unmount();
    vi.clearAllMocks();
    vi.mocked(graphMock.render).mockImplementation(() => Promise.resolve(undefined));

    // lanes=true：表/指标/字段三类均存在 → 3 锚点 + 3 真实节点，锚定边链 + 挂载边
    render(<AssetGraph nodes={nodes} edges={edges} height={300} lanes />);
    await waitFor(() => expect(Graph).toHaveBeenCalled());
    const data = lastGraphData();
    const anchors = data.nodes.filter((n) => (n.data as AssetGraphNode | undefined)?.anchor);
    expect(anchors.map((a) => a.id).sort()).toEqual(["__lane_field__", "__lane_metric__", "__lane_table__"]);
    expect(data.nodes).toHaveLength(6);
    // 锚定边：锚点链（表→指标→字段）+ 每个真实节点的挂载边
    const anchorEdges = data.edges.filter((e) => (e.data as AssetGraphEdge | undefined)?.anchorEdge);
    expect(anchorEdges).toHaveLength(2 + 3); // 2 条锚点链边 + 3 条挂载边
    expect(anchorEdges.some((e) => e.source === "__lane_table__" && e.target === "table:orders")).toBe(true);
    expect(anchorEdges.some((e) => e.source === "__lane_metric__" && e.target === "metric:revenue")).toBe(true);
  });

  it("lanes 泳道：字段折叠（showFields=false）时不插入字段锚", async () => {
    render(<AssetGraph nodes={nodes} edges={edges} height={300} lanes showFields={false} />);
    await waitFor(() => expect(Graph).toHaveBeenCalled());
    const data = lastGraphData();
    expect(data.nodes.some((n) => n.id === "__lane_field__")).toBe(false);
    // 表 + 指标两类 → 2 锚点 + 2 真实节点 = 4
    expect(data.nodes).toHaveLength(4);
  });

  it("lanes 泳道：有真环时仍强制分层（dagre acyclic 翻转环边）", async () => {
    const cycNodes: AssetGraphNode[] = [
      { id: "table:a", label: "a", type: "table", domain: "sales" },
      { id: "table:b", label: "b", type: "table", domain: "sales" },
      { id: "metric:m", label: "m", type: "metric", domain: "sales" },
    ];
    const cycEdges: AssetGraphEdge[] = [
      { source: "table:a", target: "table:b", type: "DERIVED_FROM" },
      { source: "table:b", target: "table:a", type: "DERIVED_FROM" },
      { source: "table:a", target: "metric:m", type: "DERIVED_FROM" },
    ];
    render(<AssetGraph nodes={cycNodes} edges={cycEdges} height={300} lanes />);
    await waitFor(() => expect(Graph).toHaveBeenCalled());
    // lanes 模式：即使有环（a↔b 双向 + 无 SCC>2）也走分层布局
    const ctorCalls = vi.mocked(Graph).mock.calls;
    const ctorConfig = ctorCalls[ctorCalls.length - 1][0] as { layout?: { type?: string } };
    expect(ctorConfig.layout?.type).toBe("antv-dagre");
  });

  it("全屏：点击全屏按钮打开 overlay，退出按钮关闭", async () => {
    const user = userEvent.setup();
    render(<AssetGraph nodes={nodes} edges={edges} height={300} />);
    await waitFor(() => expect(Graph).toHaveBeenCalled());

    await user.click(screen.getByTestId("asset-graph-fullscreen-btn"));
    expect(await screen.findByTestId("asset-graph-fullscreen")).toBeTruthy();
    expect(screen.getByText("图谱全屏")).toBeTruthy();

    await user.click(screen.getByText("退出全屏"));
    await waitFor(() => expect(screen.queryByTestId("asset-graph-fullscreen")).toBeNull());
  });

  it("字段折叠：控制条切换隐藏/显示字段节点", async () => {
    const user = userEvent.setup();
    render(<AssetGraph nodes={nodes} edges={edges} height={300} />);
    await waitFor(() => expect(Graph).toHaveBeenCalled());
    expect(lastGraphData().nodes).toHaveLength(3); // 默认显示字段

    await user.click(screen.getByTestId("asset-graph-show-fields"));
    await waitFor(() => {
      const data = lastGraphData();
      expect(data.nodes).toHaveLength(2);
      expect(
        data.nodes.every((n) => (n.data as AssetGraphNode | undefined)?.type !== "field"),
      ).toBe(true);
    });

    await user.click(screen.getByTestId("asset-graph-show-fields"));
    await waitFor(() => expect(lastGraphData().nodes).toHaveLength(3));
  });

  it("layerOf 数仓分层推断：表按前缀、指标按 dw_layer、未知返回 null", () => {
    const ods = { id: "table:o", label: "ods_orders", type: "table" as const };
    const dwd = { id: "table:d", label: "dwd_orders_detail", type: "table" as const };
    const noLayer = { id: "table:x", label: "orders", type: "table" as const };
    const metricLayer = { id: "metric:gmv", label: "gmv", type: "metric" as const, dw_layer: "dws" };
    const metricNoLayer = { id: "metric:gmv", label: "gmv", type: "metric" as const };
    expect(layerOf(ods)).toBe("ods");
    expect(layerOf(dwd)).toBe("dwd");
    expect(layerOf(noLayer)).toBeNull();
    expect(layerOf(metricLayer)).toBe("dws");
    expect(layerOf(metricNoLayer)).toBeNull();
  });

  it("applyLanes 单测：单类型不插锚点、other 类型不挂锚、锚点链按表→指标→字段", () => {
    const onlyTable: AssetGraphNode[] = [
      { id: "table:a", label: "a", type: "table" },
      { id: "table:b", label: "b", type: "table" },
    ];
    expect(applyLanes(onlyTable, []).nodes).toHaveLength(2); // 单类型不插锚

    const mixed: AssetGraphNode[] = [
      { id: "table:a", label: "a", type: "table" },
      { id: "metric:m", label: "m", type: "metric" },
      { id: "field:a.x", label: "a.x", type: "field" },
      { id: "other:q", label: "q", type: "other" },
    ];
    const r = applyLanes(mixed, []);
    expect(r.nodes).toHaveLength(3 + 4); // 3 锚点 + 4 真实节点
    // other 节点不挂锚（无挂载边）
    expect(r.edges.some((e) => e.target === "other:q")).toBe(false);
    // 锚点链方向：表锚 → 指标锚 → 字段锚
    expect(
      r.edges.some((e) => e.source === "__lane_table__" && e.target === "__lane_metric__"),
    ).toBe(true);
    expect(
      r.edges.some((e) => e.source === "__lane_metric__" && e.target === "__lane_field__"),
    ).toBe(true);
  });

  it("血缘度径向布局：layout=radial → concentric + sortBy degree（依赖引用数高者居中）", async () => {
    render(<AssetGraph nodes={nodes} edges={edges} height={300} layout="radial" />);
    await waitFor(() => expect(Graph).toHaveBeenCalled());
    const ctorCalls = vi.mocked(Graph).mock.calls;
    const ctorConfig = ctorCalls[ctorCalls.length - 1][0] as {
      layout?: { type?: string; sortBy?: string; preventOverlap?: boolean };
    };
    expect(ctorConfig.layout?.type).toBe("concentric");
    expect(ctorConfig.layout?.sortBy).toBe("degree");
    expect(ctorConfig.layout?.preventOverlap).toBe(true);
  });

  it("力导向大图边降采样：仅 force 布局 + 边超阈值时按血缘度保留枢纽边", async () => {
    // 构造 400 节点大图（超过 LOD_LARGE_GRAPH=200），含大量边
    const bigNodes: AssetGraphNode[] = Array.from({ length: 400 }, (_, i) => ({
      id: `table:n${i}`,
      label: `n${i}`,
      type: "table",
    }));
    const bigEdges: AssetGraphEdge[] = [];
    // 前 50 个节点相互全连（高血缘度），后 350 个各连一条（低血缘度）——总数远超阈值
    for (let i = 0; i < 50; i++) {
      for (let j = i + 1; j < 50; j++) {
        bigEdges.push({ source: `table:n${i}`, target: `table:n${j}`, type: "DERIVED_FROM" });
      }
    }
    for (let i = 50; i < 400; i++) {
      bigEdges.push({ source: `table:n${i}`, target: `table:n${i - 1}`, type: "DERIVED_FROM" });
    }
    render(<AssetGraph nodes={bigNodes} edges={bigEdges} height={300} layout="force" />);
    await waitFor(() => expect(Graph).toHaveBeenCalled());
    // 降采样后传入图的边数应 ≤ MAX_FORCE_DENSE_EDGES(600)，且保留高血缘度枢纽边
    const data = lastGraphData();
    expect(data.edges.length).toBeLessThanOrEqual(600);
    expect(data.edges.length).toBeGreaterThan(0);
    // 高血缘度节点 n0（连 49 条）应仍在其保留边中出现
    expect(data.edges.some((e) => e.source === "table:n0" || e.target === "table:n0")).toBe(true);
  });

  it("血缘度筛选：依赖 ≥ 阈值时仅保留枢纽节点（隐藏低价值叶子）", async () => {
    const user = userEvent.setup();
    // 5 节点：hub 连接全部（血缘度 4），leaf1-4 各连 hub（血缘度 1）
    const hubNodes: AssetGraphNode[] = [
      { id: "table:hub", label: "hub", type: "table" },
      { id: "table:l1", label: "l1", type: "table" },
      { id: "table:l2", label: "l2", type: "table" },
      { id: "table:l3", label: "l3", type: "table" },
      { id: "table:l4", label: "l4", type: "table" },
    ];
    const hubEdges: AssetGraphEdge[] = [
      { source: "table:hub", target: "table:l1", type: "DERIVED_FROM" },
      { source: "table:hub", target: "table:l2", type: "DERIVED_FROM" },
      { source: "table:hub", target: "table:l3", type: "DERIVED_FROM" },
      { source: "table:hub", target: "table:l4", type: "DERIVED_FROM" },
    ];
    render(<AssetGraph nodes={hubNodes} edges={hubEdges} height={300} />);
    await waitFor(() => expect(Graph).toHaveBeenCalled());
    // 默认显示全部 5 节点
    expect(lastGraphData().nodes).toHaveLength(5);

    // 血缘度筛选「依赖 ≥ 2」：只有 hub（度 4）保留，4 个叶子（度 1）被过滤
    const select = screen.getByTestId("asset-graph-min-degree");
    fireEvent.mouseDown(select.querySelector(".ant-select-selector") as Element);
    await user.click(await screen.findByText("依赖 ≥ 2"));
    await user.keyboard("{Escape}");
    await waitFor(() => {
      const data = lastGraphData();
      expect(data.nodes).toHaveLength(1);
      expect(data.nodes[0].id).toBe("table:hub");
    });

    // 清空筛选（选「依赖 ≥ 0 全部」）恢复全部
    fireEvent.mouseDown(select.querySelector(".ant-select-selector") as Element);
    await user.click(await screen.findByText("依赖 ≥ 0（全部）"));
    await user.keyboard("{Escape}");
    await waitFor(() => expect(lastGraphData().nodes).toHaveLength(5));
  });
});
