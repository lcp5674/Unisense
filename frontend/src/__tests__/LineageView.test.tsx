import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, act, fireEvent, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { LineageView, buildSubgraph, resolveRootId, parseResultToGraphData, upstreamDepsToGraphData, edgesToGraphData } from "../pages/LineageView";
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
    lineageChannelRuns: vi.fn(),
    lineageRunDetail: vi.fn(),
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
      upstream_deps: null,
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
      expect(api.parseLineage).toHaveBeenCalledWith(
        "INSERT INTO t SELECT id FROM s",
        "doris",
        "",
      ),
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

  it("解析成功后以血缘图谱展示本次解析结果（AssetGraph 图，明细表格为辅）", async () => {
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
    // 血缘图谱 Card：标题含节点/边计数（表节点 2 + 字段节点 4 = 6，表级 1 + 字段级 2 = 3 边）
    await waitFor(() => {
      expect(screen.getByText(/本次解析 · 血缘图谱（6 节点 · 3 条边）/)).toBeInTheDocument();
    });
    expect(panel.querySelector('[data-testid="asset-graph-wrap"]')).toBeTruthy();
    // 明细表格仍在（辅助展示精确映射与表达式）
    expect(screen.getByText(/本次解析 · 表级血缘（1）/)).toBeInTheDocument();
    expect(screen.getByText(/本次解析 · 字段级血缘（2）/)).toBeInTheDocument();
  });

  it("填写目标表名时，解析调用携带 target_table（方案 A 落点）", async () => {
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    await act(async () => {
      screen.getByRole("tab", { name: /SQL 血缘解析/ }).click();
    });
    const panel = await screen.findByRole("tabpanel");
    fireEvent.change(within(panel).getByPlaceholderText(/目标表名（可选）/), {
      target: { value: "dws_report" },
    });
    fireEvent.change(within(panel).getByPlaceholderText(/粘贴 SQL/), {
      target: { value: "SELECT id, name FROM ods_orders" },
    });
    fireEvent.click(within(panel).getByRole("button", { name: /解析血缘/ }));
    await waitFor(() =>
      expect(api.parseLineage).toHaveBeenCalledWith(
        "SELECT id, name FROM ods_orders",
        "mysql",
        "dws_report",
      ),
    );
  });

  it("纯 SELECT 未指定落点时展示上游依赖清单（方案 B，不写图谱）", async () => {
    vi.mocked(api.parseLineage).mockResolvedValue({
      table_edges: 0,
      field_edges: 0,
      graph_written: false,
      table_lineage: [],
      field_lineage: [],
      upstream_deps: {
        tables: ["ods_orders", "dim_user"],
        fields: ["ods_orders.id", "dim_user.name"],
      },
    });
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    await act(async () => {
      screen.getByRole("tab", { name: /SQL 血缘解析/ }).click();
    });
    const panel = await screen.findByRole("tabpanel");
    fireEvent.change(within(panel).getByPlaceholderText(/粘贴 SQL/), {
      target: {
        value: "SELECT o.id, u.name FROM ods_orders o JOIN dim_user u ON o.uid = u.uid",
      },
    });
    fireEvent.click(within(panel).getByRole("button", { name: /解析血缘/ }));
    await waitFor(() => expect(api.parseLineage).toHaveBeenCalled());
    // 上游依赖也画成图谱（中心「本次查询」+ 源表/字段节点）
    await waitFor(() => {
      expect(screen.getByText(/本次查询 · 上游依赖图谱（5 节点 · 4 条边）/)).toBeInTheDocument();
    });
    // 展示上游依赖：源表 + 源字段
    await waitFor(() => {
      expect(screen.getByText(/上游依赖（2 表 \/ 2 字段）/)).toBeInTheDocument();
    });
    expect(screen.getByText("ods_orders")).toBeInTheDocument();
    expect(screen.getByText("dim_user")).toBeInTheDocument();
    expect(screen.getByText("ods_orders.id")).toBeInTheDocument();
    expect(screen.getByText("dim_user.name")).toBeInTheDocument();
    // 提示纯 SELECT 未生成血缘边、未写图谱
    expect(screen.getByText(/未生成血缘边（未写入图谱）/)).toBeInTheDocument();
  });
});

describe("LineageView 血缘查询 / 影响分析 Tab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.lineageGraph).mockResolvedValue(graphData);
    vi.mocked(api.getCatalogDetail).mockResolvedValue(tableDetail);
    vi.mocked(api.getMetric).mockResolvedValue(metricDetail);
    vi.mocked(api.getMetricHealth).mockResolvedValue(null as never);
    vi.mocked(api.fetchRelatedMetrics).mockResolvedValue([]);
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
      // 节点元数据：供影响分析图谱点击节点侧边栏展示详情（entity_id 直达表详情）
      nodes: [
        { id: "table:orders", type: "table", label: "orders", entity_id: 42, domain: "sales", pii: true },
        { id: "table:dws", type: "table", label: "dws", entity_id: 43, domain: "sales" },
      ],
    });
  });

  // 切换到影响分析 Tab 并查询，返回活跃面板（等待图谱挂载，确保 node:click handler 已注册）
  async function openImpactTabAndQuery() {
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    await act(async () => {
      screen.getByRole("tab", { name: /血缘查询/ }).click();
    });
    const panel = await screen.findByRole("tabpanel");
    const nodeSelect = within(panel).getAllByRole("combobox")[0];
    fireEvent.mouseDown(nodeSelect);
    await screen.findByText("orders");
    fireEvent.click(screen.getByText("orders"));
    fireEvent.click(within(panel).getByRole("button", { name: /查\s*询/ }));
    await waitFor(() => expect(api.lineageImpact).toHaveBeenCalled());
    await waitFor(() => expect(panel.querySelector('[data-testid="asset-graph-wrap"]')).toBeTruthy());
    return panel;
  }

  // 影响分析图谱的 node:click 处理器（最后注册的那个——antd Tabs 保留血缘图谱
  // Tab 挂载，其 GraphCanvas 先注册；影响分析图后注册，取末位）
  function impactNodeClickHandler(): ((evt: { target: { id?: string } }) => void) | undefined {
    const calls = graphMock.on.mock.calls.filter(([evt]) => evt === "node:click");
    return calls[calls.length - 1]?.[1] as ((evt: { target: { id?: string } }) => void) | undefined;
  }

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

  it("查询结果以血缘视图（力导向图）展示，边明细表格为辅", async () => {
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    await act(async () => {
      screen.getByRole("tab", { name: /血缘查询/ }).click();
    });
    const panel = await screen.findByRole("tabpanel");
    // 从下拉选择节点
    const nodeSelect = within(panel).getAllByRole("combobox")[0];
    fireEvent.mouseDown(nodeSelect);
    await screen.findByText("orders");
    fireEvent.click(screen.getByText("orders"));
    fireEvent.click(within(panel).getByRole("button", { name: /查\s*询/ }));
    await waitFor(() => expect(api.lineageImpact).toHaveBeenCalled());
    // 血缘视图：AssetGraph 画布渲染（G6 mock），图标题含节点/边计数
    await waitFor(() => {
      expect(
        screen.getByText(/血缘视图 · table:orders 的下游影响（2 节点 · 1 条边）/),
      ).toBeInTheDocument();
    });
    expect(panel.querySelector('[data-testid="asset-graph-wrap"]')).toBeTruthy();
    // 边明细表格仍在（辅助展示）
    expect(within(panel).getByText("table:dws")).toBeInTheDocument();
  });

  it("点击影响分析图中的指标节点 → 侧边栏展示指标详情（不跳转页面）", async () => {
    vi.mocked(api.lineageImpact).mockResolvedValue({
      items: [
        {
          id: 1,
          source_node: "table:dws",
          target_node: "metric:gmv",
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
      nodes: [
        { id: "table:dws", type: "table", label: "dws", entity_id: 43, domain: "sales" },
        { id: "metric:gmv", type: "metric", label: "gmv", domain: "sales" },
      ],
    });
    graphMock.getNodeData.mockImplementation((id?: string) => {
      if (id === "metric:gmv") {
        return { id: "metric:gmv", data: { id: "metric:gmv", type: "metric", label: "gmv" } } as never;
      }
      return undefined;
    });
    await openImpactTabAndQuery();
    const handler = impactNodeClickHandler();
    expect(handler).toBeDefined();
    await act(async () => {
      handler?.({ target: { id: "metric:gmv" } });
    });
    // 侧边栏加载指标详情并展示（不跳转页面）
    await waitFor(() => expect(api.getMetric).toHaveBeenCalledWith("gmv"));
    await waitFor(() => {
      expect(screen.getByText(/指标详情：营收/)).toBeInTheDocument();
    });
    expect(currentPath).toBe("/lineage");
  });

  it("点击影响分析图中的表节点 → 侧边栏展示表详情（entity_id 直达）", async () => {
    graphMock.getNodeData.mockImplementation((id?: string) => {
      if (id === "table:orders") {
        return { id: "table:orders", data: { id: "table:orders", type: "table", label: "orders", entity_id: 42 } } as never;
      }
      return undefined;
    });
    await openImpactTabAndQuery();
    const handler = impactNodeClickHandler();
    expect(handler).toBeDefined();
    await act(async () => {
      handler?.({ target: { id: "table:orders" } });
    });
    // 表详情抽屉：entity_id → getCatalogDetail → 展示敏感度等具体信息
    await waitFor(() => expect(api.getCatalogDetail).toHaveBeenCalledWith(42));
    await waitFor(() => {
      expect(screen.getByText(/表 · orders/)).toBeInTheDocument();
    });
    expect(screen.getByText("PII-HIGH")).toBeInTheDocument();
    expect(currentPath).toBe("/lineage");
  });

  it("点击影响分析图中的字段节点 → 字段信息抽屉（含所属表详情入口）", async () => {
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
        {
          id: 2,
          source_node: "field:orders.amount",
          target_node: "field:dws.amount",
          edge_type: "DERIVED_FROM",
          granularity: "L2",
          confidence: 1,
          provenance: "sqlglot",
          pii_inherited: false,
        },
      ],
      total: 2,
      page: 1,
      page_size: 50,
      has_more: false,
      nodes: [
        { id: "table:orders", type: "table", label: "orders", entity_id: 42, domain: "sales" },
        { id: "table:dws", type: "table", label: "dws", entity_id: 43, domain: "sales" },
        { id: "field:orders.amount", type: "field", label: "orders.amount", domain: "sales" },
        { id: "field:dws.amount", type: "field", label: "dws.amount", domain: "sales" },
      ],
    });
    graphMock.getNodeData.mockImplementation((id?: string) => {
      if (id === "field:orders.amount") {
        return { id: "field:orders.amount", data: { id: "field:orders.amount", type: "field", label: "orders.amount", domain: "sales" } } as never;
      }
      return undefined;
    });
    await openImpactTabAndQuery();
    const handler = impactNodeClickHandler();
    expect(handler).toBeDefined();
    await act(async () => {
      handler?.({ target: { id: "field:orders.amount" } });
    });
    // 字段信息抽屉：字段名 + 业务域 + 所属表（orders 在当前视图）
    await waitFor(() => expect(screen.getByText("字段信息")).toBeInTheDocument());
    expect(screen.getByText("orders.amount")).toBeInTheDocument();
    // 业务域 sales 在抽屉中展示（图谱图例也可能含 sales，故用 getAllByText）
    expect(screen.getAllByText("sales").length).toBeGreaterThan(0);
    // 所属表入口 → 打开表详情（entity_id 42）
    await act(async () => {
      screen.getByRole("button", { name: /查看所属表详情/ }).click();
    });
    await waitFor(() => expect(api.getCatalogDetail).toHaveBeenCalledWith(42));
    await waitFor(() => {
      expect(screen.getByText(/表 · orders/)).toBeInTheDocument();
    });
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

  it("点击运行历史「查看」展示该次运行的具体信息（SQL 原文 / 边明细）", async () => {
    const run = {
      id: 1,
      source: "sqlglot",
      run_at: "2026-08-01T10:00:00",
      status: "success",
      total_edges: 2,
      added_count: 2,
      updated_count: 0,
      missing_count: 0,
      stale_flagged_count: 0,
      restored_count: 0,
      error: null,
    };
    vi.mocked(api.lineageChannels).mockResolvedValue([
      { source: "sqlglot", edge_count: 18, node_count: 28, stale_count: 0, last_run: run },
    ]);
    vi.mocked(api.lineageChannelRuns).mockResolvedValue([run]);
    vi.mocked(api.lineageRunDetail).mockResolvedValue({
      ...run,
      detail: {
        kind: "sql_parse",
        sql: "INSERT INTO t SELECT id, name FROM s",
        dialect: "mysql",
        target_table: null,
        source_node: null,
        actor_id: 7,
        table_lineage: [{ source: "table:s", target: "table:t" }],
        field_lineage: [
          { source_table: "s", source_column: "id", target_table: "t", target_column: "id", expression: null },
          { source_table: "s", source_column: "name", target_table: "t", target_column: "name", expression: null },
        ],
      },
    });
    renderLineage();
    await waitFor(() => expect(api.lineageGraph).toHaveBeenCalled());
    await act(async () => {
      screen.getByRole("tab", { name: /采集通道/ }).click();
    });
    await waitFor(() => expect(api.lineageChannels).toHaveBeenCalled());
    // 点击通道卡片 → 加载运行历史
    fireEvent.click(screen.getByText("SQL 解析"));
    await waitFor(() => expect(api.lineageChannelRuns).toHaveBeenCalledWith("sqlglot"));
    await waitFor(() => {
      expect(screen.getByText(/运行历史 · SQL 解析/)).toBeInTheDocument();
    });
    // 点击「查看」→ 拉取单条运行详情 → Drawer 展示
    fireEvent.click(screen.getByRole("button", { name: /查看/ }));
    await waitFor(() => expect(api.lineageRunDetail).toHaveBeenCalledWith(1));
    await waitFor(() => {
      expect(screen.getByText(/运行详情 · SQL 解析/)).toBeInTheDocument();
    });
    // 具体信息：解析上下文（方言）、SQL 原文、表级/字段级边明细
    expect(screen.getByText("解析上下文")).toBeInTheDocument();
    expect(screen.getByText("方言")).toBeInTheDocument();
    expect(screen.getByText(/本次解析 · 表级边（1）/)).toBeInTheDocument();
    expect(screen.getByText(/本次解析 · 字段级边（2）/)).toBeInTheDocument();
    expect(screen.getByText("table:s")).toBeInTheDocument();
    expect(screen.getByText("table:t")).toBeInTheDocument();
    // SQL 原文以格式化 pre 展示（formatSql 换行排版）
    const pre = document.querySelector("pre");
    expect(pre?.textContent).toContain("INSERT");
    expect(pre?.textContent).toContain("FROM s");
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

describe("parseResultToGraphData 解析结果转血缘图谱", () => {
  it("合并表级/字段级边：表节点去 table: 前缀、字段节点拼 field:表.列", () => {
    const g = parseResultToGraphData({
      table_edges: 1,
      field_edges: 2,
      graph_written: true,
      table_lineage: [{ source: "table:s", target: "table:t" }],
      field_lineage: [
        { source_table: "a", source_column: "id", target_table: "t", target_column: "id", expression: null },
        { source_table: "b", source_column: "amount", target_table: "t", target_column: "amount", expression: "SUM(amount)" },
      ],
      upstream_deps: null,
    });
    // 节点：表 2 + 字段 4 = 6；边：表级 1 + 字段级 2 = 3
    expect(g.nodes).toHaveLength(6);
    expect(g.edges).toHaveLength(3);
    // 表节点保留 table: id，label 去前缀展示表名
    const tableNode = g.nodes.find((n) => n.id === "table:s");
    expect(tableNode?.type).toBe("table");
    expect(tableNode?.label).toBe("s");
    // 字段节点拼 field:表.列，label 展示 表.列
    const fieldNode = g.nodes.find((n) => n.id === "field:a.id");
    expect(fieldNode?.type).toBe("field");
    expect(fieldNode?.label).toBe("a.id");
    // 表级边与字段级边均透传 source/target 与 DERIVED_FROM 类型
    expect(g.edges.some((e) => e.source === "table:s" && e.target === "table:t")).toBe(true);
    expect(
      g.edges.some((e) => e.source === "field:a.id" && e.target === "field:t.id"),
    ).toBe(true);
  });

  it("source_column 为空（SELECT *）时字段源节点用 .* 占位，同表多边节点去重", () => {
    const g = parseResultToGraphData({
      table_edges: 0,
      field_edges: 2,
      graph_written: true,
      table_lineage: [],
      field_lineage: [
        { source_table: "s", source_column: null, target_table: "t", target_column: "id", expression: null },
        { source_table: "s", source_column: null, target_table: "t", target_column: "name", expression: null },
      ],
      upstream_deps: null,
    });
    // 源节点同为 field:s.*（去重），目标节点 field:t.id / field:t.name
    expect(g.nodes).toHaveLength(3);
    expect(g.nodes.some((n) => n.id === "field:s.*" && n.label === "s.*")).toBe(true);
    expect(g.edges).toHaveLength(2);
  });

  it("无任何血缘边时返回空图（解析页不展示图谱，走上游依赖/空态）", () => {
    const g = parseResultToGraphData({
      table_edges: 0,
      field_edges: 0,
      graph_written: false,
      table_lineage: [],
      field_lineage: [],
      upstream_deps: null,
    });
    expect(g.nodes).toHaveLength(0);
    expect(g.edges).toHaveLength(0);
  });
});

describe("upstreamDepsToGraphData 上游依赖转图谱", () => {
  it("中心「本次查询」+ 源表/字段节点，依赖边指向中心", () => {
    const g = upstreamDepsToGraphData({
      tables: ["ods_orders", "dim_user"],
      fields: ["ods_orders.id", "dim_user.name"],
    });
    // 节点：中心 1 + 表 2 + 字段 2 = 5；边：4 条依赖边
    expect(g.nodes).toHaveLength(5);
    expect(g.edges).toHaveLength(4);
    const query = g.nodes.find((n) => n.id === "query:本次查询");
    expect(query?.label).toBe("本次查询");
    expect(
      g.nodes.some((n) => n.id === "table:ods_orders" && n.label === "ods_orders" && n.type === "table"),
    ).toBe(true);
    expect(g.nodes.some((n) => n.id === "field:ods_orders.id" && n.type === "field")).toBe(true);
    // 所有依赖边统一指向中心节点
    for (const e of g.edges) {
      expect(e.target).toBe("query:本次查询");
      expect(e.type).toBe("READS_FROM");
    }
  });

  it("重复表/字段节点去重", () => {
    const g = upstreamDepsToGraphData({
      tables: ["ods_orders", "ods_orders"],
      fields: ["ods_orders.id", "ods_orders.id"],
    });
    // 中心 1 + 表 1 + 字段 1 = 3；边 2 条
    expect(g.nodes).toHaveLength(3);
    expect(g.edges).toHaveLength(2);
  });
});

describe("edgesToGraphData 合并节点元数据", () => {
  const edge = {
    id: 1,
    source_node: "table:a",
    target_node: "metric:m",
    edge_type: "DERIVED_FROM",
    granularity: "L1",
    confidence: 1,
    provenance: "sqlglot",
    pii_inherited: false,
  };

  it("合并后端节点元数据（entity_id/域/PII/Owner），未命中节点保持默认", () => {
    const g = edgesToGraphData([edge], [
      { id: "table:a", type: "table", label: "a", entity_id: 9, domain: "sales", pii: true, owner: "3" },
      { id: "metric:m", type: "metric", label: "m", domain: "finance" },
    ]);
    const tableA = g.nodes.find((n) => n.id === "table:a");
    expect(tableA?.entity_id).toBe(9);
    expect(tableA?.domain).toBe("sales");
    expect(tableA?.pii).toBe(true);
    expect(tableA?.owner).toBe("3");
    const metricM = g.nodes.find((n) => n.id === "metric:m");
    expect(metricM?.domain).toBe("finance");
    expect(metricM?.entity_id).toBeUndefined();
  });

  it("未提供节点元数据时构建的节点不含目录属性（向后兼容）", () => {
    const g = edgesToGraphData([edge]);
    const tableA = g.nodes.find((n) => n.id === "table:a");
    expect(tableA?.entity_id).toBeUndefined();
    expect(tableA?.domain).toBeUndefined();
    expect(tableA?.pii).toBeUndefined();
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
