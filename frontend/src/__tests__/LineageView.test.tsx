import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, act, fireEvent, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { LineageView, buildSubgraph, resolveRootId } from "../pages/LineageView";
import { adaptiveBaseRadius } from "../components/assetmap/AssetGraph";
import * as api from "../api";
import type { LineageGraphData } from "../types";

const { graphMock } = vi.hoisted(() => ({
  graphMock: {
    on: vi.fn(),
    render: vi.fn().mockResolvedValue(undefined),
    destroy: vi.fn(),
    setData: vi.fn(),
    fitView: vi.fn(),
    // destroyed getter 让 data effect 在 destroy 后跳过
    get destroyed() {
      return false;
    },
    getNodeData: vi.fn<(id?: string) => Array<{ id: string; data?: Record<string, unknown> }> | undefined>(
      () => [],
    ),
    getNeighborNodesData: vi.fn(() => []),
    setElementState: vi.fn().mockResolvedValue(undefined),
    focusElement: vi.fn().mockResolvedValue(undefined),
  },
}));
vi.mock("@antv/g6", () => ({
  Graph: vi.fn(() => graphMock),
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    lineageGraph: vi.fn(),
    getCatalogDetail: vi.fn(),
    parseLineage: vi.fn(),
    lineageNodes: vi.fn(),
    lineageImpact: vi.fn(),
    lineageChannels: vi.fn(),
    lineageStale: vi.fn(),
    getMetric: vi.fn(),
    getMetricHealth: vi.fn(),
    fetchRelatedMetrics: vi.fn(),
  };
});

const graphData: LineageGraphData = {
  nodes: [
    { id: "metric:revenue", type: "metric", label: "营收", domain: "finance" },
    { id: "table:orders", type: "table", label: "订单表", domain: "sales", pii: true, entity_id: 42 },
  ],
  edges: [{ source: "metric:revenue", target: "table:orders", type: "DERIVED_FROM" }],
};

const metricDetail = {
  metric_code: "revenue",
  name: "营收",
  domain: "finance",
  type: "atomic",
  granularity: "day",
  unit: "元",
  currency: null,
  aggregation: "SUM",
  time_semantics: "PERIOD",
  freshness: "T1",
  sla: null,
  dw_layer: "DWS",
  metric_tier: "T1",
  serving_mode: "BATCH_ONLY",
  additivity: "ADDITIVE",
  non_additive_dimensions: null,
  definition_json: { expression: "SUM(amount)", source_tables: ["dwd_finance_order"] },
  version: 1,
  row_version: 1,
  status: "PUBLISHED",
  owner_id: 1,
  backup_owner_id: null,
  approver_id: null,
  submitted_by: null,
  pii_flag: false,
  compliance_reviewed: false,
  effective_version: null,
  consumption_guide: null,
  successor_code: null,
  deprecated_at: null,
  sunset_until: null,
  emergency_publish: false,
  emergency_reason: null,
  gray_tenant_ids: null,
  pending_conflict: false,
  pending_conflict_detail: null,
  pending_version: false,
  created_at: "2026-08-01T00:00:00",
  updated_at: "2026-08-01T00:00:00",
} as never as import("../types").MetricResponse;

const tableDetail = {
  source_id: "mysql_a",
  entity_name: "orders",
  entity_type: "TABLE",
  schema_def: {
    columns: [
      { name: "id", type: "bigint", nullable: false, comment: "主键" },
      { name: "amount", type: "decimal(18,2)", nullable: true, comment: "金额" },
    ],
  },
  etl_sql: "SELECT * FROM orders",
  sensitivity_level: "PII-HIGH",
  owner_id: null,
  upstream_signature: "sig",
  content_signature: null,
  schema_incomplete: false,
  source_deleted: false,
  source_name: "MySQL 主库",
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
    vi.mocked(api.getCatalogDetail).mockResolvedValue(tableDetail);
    vi.mocked(api.getMetric).mockResolvedValue(metricDetail);
    vi.mocked(api.getMetricHealth).mockResolvedValue(null as never);
    vi.mocked(api.fetchRelatedMetrics).mockResolvedValue([]);
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

  it("点击指标节点在本页打开指标详情侧边栏（不跳转页面）", async () => {
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    const clickHandler = graphMock.on.mock.calls.find(([evt]) => evt === "node:click")?.[1] as
      | ((evt: unknown) => void)
      | undefined;
    expect(clickHandler).toBeDefined();
    await act(async () => {
      clickHandler?.({ target: { id: "metric:revenue" } });
    });
    // 侧边栏加载指标详情并展示（不再跳转页面）
    await waitFor(() => expect(api.getMetric).toHaveBeenCalledWith("revenue"));
    await waitFor(() => {
      expect(screen.getByText(/指标详情：营收/)).toBeInTheDocument();
    });
    expect(screen.getByText("revenue")).toBeInTheDocument();
    // 未跳转指标详情页
    expect(currentPath).toBe("/lineage");
  });

  it("指标详情侧边栏提供「前往完整详情」补充入口", async () => {
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    const clickHandler = graphMock.on.mock.calls.find(([evt]) => evt === "node:click")?.[1] as
      | ((evt: unknown) => void)
      | undefined;
    await act(async () => {
      clickHandler?.({ target: { id: "metric:revenue" } });
    });
    await waitFor(() => {
      expect(screen.getByText(/指标详情：营收/)).toBeInTheDocument();
    });
    await act(async () => {
      screen.getByRole("button", { name: /前往完整详情/ }).click();
    });
    expect(currentPath).toBe("/detail/revenue");
  });

  it("点击表节点在本页打开表详情抽屉（不直接跳转）", async () => {
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    const clickHandler = graphMock.on.mock.calls.find(([evt]) => evt === "node:click")?.[1] as
      | ((evt: unknown) => void)
      | undefined;
    await act(async () => {
      clickHandler?.({ target: { id: "table:orders" } });
    });
    await waitFor(() => expect(api.getCatalogDetail).toHaveBeenCalledWith(42));
    await waitFor(() => {
      expect(screen.getByText(/表 · orders/)).toBeInTheDocument();
    });
    // 未直接跳转
    expect(currentPath).toBe("/lineage");
  });

  it("表详情抽屉展示字段清单，并可在指标目录中查看", async () => {
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    const clickHandler = graphMock.on.mock.calls.find(([evt]) => evt === "node:click")?.[1] as
      | ((evt: unknown) => void)
      | undefined;
    await act(async () => {
      clickHandler?.({ target: { id: "table:orders" } });
    });
    // 抽屉内展示敏感度 / 源名称 / 字段
    await waitFor(() => {
      expect(screen.getByText("PII-HIGH")).toBeInTheDocument();
    });
    expect(screen.getByText("MySQL 主库")).toBeInTheDocument();
    expect(screen.getByText("amount")).toBeInTheDocument();
    expect(screen.getByText("decimal(18,2)")).toBeInTheDocument();
    // 点击「在指标目录中查看」跳转采集目录
    await act(async () => {
      screen.getByRole("button", { name: /在指标目录中查看/ }).click();
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

  it("提供统一的返回按钮（返回上一入口）", async () => {
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/assetmap", "/lineage"]}>
        <Routes>
          <Route path="/assetmap" element={<div>assetmap-page</div>} />
          <Route path="/lineage" element={<LineageView />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    screen.getByRole("button", { name: /返\s*回/ }).click();
    await screen.findByText("assetmap-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    render(
      <MemoryRouter initialEntries={["/lineage"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/lineage" element={<LineageView />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    screen.getByRole("button", { name: /返\s*回/ }).click();
    await screen.findByText("dashboard-page");
  });

  it("提供布局切换 Select（分层 / 力导向），保证用户可手动覆盖 auto 检测", async () => {
    // 验证布局切换控件存在（testid "asset-graph-layout" 在 AssetGraph 组件内）
    // 布局切换的真实行为（layoutTick → 图重建 + Spin + fitView）由浏览器 E2E 覆盖
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    await waitFor(() =>
      expect(document.querySelector('[data-testid="asset-graph-layout"]')).toBeTruthy(),
    );
  });
});

describe("LineageView SQL 血缘解析 Tab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.lineageGraph).mockResolvedValue(graphData);
    vi.mocked(api.getCatalogDetail).mockResolvedValue(tableDetail);
    vi.mocked(api.parseLineage).mockResolvedValue({
      table_edges: 1,
      field_edges: 2,
      graph_written: true,
      table_lineage: [{ source: "table:s", target: "table:t" }],
      field_lineage: [
        { source_table: "a", source_column: "id", target_table: "t", target_column: "id", expression: null },
        { source_table: "b", source_column: "amount", target_table: "t", target_column: "amount", expression: "SUM(amount)" },
      ],
    });
  });

  it("方言下拉支持 Doris 与 ClickHouse，并按所选方言调用解析", async () => {
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    // 切到 SQL 血缘解析 Tab（antd 非激活 tabpanel 为 display:none，getByRole 仅命中可见面板）
    await act(async () => {
      screen.getByRole("tab", { name: /SQL 血缘解析/ }).click();
    });
    const panel = await screen.findByRole("tabpanel");
    const dialectSelect = within(panel).getByRole("combobox");

    // 打开方言下拉，确认 Doris 与 ClickHouse 均可选
    fireEvent.mouseDown(dialectSelect);
    await screen.findByText("Doris");
    expect(screen.getByText("ClickHouse")).toBeTruthy();
    fireEvent.click(screen.getByText("Doris"));

    // 输入 SQL 并解析，校验按所选方言（doris）调用
    fireEvent.change(within(panel).getByPlaceholderText(/粘贴 SQL/), {
      target: { value: "INSERT INTO t SELECT id FROM s" },
    });
    fireEvent.click(within(panel).getByRole("button", { name: /解析血缘/ }));
    await waitFor(() =>
      expect(api.parseLineage).toHaveBeenCalledWith("INSERT INTO t SELECT id FROM s", "doris"),
    );
  });

  it("解析成功后当页展示本次表级/字段级边明细表格（方案 A）", async () => {
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    await act(async () => {
      screen.getByRole("tab", { name: /SQL 血缘解析/ }).click();
    });
    const panel = await screen.findByRole("tabpanel");
    fireEvent.change(within(panel).getByPlaceholderText(/粘贴 SQL/), {
      target: { value: "INSERT INTO t SELECT a.id, SUM(b.amount) FROM a JOIN b" },
    });
    fireEvent.click(within(panel).getByRole("button", { name: /解析血缘/ }));
    await waitFor(() => expect(api.parseLineage).toHaveBeenCalled());
    // Alert 统计 + 表级明细表格
    await waitFor(() => {
      expect(screen.getByText("本次解析 · 表级血缘（1）")).toBeInTheDocument();
    });
    expect(screen.getByText("本次解析 · 字段级血缘（2）")).toBeInTheDocument();
    // 表级边明细：源表 s → 目标表 t
    expect(screen.getByText("table:s")).toBeInTheDocument();
    expect(screen.getByText("table:t")).toBeInTheDocument();
    // 字段级边明细：a.id → t.id，b.amount → t.amount（带派生表达式）
    expect(screen.getByText("a.id")).toBeInTheDocument();
    expect(screen.getByText("t.id")).toBeInTheDocument();
    expect(screen.getByText("SUM(amount)")).toBeInTheDocument();
  });
});

describe("LineageView 血缘查询 / 影响分析 Tab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.lineageGraph).mockResolvedValue(graphData);
    vi.mocked(api.lineageNodes).mockResolvedValue([
      { id: "table:orders", label: "orders", type: "table", count: 12 },
      { id: "metric:gmv", label: "gmv", type: "metric", count: 8 },
    ]);
    vi.mocked(api.lineageImpact).mockResolvedValue({
      items: [
        {
          id: 1,
          source_node: "table:orders",
          target_node: "table:dws",
          edge_type: "DERIVED_FROM",
          granularity: "L1",
          confidence: 1,
          provenance: "sqlglot",
          pii_inherited: false,
        },
      ],
      total: 1,
      page: 1,
      page_size: 50,
      has_more: false,
    });
  });

  it("进入 Tab 预加载候选节点，可从下拉选择节点并查询影响", async () => {
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    await act(async () => {
      screen.getByRole("tab", { name: /血缘查询/ }).click();
    });
    const panel = await screen.findByRole("tabpanel");
    // 预加载候选节点（无关键词）
    await waitFor(() => expect(api.lineageNodes).toHaveBeenCalledWith(undefined, 50));
    // 打开节点下拉，看到候选（表 / 指标）
    const nodeSelect = within(panel).getAllByRole("combobox")[0];
    fireEvent.mouseDown(nodeSelect);
    await screen.findByText("orders");
    fireEvent.click(screen.getByText("gmv"));
    // 查询 → 按所选节点调用影响分析
    fireEvent.click(within(panel).getByRole("button", { name: /查\s*询/ }));
    await waitFor(() =>
      expect(api.lineageImpact).toHaveBeenCalledWith({
        node: "metric:gmv",
        direction: "downstream",
        max_hops: 5,
      }),
    );
    // 展示血缘边结果（限定在面板内，避免与下拉残留选项冲突）
    await waitFor(() => expect(within(panel).getByText("table:orders")).toBeInTheDocument());
  });

  it("输入关键词触发远程搜索候选节点，未命中时兜底「使用输入值」", async () => {
    const user = userEvent.setup();
    vi.mocked(api.lineageNodes).mockResolvedValue([]);
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    await act(async () => {
      screen.getByRole("tab", { name: /血缘查询/ }).click();
    });
    const panel = await screen.findByRole("tabpanel");
    const nodeSelect = within(panel).getAllByRole("combobox")[0];
    fireEvent.mouseDown(nodeSelect);
    // 等待下拉完全打开（open 状态生效后搜索框才可交互）
    await waitFor(() => {
      expect(document.querySelector(".ant-select-open")).toBeTruthy();
    });
    // 限定在打开的 Select 内取搜索框（页面存在多个 Select 的隐藏 search input）
    const searchInput = document.querySelector<HTMLInputElement>(".ant-select-open .ant-select-selection-search-input")!;
    await user.type(searchInput, "external");
    // 输入关键词 → 远程搜索候选节点（含中间态与最终态调用）
    await waitFor(() => expect(api.lineageNodes).toHaveBeenCalledWith("external", 50));
    // 兜底「使用输入值」选项出现，支持自由指定节点
    await screen.findByText(/使用「external」/);
  });
});

describe("LineageView 采集通道 Tab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.lineageGraph).mockResolvedValue(graphData);
    vi.mocked(api.lineageChannels).mockResolvedValue([
      { source: "sqlglot", edge_count: 18, node_count: 28, stale_count: 0, last_run: null },
      { source: "dp_csv", edge_count: 10553, node_count: 4012, stale_count: 0, last_run: null },
    ]);
    vi.mocked(api.lineageStale).mockResolvedValue([]);
  });

  it("采集通道卡片展示友好名称（SQL 解析 / DP 同步）与原始标识", async () => {
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    await act(async () => {
      screen.getByRole("tab", { name: /采集通道/ }).click();
    });
    await waitFor(() => expect(api.lineageChannels).toHaveBeenCalled());
    expect(screen.getByText("SQL 解析")).toBeInTheDocument();
    expect(screen.getByText("DP 同步")).toBeInTheDocument();
    // 原始 provenance 标识以小字一并展示
    expect(screen.getByText("sqlglot")).toBeInTheDocument();
    expect(screen.getByText("dp_csv")).toBeInTheDocument();
  });
});

describe("LineageView 血缘图谱聚焦（?node= 参数）", () => {
  // 较大图：metric:gmv 上游(表)->表->指标，下游->ttm；无关指标链不与 gmv 连通
  const bigGraph: LineageGraphData = {
    nodes: [
      { id: "metric:gmv", type: "metric", label: "GMV", domain: "sales" },
      { id: "metric:gmv_ttm", type: "metric", label: "GMV TTM", domain: "sales" },
      { id: "table:ods_orders", type: "table", label: "订单明细", domain: "sales" },
      { id: "table:dws_gmv", type: "table", label: "GMV 汇总", domain: "sales" },
      { id: "metric:other", type: "metric", label: "无关指标", domain: "finance" },
      { id: "table:dim_shop", type: "table", label: "门店维度", domain: "finance" },
    ],
    edges: [
      { source: "table:ods_orders", target: "table:dws_gmv", type: "DERIVED_FROM" },
      { source: "table:dws_gmv", target: "metric:gmv", type: "DERIVED_FROM" },
      { source: "metric:gmv", target: "metric:gmv_ttm", type: "DERIVED_FROM" },
      // 无关指标链（finance 域），不与 gmv 连通
      { source: "table:dim_shop", target: "metric:other", type: "DERIVED_FROM" },
    ],
  };

  async function renderLineageAt(path: string) {
    render(
      <MemoryRouter initialEntries={[path]}>
        <LineageView />
        <PathProbe />
      </MemoryRouter>,
    );
  }

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.lineageGraph).mockResolvedValue(bigGraph);
  });

  it("URL 带 ?node=gmv 时仅展示该指标上下游子图（不含无关节点）", async () => {
    renderLineageAt("/lineage?node=gmv");
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalledWith({ limit: 2000 }));
    // 聚焦标签 + 限定计数（gmv 双向 3 跳 = gmv/gmv_ttm/ods_orders/dws_gmv 4 节点 3 边）
    await waitFor(() => {
      expect(screen.getByText(/聚焦：gmv 的上下游血缘/)).toBeInTheDocument();
    });
    expect(screen.getByText(/已限定为 4 节点 · 3 条血缘边/)).toBeInTheDocument();
    // 无关指标链（finance 域）不在子图内
    expect(screen.queryByText("无关指标")).not.toBeInTheDocument();
    expect(screen.queryByText("门店维度")).not.toBeInTheDocument();
  });

  it("支持完整节点 id 形态（?node=metric:gmv）", async () => {
    renderLineageAt("/lineage?node=metric:gmv");
    await waitFor(() => {
      expect(screen.getByText(/聚焦：metric:gmv 的上下游血缘/)).toBeInTheDocument();
    });
    expect(screen.getByText(/已限定为 4 节点 · 3 条血缘边/)).toBeInTheDocument();
  });

  it("聚焦节点不在图谱中时展示空态，并可一键查看全量", async () => {
    renderLineageAt("/lineage?node=not_exist");
    await waitFor(() => {
      expect(screen.getByText(/「not_exist」暂无血缘数据/)).toBeInTheDocument();
    });
    await act(async () => {
      screen.getByRole("button", { name: /查看全量血缘图谱/ }).click();
    });
    // 清除聚焦：URL node 参数移除，回到全量
    expect(currentPath).toBe("/lineage");
    await waitFor(() => {
      expect(screen.getByText(/共 6 节点 · 4 条血缘边/)).toBeInTheDocument();
    });
  });

  it("点击「清除」退出聚焦并移除 URL node 参数", async () => {
    renderLineageAt("/lineage?node=gmv");
    await waitFor(() => {
      expect(screen.getByText(/聚焦：gmv 的上下游血缘/)).toBeInTheDocument();
    });
    await act(async () => {
      screen.getByRole("button", { name: /清除/ }).click();
    });
    expect(currentPath).toBe("/lineage");
    await waitFor(() => {
      expect(screen.getByText(/共 6 节点 · 4 条血缘边/)).toBeInTheDocument();
    });
  });
});

describe("buildSubgraph / resolveRootId 单元测试", () => {
  const nodes = [
    { id: "metric:gmv", type: "metric", label: "GMV" },
    { id: "metric:gmv_ttm", type: "metric", label: "GMV TTM" },
    { id: "table:ods_orders", type: "table", label: "订单明细" },
    { id: "table:dws_gmv", type: "table", label: "GMV 汇总" },
    { id: "metric:other", type: "metric", label: "无关指标" },
  ] as never as import("../components/assetmap/AssetGraph").AssetGraphNode[];
  const edges = [
    { source: "table:ods_orders", target: "table:dws_gmv", type: "DERIVED_FROM" },
    { source: "table:dws_gmv", target: "metric:gmv", type: "DERIVED_FROM" },
    { source: "metric:gmv", target: "metric:gmv_ttm", type: "DERIVED_FROM" },
    { source: "metric:other", target: "metric:gmv", type: "DERIVED_FROM" },
  ] as never as import("../components/assetmap/AssetGraph").AssetGraphEdge[];

  it("resolveRootId 支持完整 id / 裸编码 / 不存在", () => {
    expect(resolveRootId("metric:gmv", nodes)).toBe("metric:gmv");
    expect(resolveRootId("gmv", nodes)).toBe("metric:gmv");
    expect(resolveRootId("nope", nodes)).toBeNull();
  });

  it("buildSubgraph 从根节点双向 BFS 展开指定跳数，保留自包含边", () => {
    // 1 跳：gmv 的上下游 = gmv/ttm/dws_gmv/other（4 节点，4 边全包含）
    const sub1 = buildSubgraph(nodes, edges, "metric:gmv", 1);
    expect(sub1.nodes.map((n) => n.id).sort()).toEqual([
      "metric:gmv",
      "metric:gmv_ttm",
      "metric:other",
      "table:dws_gmv",
    ]);
    expect(sub1.edges).toHaveLength(3);
    // 2 跳：dws_gmv 的上游 ods_orders 也进入子图
    const sub2 = buildSubgraph(nodes, edges, "metric:gmv", 2);
    expect(sub2.nodes.map((n) => n.id).sort()).toContain("table:ods_orders");
    expect(sub2.edges).toHaveLength(4);
  });

  it("buildSubgraph 根节点不存在时返回空", () => {
    const sub = buildSubgraph(nodes, edges, "metric:nope", 3);
    expect(sub.nodes).toHaveLength(0);
    expect(sub.edges).toHaveLength(0);
  });
});

describe("adaptiveBaseRadius 自适应节点半径", () => {
  it("聚焦视图（节点少）用小半径，全景大图维持可读半径", () => {
    expect(adaptiveBaseRadius(1)).toBe(16); // 从指标目录跳转聚焦：1-3 节点
    expect(adaptiveBaseRadius(3)).toBe(16);
    expect(adaptiveBaseRadius(8)).toBe(18);
    expect(adaptiveBaseRadius(25)).toBe(20);
    expect(adaptiveBaseRadius(100)).toBe(24); // 全景大图
  });
});
