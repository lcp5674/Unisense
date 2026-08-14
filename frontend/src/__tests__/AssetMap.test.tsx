import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BrowserRouter } from "react-router-dom";
import { AssetMap } from "../pages/AssetMap";

// Mock API
vi.mock("../api", () => ({
  fetchAssetGraph: vi.fn(),
  fetchAssetHeatmap: vi.fn(),
  fetchAssetOwnerView: vi.fn(),
  fetchAssetSummary: vi.fn(),
  fetchAssetClassification: vi.fn(),
  fetchAssetMetricSummary: vi.fn(),
  fetchAssetTables: vi.fn(),
  fetchAssetOrphans: vi.fn(),
  fetchAssetEntityDetail: vi.fn(),
}));

vi.mock("@ant-design/charts", () => ({
  Pie: () => <div data-testid="mock-pie" />,
}));

// Mock @antv/g6：jsdom 无 canvas，G6 渲染必然失败；mock 让 AssetGraph 走正常路径
const { g6GraphMock } = vi.hoisted(() => ({
  g6GraphMock: {
    on: vi.fn(),
    render: vi.fn().mockResolvedValue(undefined),
    destroy: vi.fn(),
    getNodeData: vi.fn(() => ({ data: undefined })),
    getNeighborNodesData: vi.fn(() => []),
    setElementState: vi.fn(),
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
  fetchAssetHeatmap,
  fetchAssetOwnerView,
  fetchAssetSummary,
  fetchAssetClassification,
  fetchAssetMetricSummary,
  fetchAssetTables,
  fetchAssetOrphans,
  fetchAssetEntityDetail,
} from "../api";

const mockGraphData = {
  nodes: [
    { id: "m1", label: "finance_revenue_sum_d", type: "metric", domain: "finance" },
    { id: "m2", label: "finance_cost_sum_d", type: "metric", domain: "finance" },
  ],
  edges: [
    { source: "m1", target: "m2", type: "derives_from" },
  ],
};

const mockHeatmapData = {
  dimension: "domain",
  buckets: [
    { key: "finance", pii_count: 5, total: 40 },
    { key: "marketing", pii_count: 2, total: 30 },
  ],
};

const mockOwnerViewData = {
  owner_id: 1,
  metrics: { total: 50, published: 30, draft: 10, pii_count: 5, by_domain: { finance: 40, marketing: 10 } },
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
    vi.mocked(fetchAssetHeatmap).mockResolvedValue(mockHeatmapData);
    vi.mocked(fetchAssetOwnerView).mockResolvedValue(mockOwnerViewData);
    vi.mocked(fetchAssetSummary).mockResolvedValue({ total: 10, by_entity_type: { table: 8, field: 2 }, by_sensitivity: { PUBLIC: 6, PII: 4 }, orphan_assets: 1 });
    vi.mocked(fetchAssetClassification).mockResolvedValue({ by_sensitivity: { PUBLIC: 6, PII: 4 } });
    vi.mocked(fetchAssetMetricSummary).mockResolvedValue({ by_domain: { finance: 2 }, by_status: { PUBLISHED: 1 } });
    vi.mocked(fetchAssetTables).mockResolvedValue({ items: [], total: 0 });
    vi.mocked(fetchAssetOrphans).mockResolvedValue({ items: [], total: 0 });
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
      expect(fetchAssetHeatmap).toHaveBeenCalled();
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
      | ((evt: { target?: { id?: string } }) => void)
      | undefined;
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
      | ((evt: { target?: { id?: string } }) => void)
      | undefined;
    g6GraphMock.getNodeData.mockReturnValue({
      data: { id: "table:sales.ods", label: "sales.ods", type: "table", entity_id: 5, domain: "sales" },
    });
    clickHandler?.({ target: { id: "table:sales.ods" } });

    await waitFor(() => expect(fetchAssetEntityDetail).toHaveBeenCalledWith(5));
    expect(screen.getByText(/实体详情/)).toBeInTheDocument();
  });
});
