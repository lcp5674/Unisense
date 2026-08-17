import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within, fireEvent, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BrowserRouter, MemoryRouter, Routes, Route } from "react-router-dom";
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
  fetchAssetMetricDimensions: vi.fn(),
  fetchAssetTables: vi.fn(),
  fetchAssetOrphans: vi.fn(),
  fetchAssetEntityDetail: vi.fn(),
  fetchAssetSearch: vi.fn(),
  fetchAssetChanges: vi.fn(),
  fetchAssetMyAssets: vi.fn(),
  fetchAssetHealth: vi.fn(),
  fetchAssetPiiOverview: vi.fn(),
  lineageGraph: vi.fn(),
  assignAssetOwner: vi.fn(),
  reclassifyAssetSensitivity: vi.fn(),
  batchAssignAssetOwner: vi.fn(),
  batchReclassifyAssetSensitivity: vi.fn(),
  bulkDeprecateCatalogs: vi.fn(),
  listUsers: vi.fn(),
  fetchDescriptionCoverage: vi.fn(),
  inferColumnDescription: vi.fn(),
  inferDescriptions: vi.fn(),
  inferTableDescription: vi.fn(),
  updateColumnDescription: vi.fn(),
  updateTableDescription: vi.fn(),
  getMetric: vi.fn(),
  listSnapshots: vi.fn(),
  queryMetricInternal: vi.fn(),
  updateMetricDescription: vi.fn(),
  inferMetricDescription: vi.fn(),
  listCatalogs: vi.fn(),
  listDataSources: vi.fn(),
  listDomainTree: vi.fn(),
  listMetrics: vi.fn(),
  fetchCurrentUser: vi.fn(),
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
    getZoom: vi.fn(() => 1),
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
  fetchAssetMetricDimensions,
  fetchAssetTables,
  lineageGraph,
  fetchAssetOrphans,
  fetchAssetEntityDetail,
  fetchAssetSearch,
  fetchAssetChanges,
  fetchAssetMyAssets,
  fetchDescriptionCoverage,
  updateTableDescription,
  inferTableDescription,
  assignAssetOwner,
  reclassifyAssetSensitivity,
  batchAssignAssetOwner,
  batchReclassifyAssetSensitivity,
  bulkDeprecateCatalogs,
  listUsers,
  listCatalogs,
  listDataSources,
  listDomainTree,
  listMetrics,
  getMetric,
  listSnapshots,
  queryMetricInternal,
  updateMetricDescription,
  inferMetricDescription,
  fetchCurrentUser,
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
  owner_name: "Bob",
  role: "metric_owner",
  domain: "sales",
  metrics: {
    total: 50,
    published: 30,
    draft: 10,
    pii_count: 5,
    by_domain: { finance: 40, marketing: 10 },
    by_type: { atomic: 45, derived: 5 },
    by_metric_tier: { T1: 20, T2: 20, T3: 10 },
    snapshot_covered: 25,
    todo: { pii_unreviewed: 2, deprecated_without_successor: 1 },
  },
  catalogs: {
    total: 8,
    items: [
      {
        id: 1,
        entity_name: "catalog.db.orders",
        entity_type: "table",
        sensitivity_level: "PII",
        source_id: "s1",
        source_name: "Source A",
        updated_at: null,
      },
    ],
  },
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
    vi.mocked(lineageGraph).mockResolvedValue({
      nodes: [
        { id: "table:wedw_dwd.tjhis_dic_drug_df", type: "table", label: "tjhis_dic_drug_df" },
        { id: "table:wedw_dwd.tjhis_all_dic_drug_df", type: "table", label: "tjhis_all_dic_drug_df" },
      ],
      edges: [
        {
          source: "table:wedw_dwd.tjhis_dic_drug_df",
          target: "table:wedw_dwd.tjhis_all_dic_drug_df",
          type: "DERIVED_FROM",
        },
      ],
    });
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
    vi.mocked(fetchAssetMetricDimensions).mockResolvedValue({
      total: 10,
      by_type: { atomic: 8, derived: 2 },
      by_granularity: { day: 7, month: 3 },
      by_dw_layer: { DWS: 7, ADS: 3 },
      by_metric_tier: { T1: 3, T2: 2, T3: 5 },
      by_unit: { CNY: 3, cnt: 7 },
      by_currency: { CNY: 3, USD: 1 },
      by_aggregation: { SUM: 8, AVG: 2 },
      by_time_semantics: { PERIOD: 9, YTD: 1 },
      by_freshness: { T1: 6, HOURLY: 4 },
      by_serving_mode: { BATCH_ONLY: 7, REALTIME_ONLY: 3 },
      by_additivity: { ADDITIVE: 8, NON_ADDITIVE: 2 },
      by_status: { PUBLISHED: 5, DRAFT: 3, DEPRECATED: 2 },
      by_domain: { finance: 6, sales: 4 },
      pii_compliance: { pii_total: 4, pii_reviewed: 3, pii_unreviewed: 1, review_rate: 0.75 },
    });
    vi.mocked(fetchAssetTables).mockResolvedValue({ items: [], total: 0 });
    vi.mocked(fetchAssetOrphans).mockResolvedValue({ items: [], total: 0 });
    // 孤儿资产认领：当前登录用户
    vi.mocked(fetchCurrentUser).mockResolvedValue({
      id: 1,
      username: "admin",
      display_name: "管理员",
      role: "platform_admin",
      domain: null,
    } as never);
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
    vi.mocked(fetchAssetChanges).mockResolvedValue({
      catalogs: [],
      metrics: [],
      drift: [],
      days: 7,
    });
    vi.mocked(fetchAssetMyAssets).mockResolvedValue({
      owner_id: 1,
      catalogs: [],
      metrics: [],
      summary: {
        catalog_count: 0,
        metric_count: 0,
        draft_count: 0,
        pii_count: 0,
        snapshot_covered: 0,
        snapshot_total: 0,
      },
      claimable_orphans: 0,
    });
    vi.mocked(listCatalogs).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 });
    vi.mocked(listMetrics).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    // 数据表 Tab 数据源筛选候选（含名称）
    vi.mocked(listDataSources).mockResolvedValue({
      items: [
        {
          source_id: "s1",
          name: "销售库",
          source_type: "mysql",
          domain: "sales",
          cluster_id: null,
          coverage: 0,
          health_status: "healthy",
          connection_config_present: false,
          schedule_cron: null,
          collection_mode: "manual",
          enabled: true,
          created_by: 1,
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
        },
      ],
      total: 1,
      page: 1,
      page_size: 100,
    });
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
          catalog_id: 1, entity_name: "ods_order", source_id: "s1", source_name: "Sales MySQL",
          entity_type: "TABLE", domain: "sales", sensitivity_level: "INTERNAL", table_desc: false,
          description: null, description_source: null, owner_name: null,
          total_fields: 2, covered_fields: 1, missing_fields: 1,
          missing_field_names: ["id"], updated_at: "2026-08-14T02:30:00",
        },
        {
          catalog_id: 2, entity_name: "dwd_user", source_id: "s2", source_name: "Platform MySQL",
          entity_type: "TABLE", domain: "platform", sensitivity_level: "CONFIDENTIAL", table_desc: true,
          description: "用户明细表", description_source: "manual", owner_name: "张三",
          total_fields: 2, covered_fields: 2, missing_fields: 0,
          missing_field_names: [], updated_at: "2026-08-14T03:00:00",
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

  it("来源下拉锁定资产视角：不再提供血缘通道切换选项（方案A）", async () => {
    const user = userEvent.setup();
    renderAssetMap();

    // 默认资产视角：走 fetchAssetGraph，不调用 lineageGraph
    await waitFor(() => expect(fetchAssetGraph).toHaveBeenCalled());
    expect(lineageGraph).not.toHaveBeenCalled();

    // 展开来源下拉：只有「资产视角」，无血缘通道选项
    const sourceSelect = screen.getByText("来源：").closest(".ant-col") as HTMLElement;
    await user.click(within(sourceSelect).getByRole("combobox"));
    // 下拉与触发区都含「资产视角（采集目录）」（≥1 处即存在）
    const assetLabels = await screen.findAllByText("资产视角（采集目录）");
    expect(assetLabels.length).toBeGreaterThan(0);
    // 血缘通道选项已移除（下拉中不应出现）
    expect(screen.queryByText("全部血缘（含 DP/SQL/指标）")).not.toBeInTheDocument();
    expect(screen.queryByText("DP 同步血缘")).not.toBeInTheDocument();
  });

  it("点击完整血缘引导：跳转血缘视图（/lineage）", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/assetmap"]}>
        <Routes>
          <Route path="/lineage" element={<div>lineage-page</div>} />
          <Route path="/assetmap" element={<AssetMap />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByRole("heading", { name: "资产地图" })).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /完整血缘图谱/ }));
    await screen.findByText("lineage-page");
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

  it("metric drawer AI 推断（已有 LLM 描述）：按钮显示「重新生成」，确认后 force=true 重新推断", async () => {
    const baseMetric = {
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
    };
    vi.mocked(getMetric).mockResolvedValue({
      ...baseMetric,
      description: "AI 推断的每日营收总额描述",
      description_source: "llm",
    } as never);
    vi.mocked(listSnapshots).mockResolvedValue([] as never);
    vi.mocked(inferMetricDescription).mockResolvedValue({
      ...baseMetric,
      description: "AI 推断的每日营收总额描述",
      description_source: "llm",
    } as never);
    renderAssetMap();
    await waitFor(() => expect(fetchAssetGraph).toHaveBeenCalled());

    const clickHandler = g6GraphMock.on.mock.calls.find(([name]) => name === "node:click")?.[1] as
      ((evt: { target?: { id?: string } }) => void) | undefined;
    g6GraphMock.getNodeData.mockReturnValue({
      data: { id: "metric:m1", label: "finance_revenue_sum_d", type: "metric", domain: "finance" },
    });
    clickHandler?.({ target: { id: "metric:m1" } });

    // 已有 LLM 描述 → 按钮显示「重新生成」而非「AI 推断」（去重防线）
    await waitFor(() => expect(screen.getByText("重新生成")).toBeInTheDocument());
    // 点击不直接调接口，先弹确认框（antd confirm 标题渲染两处 → getAllByText）
    await userEvent.click(screen.getByText("重新生成"));
    await waitFor(() =>
      expect(screen.getAllByText("重新生成指标描述？").length).toBeGreaterThan(0),
    );
    expect(inferMetricDescription).not.toHaveBeenCalled();
    // 确认后以 force=true 重新推断
    await userEvent.click(screen.getByRole("button", { name: "确认重新生成" }));
    await waitFor(() =>
      expect(inferMetricDescription).toHaveBeenCalledWith("finance_revenue_sum_d", {
        force: true,
      }),
    );
  });

  it("metric drawer AI 推断（无描述）：按钮显示「AI 推断」，直接调用不带 force", async () => {
    const baseMetric = {
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
    };
    vi.mocked(getMetric).mockResolvedValue({ ...baseMetric, description: null } as never);
    vi.mocked(listSnapshots).mockResolvedValue([] as never);
    vi.mocked(inferMetricDescription).mockResolvedValue({
      ...baseMetric,
      description: "每日营收汇总",
      description_source: "llm",
    } as never);
    renderAssetMap();
    await waitFor(() => expect(fetchAssetGraph).toHaveBeenCalled());

    const clickHandler = g6GraphMock.on.mock.calls.find(([name]) => name === "node:click")?.[1] as
      ((evt: { target?: { id?: string } }) => void) | undefined;
    g6GraphMock.getNodeData.mockReturnValue({
      data: { id: "metric:m1", label: "finance_revenue_sum_d", type: "metric", domain: "finance" },
    });
    clickHandler?.({ target: { id: "metric:m1" } });

    // 无描述 → 按钮显示「AI 推断」，点击直接调用（无确认框、不带 force）
    await waitFor(() => expect(screen.getByText("AI 推断")).toBeInTheDocument());
    await userEvent.click(screen.getByText("AI 推断"));
    await waitFor(() =>
      expect(inferMetricDescription).toHaveBeenCalledWith("finance_revenue_sum_d", { force: false }),
    );
    // 推断成功后刷新描述展示（source=llm）
    await waitFor(() =>
      expect(screen.getByText("每日营收汇总")).toBeInTheDocument(),
    );
  });

  it("metric drawer 查询最新数据：真实查询并展示结果 + 刷新快照", async () => {
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
    vi.mocked(listSnapshots)
      .mockResolvedValueOnce([] as never)
      .mockResolvedValueOnce([
        {
          id: 1,
          metric_code: "finance_revenue_sum_d",
          version: 1,
          dims: {},
          date_range: "2026-08-01",
          value_json: { rows: [], total: 0, engine: "mysql" },
          quality_flag: null,
          generated_at: "2026-08-14T10:00:00Z",
          generated_by: "QUERY",
        },
      ] as never);
    vi.mocked(queryMetricInternal).mockResolvedValue({
      metric_code: "finance_revenue_sum_d",
      degraded: false,
      data: {
        rows: [
          { region: "east", revenue: 100.5 },
          { region: "west", revenue: 200 },
        ],
        total: 2,
        engine: "mysql",
      },
      execution_plan: {},
      meta: {},
    } as never);
    renderAssetMap();
    await waitFor(() => expect(fetchAssetGraph).toHaveBeenCalled());

    const clickHandler = g6GraphMock.on.mock.calls.find(([name]) => name === "node:click")?.[1] as
      ((evt: { target?: { id?: string } }) => void) | undefined;
    g6GraphMock.getNodeData.mockReturnValue({
      data: { id: "metric:m1", label: "finance_revenue_sum_d", type: "metric", domain: "finance" },
    });
    clickHandler?.({ target: { id: "metric:m1" } });
    await waitFor(() => expect(screen.getByText("营收汇总")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: /查询最新数据/ }));
    await waitFor(() =>
      expect(queryMetricInternal).toHaveBeenCalledWith("finance_revenue_sum_d", {
        dimensions: [],
        date_range: "",
      }),
    );
    // 展示本次真实查询结果
    await waitFor(() => expect(screen.getByText(/本次查询结果（2 行/)).toBeInTheDocument());
    expect(screen.getByText("east")).toBeInTheDocument();
    expect(screen.getByText("west")).toBeInTheDocument();
    // 快照列表已刷新（自动落库后回读，周期列展示 date_range）
    await waitFor(() => expect(screen.getByText("2026-08-01")).toBeInTheDocument());
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

  it("实体详情抽屉：废弃此资产调用单实体废弃接口并刷新", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetEntityDetail).mockResolvedValue({
      id: 5,
      entity_name: "sales.ods",
      entity_type: "TABLE",
      source_id: "s1",
      sensitivity_level: "INTERNAL",
      owner_id: null,
      schema_incomplete: false,
      content_signature: null,
    });
    vi.mocked(bulkDeprecateCatalogs).mockResolvedValue({
      succeeded: [{ source_id: "s1", entity_name: "sales.ods" }],
      failed: [],
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

    await waitFor(() => expect(screen.getByText(/实体详情/)).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /废弃此资产/ }));
    await user.click(screen.getByRole("button", { name: /确认废弃/ }));

    await waitFor(() => {
      expect(bulkDeprecateCatalogs).toHaveBeenCalledWith([
        { source_id: "s1", entity_name: "sales.ods" },
      ]);
    });
  });

  it("实体详情抽屉：表已有 LLM 描述时点「推断」先确认，确认后 force=true 重新生成", async () => {
    const user = userEvent.setup();
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
      schema_summary: [
        { name: "id", type: "bigint", description: "主键", description_source: "schema" },
      ],
      description: "已有 LLM 表描述",
      description_source: "llm",
    } as any);
    renderAssetMap();
    await waitFor(() => expect(fetchAssetGraph).toHaveBeenCalled());

    const clickHandler = g6GraphMock.on.mock.calls.find(([name]) => name === "node:click")?.[1] as
      ((evt: { target?: { id?: string } }) => void) | undefined;
    g6GraphMock.getNodeData.mockReturnValue({
      data: { id: "table:sales.ods", label: "sales.ods", type: "table", entity_id: 5 },
    });
    clickHandler?.({ target: { id: "table:sales.ods" } });
    await waitFor(() => expect(screen.getByText(/实体详情/)).toBeInTheDocument());

    // 已有 LLM 表描述 → 点「推断」不直接调接口，先弹确认框
    await user.click(screen.getByRole("button", { name: /推\s*断/ }));
    await waitFor(() =>
      expect(screen.getAllByText("重新生成表级描述？").length).toBeGreaterThan(0),
    );
    expect(inferTableDescription).not.toHaveBeenCalled();
    // 确认后以 force=true 重新生成
    await user.click(screen.getByRole("button", { name: "确认重新生成" }));
    await waitFor(() =>
      expect(inferTableDescription).toHaveBeenCalledWith(
        5,
        [{ name: "id", type: "bigint" }],
        true,
      ),
    );
  });

  it("实体详情抽屉：表无 LLM 描述时点「推断」直接调用（force=false）", async () => {
    const user = userEvent.setup();
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
      schema_summary: [],
      description: null,
      description_source: null,
    } as any);
    renderAssetMap();
    await waitFor(() => expect(fetchAssetGraph).toHaveBeenCalled());

    const clickHandler = g6GraphMock.on.mock.calls.find(([name]) => name === "node:click")?.[1] as
      ((evt: { target?: { id?: string } }) => void) | undefined;
    g6GraphMock.getNodeData.mockReturnValue({
      data: { id: "table:sales.ods", label: "sales.ods", type: "table", entity_id: 5 },
    });
    clickHandler?.({ target: { id: "table:sales.ods" } });
    await waitFor(() => expect(screen.getByText(/实体详情/)).toBeInTheDocument());

    // 无 LLM 描述 → 直接调用（force=false，无确认框）
    await user.click(screen.getByRole("button", { name: /推\s*断/ }));
    await waitFor(() =>
      expect(inferTableDescription).toHaveBeenCalledWith(5, [], false),
    );
  });

  it("click table node without entity_id falls back to catalog lookup", async () => {
    // Neo4j 图谱路径表节点可能不带 entity_id：按表名回查采集目录后打开详情
    vi.mocked(fetchAssetGraph).mockResolvedValue({
      nodes: [
        { id: "table:ods_orders", label: "ods_orders", type: "table", domain: "sales" },
      ],
      edges: [],
    });
    vi.mocked(listCatalogs).mockResolvedValue({
      items: [
        {
          id: 42,
          source_id: "s1",
          entity_name: "ods_orders",
          entity_type: "TABLE",
          schema_def: {},
          etl_sql: null,
          sensitivity_level: "INTERNAL",
          owner_id: null,
          upstream_signature: "",
          content_signature: null,
          schema_incomplete: false,
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
    });
    vi.mocked(fetchAssetEntityDetail).mockResolvedValue({
      id: 42,
      entity_name: "ods_orders",
      entity_type: "TABLE",
      source_id: "s1",
      sensitivity_level: "INTERNAL",
      owner_id: null,
      schema_incomplete: false,
      content_signature: null,
      pii_flag: false,
    });
    renderAssetMap();
    await waitFor(() => expect(fetchAssetGraph).toHaveBeenCalled());

    const clickHandler = g6GraphMock.on.mock.calls.find(([name]) => name === "node:click")?.[1] as
      ((evt: { target?: { id?: string } }) => void) | undefined;
    g6GraphMock.getNodeData.mockReturnValue({
      data: { id: "table:ods_orders", label: "ods_orders", type: "table", domain: "sales" },
    });
    clickHandler?.({ target: { id: "table:ods_orders" } });

    await waitFor(() =>
      expect(listCatalogs).toHaveBeenCalledWith(
        expect.objectContaining({ keyword: "ods_orders" }),
      ),
    );
    await waitFor(() => expect(fetchAssetEntityDetail).toHaveBeenCalledWith(42));
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

  it("overview 指标体系展示粒度等 13 类维度并可下钻", async () => {
    const user = userEvent.setup();
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("概览")).toBeInTheDocument());
    await user.click(screen.getByText("概览"));
    await waitFor(() => expect(fetchAssetMetricDimensions).toHaveBeenCalled());

    // 指标体系卡片标题
    expect(screen.getByText("指标体系")).toBeInTheDocument();
    // 粒度维度组及其值（mock: day 7 / month 3）——Tag 文本为「日 7」拆分节点，用函数匹配
    expect(screen.getByText("粒度")).toBeInTheDocument();
    const dayTag = screen.getByText((content) => content.includes("日") && content.includes("7"));
    expect(dayTag).toBeInTheDocument();
    // 其它新增维度组也渲染
    expect(screen.getByText("币种")).toBeInTheDocument();
    expect(screen.getByText("新鲜度")).toBeInTheDocument();
    expect(screen.getByText("服务模式")).toBeInTheDocument();
    expect(screen.getByText("可加性")).toBeInTheDocument();

    // 点击粒度 Tag → 打开分布明细抽屉
    await user.click(dayTag);
    await waitFor(() => expect(screen.getByText(/粒度分布明细/)).toBeInTheDocument());
    expect(screen.getByText("维度值")).toBeInTheDocument();
    expect(screen.getByText("指标数")).toBeInTheDocument();
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
          version: 3,
          owner_id: 1,
          change_type: "updated",
          updated_at: "2026-08-01T00:00:00Z",
        },
      ],
      drift: [],
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

  it("changes tab metric row click opens metric detail drawer", async () => {
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
          version: 3,
          owner_id: 1,
          change_type: "updated",
          updated_at: "2026-08-01T00:00:00Z",
        },
      ],
      drift: [],
      days: 7,
    });
    vi.mocked(getMetric).mockResolvedValue({
      id: 1,
      metric_code: "finance_revenue_sum_d",
      name: "收入",
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
      definition_json: { sql: "SELECT SUM(amount) FROM ods_order" },
      version: 3,
      status: "PUBLISHED",
      owner_id: 1,
      pii_flag: false,
    } as never);
    vi.mocked(listSnapshots).mockResolvedValue([] as never);
    renderAssetMap();

    await waitFor(() => expect(screen.getByRole("tab", { name: /变更追踪/ })).toBeInTheDocument());
    await user.click(screen.getByRole("tab", { name: /变更追踪/ }));
    await waitFor(() => expect(screen.getByText("收入")).toBeInTheDocument());
    // 点击行（名称单元格，非编码链接）→ 本页打开指标详情侧边栏抽屉，不跳转
    const beforePath = window.location.pathname;
    await user.click(screen.getByText("收入"));
    await waitFor(() => expect(screen.getByText(/变更指标详情：收入/)).toBeInTheDocument());
    expect(window.location.pathname).toBe(beforePath);
  });

  it("changes tab catalog row click opens entity detail drawer", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetChanges).mockResolvedValue({
      catalogs: [
        {
          id: 42,
          entity_name: "ods_order",
          entity_type: "table",
          sensitivity_level: "INTERNAL",
          owner_id: null,
          source_id: "s1",
          source_name: "Source A",
          created_at: null,
          updated_at: "2026-08-01T00:00:00Z",
          change_type: "updated",
        },
      ],
      metrics: [],
      drift: [],
      days: 7,
    });
    vi.mocked(fetchAssetEntityDetail).mockResolvedValue({
      id: 42,
      entity_name: "ods_order",
      entity_type: "table",
      source_id: "s1",
      source_name: "Source A",
      sensitivity_level: "INTERNAL",
      owner_id: null,
      schema_incomplete: false,
      schema_summary: [
        { name: "order_id", type: "bigint", comment: "订单ID" },
        { name: "amount", type: "decimal(18,2)", comment: "金额" },
      ],
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
    } as never);
    renderAssetMap();

    await waitFor(() => expect(screen.getByRole("tab", { name: /变更追踪/ })).toBeInTheDocument());
    await user.click(screen.getByRole("tab", { name: /变更追踪/ }));
    await waitFor(() => expect(screen.getByText("ods_order")).toBeInTheDocument());
    await user.click(screen.getByText("ods_order"));
    await waitFor(() => expect(screen.getByText(/变更目录详情：ods_order/)).toBeInTheDocument());
    expect(fetchAssetEntityDetail).toHaveBeenCalledWith(42);
    // 字段清单随详情加载展示
    await waitFor(() => expect(screen.getByText("order_id")).toBeInTheDocument());
  });

  it("changes tab catalog drawer jumps to /catalogs with from/focus params", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetChanges).mockResolvedValue({
      catalogs: [
        {
          id: 42,
          entity_name: "ods_order",
          entity_type: "table",
          sensitivity_level: "INTERNAL",
          owner_id: null,
          source_id: "s1",
          source_name: "Source A",
          created_at: null,
          updated_at: "2026-08-01T00:00:00Z",
          change_type: "updated",
        },
      ],
      metrics: [],
      drift: [],
      days: 7,
    });
    vi.mocked(fetchAssetEntityDetail).mockResolvedValue({
      id: 42,
      entity_name: "ods_order",
      entity_type: "table",
      source_id: "s1",
      source_name: "Source A",
      sensitivity_level: "INTERNAL",
      owner_id: null,
      schema_incomplete: false,
      schema_summary: [],
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
    } as never);
    renderAssetMap();

    await waitFor(() => expect(screen.getByRole("tab", { name: /变更追踪/ })).toBeInTheDocument());
    await user.click(screen.getByRole("tab", { name: /变更追踪/ }));
    await waitFor(() => expect(screen.getByText("ods_order")).toBeInTheDocument());
    await user.click(screen.getByText("ods_order"));
    await waitFor(() => expect(screen.getByText(/变更目录详情：ods_order/)).toBeInTheDocument());
    // 抽屉「在采集目录中查看」→ 应跳采集目录（/catalogs，非指标目录 /catalog）并带来源/定位参数
    await user.click(screen.getByText("在采集目录中查看"));
    await waitFor(() => expect(window.location.pathname).toBe("/catalogs"));
    const qs = new URLSearchParams(window.location.search);
    expect(qs.get("kw")).toBe("ods_order");
    expect(qs.get("focus")).toBe("ods_order");
    expect(qs.get("from")).toBe("变更追踪");
  });

  it("changes tab drift row click opens drift diff drawer", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetChanges).mockResolvedValue({
      catalogs: [],
      metrics: [],
      drift: [
        {
          id: 7,
          source_id: "s1",
          entity_name: "ods_order",
          change_type: "column_add",
          diff_json: {
            change_type: "column_add",
            diff_json: { columns_added: ["channel"] },
            before_schema: { columns: ["order_id", "amount"] },
            after_schema: { columns: ["order_id", "amount", "channel"] },
          },
          created_at: "2026-08-01T00:00:00Z",
        },
      ],
      days: 7,
    });
    renderAssetMap();

    await waitFor(() => expect(screen.getByRole("tab", { name: /变更追踪/ })).toBeInTheDocument());
    await user.click(screen.getByRole("tab", { name: /变更追踪/ }));
    await waitFor(() => expect(screen.getByText("column_add")).toBeInTheDocument());
    await user.click(screen.getByText("column_add"));
    await waitFor(() => expect(screen.getByText(/Schema 漂移详情：ods_order/)).toBeInTheDocument());
    // before/after 对照展示
    await waitFor(() => expect(screen.getByText("变更前")).toBeInTheDocument());
    expect(screen.getByText("变更后")).toBeInTheDocument();
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
      summary: {
        catalog_count: 0,
        metric_count: 1,
        draft_count: 1,
        pii_count: 0,
        snapshot_covered: 0,
        snapshot_total: 1,
      },
      claimable_orphans: 0,
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
    expect(screen.getByText(/Bob 指标明细（状态：PUBLISHED）/)).toBeInTheDocument();
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

  it("description coverage 字段描述覆盖率明细：展示覆盖率进度列", async () => {
    const user = userEvent.setup();
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("描述缺失")).toBeInTheDocument());
    await user.click(screen.getByText("描述缺失"));
    await waitFor(() => expect(screen.getByText("字段描述覆盖率")).toBeInTheDocument());

    const card = screen.getByText("字段描述覆盖率").closest(".ant-card") as HTMLElement;
    await user.click(within(card).getByText("查看明细"));

    await waitFor(() =>
      expect(screen.getByText(/字段描述覆盖率明细/)).toBeInTheDocument(),
    );
    const drawer = screen.getByRole("dialog") as HTMLElement;
    // 差异化列：覆盖率进度（区别于其他明细）
    expect(within(drawer).getByText("覆盖率")).toBeInTheDocument();
    expect(within(drawer).getByText("ods_order")).toBeInTheDocument();
  });

  it("description coverage 缺失字段明细：只列有缺失的表 + 缺失字段名 Tag", async () => {
    const user = userEvent.setup();
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("描述缺失")).toBeInTheDocument());
    await user.click(screen.getByText("描述缺失"));
    await waitFor(() => expect(screen.getByText("缺失字段数")).toBeInTheDocument());

    const card = screen.getByText("缺失字段数").closest(".ant-card") as HTMLElement;
    await user.click(within(card).getByText("查看明细"));

    await waitFor(() =>
      expect(screen.getByText(/缺失字段明细/)).toBeInTheDocument(),
    );
    const drawer = screen.getByRole("dialog") as HTMLElement;
    // 差异化列：缺失字段名 Tag（ods_order 缺 id）
    expect(within(drawer).getByText("缺失字段名")).toBeInTheDocument();
    expect(within(drawer).getByText("id")).toBeInTheDocument();
    // 无缺失字段的表（dwd_user）不应出现在此明细
    expect(within(drawer).queryByText("dwd_user")).not.toBeInTheDocument();
  });

  it("description coverage 缺表描述明细：展示表描述现状 + 责任人列", async () => {
    const user = userEvent.setup();
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("描述缺失")).toBeInTheDocument());
    await user.click(screen.getByText("描述缺失"));
    await waitFor(() => expect(screen.getByText("缺表描述")).toBeInTheDocument());

    const card = screen.getByText("缺表描述").closest(".ant-card") as HTMLElement;
    await user.click(within(card).getByText("查看明细"));

    await waitFor(() =>
      expect(screen.getByText(/缺表描述明细/)).toBeInTheDocument(),
    );
    const drawer = screen.getByRole("dialog") as HTMLElement;
    // 差异化列：责任人 + 表描述「缺失」标记
    expect(within(drawer).getByText("责任人")).toBeInTheDocument();
    // ods_order 缺表描述 → 在列；dwd_user 有描述 → 不在列
    expect(within(drawer).getByText("ods_order")).toBeInTheDocument();
    expect(within(drawer).queryByText("dwd_user")).not.toBeInTheDocument();
  });

  it("description coverage 全部表资产明细：展示责任人 + 更新时间（上海时区中文）", async () => {
    const user = userEvent.setup();
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("描述缺失")).toBeInTheDocument());
    await user.click(screen.getByText("描述缺失"));
    await waitFor(() => expect(screen.getByText("表总数")).toBeInTheDocument());

    const card = screen.getByText("表总数").closest(".ant-card") as HTMLElement;
    await user.click(within(card).getByText("查看明细"));

    await waitFor(() =>
      expect(screen.getByText(/全部表资产明细/)).toBeInTheDocument(),
    );
    const drawer = screen.getByRole("dialog") as HTMLElement;
    // 差异化列：责任人中文名 + 更新时间（上海时区）
    expect(within(drawer).getByText("责任人")).toBeInTheDocument();
    expect(within(drawer).getByText("张三")).toBeInTheDocument();
    expect(within(drawer).getByText("更新时间")).toBeInTheDocument();
    // 2026-08-14T02:30:00（UTC）→ 上海 10:30
    expect(within(drawer).getByText("2026年8月14日 10:30")).toBeInTheDocument();
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

  it("description coverage 下钻明细行点击：打开实体详情并关闭明细抽屉（防覆盖）", async () => {
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
      schema_summary: [{ name: "id", type: "bigint", description: "主键" }],
      description: "订单明细表",
      description_source: "manual",
    } as any);
    renderAssetMap();

    await waitFor(() => expect(screen.getByText("描述缺失")).toBeInTheDocument());
    await user.click(screen.getByText("描述缺失"));
    await waitFor(() => expect(screen.getByText("缺表描述")).toBeInTheDocument());

    // 打开「缺表描述明细」抽屉
    const card = screen.getByText("缺表描述").closest(".ant-card") as HTMLElement;
    await user.click(within(card).getByText("查看明细"));
    await waitFor(() => expect(screen.getByText(/缺表描述明细/)).toBeInTheDocument());
    const drillDrawer = screen.getByRole("dialog") as HTMLElement;
    expect(within(drillDrawer).getByText("ods_order")).toBeInTheDocument();

    // 点击明细中的行 → 打开实体详情抽屉；明细抽屉应关闭（让位，避免详情被覆盖）
    await user.click(within(drillDrawer).getByText("ods_order"));

    await waitFor(() => {
      expect(fetchAssetEntityDetail).toHaveBeenCalled();
      expect(screen.getByText(/订单明细表/)).toBeInTheDocument();
    });
    // 明细抽屉已关闭（标题不再渲染），详情抽屉成为唯一可见抽屉
    expect(screen.queryByText(/缺表描述明细/)).not.toBeInTheDocument();
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

  it("描述缺失 tab：推断进行中（退出再进）二次点击被模块级 in-flight 拦截", async () => {
    const user = userEvent.setup();
    let resolveInfer: (v: any) => void = () => {};
    vi.mocked(inferTableDescription).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveInfer = resolve;
        }),
    );
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

    try {
      // 第一次进入：打开详情抽屉并触发表级推断（挂起，模拟 LLM 慢调用）
      const first = renderAssetMap();
      await waitFor(() => expect(screen.getByText("描述缺失")).toBeInTheDocument());
      await user.click(screen.getByText("描述缺失"));
      await waitFor(() => expect(screen.getByText("ods_order")).toBeInTheDocument());
      await user.click(screen.getByText("ods_order"));
      await waitFor(() => expect(screen.getByText("暂无表级描述")).toBeInTheDocument());
      await user.click(screen.getByRole("button", { name: /推\s*断/ }));
      await waitFor(() => expect(inferTableDescription).toHaveBeenCalledTimes(1));

      // 模拟退出页面：卸载组件（模块级 inferInflight 不随组件卸载重置）
      first.unmount();

      // 重新进入：再次触发表级推断，应被模块级 Map 拦截，不再发第二次请求
      renderAssetMap();
      await waitFor(() => expect(screen.getByText("描述缺失")).toBeInTheDocument());
      await user.click(screen.getByText("描述缺失"));
      await waitFor(() => expect(screen.getByText("ods_order")).toBeInTheDocument());
      await user.click(screen.getByText("ods_order"));
      await waitFor(() => expect(screen.getByText("暂无表级描述")).toBeInTheDocument());
      await user.click(screen.getByRole("button", { name: /推\s*断/ }));

      await waitFor(() => {
        expect(inferTableDescription).toHaveBeenCalledTimes(1); // 仍为 1 次，第二次被拦截
        expect(screen.getByText("该表的表级推断正在进行中，请稍候")).toBeInTheDocument();
      });
    } finally {
      // 清理：resolve 挂起的推断，触发模块级 Map 清理，避免污染后续测试
      await act(async () => {
        resolveInfer({});
      });
    }
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

  it("数据表目录：业务域筛选触发按 domain 请求", async () => {
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
          domain: "sales",
        },
      ],
      total: 1,
    });
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("数据表")).toBeInTheDocument());
    await user.click(screen.getByText("数据表"));
    await waitFor(() => expect(fetchAssetTables).toHaveBeenCalled());

    const domainItem = screen.getByText("全部业务域").closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(within(domainItem).getByRole("combobox"));
    await user.click(await screen.findByText("销售域"));

    await waitFor(() => {
      expect(fetchAssetTables).toHaveBeenCalledWith(expect.objectContaining({ domain: "sales" }));
    });
    // 激活筛选计数出现
    expect(screen.getByText(/已筛选/)).toBeInTheDocument();
  });

  it("数据表目录：关键字搜索触发按 keyword 请求", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetTables).mockResolvedValue({ items: [], total: 0 });
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("数据表")).toBeInTheDocument());
    await user.click(screen.getByText("数据表"));
    await waitFor(() => expect(fetchAssetTables).toHaveBeenCalled());

    const search = screen.getByPlaceholderText("搜索表名 / 数据源");
    await user.type(search, "ods");
    await user.keyboard("{Enter}");

    await waitFor(() => {
      expect(fetchAssetTables).toHaveBeenCalledWith(expect.objectContaining({ keyword: "ods" }));
    });
  });

  it("数据表目录：重置筛选清空全部条件", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetTables).mockResolvedValue({ items: [], total: 0 });
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("数据表")).toBeInTheDocument());
    await user.click(screen.getByText("数据表"));
    await waitFor(() => expect(fetchAssetTables).toHaveBeenCalled());

    // 先设置一个筛选（敏感度=PII）
    const sensItem = screen.getByText("全部敏感度").closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(within(sensItem).getByRole("combobox"));
    await user.click(await screen.findByText("PII"));
    await waitFor(() => {
      expect(fetchAssetTables).toHaveBeenCalledWith(expect.objectContaining({ sensitivity: "PII" }));
    });

    await user.click(screen.getByRole("button", { name: /重\s*置\s*筛\s*选/ }));
    await waitFor(() => {
      // 最近一次请求不再携带 sensitivity 筛选
      const calls = vi.mocked(fetchAssetTables).mock.calls;
      const last = calls[calls.length - 1][0] ?? {};
      expect(last.sensitivity).toBeUndefined();
    });
  });

  it("提供统一的返回按钮（返回上一入口）", async () => {
    renderAssetMap();
    await waitFor(() => expect(screen.getByRole("heading", { name: "资产地图" })).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/lineage", "/assetmap"]}>
        <Routes>
          <Route path="/lineage" element={<div>lineage-page</div>} />
          <Route path="/assetmap" element={<AssetMap />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByRole("heading", { name: "资产地图" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("lineage-page");
    lengthSpy.mockRestore();
  });

  it("孤儿资产：合规统计条展示总数 / PII / 机密级孤儿数", async () => {
    vi.mocked(fetchAssetOrphans).mockResolvedValue({
      items: [
        {
          id: 1, source_id: "s1", entity_name: "ods_user", entity_type: "TABLE",
          sensitivity_level: "PII", owner_id: null, schema_incomplete: false,
        },
        {
          id: 2, source_id: "s2", entity_name: "ads_finance", entity_type: "TABLE",
          sensitivity_level: "CONFIDENTIAL", owner_id: null, schema_incomplete: true,
        },
        {
          id: 3, source_id: "s3", entity_name: "tmp_log", entity_type: "TABLE",
          sensitivity_level: "INTERNAL", owner_id: null, schema_incomplete: false,
        },
      ],
      total: 3,
    });
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("概览")).toBeInTheDocument());
    await userEvent.setup().click(screen.getByRole("tab", { name: /孤儿资产/ }));
    await waitFor(() => expect(fetchAssetOrphans).toHaveBeenCalled());

    const piiStat = screen
      .getByText("PII 孤儿", { selector: ".ant-statistic-title" })
      .closest(".ant-statistic") as HTMLElement;
    expect(within(piiStat).getByText("1")).toBeInTheDocument();
    const confStat = screen
      .getByText("机密级孤儿", { selector: ".ant-statistic-title" })
      .closest(".ant-statistic") as HTMLElement;
    expect(within(confStat).getByText("1")).toBeInTheDocument();
  });

  it("孤儿资产：关键字筛选触发按 keyword 请求", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetOrphans).mockResolvedValue({ items: [], total: 0 });
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("概览")).toBeInTheDocument());
    await user.click(screen.getByRole("tab", { name: /孤儿资产/ }));
    await waitFor(() => expect(fetchAssetOrphans).toHaveBeenCalled());

    const search = screen.getByPlaceholderText("搜索实体名 / 数据源");
    await user.type(search, "ods");
    await user.keyboard("{Enter}");

    await waitFor(() => {
      expect(fetchAssetOrphans).toHaveBeenCalledWith(
        expect.objectContaining({ keyword: "ods" }),
      );
    });
  });

  it("孤儿资产：单行认领调用 assignAssetOwner 归属当前用户", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetOrphans).mockResolvedValue({
      items: [
        {
          id: 5, source_id: "s1", entity_name: "sales.ods", entity_type: "TABLE",
          sensitivity_level: "INTERNAL", owner_id: null, schema_incomplete: false,
        },
      ],
      total: 1,
    });
    vi.mocked(assignAssetOwner).mockResolvedValue({ entity_id: 5, owner_id: 1 });
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("概览")).toBeInTheDocument());
    await user.click(screen.getByRole("tab", { name: /孤儿资产/ }));
    await waitFor(() => expect(screen.getByText("sales.ods")).toBeInTheDocument());

    const row = screen.getByText("sales.ods").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /认\s*领/ }));
    await user.click(screen.getByRole("button", { name: /确认认领/ }));

    await waitFor(() => {
      expect(assignAssetOwner).toHaveBeenCalledWith(5, 1);
    });
  });

  it("孤儿资产：批量认领（给我）调用 batchAssignAssetOwner 归属当前用户", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetOrphans).mockResolvedValue({
      items: [
        {
          id: 5, source_id: "s1", entity_name: "sales.ods", entity_type: "TABLE",
          sensitivity_level: "INTERNAL", owner_id: null, schema_incomplete: false,
        },
        {
          id: 6, source_id: "s1", entity_name: "sales.ods_ext", entity_type: "TABLE",
          sensitivity_level: "PII", owner_id: null, schema_incomplete: false,
        },
      ],
      total: 2,
    });
    vi.mocked(batchAssignAssetOwner).mockResolvedValue({ affected: 2, owner_id: 1, total: 2 });
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("概览")).toBeInTheDocument());
    await user.click(screen.getByRole("tab", { name: /孤儿资产/ }));
    await waitFor(() => expect(screen.getByText("sales.ods")).toBeInTheDocument());

    // 勾选两行
    const rows = screen.getAllByRole("row").slice(1); // 跳过表头
    for (const row of rows) {
      await user.click(within(row).getByRole("checkbox"));
    }
    await user.click(screen.getByRole("button", { name: /批量认领/ }));
    await user.click(screen.getByRole("button", { name: /确认认领/ }));

    await waitFor(() => {
      expect(batchAssignAssetOwner).toHaveBeenCalledWith([5, 6], 1);
    });
  });

  it("孤儿资产：转交指定责任人调用 assignAssetOwner", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchAssetOrphans).mockResolvedValue({
      items: [
        {
          id: 5, source_id: "s1", entity_name: "sales.ods", entity_type: "TABLE",
          sensitivity_level: "INTERNAL", owner_id: null, schema_incomplete: false,
        },
      ],
      total: 1,
    });
    vi.mocked(assignAssetOwner).mockResolvedValue({ entity_id: 5, owner_id: 1 });
    renderAssetMap();
    await waitFor(() => expect(screen.getByText("概览")).toBeInTheDocument());
    await user.click(screen.getByRole("tab", { name: /孤儿资产/ }));
    await waitFor(() => expect(screen.getByText("sales.ods")).toBeInTheDocument());

    const row = screen.getByText("sales.ods").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /转\s*交/ }));
    await waitFor(() => expect(screen.getByText("转交资产归属")).toBeInTheDocument());
    const dialog = screen.getByRole("dialog");
    fireEvent.mouseDown(within(dialog).getByRole("combobox"));
    await user.click(await screen.findByText("管理员 (#1)"));
    await user.click(screen.getByRole("button", { name: /确认转交/ }));

    await waitFor(() => {
      expect(assignAssetOwner).toHaveBeenCalledWith(5, 1);
    });
  });
});
