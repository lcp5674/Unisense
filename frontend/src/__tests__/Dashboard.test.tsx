import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { BrowserRouter, MemoryRouter, useLocation } from "react-router-dom";
import { Dashboard } from "../pages/Dashboard";

// Mock API
vi.mock("../api", () => ({
  fetchDashboard: vi.fn(),
  fetchRecommendedMetrics: vi.fn(),
  fetchRecommendedTerms: vi.fn(),
}));

// Mock 图表库（jsdom 无 canvas 环境）
vi.mock("@ant-design/charts", () => ({
  Pie: () => <div data-testid="mock-pie" />,
  Bar: () => <div data-testid="mock-bar" />,
}));

// Mock useTracking hook（返回稳定引用，避免 effect 依赖反复触发）
const trackMock = vi.fn();
vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: trackMock }),
}));

import { fetchDashboard, fetchRecommendedMetrics, fetchRecommendedTerms } from "../api";
const mockedFetchDashboard = vi.mocked(fetchDashboard);

const mockDashboardData = {
  total: 100,
  by_status: { DRAFT: 25, EXPERIMENTAL: 3, REVIEW: 7, PUBLISHED: 60, DEPRECATED: 5 },
  by_tier: { T1: 20, T2: 50, T3: 30 },
  by_domain: { finance: 40, marketing: 30, growth: 20, risk: 10 },
  pii_count: 12,
  pii_ratio: 0.12,
  by_owner: {
    1: { name: "Alice", total: 60, by_status: { DRAFT: 20, REVIEW: 4, PUBLISHED: 36 } },
    2: { name: "Bob", total: 40, by_status: { DRAFT: 5, REVIEW: 3, PUBLISHED: 24, EXPERIMENTAL: 3, DEPRECATED: 5 } },
  },
  quality: { total: 9, by_severity: { P0: 2, P1: 3, P2: 4 }, pending: 5 },
  compliance: { total: 100, reviewed: 72, pending: 28, reviewed_ratio: 0.72 },
  conflict: { total: 4, open: 3, escalated: 1, by_status: { OPEN: 2, NEGOTIATING: 1, ESCALATED: 1 } },
  freshness: { total: 100, updated_30d: 34, updated_30d_ratio: 0.34 },
  assets: {
    metric: { total: 100, by_status: { DRAFT: 25, EXPERIMENTAL: 3, REVIEW: 7, PUBLISHED: 60, DEPRECATED: 5 } },
    table: { total: 40, by_status: { PUBLIC: 5, INTERNAL: 25, CONFIDENTIAL: 6, PII: 3, NEEDS_REVIEW: 1 } },
    source: { total: 8, by_status: { healthy: 6, unhealthy: 1, unknown: 1 } },
    dimension: { total: 15, by_status: { DRAFT: 2, PUBLISHED: 12, DEPRECATED: 1 } },
    term: { total: 22, by_status: { DRAFT: 4, PUBLISHED: 17, DEPRECATED: 1 } },
    template: { total: 10, by_status: { active: 9, inactive: 1 } },
    collection_task: { total: 6, by_status: { QUEUED: 1, RUNNING: 1, COMPLETED: 3, FAILED: 1 } },
    system_dict: { total: 30, by_status: { active: 28, inactive: 2 } },
  },
};

function renderDashboard() {
  return render(
    <BrowserRouter>
      <Dashboard />
    </BrowserRouter>,
  );
}

/** 渲染 Dashboard 并捕获路由跳转结果（MemoryRouter + useLocation 探针）。 */
function renderWithLocation() {
  let captured: { pathname: string; search: string } | null = null;
  function Probe() {
    captured = useLocation();
    return null;
  }
  render(
    <MemoryRouter initialEntries={["/dashboard"]}>
      <Probe />
      <Dashboard />
    </MemoryRouter>,
  );
  return {
    location: () => captured,
  };
}

describe("Dashboard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedFetchDashboard.mockResolvedValue(mockDashboardData);
    vi.mocked(fetchRecommendedMetrics).mockResolvedValue([]);
    vi.mocked(fetchRecommendedTerms).mockResolvedValue([]);
  });

  it("shows loading state initially", () => {
    mockedFetchDashboard.mockReturnValue(new Promise(() => {})); // never resolves
    const { container } = renderDashboard();
    expect(container.querySelector(".ant-spin-spinning")).toBeTruthy();
  });

  it("renders dashboard data after successful fetch", async () => {
    renderDashboard();

    await waitFor(() => {
      // KPI 指标总数 + 指标资产卡 total 均为 100
      expect(screen.getAllByText("100").length).toBeGreaterThan(0);
    });

    expect(screen.getAllByText("60").length).toBeGreaterThan(0);
    expect(screen.getAllByText("25").length).toBeGreaterThan(0);
    expect(screen.getByText("总览仪表")).toBeInTheDocument();
  });

  it("shows error message on fetch failure", async () => {
    mockedFetchDashboard.mockRejectedValue(new Error("网络错误"));
    renderDashboard();

    await waitFor(() => {
      expect(screen.getByText(/加载失败/)).toBeInTheDocument();
    });
    expect(screen.getByText(/网络错误/)).toBeInTheDocument();
  });

  it("calls fetchDashboard on mount", () => {
    renderDashboard();
    expect(mockedFetchDashboard).toHaveBeenCalledOnce();
  });

  it("资产总览：渲染全部 8 类资产卡片与计数", async () => {
    const { container } = renderDashboard();
    await waitFor(() => expect(screen.getByText("资产总览")).toBeInTheDocument());

    // 8 类资产名称
    for (const label of ["指标", "数据表", "数据源", "维度", "术语", "指标模板", "采集任务", "数据字典"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    // 各资产总数（仅限资产卡 .ac-total，避免与 Owner 分布/治理卡的重复数字歧义）
    const acTotals = Array.from(container.querySelectorAll(".ac-total")).map((el) => el.textContent);
    for (const total of ["40", "8", "15", "22", "10", "30"]) {
      expect(acTotals).toContain(total);
    }
    // 数据表 INTERNAL=25（与指标 DRAFT=25 重复出现，用 getAllByText）
    expect(screen.getAllByText("25").length).toBeGreaterThan(0);
    // 数据源 healthy=6 / 采集任务 total=6
    expect(screen.getAllByText("6").length).toBeGreaterThan(0);
  });

  it("资产卡片下钻：点击资产名跳转对应目录（无状态）", async () => {
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("资产总览")).toBeInTheDocument());

    fireEvent.click(screen.getByText("数据表"));
    expect(probe.location()?.pathname).toBe("/catalogs");
  });

  it("资产卡片下钻：点击状态段跳转对应目录并携带状态参数", async () => {
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("资产总览")).toBeInTheDocument());

    // 数据表 → PII 敏感段
    fireEvent.click(screen.getByText("PII"));
    expect(probe.location()?.pathname).toBe("/catalogs");
    expect(probe.location()?.search).toContain("sensitivity=PII");
  });

  it("资产卡片下钻：采集任务状态段携带状态参数", async () => {
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("资产总览")).toBeInTheDocument());

    // 采集任务 → RUNNING 段（「采集中」标签唯一）
    fireEvent.click(screen.getByText("采集中"));
    expect(probe.location()?.pathname).toBe("/collection-tasks");
    expect(probe.location()?.search).toContain("status=RUNNING");
  });

  it("KPI 读数格去重：不再展示与信号条重复的已发布/待审核/草稿中", async () => {
    renderDashboard();
    await waitFor(() => expect(screen.getByText("指标总数")).toBeInTheDocument());

    // 信号条仍展示（生命周期五站保留）
    expect(document.querySelector(".lifecycle-track")).toBeTruthy();
    // KPI 读数格（.g-label）不再有这三个重复读数
    const gaugeLabels = Array.from(document.querySelectorAll(".g-label")).map((el) => el.textContent);
    expect(gaugeLabels).not.toContain("已发布");
    expect(gaugeLabels).not.toContain("待审核");
    expect(gaugeLabels).not.toContain("草稿中");
  });

  it("Owner 责任分布：渲染各 Owner 总数/待审积压/已发布，待审>0 高亮", async () => {
    renderDashboard();
    await waitFor(() => expect(screen.getByText("Owner 责任分布")).toBeInTheDocument());

    expect(screen.getByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("Bob")).toBeInTheDocument();
    // 待审积压 > 0 的 Owner 行高亮（Alice REVIEW=4 / Bob REVIEW=3 都有）
    expect(document.querySelectorAll(".owner-hot").length).toBeGreaterThan(0);
  });

  it("Owner 分布以图表样式展示：堆积条各段宽度与状态构成对应", async () => {
    const { container } = renderDashboard();
    await waitFor(() => expect(screen.getByText("Owner 责任分布")).toBeInTheDocument());

    // 每个 Owner 一行：名字 + 堆积条 + 总数（Alice total=60 排前）
    const rows = Array.from(container.querySelectorAll(".owner-row"));
    expect(rows.length).toBe(2);
    const aliceBar = rows[0].querySelector(".owner-bar");
    expect(aliceBar).toBeTruthy();
    // Alice: DRAFT=20 / REVIEW=4 / PUBLISHED=36，total=60 → 宽度 33.33% / 6.67% / 60%
    const segs = Array.from(aliceBar!.querySelectorAll(".ob-seg"));
    expect(segs.length).toBe(3);
    expect(segs[0].getAttribute("style")).toContain("33.33%");
    expect(segs[1].getAttribute("style")).toContain("6.67%");
    expect(segs[2].getAttribute("style")).toContain("60%");
    // 每段标注状态名（可读性，非仅色块）
    expect(segs[0].textContent).toContain("草稿");
    expect(segs[1].textContent).toContain("审核");
    expect(segs[2].textContent).toContain("已发布");
  });

  it("Owner 下钻：点击 Owner 跳转 /catalog?owner_id=", async () => {
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("Owner 责任分布")).toBeInTheDocument());

    fireEvent.click(screen.getByText("Alice"));
    expect(probe.location()?.pathname).toBe("/catalog");
    expect(probe.location()?.search).toContain("owner_id=1");
  });

  it("治理指标卡：质量健康渲染严重级分布与待处理", async () => {
    renderDashboard();
    await waitFor(() => expect(screen.getByText("质量健康")).toBeInTheDocument());

    expect(screen.getByText("P0")).toBeInTheDocument();
    expect(screen.getByText("P1")).toBeInTheDocument();
    expect(screen.getByText("P2")).toBeInTheDocument();
    expect(screen.getByText(/待处理/)).toBeInTheDocument();
  });

  it("治理指标卡：合规渲染复核率", async () => {
    renderDashboard();
    await waitFor(() => expect(screen.getByText("合规复核")).toBeInTheDocument());

    // 72% 复核率（compliance 卡内）
    expect(screen.getByText("72%")).toBeInTheDocument();
  });

  it("治理指标卡：冲突风险渲染待仲裁与升级中", async () => {
    renderDashboard();
    await waitFor(() => expect(screen.getByText("冲突风险")).toBeInTheDocument());

    expect(screen.getByText(/待仲裁/)).toBeInTheDocument();
    expect(screen.getByText(/升级中/)).toBeInTheDocument();
  });

  it("治理指标卡：新鲜度渲染近 30 天更新", async () => {
    renderDashboard();
    await waitFor(() => expect(screen.getByText("近 30 天更新")).toBeInTheDocument());

    expect(screen.getByText("34")).toBeInTheDocument();
  });

  it("治理指标卡下钻：质量→/quality、冲突→/review", async () => {
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("质量健康")).toBeInTheDocument());

    fireEvent.click(screen.getByText("质量健康"));
    expect(probe.location()?.pathname).toBe("/quality");
  });
});

describe("Dashboard 推荐卡片", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedFetchDashboard.mockResolvedValue(mockDashboardData);
    vi.mocked(fetchRecommendedMetrics).mockResolvedValue([]);
    vi.mocked(fetchRecommendedTerms).mockResolvedValue([]);
  });

  it("渲染推荐指标卡片：展示 metric_id 与后端下发的 reason 文案", async () => {
    vi.mocked(fetchRecommendedMetrics).mockResolvedValue([
      {
        metric_id: "sales_gmv",
        via: "collaborative_filtering",
        score: 0.667,
        edge_type: "CF_RECOMMEND",
        reason: "与你行为相似的同事也关注",
      },
      {
        metric_id: "sales_uv",
        via: "global_hot",
        edge_type: "POPULAR",
        reason: "全站热门指标",
      },
    ]);
    renderDashboard();

    await waitFor(() => expect(screen.getByText("sales_gmv")).toBeInTheDocument());
    expect(screen.getByText(/与你行为相似的同事也关注/)).toBeInTheDocument();
    expect(screen.getByText(/全站热门指标/)).toBeInTheDocument();
    // 协同过滤项展示相似度（0.667 → 67%）
    expect(screen.getByText(/67%/)).toBeInTheDocument();
    expect(screen.getByText("sales_uv")).toBeInTheDocument();
  });

  it("推荐卡片无 reason 时按 via/edge_type 渲染兜底文案", async () => {
    vi.mocked(fetchRecommendedMetrics).mockResolvedValue([
      { metric_id: "m_lineage", via: "m_seed", edge_type: "LINEAGE" },
    ]);
    renderDashboard();

    await waitFor(() => expect(screen.getByText("m_lineage")).toBeInTheDocument());
    expect(screen.getByText(/血缘 · 关联/)).toBeInTheDocument();
  });

  it("推荐为空时展示引导文案（空态）", async () => {
    vi.mocked(fetchRecommendedMetrics).mockResolvedValue([]);
    renderDashboard();

    await waitFor(() => expect(screen.getByText(/暂无推荐（去指标目录逛逛/)).toBeInTheDocument());
    expect(screen.getByText(/很快就有专属推荐/)).toBeInTheDocument();
  });

  it("查看更多推荐：点击后拉取更多并去重合并", async () => {
    vi.mocked(fetchRecommendedMetrics)
      .mockResolvedValueOnce([{ metric_id: "m1", via: "global_hot", edge_type: "POPULAR", reason: "全站热门指标" }])
      .mockResolvedValueOnce([
        { metric_id: "m1", via: "global_hot", edge_type: "POPULAR", reason: "全站热门指标" },
        { metric_id: "m2", via: "global_hot", edge_type: "POPULAR", reason: "全站热门指标" },
        { metric_id: "m3", via: "global_hot", edge_type: "POPULAR", reason: "全站热门指标" },
        { metric_id: "m4", via: "global_hot", edge_type: "POPULAR", reason: "全站热门指标" },
        { metric_id: "m5", via: "global_hot", edge_type: "POPULAR", reason: "全站热门指标" },
        { metric_id: "m6", via: "global_hot", edge_type: "POPULAR", reason: "全站热门指标" },
      ]);
    renderDashboard();
    await waitFor(() => expect(screen.getByText("m1")).toBeInTheDocument());

    // 初始 1 条 < 6，仍显示「查看更多推荐」
    const moreBtn = screen.getByRole("button", { name: /查看更多推荐/ });
    fireEvent.click(moreBtn);

    // 合并去重：m1 不重复出现，m2~m6 被追加
    await waitFor(() => expect(screen.getByText("m2")).toBeInTheDocument());
    expect(screen.getAllByText("m1")).toHaveLength(1);
    expect(screen.getByText("m6")).toBeInTheDocument();
  });

  it("查看更多无新增候选时判定已展示全部（不再显示查看更多）", async () => {
    // 后端候选集很小：两次请求返回同一批指标（如库中 PUBLISHED 指标本身少于 limit）
    vi.mocked(fetchRecommendedMetrics)
      .mockResolvedValueOnce([
        { metric_id: "m1", via: "latest_published", edge_type: "RECENT", reason: "最新发布指标" },
        { metric_id: "m2", via: "latest_published", edge_type: "RECENT", reason: "最新发布指标" },
      ])
      .mockResolvedValueOnce([
        { metric_id: "m1", via: "latest_published", edge_type: "RECENT", reason: "最新发布指标" },
        { metric_id: "m2", via: "latest_published", edge_type: "RECENT", reason: "最新发布指标" },
      ]);
    renderDashboard();
    await waitFor(() => expect(screen.getByText("m1")).toBeInTheDocument());

    const moreBtn = screen.getByRole("button", { name: /查看更多推荐/ });
    fireEvent.click(moreBtn);

    // 去重后无新增 → 按钮变为「已展示全部推荐」且禁用，不再"点了没反应"
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: /已展示全部推荐/ });
      expect(btn).toBeDisabled();
    });
    // 列表无新增重复项
    expect(screen.getAllByText("m1")).toHaveLength(1);
  });
});
