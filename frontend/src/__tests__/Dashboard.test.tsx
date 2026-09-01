import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { BrowserRouter, MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import type { NavigateOptions } from "react-router-dom";
import { Dashboard } from "../pages/Dashboard";

// Mock API
vi.mock("../api", () => ({
  fetchDashboard: vi.fn(),
  fetchObsOverview: vi.fn(),
  fetchRecommendedMetrics: vi.fn(),
  fetchRecommendedTerms: vi.fn(),
  listDomainTree: vi.fn(),
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

// Mock usePermission（返回管理角色快照——总览仪表 Owner 责任分布的管理视角基线；
// 非管理角色用例通过修改 mockPermRole 动态切换）。
// useGuardedNavigate 用可感知 mockPermRole 的守卫替换（渲染期调用 useNavigate），
// 使"管理角色跳转 / 普通用户不跳转"可被真实 MemoryRouter location 断言验证。
let mockPermRole = "platform_admin";
vi.mock("../hooks/usePermission", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../hooks/usePermission")>();
  return {
    ...actual,
    usePermission: () => ({
      can: () => mockPermRole === "platform_admin" || mockPermRole === "domain_admin",
      canAny: () => true,
      canAll: () => true,
      snapshot: { role: mockPermRole, user_id: 1, ui_actions: [], home_domain: null },
      loading: false,
      error: false,
      refresh: async () => undefined,
    }),
    useGuardedNavigate: () => {
      const navigate = useNavigate();
      return (to: string | number, opts?: NavigateOptions) => {
        const isAdmin = mockPermRole === "platform_admin" || mockPermRole === "domain_admin";
        if (!isAdmin) return; // 非管理角色：不跳转（模拟真实守卫拦截，无反应）
        if (typeof to === "number") navigate(to);
        else navigate(to, opts);
      };
    },
  };
});

import {
  fetchDashboard,
  fetchObsOverview,
  fetchRecommendedMetrics,
  fetchRecommendedTerms,
  listDomainTree,
} from "../api";
const mockedFetchDashboard = vi.mocked(fetchDashboard);
const mockedFetchObsOverview = vi.mocked(fetchObsOverview);

const mockDashboardData = {
  total: 100,
  by_status: { DRAFT: 25, EXPERIMENTAL: 3, REVIEW: 7, PUBLISHED: 60, DEPRECATED: 5 },
  by_tier: { T1: 20, T2: 50, T3: 30 },
  by_domain: { finance: 40, marketing: 30, growth: 20, risk: 10 },
  pii_count: 12,
  pii_ratio: 0.12,
  by_owner: {
    1: {
      name: "Alice",
      total: 82,
      metrics: { total: 60, by_status: { DRAFT: 20, REVIEW: 4, PUBLISHED: 36 } },
      tables: { total: 8, by_status: {} },
      sources: { total: 4, by_status: {} },
      dimensions: { total: 3, by_status: { DRAFT: 1, PUBLISHED: 2 } },
      terms: { total: 5, by_status: { DRAFT: 2, PUBLISHED: 3 } },
      templates: { total: 2, by_status: {} },
    },
    2: {
      name: "Bob",
      total: 52,
      metrics: { total: 40, by_status: { DRAFT: 5, REVIEW: 3, PUBLISHED: 24, EXPERIMENTAL: 3, DEPRECATED: 5 } },
      tables: { total: 5, by_status: {} },
      sources: { total: 3, by_status: {} },
      dimensions: { total: 1, by_status: { DRAFT: 1 } },
      terms: { total: 2, by_status: { PUBLISHED: 2 } },
      templates: { total: 1, by_status: {} },
    },
    // Charlie 名下仅 5 条指标，无数据表/数据源/维度/术语/模板——验证 0 值资产段不被过滤
    3: {
      name: "Charlie",
      total: 5,
      metrics: { total: 5, by_status: { DRAFT: 5 } },
      tables: { total: 0, by_status: {} },
      sources: { total: 0, by_status: {} },
      dimensions: { total: 0, by_status: {} },
      terms: { total: 0, by_status: {} },
      templates: { total: 0, by_status: {} },
    },
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

// 指标可信度数据：模拟 /observability/overview quality.metric_health（与可观测中心同源）
const mockOverview = {
  quality: {
    metric_health: {
      by_level: { EXCELLENT: 40, GOOD: 35, WARNING: 18, CRITICAL: 7 },
      total_scored: 100,
      coverage_pct: 100,
      avg_score: 82,
      top_risk: [
        { metric_id: 1, metric_name: "坏账率", metric_code: "bad_debt_rate", score: 41, level: "CRITICAL", missing_dimensions: ["口径完整度"] },
        { metric_id: 2, metric_name: null, metric_code: "stale_metric", score: 55, level: "WARNING", missing_dimensions: [] },
      ],
    },
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
    mockedFetchObsOverview.mockResolvedValue(mockOverview as never);
    vi.mocked(fetchRecommendedMetrics).mockResolvedValue([]);
    vi.mocked(fetchRecommendedTerms).mockResolvedValue([]);
    // 域列表默认空 → 域映射回退显示编码，不影响既有断言
    vi.mocked(listDomainTree).mockResolvedValue([]);
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

  it("资产卡片：采集任务不可用时明示「采集服务暂不可用」而非伪装 0", async () => {
    mockedFetchDashboard.mockResolvedValue({
      ...mockDashboardData,
      assets: {
        ...mockDashboardData.assets,
        collection_task: {
          total: 0,
          by_status: {},
          unavailable: true,
          message: "采集服务暂不可用，采集任务数可能不完整",
        },
      },
    });
    const { container } = renderDashboard();
    await waitFor(() => expect(screen.getByText("采集服务暂不可用")).toBeInTheDocument());
    // 采集任务卡总数显示 —（而非 0，避免故障被误读为「真无任务」）
    const card = screen.getByText("采集任务").closest(".asset-card") as HTMLElement;
    expect(card.querySelector(".ac-total")?.textContent).toBe("—");
    // 不渲染状态段（避免「0 个排队/采集中」误导）
    expect(card.querySelector(".ac-statuses")).toBeNull();
    expect(container.querySelector(".ac-unavailable")).toBeTruthy();
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

  it("Owner 责任分布：渲染各 Owner 卡片，待审>0 高亮", async () => {
    renderDashboard();
    await waitFor(() => expect(screen.getByText("Owner 责任分布")).toBeInTheDocument());

    expect(screen.getByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("Bob")).toBeInTheDocument();
    // 待处理积压 > 0 的 Owner 卡片高亮（Alice 7 / Bob 4 都有，跨资产：指标待审核+维度草稿+术语草稿）
    expect(document.querySelectorAll(".owner-card.owner-hot").length).toBeGreaterThan(0);
  });

  it("Owner 责任分布跨资产：卡片含 6 类资产构成条与跨资产总计", async () => {
    const { container } = renderDashboard();
    await waitFor(() => expect(screen.getByText("Owner 责任分布")).toBeInTheDocument());

    // Alice（total=82 排前）：指标 60 / 数据表 8 / 数据源 4 / 维度 3 / 术语 5 / 模板 2
    const aliceCard = container.querySelectorAll(".owner-card")[0];
    const segs = Array.from(aliceCard!.querySelectorAll(".oc-seg"));
    expect(segs.length).toBe(6);
    // 段内标注智能显示：宽段（≥15%，指标 73.17%）显示「标签 数量」；中段（≥5%，数据表 9.76% / 术语 6.1%）仅显示数量；
    // 过窄段（<5%，数据源 4.88% / 维度 3.66% / 模板 2.44%）不渲染文字，避免被 overflow 裁出半字
    expect(segs[0].textContent).toContain("指标");
    expect(segs[0].textContent).toContain("60");
    expect(segs[1].textContent).toBe("8");
    expect(segs[2].textContent).toBe("");
    expect(segs[3].textContent).toBe("");
    expect(segs[4].textContent).toBe("5");
    expect(segs[5].textContent).toBe("");
    // 完整标注由图例保证：6 类资产标签 + 数量恒完整展示（窄段/0 值段也能看清全维度）
    const chips = Array.from(aliceCard!.querySelectorAll(".oc-legend .oc-chip"));
    expect(chips.length).toBe(6);
    expect(chips.map((c) => c.textContent?.replace(/\s/g, ""))).toEqual([
      "指标60",
      "数据表8",
      "数据源4",
      "维度3",
      "术语5",
      "模板2",
    ]);
    // 跨资产总计（卡片头部）
    expect(aliceCard!.querySelector(".oc-total")?.textContent).toContain("82");
  });

  it("Owner 分布以图表样式展示：资产构成条各段宽度与占比对应", async () => {
    const { container } = renderDashboard();
    await waitFor(() => expect(screen.getByText("Owner 责任分布")).toBeInTheDocument());

    // 每个 Owner 一张卡片：头像 + 名字 + 资产构成条 + 生命周期（Alice/Bob/Charlie 共 3 张）
    const cards = Array.from(container.querySelectorAll(".owner-card"));
    expect(cards.length).toBe(3);
    const aliceBar = cards[0].querySelector(".oc-bar");
    expect(aliceBar).toBeTruthy();
    // Alice: 指标 60 / 数据表 8 / 数据源 4 / 维度 3 / 术语 5 / 模板 2，total=82
    const segs = Array.from(aliceBar!.querySelectorAll(".oc-seg"));
    expect(segs.length).toBe(6);
    expect(segs[0].getAttribute("style")).toContain("73.17%");
    expect(segs[1].getAttribute("style")).toContain("9.76%");
    expect(segs[2].getAttribute("style")).toContain("4.88%");
    expect(segs[3].getAttribute("style")).toContain("3.66%");
    // jsdom 的 CSSStyleDeclaration 会归一化尾随零（6.10% → 6.1%），用无尾零形式断言
    expect(segs[4].getAttribute("style")).toContain("6.1%");
    expect(segs[5].getAttribute("style")).toContain("2.44%");
    // 生命周期作为次级信息展示（色点 + 标签 + 计数）
    const life = cards[0].querySelector(".oc-life");
    expect(life?.textContent).toContain("草稿");
    expect(life?.textContent).toContain("20");
    expect(life?.textContent).toContain("已发布");
    expect(life?.textContent).toContain("36");
  });

  it("Owner 资产段跳转：点击「数据表」段跳 /catalogs?owner_id=、点「维度」段跳 /dimensions?owner_id=", async () => {
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("Owner 责任分布")).toBeInTheDocument());

    // Alice 卡片（total=82 排前）的第 2 段 = 数据表
    const aliceCard = document.querySelectorAll(".owner-card")[0];
    const segs = Array.from(aliceCard!.querySelectorAll(".oc-seg"));
    fireEvent.click(segs[1]); // 数据表
    expect(probe.location()?.pathname).toBe("/catalogs");
    expect(probe.location()?.search).toContain("owner_id=1");

    // 再点击「维度」段（第 4 段）→ /dimensions?owner_id=1
    const segs2 = Array.from(document.querySelectorAll(".owner-card")[0].querySelectorAll(".oc-seg"));
    fireEvent.click(segs2[3]); // 维度
    expect(probe.location()?.pathname).toBe("/dimensions");
    expect(probe.location()?.search).toContain("owner_id=1");
  });

  it("Owner 下钻：点击 Owner 跳转 /catalog?owner_id=", async () => {
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("Owner 责任分布")).toBeInTheDocument());

    fireEvent.click(screen.getByText("Alice"));
    expect(probe.location()?.pathname).toBe("/catalog");
    expect(probe.location()?.search).toContain("owner_id=1");
  });

  it("Owner 生命周期色点：点击「草稿」色点跳 /catalog 并携带 status=DRAFT + owner_id 过滤", async () => {
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("Owner 责任分布")).toBeInTheDocument());

    // Alice 卡片（total=82 排前）生命周期色点：草稿 20 / 审核 4 / 已发布 36
    const aliceCard = document.querySelectorAll(".owner-card")[0];
    const lifeItems = Array.from(aliceCard!.querySelectorAll(".oc-life-item"));
    fireEvent.click(lifeItems[0]); // 草稿 20
    expect(probe.location()?.pathname).toBe("/catalog");
    expect(probe.location()?.search).toContain("status=DRAFT");
    expect(probe.location()?.search).toContain("owner_id=1");
  });

  it("Owner 待处理徽标：跨资产汇总（指标待审核+维度草稿+术语草稿），点「指标待审核」下钻 /catalog?status=REVIEW&owner_id", async () => {
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("Owner 责任分布")).toBeInTheDocument());

    // Alice 卡片头部「待处理 7」高亮徽标：指标 REVIEW=4 + 维度草稿 1 + 术语草稿 2 = 7（跨资产汇总，非仅指标）
    const aliceCard = document.querySelectorAll(".owner-card")[0];
    const hotBadge = aliceCard!.querySelector(".oc-hot");
    expect(hotBadge).toBeTruthy();
    expect(hotBadge!.textContent).toContain("待处理");
    expect(hotBadge!.textContent).toContain("7");
    expect(hotBadge!.textContent).not.toContain("待审");

    // 点击徽标 → 弹 Popover 分类明细（3 类：指标待审核 / 维度草稿 / 术语草稿）
    fireEvent.click(hotBadge!);
    await waitFor(() => expect(document.querySelector(".oc-hot-pop")).toBeTruthy());
    const items = Array.from(document.querySelectorAll(".oc-hot-pop-item"));
    expect(items.length).toBe(3);
    expect(items[0].textContent).toContain("指标待审核");
    expect(items[0].textContent).toContain("4");

    // 点「指标待审核 4」→ 精确跳转指标目录并携带 status=REVIEW + owner_id 过滤
    fireEvent.click(items[0]);
    expect(probe.location()?.pathname).toBe("/catalog");
    expect(probe.location()?.search).toContain("status=REVIEW");
    expect(probe.location()?.search).toContain("owner_id=1");
  });

  it("Owner 待处理分类下钻：维度草稿→/dimensions?status=DRAFT、术语草稿→/glossary?status=DRAFT（均带 owner_id）", async () => {
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("Owner 责任分布")).toBeInTheDocument());

    const aliceCard = document.querySelectorAll(".owner-card")[0];
    const hotBadge = aliceCard!.querySelector(".oc-hot");
    expect(hotBadge).toBeTruthy();

    // 维度草稿 1 → /dimensions?status=DRAFT&owner_id=1
    fireEvent.click(hotBadge!);
    await waitFor(() => expect(document.querySelector(".oc-hot-pop")).toBeTruthy());
    let items = Array.from(document.querySelectorAll(".oc-hot-pop-item"));
    expect(items[1].textContent).toContain("维度草稿");
    expect(items[1].textContent).toContain("1");
    fireEvent.click(items[1]);
    expect(probe.location()?.pathname).toBe("/dimensions");
    expect(probe.location()?.search).toContain("status=DRAFT");
    expect(probe.location()?.search).toContain("owner_id=1");

    // 术语草稿 2 → /glossary?status=DRAFT&owner_id=1
    fireEvent.click(hotBadge!);
    await waitFor(() => expect(document.querySelector(".oc-hot-pop")).toBeTruthy());
    items = Array.from(document.querySelectorAll(".oc-hot-pop-item"));
    expect(items[2].textContent).toContain("术语草稿");
    expect(items[2].textContent).toContain("2");
    fireEvent.click(items[2]);
    expect(probe.location()?.pathname).toBe("/glossary");
    expect(probe.location()?.search).toContain("status=DRAFT");
    expect(probe.location()?.search).toContain("owner_id=1");
  });

  it("Owner 待处理徽标：兼容旧版纯数字 by_owner（维度/术语无 by_status 不崩溃，徽标仅统计指标待审）", async () => {
    // 旧版后端 by_owner 结构：metrics 为 {total, by_status}，其余资产为纯数字。
    // 前端新代码若直接读 o.dimensions.by_status.DRAFT 会在数字上取属性 → undefined → 崩溃。
    mockedFetchDashboard.mockResolvedValue({
      ...mockDashboardData,
      by_owner: {
        1: {
          name: "Alice",
          total: 82,
          metrics: { total: 60, by_status: { DRAFT: 20, REVIEW: 4, PUBLISHED: 36 } },
          tables: 8,
          sources: 4,
          dimensions: 3,
          terms: 5,
          templates: 2,
        },
      },
    });
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("Owner 责任分布")).toBeInTheDocument());

    // 不崩溃，卡片正常渲染
    const aliceCard = document.querySelector(".owner-card");
    expect(aliceCard).toBeTruthy();

    // 徽标「待处理 4」= 仅指标 REVIEW=4（维度/术语是数字无法取 by_status，回退为 0，不崩）
    const hotBadge = aliceCard!.querySelector(".oc-hot");
    expect(hotBadge).toBeTruthy();
    expect(hotBadge!.textContent).toContain("待处理");
    expect(hotBadge!.textContent).toContain("4");

    // 点击徽标 → 分类明细仅含指标（维度/术语为纯数字时无草稿可列）
    fireEvent.click(hotBadge!);
    await waitFor(() => expect(document.querySelector(".oc-hot-pop")).toBeTruthy());
    const items = Array.from(document.querySelectorAll(".oc-hot-pop-item"));
    expect(items.length).toBe(1);
    expect(items[0].textContent).toContain("指标待审核");
    fireEvent.click(items[0]);
    expect(probe.location()?.pathname).toBe("/catalog");
    expect(probe.location()?.search).toContain("status=REVIEW");
    expect(probe.location()?.search).toContain("owner_id=1");
  });

  it("Owner 卡片：含 0 值资产类型仍完整渲染 6 段（数据表/数据源为 0 不被过滤）", async () => {
    const { container } = renderDashboard();
    await waitFor(() => expect(screen.getByText("Owner 责任分布")).toBeInTheDocument());

    // cards 按 total 降序：Alice(82) → Bob(52) → Charlie(5)
    const cards = Array.from(container.querySelectorAll(".owner-card"));
    const charlieCard = cards[2];
    expect(charlieCard).toBeTruthy();
    const segs = Array.from(charlieCard!.querySelectorAll(".oc-seg"));
    // 6 类全渲染（含 0 值的 tables/sources/dimensions/terms/templates）
    expect(segs.length).toBe(6);
    // 数据表段（index=1）：count=0，含 oc-zero 类，宽度为最小占位 1.5%——过窄段不渲染文字（标签在 title 提示，完整标注见图例）
    const tableSeg = segs[1];
    expect(tableSeg.textContent).toBe("");
    expect(tableSeg.className).toContain("oc-zero");
    expect(tableSeg.getAttribute("style") ?? "").toMatch(/1[.,]5/);
    expect(tableSeg.getAttribute("title") ?? "").toContain("数据表");
    expect(tableSeg.getAttribute("title") ?? "").toContain("暂无");
    // 数据源段（index=2）：同样 0 + oc-zero + 不渲染文字
    const sourceSeg = segs[2];
    expect(sourceSeg.textContent).toBe("");
    expect(sourceSeg.className).toContain("oc-zero");
    expect(sourceSeg.getAttribute("title") ?? "").toContain("数据源");
    // 指标段（index=0）：count=5（>0），无 oc-zero 类
    const metricSeg = segs[0];
    expect(metricSeg.textContent).toContain("指标");
    expect(metricSeg.textContent).toContain("5");
    expect(metricSeg.className).not.toContain("oc-zero");
  });

  it("Owner 图例：构成条下方展示 6 类资产图例，0 值项也显示标签与数量（数据表 0 / 数据源 0）", async () => {
    const { container } = renderDashboard();
    await waitFor(() => expect(screen.getByText("Owner 责任分布")).toBeInTheDocument());

    // cards 按 total 降序：Alice(82) → Bob(52) → Charlie(5)
    const cards = Array.from(container.querySelectorAll(".owner-card"));
    const charlieCard = cards[2];
    const legend = charlieCard!.querySelector(".oc-legend");
    expect(legend).toBeTruthy();
    const chips = Array.from(legend!.querySelectorAll(".oc-chip"));
    expect(chips.length).toBe(6);
    // 0 值项（数据表/数据源）：图例中显示标签 + 数量（不再只是裸 "0"），且灰显 oc-chip-zero
    expect(chips[1].textContent).toContain("数据表");
    expect(chips[1].textContent).toContain("0");
    expect(chips[1].className).toContain("oc-chip-zero");
    expect(chips[2].textContent).toContain("数据源");
    expect(chips[2].textContent).toContain("0");
    expect(chips[2].className).toContain("oc-chip-zero");
    // 非 0 项（指标）：正常显示标签 + 数量，无 oc-chip-zero
    expect(chips[0].textContent).toContain("指标");
    expect(chips[0].textContent).toContain("5");
    expect(chips[0].className).not.toContain("oc-chip-zero");
  });

  it("治理指标卡：质量健康渲染严重级分布与待处理", async () => {
    renderDashboard();
    await waitFor(() => expect(screen.getByText("质量健康")).toBeInTheDocument());

    expect(screen.getByText("P0")).toBeInTheDocument();
    expect(screen.getByText("P1")).toBeInTheDocument();
    expect(screen.getByText("P2")).toBeInTheDocument();
    // 「待处理 5 项」限定质量健康卡（Owner 徽标是「待处理 N」不带「项」，避免歧义）
    expect(screen.getByText(/待处理 5 项/)).toBeInTheDocument();
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

  it("指标可信度卡：主读数显示绿档可信率而非覆盖率（修复误导）", async () => {
    renderDashboard();
    // mockOverview by_level = { EXCELLENT: 40, GOOD: 35, WARNING: 18, CRITICAL: 7 } →
    // 绿档可信率 = (40+35)/100 = 75%；覆盖率 100% 只作次要信息（每日强制全量评分，无区分度）
    await waitFor(() => expect(screen.getByText("绿档可信率")).toBeInTheDocument());
    expect(screen.getByText("75%")).toBeInTheDocument();
    expect(screen.getByText(/健康覆盖率 100%/)).toBeInTheDocument();
  });

  it("指标可信度卡：点击健康度档位下钻指标目录 ?health=", async () => {
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("绿档可信率")).toBeInTheDocument());

    // WARNING 档位 pill（文案「警告 18」）可点击下钻，且不冒泡触发外层跳可观测中心
    fireEvent.click(screen.getByText(/警告/));
    expect(probe.location()?.pathname).toBe("/catalog");
    expect(probe.location()?.search).toContain("health=WARNING");
  });

  it("指标可信度卡：点击低健康指标直达详情", async () => {
    const probe = renderWithLocation();
    await waitFor(() =>
      expect(screen.getByText("低健康指标 Top 2（按评分升序）")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByText("坏账率"));
    expect(probe.location()?.pathname).toBe("/detail/bad_debt_rate");
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

  it("推荐术语的域 Tag 展示中文名（listDomainTree 映射，非英文编码）", async () => {
    vi.mocked(listDomainTree).mockResolvedValue([
      {
        id: 1,
        code: "outpatient",
        name: "门诊",
        parent_id: null,
        level: 1,
        sort_order: 0,
        status: "active",
        metric_count: 0,
        children: [],
      },
    ]);
    vi.mocked(fetchRecommendedTerms).mockResolvedValue([
      {
        id: 1,
        term_code: "t_outpatient",
        name: "挂号术语",
        definition: "门诊相关业务术语",
        domain: "outpatient",
        synonyms: [],
        boundary: null,
        status: "PUBLISHED",
        owner_id: 1,
        created_at: null,
        updated_at: null,
      },
    ]);
    renderDashboard();

    await waitFor(() => expect(screen.getByText("挂号术语")).toBeInTheDocument());
    // 域 Tag 中文化：显示「门诊」而非英文编码 outpatient
    await waitFor(() => expect(screen.getByText("门诊")).toBeInTheDocument());
    expect(screen.queryByText("outpatient")).not.toBeInTheDocument();
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

describe("Dashboard 数据隔离（非管理角色视角）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPermRole = "analyst";
    mockedFetchDashboard.mockResolvedValue(mockDashboardData);
    mockedFetchObsOverview.mockResolvedValue(mockOverview as never);
    vi.mocked(fetchRecommendedMetrics).mockResolvedValue([]);
    vi.mocked(fetchRecommendedTerms).mockResolvedValue([]);
    vi.mocked(listDomainTree).mockResolvedValue([]);
  });

  it("普通用户：Owner 责任分布标题为「我的资产责任分布」而非「Owner 责任分布」", async () => {
    renderDashboard();
    await waitFor(() => expect(screen.getByText("我的资产责任分布")).toBeInTheDocument());
    expect(screen.queryByText("Owner 责任分布")).not.toBeInTheDocument();
  });

  it("普通用户：后端返回空 by_owner 时展示空状态引导而非隐藏区块", async () => {
    mockedFetchDashboard.mockResolvedValue({
      ...mockDashboardData,
      by_owner: {},
    } as never);
    renderDashboard();
    await waitFor(() => expect(screen.getByText("我的资产责任分布")).toBeInTheDocument());
    expect(screen.getByText(/您名下暂无负责的资产/)).toBeInTheDocument();
  });

  it("普通用户：不展示「指标待审核」评审动作告警（无评审能力，TD §13）", async () => {
    renderDashboard();
    await waitFor(() => expect(screen.getByText("我的资产责任分布")).toBeInTheDocument());
    // 本人名下审核中状态在 Owner 分布体现，但评审动作告警（去评审）不出现
    expect(screen.queryByText(/个指标待审核/)).not.toBeInTheDocument();
  });

  it("评审人：展示指派给我的待审数（assigned_review，TD §13）", async () => {
    mockPermRole = "reviewer";
    mockedFetchDashboard.mockResolvedValue({
      ...mockDashboardData,
      assigned_review: 5,
    } as never);
    renderDashboard();
    await waitFor(() => expect(screen.getByText(/5 个指标待审核/)).toBeInTheDocument());
    expect(screen.queryByText(/7 个指标待审核/)).not.toBeInTheDocument();
  });
});

describe("总览仪表跳转权限守卫（按钮级）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedFetchDashboard.mockResolvedValue(mockDashboardData);
    mockedFetchObsOverview.mockResolvedValue(mockOverview as never);
    vi.mocked(fetchRecommendedMetrics).mockResolvedValue([]);
    vi.mocked(fetchRecommendedTerms).mockResolvedValue([]);
    vi.mocked(listDomainTree).mockResolvedValue([]);
  });

  it("管理角色：点击资产卡片可跳转目录（守卫放行）", async () => {
    mockPermRole = "platform_admin";
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("Owner 责任分布")).toBeInTheDocument());
    const head = document.querySelector<HTMLButtonElement>(".asset-card .ac-head");
    expect(head).not.toBeNull();
    fireEvent.click(head!);
    await waitFor(() => expect(probe.location()?.pathname).toBe("/catalog"));
  });

  it("普通用户：点击资产卡片无反应（守卫拦截，路由不变）", async () => {
    mockPermRole = "analyst";
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("我的资产责任分布")).toBeInTheDocument());
    const head = document.querySelector<HTMLButtonElement>(".asset-card .ac-head");
    fireEvent.click(head!);
    // 无权限：路由保持 /dashboard，未发生跳转
    expect(probe.location()?.pathname).toBe("/dashboard");
  });

  it("普通用户：点击快捷入口（质量中心）无反应", async () => {
    mockPermRole = "analyst";
    const probe = renderWithLocation();
    await waitFor(() => expect(screen.getByText("我的资产责任分布")).toBeInTheDocument());
    const qualityEntry = screen.getByText("质量中心");
    const btn = qualityEntry.closest("button");
    expect(btn).not.toBeNull();
    fireEvent.click(btn!);
    expect(probe.location()?.pathname).toBe("/dashboard");
  });
});
