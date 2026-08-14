import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
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
  listCatalogs: vi.fn(),
  listMetrics: vi.fn(),
}));

vi.mock("@ant-design/charts", () => ({
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
  heatmapReadyRef: {
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
  listCatalogs,
  listMetrics,
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
    vi.mocked(fetchAssetChanges).mockResolvedValue({ catalogs: [], metrics: [], days: 7 });
    vi.mocked(fetchAssetMyAssets).mockResolvedValue({ owner_id: 1, catalogs: [], metrics: [] });
    vi.mocked(listCatalogs).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 });
    vi.mocked(listMetrics).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
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

  it("click metric node navigates to metric detail", async () => {
    renderAssetMap();
    await waitFor(() => expect(fetchAssetGraph).toHaveBeenCalled());

    const clickHandler = g6GraphMock.on.mock.calls.find(([name]) => name === "node:click")?.[1] as
      ((evt: { target?: { id?: string } }) => void) | undefined;
    expect(typeof clickHandler).toBe("function");
    g6GraphMock.getNodeData.mockReturnValue({
      data: { id: "metric:m1", label: "finance_revenue_sum_d", type: "metric", domain: "finance" },
    });
    clickHandler?.({ target: { id: "metric:m1" } });

    await waitFor(() => expect(window.location.pathname).toBe("/detail/finance_revenue_sum_d"));
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
      ((evt: { data?: { data?: { sensKey?: string; y?: string } } }) => void) | undefined;
    heatmapReadyRef.onReady?.({
      on: (name, fn) => {
        if (name === "element:click") cellClick = fn as typeof cellClick;
      },
    });
    cellClick?.({ data: { data: { sensKey: "PII", y: "sales" } } });

    await waitFor(() =>
      expect(listCatalogs).toHaveBeenCalledWith(
        expect.objectContaining({ sensitivity_level: "PII" }),
      ),
    );
    expect(screen.getByText(/sales · PII 资产明细/)).toBeInTheDocument();
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
});
