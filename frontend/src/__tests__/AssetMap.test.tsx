import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BrowserRouter } from "react-router-dom";
import { AssetMap } from "../pages/AssetMap";

// Mock API
vi.mock("../api", () => ({
  fetchAssetGraph: vi.fn(),
  fetchAssetHeatmap: vi.fn(),
  fetchAssetHeatmapMatrix: vi.fn(),
  fetchAssetOwnerView: vi.fn(),
  fetchAssetSummary: vi.fn(),
  fetchAssetClassification: vi.fn(),
  fetchAssetMetricSummary: vi.fn(),
  fetchAssetTables: vi.fn(),
  fetchAssetOrphans: vi.fn(),
  fetchAssetEntityDetail: vi.fn(),
  fetchAssetSearch: vi.fn(),
  fetchAssetChanges: vi.fn(),
  fetchAssetMyAssets: vi.fn(),
  fetchAssetHealth: vi.fn(),
  fetchAssetPiiOverview: vi.fn(),
  assignAssetOwner: vi.fn(),
  reclassifyAssetSensitivity: vi.fn(),
  batchAssignAssetOwner: vi.fn(),
  batchReclassifyAssetSensitivity: vi.fn(),
  listUsers: vi.fn(),
  fetchDescriptionCoverage: vi.fn(),
  inferColumnDescription: vi.fn(),
  inferDescriptions: vi.fn(),
  inferTableDescription: vi.fn(),
  updateColumnDescription: vi.fn(),
  updateTableDescription: vi.fn(),
  getMetric: vi.fn(),
  listSnapshots: vi.fn(),
  updateMetricDescription: vi.fn(),
  listCatalogs: vi.fn(),
  listDomainTree: vi.fn(),
  listMetrics: vi.fn(),
}));

vi.mock("@ant-design/charts", () => ({
  Bar: ({ onReady, data, yField, xField, colorField, isStack }: any) => {
    // 模拟 onReady 回调，让测试可以触发点击事件
    if (onReady) {
      const mockPlot = {
        on: vi.fn((event: string, handler: any) => {
          if (event === "element:click") {
            heatmapReadyRef.lastClickHandler = handler;
          }
        }),
      };
      onReady(mockPlot);
      // 同步到 heatmapReadyRef 兼容旧测试模式
      heatmapReadyRef.onReady = onReady;
    }
    return <div data-testid="mock-bar" data-rows={data?.length} data-yfield={yField} data-xfield={xField} data-colorfield={colorField} data-stack={String(isStack)} />;
  },
  Pie: () => <div data-testid="mock-pie" />,
  Heatmap: ({
    onReady,
  }: {
    onReady?: (plot: { on: (name: string, fn: (evt: unknown) => void) => void }) => void;
  }) => {
    heatmapReadyRef.onReady = onReady;
    return <div data-testid="mock-heatmap" />;
  },
}));

// 捕获 Heatmap 的 onReady，供单元格下钻测试手动触发 element:click
const { g6GraphMock, heatmapReadyRef } = vi.hoisted(() => ({
  g6GraphMock: {
    destroyed: false,
    on: vi.fn(),
    render: vi.fn().mockResolvedValue(undefined),
    destroy: vi.fn(),
    setData: vi.fn(),
    getNodeData: vi.fn<() => { data: Record<string, unknown> | undefined }>(() => ({
      data: undefined,
    })),
    getNeighborNodesData: vi.fn(() => []),
    setElementState: vi.fn(),
  },
  heatmapReadyRef: { lastClickHandler: undefined as ((evt: { data?: { data?: { sensKey?: string; domain?: string } } }) => void) | undefined,
    onReady: undefined as
      ((plot: { on: (name: string, fn: (evt: unknown) => void) => void }) => void) | undefined,
  },
}));
vi.mock("@antv/g6", () => ({
  Graph: vi.fn(() => g6GraphMock),
}));

// Mock useTracking hook（返回稳定引用，避免 effect 依赖反复触发）
const trackMock = vi.fn();
vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: trackMock }),
}));

import {
  fetchAssetGraph,
  fetchAssetHeatmapMatrix,
  fetchAssetOwnerView,
  fetchAssetSummary,
  fetchAssetClassification,
  fetchAssetMetricSummary,
  fetchAssetTables,
  fetchAssetOrphans,
  fetchAssetEntityDetail,
  fetchAssetSearch,
  fetchAssetChanges,
  fetchAssetMyAssets,
  fetchDescriptionCoverage,
  updateTableDescription,
  assignAssetOwner,
  reclassifyAssetSensitivity,
  batchAssignAssetOwner,
  batchReclassifyAssetSensitivity,
  listUsers,
  listCatalogs,
  listDomainTree,
  listMetrics,
  getMetric,
  listSnapshots,
  updateMetricDescription,
} from "../api";

const mockGraphData = {
  nodes: [
    { id: "m1", label: "finance_revenue_sum_d", type: "metric", domain: "finance" },
    { id: "m2", label: "finance_cost_sum_d", type: "metric", domain: "finance" },
  ],
  edges: [{ source: "m1", target: "m2", type: "derives_from" }],
};

const mockOwnerViewData = {
  owner_id: 1,
  metrics: {
    total: 50,
    published: 30,
    draft: 10,
    pii_count: 5,
    by_domain: { finance: 40, marketing: 10 },
  },
  catalogs: { total: 8 },
};

function renderAssetMap() {
  return render(
    <BrowserRouter>
      <AssetMap />
    </BrowserRouter>,
  );
}

describe("AssetMap", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchAssetGraph).mockResolvedValue(mockGraphData);
    vi.mocked(fetchAssetHeatmapMatrix).mockResolvedValue({
      cells: [
        { domain: "sales", sensitivity: "PII", count: 3, pii_count: 3 },
        { domain: "sales", sensitivity: "INTERNAL", count: 2, pii_count: 0 },
      ],
      columns: ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", "NEEDS_REVIEW"],
    });
    vi.mocked(fetchAssetOwnerView).mockResolvedValue(mockOwnerViewData);
    vi.mocked(fetchAssetSummary).mockResolvedValue({
      total: 10,
      by_entity_type: { table: 8, field: 2 },
      by_sensitivity: { PUBLIC: 6, PII: 4 },
      orphan_assets: 1,
    });
    vi.mocked(fetchAssetClassification).mockResolvedValue({
      by_sensitivity: { PUBLIC: 6, PII: 4 },
    });
    vi.mocked(fetchAssetMetricSummary).mockResolvedValue({
      by_domain: { finance: 2 },
      by_status: { PUBLISHED: 1 },
    });
    vi.mocked(fetchAssetTables).mockResolvedValue({ items: [], total: 0 });
    vi.mocked(fetchAssetOrphans).mockResolvedValue({ items: [], total: 0 });
    // 数据表 tab 治理设置的责任人候选（Owner 下拉选项）
    vi.mocked(listUsers).mockResolvedValue([
      {
        id: 1,
        username: "admin",
        display_name: "管理员",
        role: "platform_admin",
        domain: null,
        status: "active",
      },
    ]);
    vi.mocked(fetchAssetChanges).mockResolvedValue({ catalogs: [], metrics: [], days: 7 });
    vi.mocked(fetchAssetMyAssets).mockResolvedValue({ owner_id: 1, catalogs: [], metrics: [] });
    vi.mocked(listCatalogs).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 });
    vi.mocked(listMetrics).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    // 默认域树（含中文名，供热力 Tab 中文域展示断言）
    vi.mocked(listDomainTree).mockResolvedValue([
      {
        id: 1, code: "sales", name: "销售域", parent_id: null, level: 1,
        sort_order: 0, status: "active", metric_count: 3, children: [],
      },
      {
        id: 2, code: "finance", name: "财务域", parent_id: null, level: 1,
        sort_order: 1, status: "active", metric_count: 2, children: [],
      },
    ]);
    vi.mocked(fetchDescriptionCoverage).mockResolvedValue({
      total_tables: 2,
      tables_with_desc: 1,
      tables_missing_desc: 1,
      total_fields: 4,
      fields_with_desc: 2,
      fields_missing_desc: 2,
      per_table: [
        {
          catalog_id: 1, entity_name: "ods_order", source_id: "s1", entity_type: "TABLE",
          domain: "sales", sensitivity_level: "INTERNAL", table_desc: false,
          total_fields: 2, covered_fields: 1, missing_fields: 1,
        },
        {
          catalog_id: 2, entity_name: "dwd_user", source_id: "s2", entity_type: "TABLE",
          domain: "platform", sensitivity_level: "CONFIDENTIAL", table_desc: true,
          total_fields: 2, covered_fields: 2, missing_fields: 0,
        },
      ],
    });
  });

  it("renders with default graph tab", async () => {
    renderAssetMap();

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /资产地图/ })).toBeInTheDocument();
    });

    expect(screen.getByText("热力视图")).toBeInTheDocument();
    expect(screen.getByText("Owner 视图")).toBeInTheDocument();
  });

  it("loads graph data on mount", async () => {
    renderAssetMap();

    await waitFor(() => {
      expect(fetchAssetGraph).toHaveBeenCalled();
    });
  });

  it("switches to heatmap tab", async () => {
    const user = userEvent.setup();
    renderAssetMap();

    await waitFor(() => {
      expect(screen.getByText("热力视图")).toBeInTheDocument();
    });

    await user.click(screen.getByText("热力视图"));

    await waitFor(() => {
      expect(fetchAssetHeatmapMatrix).toHaveBeenCalled();
    });
  });

  it("switches to owner view tab", async () => {
    const user = userEvent.setup();
    renderAssetMap();

    await waitFor(() => {
      expect(screen.getByText("Owner 视图")).toBeInTheDocument();
    });

    await user.click(screen.getByText("Owner 视图"));

    await waitFor(() => {
      expect(fetchAssetOwnerView).toHaveBeenCalled();
    });
  });

  it("click metric node opens in-page metric drawer (no navigation)", async () => {
    vi.mocked(getMetric).mockResolvedValue({
      id: 1,
      metric_code: "finance_revenue_sum_d",
      name: "营收汇总",
      domain: "finance",
      type: "atomic",
      granularity: "day",
      unit: "yuan",
      aggregation: "SUM",
      time_semantics: "PERIOD",
      freshness: "T1",
      dw_layer: "DWD",
      metric_tier: "T2",
      serving_mode: "BATCH_ONLY",
      additivity: "ADDITIVE",
      definition_json: {
        sql: "SELECT SUM(amount) FROM ods_order",
        period: "day",
        measures: [{ name: "revenue", aggregation: "SUM" }],
      },
      version: 1,
      row_version: 1,
      status: "PUBLISHED",
      owner_id: 1,
      pii_flag: false,
      compliance_reviewed: true,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    } as never);
    vi.mocked(listSnapshots).mockResolvedValue([] as never);
    renderAssetMap();
    await waitFor(() => expect(fetchAssetGraph).toHaveBeenCalled());

    const clickHandler = g6GraphMock.on.mock.calls.find(([name]) => name === "node:click")?.[1] as
      ((evt: { target?: { id?: string } }) => void) | undefined;
    expect(typeof clickHandler).toBe("function");
    g6GraphMock.getNodeData.mockReturnValue({
      data: { id: "metric:m1", label: "finance_revenue_sum_d", type: "metric", domain: "finance" },
    });
    clickHandler?.({ target: { id: "metric:m1" } });

    // 本页打开指标详情抽屉（明细 + 补充描述），不再直接跳转指标详情
    await waitFor(() => expect(screen.getByText("营收汇总")).toBeInTheDocument());
    expect(getMetric).toHaveBeenCalledWith("finance_revenue_sum_d");
    expect(window.location.pathname).not.toBe("/detail/finance_revenue_sum_d");
  });

  it("metric drawer supports supplementing description", async () => {
    vi.mocked(getMetric).mockResolvedValue({
      id: 1,
      metric_code: "finance_revenue_sum_d",
      name: "营收汇总",
      domain: "finance",
      type: "atomic",
      granularity: "day",
      unit: "yuan",
      aggregation: "SUM",
      time_semantics: "PERIOD",
      freshness: "T1",
      dw_layer: "DWD",
      metric_tier: "T2",
      serving_mode: "BATCH_ONLY",
      additivity: "ADDITIVE",
      definition_json: {},
      version: 1,
      row_version: 1,
      status: "PUBLISHED",
      owner_id: 1,
      pii_flag: false,
      compliance_reviewed: true,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    } as never);
    vi.mocked(listSnapshots).mockResolvedValue([] as never);
    vi.mocked(updateMetricDescription).mockResolvedValue({
      ...vi.mocked(getMetric).mock.results[0]?.value,
      description: "每日营收总额",
      description_source: "manual",
    } as never);
    renderAssetMap();
    await waitFor(() => expect(fetchAssetGraph).toHaveBeenCalled());

    const clickHandler = g6GraphMock.on.mock.calls.find(([name]) => name === "node:click")?.[1] as
      ((evt: { target?: { id?: string } }) => void) | undefined;
    g6GraphMock.getNodeData.mockReturnValue({
      data: { id: "metric:m1", label: "finance_revenue_sum_d", type: "metric", domain: "finance" },
    });
    clickHandler?.({ target: { id: "metric:m1" } });

    await waitFor(() => expect(screen.getByText("补充描述")).toBeInTheDocument());
    await userEvent.click(screen.getByText("补充描述"));
    await userEvent.type(screen.getByPlaceholderText(/补充指标的业务含义/), "每日营收总额");
    await userEvent.click(screen.getByRole("button", { name: /保\s*存/ }));

    await waitFor(() =>
      expect(updateMetricDescription).toHaveBeenCalledWith(
        "finance_revenue_sum_d",
        "每日营收总额",
      ),
    );
  });

  it("click table node opens entity detail drawer", async () => {
    vi.mocked(fetchAssetEntityDetail).mockResolvedValue({
      id: 5,
      entity_name: "sales.ods",
      entity_type: "TABLE",
      source_id: "s1",
      sensitivity_level: "PII",
      owner_id: null,
      schema_incomplete: false,
      content_signature: null,
      pii_flag: true,
    });
    renderAssetMap();
    await waitFor(() => expect(fetchAssetGraph).toHaveBeenCalled());

    const clickHandler = g6GraphMock.on.mock.calls.find(([name]) => name === "node:click")?.[1] as
      ((evt: { target?: { id?: string } }) => void) | undefined;
    g6GraphMock.getNodeData.mockReturnValue({
      data: {
        id: "table:sales.ods",
        label: "sales.ods",
        type: "table",
        entity_id: 5,
        domain: "sales",
      },
    });
    clickHandler?.({ target: { id: "table:sales.ods" } });

    await waitFor(() => expect(fetchAssetEntityDetail).toHaveBeenCalledWith(5));
    expect(screen.getByText(/实体详情/)).toBeInTheDocument();
  });

  it("overview statistic click drills into catalog detail", async () => {
    const user = userEvent.setup();
    renderAssetMap();
    // 切到概览 tab
    await waitFor(() => expect(screen.getByText("概览")).toBeInTheDocument());
    await user.click(screen.getByText("概览"));
    await waitFor(() => expect(fetchAssetSummary).toHaveBeenCalled());

    // 点击「目录资产总数」的值
    const totalValue = screen.getByText("10");
    await user.click(totalValue);

    await waitFor(() => expect(listCatalogs).toHaveBeenCalled());
    expect(screen.getByText(/目录资产明细/)).toBeInTheDocument();
  });

  it("overview orphan statistic drills into orphan detail", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetOrphans).mockResolvedValue({
      items: [
        {
          entity_name: "o1",
          entity_type: "TABLE",
          source_id: "s1",
          owner_id: null,
          schema_incomplete: false,
        },
      ],
      total: 1,
    });
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("概览")).toBeInTheDocument());
    await user.click(screen.getByText("概览"));
    await waitFor(() => expect(fetchAssetSummary).toHaveBeenCalled());

    // 定位「孤儿资产」Statistic 内的可点击值（避开同名 tab 标签）
    const orphanStat = screen
      .getByText("孤儿资产", { selector: ".ant-statistic-title" })
      .closest(".ant-statistic") as HTMLElement;
    await user.click(within(orphanStat).getByRole("link"));

    await waitFor(() => expect(fetchAssetOrphans).toHaveBeenCalled());
    expect(screen.getByText(/孤儿资产明细/)).toBeInTheDocument();
  });

  it("click field node opens field info drawer with table drill entry", async () => {
    vi.mocked(fetchAssetGraph).mockResolvedValue({
      nodes: [
        { id: "metric:m1", label: "m1", type: "metric", domain: "sales" },
        { id: "table:sales.ods", label: "sales.ods", type: "table", entity_id: 5, domain: "sales" },
        { id: "field:sales.ods.amount", label: "sales.ods.amount", type: "field", domain: "sales" },
      ],
      edges: [
        { source: "table:sales.ods", target: "field:sales.ods.amount", type: "contains" },
        { source: "field:sales.ods.amount", target: "metric:m1", type: "derives_from" },
      ],
    });
    renderAssetMap();
    await waitFor(() => expect(fetchAssetGraph).toHaveBeenCalled());

    const clickHandler = g6GraphMock.on.mock.calls.find(([name]) => name === "node:click")?.[1] as
      ((evt: { target?: { id?: string } }) => void) | undefined;
    g6GraphMock.getNodeData.mockReturnValue({
      data: {
        id: "field:sales.ods.amount",
        label: "sales.ods.amount",
        type: "field",
        domain: "sales",
      },
    });
    clickHandler?.({ target: { id: "field:sales.ods.amount" } });

    // 字段信息抽屉打开，且推导出所属表后提供「查看所属表详情」入口
    await waitFor(() => expect(screen.getByText("字段信息")).toBeInTheDocument());
    expect(screen.getByText("sales.ods.amount")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /查看所属表详情/ })).toBeInTheDocument();
  });

  it("click field node without table node still shows field info", async () => {
    vi.mocked(fetchAssetGraph).mockResolvedValue({
      nodes: [{ id: "field:only.col", label: "only.col", type: "field", domain: "sales" }],
      edges: [],
    });
    renderAssetMap();
    await waitFor(() => expect(fetchAssetGraph).toHaveBeenCalled());

    const clickHandler = g6GraphMock.on.mock.calls.find(([name]) => name === "node:click")?.[1] as
      ((evt: { target?: { id?: string } }) => void) | undefined;
    g6GraphMock.getNodeData.mockReturnValue({
      data: { id: "field:only.col", label: "only.col", type: "field", domain: "sales" },
    });
    clickHandler?.({ target: { id: "field:only.col" } });

    await waitFor(() => expect(screen.getByText("字段信息")).toBeInTheDocument());
    // 所属表不在图中：不渲染「查看所属表详情」按钮，避免无详情死路
    expect(screen.queryByRole("button", { name: /查看所属表详情/ })).not.toBeInTheDocument();
  });

  it("heatmap cell click drills into sensitivity catalog detail", async () => {
    const user = userEvent.setup();
    vi.mocked(listCatalogs).mockResolvedValue({
      items: [
        {
          source_id: "s1",
          entity_name: "sales.ods",
          entity_type: "TABLE",
          schema_def: {},
          etl_sql: null,
          sensitivity_level: "PII",
          owner_id: null,
          upstream_signature: "sig",
          content_signature: null,
          schema_incomplete: false,
        },
      ],
      total: 1,
      page: 1,
      page_size: 200,
    });
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("热力视图")).toBeInTheDocument());
    await user.click(screen.getByText("热力视图"));
    await waitFor(() => expect(fetchAssetHeatmapMatrix).toHaveBeenCalled());

    // 触发 Heatmap onReady，模拟单元格点击（data 携带 sensKey/y）
    expect(typeof heatmapReadyRef.onReady).toBe("function");
    let cellClick:
      ((evt: { data?: { data?: { sensKey?: string; domain?: string } } }) => void) | undefined;
    heatmapReadyRef.onReady?.({
      on: (name, fn) => {
        if (name === "element:click") cellClick = fn as typeof cellClick;
      },
    });
    cellClick?.({ data: { data: { sensKey: "PII", domain: "sales" } } });

    await waitFor(() =>
      expect(listCatalogs).toHaveBeenCalledWith(
        expect.objectContaining({ sensitivity_level: "PII" }),
      ),
    );
    expect(screen.getByText(/销售域 · PII 资产明细/)).toBeInTheDocument();
  });

  it("search tab metric row click navigates to metric detail", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetSearch).mockResolvedValue({
      items: [
        {
          type: "metric",
          id: 1,
          name: "finance_revenue_sum_d",
          entity_type: "metric",
          sensitivity_level: "INTERNAL",
          domain: "finance",
          owner_id: 1,
          status: "PUBLISHED",
        },
        {
          type: "catalog",
          id: 2,
          name: "sales.ods",
          entity_type: "TABLE",
          sensitivity_level: "PII",
          domain: null,
          owner_id: null,
          status: null,
        },
      ],
      total: 2,
    });
    renderAssetMap();

    await waitFor(() => expect(screen.getByRole("tab", { name: /搜索/ })).toBeInTheDocument());
    await user.click(screen.getByRole("tab", { name: /搜索/ }));

    await user.type(screen.getByPlaceholderText(/输入表名 \/ 字段名 \/ 指标编码/), "revenue");
    await user.click(screen.getByRole("button", { name: /搜\s*索/ }));

    // 指标行渲染为可点击链接
    await waitFor(() => expect(screen.getByText("finance_revenue_sum_d")).toBeInTheDocument());
    expect(screen.getByText("finance_revenue_sum_d").closest("a")).not.toBeNull();
    await user.click(screen.getByText("finance_revenue_sum_d"));
    await waitFor(() => expect(window.location.pathname).toBe("/detail/finance_revenue_sum_d"));
  });

  it("search tab catalog row is not a navigable link", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetSearch).mockResolvedValue({
      items: [
        {
          type: "catalog",
          id: 2,
          name: "sales.ods",
          entity_type: "TABLE",
          sensitivity_level: "PII",
          domain: null,
          owner_id: null,
          status: null,
        },
      ],
      total: 1,
    });
    renderAssetMap();

    await waitFor(() => expect(screen.getByRole("tab", { name: /搜索/ })).toBeInTheDocument());
    await user.click(screen.getByRole("tab", { name: /搜索/ }));

    await user.type(screen.getByPlaceholderText(/输入表名 \/ 字段名 \/ 指标编码/), "sales.ods");
    await user.click(screen.getByRole("button", { name: /搜\s*索/ }));

    await waitFor(() => expect(screen.getByText("sales.ods")).toBeInTheDocument());
    // 目录行名称不是链接（仅指标行可跳转详情）
    expect(screen.getByText("sales.ods").closest("a")).toBeNull();
  });

  it("changes tab metric row click navigates to metric detail", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetChanges).mockResolvedValue({
      catalogs: [],
      metrics: [
        {
          metric_code: "finance_revenue_sum_d",
          name: "收入",
          status: "PUBLISHED",
          domain: "finance",
          pii_flag: false,
          updated_at: "2026-08-01T00:00:00Z",
        },
      ],
      days: 7,
    });
    renderAssetMap();

    await waitFor(() => expect(screen.getByRole("tab", { name: /变更追踪/ })).toBeInTheDocument());
    await user.click(screen.getByRole("tab", { name: /变更追踪/ }));

    await waitFor(() => expect(screen.getByText("finance_revenue_sum_d")).toBeInTheDocument());
    expect(screen.getByText("finance_revenue_sum_d").closest("a")).not.toBeNull();
    await user.click(screen.getByText("finance_revenue_sum_d"));
    await waitFor(() => expect(window.location.pathname).toBe("/detail/finance_revenue_sum_d"));
  });

  it("my assets tab metric row click navigates to metric detail", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetMyAssets).mockResolvedValue({
      owner_id: 1,
      catalogs: [],
      metrics: [
        {
          metric_code: "finance_cost_sum_d",
          name: "成本",
          status: "DRAFT",
          domain: "finance",
          pii_flag: false,
        },
      ],
    });
    renderAssetMap();

    await waitFor(() => expect(screen.getByRole("tab", { name: /我的资产/ })).toBeInTheDocument());
    await user.click(screen.getByRole("tab", { name: /我的资产/ }));

    await waitFor(() => expect(screen.getByText("finance_cost_sum_d")).toBeInTheDocument());
    expect(screen.getByText("finance_cost_sum_d").closest("a")).not.toBeNull();
    await user.click(screen.getByText("finance_cost_sum_d"));
    await waitFor(() => expect(window.location.pathname).toBe("/detail/finance_cost_sum_d"));
  });

  // 触发热力单元格点击（通过 Heatmap onReady 捕获 element:click）
  function triggerHeatmapCellClick(sensKey: string, domain: string) {
    expect(typeof heatmapReadyRef.lastClickHandler).toBe("function");
    heatmapReadyRef.lastClickHandler?.({ data: { data: { sensKey, domain } } });
    return;
    // legacy path (kept for reference)

    expect(typeof heatmapReadyRef.onReady).toBe("function");
    let cellClick:
      | ((evt: { data?: { data?: { sensKey?: string; domain?: string } } }) => void)
      | undefined;
    heatmapReadyRef.onReady?.({
      on: (name, fn) => {
        if (name === "element:click") cellClick = fn as typeof cellClick;
      },
    });
    cellClick?.({ data: { data: { sensKey, domain } } });
  }

  it("heatmap switches to metric asset view and refetches", async () => {
    const user = userEvent.setup();
    renderAssetMap();

    await waitFor(() => expect(screen.getByText("热力视图")).toBeInTheDocument());
    await user.click(screen.getByText("热力视图"));
    await waitFor(() => expect(fetchAssetHeatmapMatrix).toHaveBeenCalled());

    // 切换到「指标资产」视角 → 应带 assetType=metric 重新请求
    await user.click(screen.getByText("指标资产"));
    await waitFor(() =>
      expect(fetchAssetHeatmapMatrix).toHaveBeenCalledWith(
        expect.stringMatching(/^metric$/),
      ),
    );
    // 标题随视角更新
    expect(screen.getByText(/指标资产分布/)).toBeInTheDocument();
  });

  it("heatmap cell drill filters by domain + sensitivity (catalog view)", async () => {
    const user = userEvent.setup();
    renderAssetMap();

    await waitFor(() => expect(screen.getByText("热力视图")).toBeInTheDocument());
    await user.click(screen.getByText("热力视图"));
    await waitFor(() => expect(fetchAssetHeatmapMatrix).toHaveBeenCalled());

    triggerHeatmapCellClick("PII", "sales");

    // 目录视角：域 + 敏感度双过滤（修复"点格子明细对不上"bug）
    await waitFor(() =>
      expect(listCatalogs).toHaveBeenCalledWith(
        expect.objectContaining({ sensitivity_level: "PII", domain: "sales" }),
      ),
    );
    expect(screen.getByText(/销售域 · PII 资产明细/)).toBeInTheDocument();
  });

  it("heatmap metric view cell drill filters by domain + pii_flag", async () => {
    const user = userEvent.setup();
    // 指标视角返回 INTERNAL/PII 两列矩阵
    vi.mocked(fetchAssetHeatmapMatrix).mockResolvedValue({
      cells: [
        { domain: "sales", sensitivity: "PII", count: 3, pii_count: 3 },
        { domain: "sales", sensitivity: "INTERNAL", count: 5, pii_count: 0 },
      ],
      columns: ["INTERNAL", "PII"],
    });
    renderAssetMap();

    await waitFor(() => expect(screen.getByText("热力视图")).toBeInTheDocument());
    await user.click(screen.getByText("热力视图"));
    await user.click(screen.getByText("指标资产"));
    await waitFor(() =>
      expect(fetchAssetHeatmapMatrix).toHaveBeenCalledWith(expect.stringMatching(/^metric$/)),
    );

    triggerHeatmapCellClick("PII", "sales");

    // 指标视角：域 + PII 过滤，抽屉用指标列
    await waitFor(() =>
      expect(listMetrics).toHaveBeenCalledWith(
        expect.objectContaining({ domain: "sales", pii_flag: true }),
      ),
    );
    expect(screen.getByText(/销售域 · PII 资产明细/)).toBeInTheDocument();
  });

  it("owner view statistic click drills into owner metric list", async () => {
    const user = userEvent.setup();
    renderAssetMap();

    await waitFor(() => expect(screen.getByText("Owner 视图")).toBeInTheDocument());
    await user.click(screen.getByText("Owner 视图"));
    // mock 无 owner 节点 → 回退责任人 #1，加载视图
    await waitFor(() => expect(fetchAssetOwnerView).toHaveBeenCalledWith(1));

    // 点击「已发布」统计值 30 → 按 owner_id + status=PUBLISHED 下钻
    await user.click(screen.getByText("30"));
    await waitFor(() =>
      expect(listMetrics).toHaveBeenCalledWith(
        expect.objectContaining({ owner_id: 1, status: "PUBLISHED" }),
      ),
    );
    expect(screen.getByText(/责任人 #1 指标明细（状态：PUBLISHED）/)).toBeInTheDocument();
  });

  it("description coverage tab shows stats and per-table rows", async () => {
    const user = userEvent.setup();
    renderAssetMap();

    await waitFor(() => expect(screen.getByText("描述缺失")).toBeInTheDocument());
    await user.click(screen.getByText("描述缺失"));

    await waitFor(() => {
      expect(screen.getByText("字段描述覆盖率")).toBeInTheDocument();
      expect(screen.getByText("ods_order")).toBeInTheDocument();
      expect(screen.getByText("dwd_user")).toBeInTheDocument();
      expect(fetchDescriptionCoverage).toHaveBeenCalled();
    });
  });

  it("description coverage row click opens detail drawer with table description", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetEntityDetail).mockResolvedValue({
      id: 1,
      entity_name: "ods_order",
      entity_type: "TABLE",
      source_id: "s1",
      sensitivity_level: "INTERNAL",
      owner_id: null,
      schema_incomplete: false,
      content_signature: "sig1",
      schema_summary: [
        { name: "id", type: "bigint", description: "主键" },
        { name: "name", type: "varchar" },
      ],
      description: "订单明细表",
      description_source: "manual",
    } as any);
    renderAssetMap();

    await waitFor(() => expect(screen.getByText("描述缺失")).toBeInTheDocument());
    await user.click(screen.getByText("描述缺失"));
    await waitFor(() => expect(screen.getByText("ods_order")).toBeInTheDocument());

    await user.click(screen.getByText("ods_order"));
    await waitFor(() => {
      expect(screen.getByText(/订单明细表/)).toBeInTheDocument();
      expect(screen.getByText("主键")).toBeInTheDocument();
    });
  });

  it("description coverage edit table description saves", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetEntityDetail).mockResolvedValue({
      id: 1,
      entity_name: "ods_order",
      entity_type: "TABLE",
      source_id: "s1",
      sensitivity_level: "INTERNAL",
      owner_id: null,
      schema_incomplete: false,
      content_signature: "sig1",
      schema_summary: [],
      description: null,
      description_source: null,
    } as any);
    vi.mocked(updateTableDescription).mockResolvedValue({
      catalog_id: 1,
      description: "新表描述",
      source: "manual",
      updated_by: 1,
      updated_at: null,
    });
    renderAssetMap();

    await waitFor(() => expect(screen.getByText("描述缺失")).toBeInTheDocument());
    await user.click(screen.getByText("描述缺失"));
    await waitFor(() => expect(screen.getByText("ods_order")).toBeInTheDocument());
    await user.click(screen.getByText("ods_order"));
    await waitFor(() => expect(screen.getByText("暂无表级描述")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /编辑/ }));
    const textarea = screen.getByRole("textbox");
    await user.clear(textarea);
    await user.type(textarea, "新表描述");
    await user.click(screen.getByRole("button", { name: "保存表描述" }));

    await waitFor(() => {
      expect(updateTableDescription).toHaveBeenCalledWith(1, "新表描述");
    });
  });

  it("数据表行设置：单条重分类敏感度调用 reclassifyAssetSensitivity", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetTables).mockResolvedValue({
      items: [
        {
          id: 5,
          source_id: "s1",
          entity_name: "sales.ods",
          entity_type: "TABLE",
          sensitivity_level: "INTERNAL",
          owner_id: null,
          schema_incomplete: false,
        },
      ],
      total: 1,
    });
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("数据表")).toBeInTheDocument());
    await user.click(screen.getByText("数据表"));
    await waitFor(() => expect(fetchAssetTables).toHaveBeenCalled());

    const row = screen.getByText("sales.ods").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /设\s*置/ }));

    await waitFor(() => {
      expect(screen.getByText("设置资产治理信息")).toBeInTheDocument();
    });
    const dialog = screen.getByRole("dialog");
    const sensItem = within(dialog).getByText("敏感度").closest(".ant-form-item") as HTMLElement;
    fireEvent.mouseDown(within(sensItem).getByRole("combobox"));
    await user.click(await screen.findByText("PII"));
    await user.click(screen.getByRole("button", { name: /保\s*存/ }));

    await waitFor(() => {
      expect(reclassifyAssetSensitivity).toHaveBeenCalledWith(5, "PII");
    });
  });

  it("数据表行设置：设置责任人调用 assignAssetOwner", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetTables).mockResolvedValue({
      items: [
        {
          id: 5,
          source_id: "s1",
          entity_name: "sales.ods",
          entity_type: "TABLE",
          sensitivity_level: "INTERNAL",
          owner_id: null,
          schema_incomplete: false,
        },
      ],
      total: 1,
    });
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("数据表")).toBeInTheDocument());
    await user.click(screen.getByText("数据表"));
    await waitFor(() => expect(fetchAssetTables).toHaveBeenCalled());

    const row = screen.getByText("sales.ods").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /设\s*置/ }));

    await waitFor(() => {
      expect(screen.getByText("设置资产治理信息")).toBeInTheDocument();
    });
    const dialog = screen.getByRole("dialog");
    const ownerItem = within(dialog).getByText("责任人").closest(".ant-form-item") as HTMLElement;
    fireEvent.mouseDown(within(ownerItem).getByRole("combobox"));
    await user.click(await screen.findByText("管理员 (#1)"));
    await user.click(screen.getByRole("button", { name: /保\s*存/ }));

    await waitFor(() => {
      expect(assignAssetOwner).toHaveBeenCalledWith(5, 1);
    });
  });

  it("数据表批量设置：勾选多行后批量分配责任人", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetTables).mockResolvedValue({
      items: [
        {
          id: 1,
          source_id: "s1",
          entity_name: "sales.ods",
          entity_type: "TABLE",
          sensitivity_level: "INTERNAL",
          owner_id: null,
          schema_incomplete: false,
        },
        {
          id: 2,
          source_id: "s1",
          entity_name: "sales.dwd",
          entity_type: "TABLE",
          sensitivity_level: "CONFIDENTIAL",
          owner_id: null,
          schema_incomplete: false,
        },
      ],
      total: 2,
    });
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("数据表")).toBeInTheDocument());
    await user.click(screen.getByText("数据表"));
    await waitFor(() => expect(fetchAssetTables).toHaveBeenCalled());

    // 表头全选（rowSelection 选择所有行）
    const checkboxes = screen.getAllByRole("checkbox");
    await user.click(checkboxes[0]);

    await user.click(screen.getByRole("button", { name: /批量设置/ }));

    await waitFor(() => {
      expect(screen.getByText("批量设置（2 项资产）")).toBeInTheDocument();
    });
    const dialog = screen.getByRole("dialog");
    const ownerItem = within(dialog).getByText("责任人").closest(".ant-form-item") as HTMLElement;
    fireEvent.mouseDown(within(ownerItem).getByRole("combobox"));
    await user.click(await screen.findByText("管理员 (#1)"));
    await user.click(screen.getByRole("button", { name: /保\s*存/ }));

    await waitFor(() => {
      expect(batchAssignAssetOwner).toHaveBeenCalledWith([1, 2], 1);
    });
  });

  it("数据表批量设置：批量重分类敏感度调用 batchReclassifyAssetSensitivity", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetTables).mockResolvedValue({
      items: [
        {
          id: 1,
          source_id: "s1",
          entity_name: "sales.ods",
          entity_type: "TABLE",
          sensitivity_level: "INTERNAL",
          owner_id: null,
          schema_incomplete: false,
        },
        {
          id: 2,
          source_id: "s1",
          entity_name: "sales.dwd",
          entity_type: "TABLE",
          sensitivity_level: "INTERNAL",
          owner_id: null,
          schema_incomplete: false,
        },
      ],
      total: 2,
    });
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("数据表")).toBeInTheDocument());
    await user.click(screen.getByText("数据表"));
    await waitFor(() => expect(fetchAssetTables).toHaveBeenCalled());

    const checkboxes = screen.getAllByRole("checkbox");
    await user.click(checkboxes[0]);
    await user.click(screen.getByRole("button", { name: /批量设置/ }));
    await waitFor(() => {
      expect(screen.getByText("批量设置（2 项资产）")).toBeInTheDocument();
    });
    const dialog = screen.getByRole("dialog");
    const sensItem = within(dialog).getByText("敏感度").closest(".ant-form-item") as HTMLElement;
    fireEvent.mouseDown(within(sensItem).getByRole("combobox"));
    await user.click(await screen.findByText("机密"));
    await user.click(screen.getByRole("button", { name: /保\s*存/ }));

    await waitFor(() => {
      expect(batchReclassifyAssetSensitivity).toHaveBeenCalledWith([1, 2], "CONFIDENTIAL");
    });
  });
});
