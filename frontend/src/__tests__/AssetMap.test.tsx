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
}));

// Mock useTracking hook
vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: vi.fn() }),
}));

import { fetchAssetGraph, fetchAssetHeatmap, fetchAssetOwnerView } from "../api";

const mockGraphData = {
  nodes: [
    { id: "m1", label: "finance_revenue_sum_d", type: "metric" },
    { id: "m2", label: "finance_cost_sum_d", type: "metric" },
  ],
  edges: [
    { source: "m1", target: "m2", type: "derives_from" },
  ],
};

const mockHeatmapData = {
  buckets: [
    { domain: "finance", pii_count: 5, total: 40, pii_ratio: 0.125 },
    { domain: "marketing", pii_count: 2, total: 30, pii_ratio: 0.067 },
  ],
};

const mockOwnerViewData = {
  owners: [
    { owner_id: 1, owner_name: "admin", metric_count: 50, pii_count: 10 },
    { owner_id: 2, owner_name: "analyst", metric_count: 30, pii_count: 5 },
  ],
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
  });

  it("renders with default graph tab", async () => {
    renderAssetMap();

    await waitFor(() => {
      expect(screen.getByText("图谱视图")).toBeInTheDocument();
    });

    expect(screen.getByText("热力视图")).toBeInTheDocument();
    expect(screen.getByText("Owner 视图")).toBeInTheDocument();
  });

  it("loads graph data on mount", async () => {
    renderAssetMap();

    await waitFor(() => {
      expect(fetchAssetGraph).toHaveBeenCalledOnce();
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
});
