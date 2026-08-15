import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, act, fireEvent, within } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { LineageView } from "../pages/LineageView";
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
    vi.mocked(api.parseLineage).mockResolvedValue({ table_edges: 1, field_edges: 2, graph_written: true });
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
});
