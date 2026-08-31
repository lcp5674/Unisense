import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { Favorites } from "../pages/Favorites";
import type { FavoriteDetail } from "../api";

vi.mock("../api", () => {
  class UnisenseApiError extends Error {
    code: string;
    traceId: string;
    status: number;
    detail?: Record<string, unknown> | null;
    constructor(message: string, code: string, status: number, traceId: string, detail?: Record<string, unknown> | null) {
      super(message);
      this.name = "UnisenseApiError";
      this.code = code;
      this.status = status;
      this.traceId = traceId;
      this.detail = detail;
    }
  }
  return {
    listFavoriteDetails: vi.fn(),
    addFavorite: vi.fn(),
    removeFavorite: vi.fn(),
    listMetrics: vi.fn(),
    UnisenseApiError,
  };
});

vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: vi.fn() }),
}));

import { listFavoriteDetails, addFavorite, removeFavorite, listMetrics, UnisenseApiError } from "../api";

const mockedList = vi.mocked(listFavoriteDetails);
const mockedAdd = vi.mocked(addFavorite);
const mockedRemove = vi.mocked(removeFavorite);
const mockedListMetrics = vi.mocked(listMetrics);

function PathSpy() {
  const loc = useLocation();
  return <div data-testid="path">{loc.pathname + loc.search}</div>;
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/favorites"]}>
      <Routes>
        <Route path="/favorites" element={<Favorites />} />
        <Route path="*" element={<PathSpy />} />
      </Routes>
    </MemoryRouter>,
  );
}

/** 定位列表项 title 中的名称元素（.fav-name，可点击跳转）。 */
function nameEl(name: string) {
  const el = screen.getAllByText(name)[0];
  return el.closest(".fav-name") ?? el;
}

/** 定位概览统计卡（.fav-stat-card），按卡内标签文本匹配。 */
function statCard(label: string) {
  const el = screen
    .getAllByText(label)
    .map((node) => node.closest(".fav-stat-card"))
    .find(Boolean);
  if (!el) throw new Error(`找不到统计卡: ${label}`);
  return el;
}

/** 定位卡片内「移除」按钮（卡片整体也是 role=button，需精确定位到真实按钮）。 */
function removeBtn() {
  const btn = screen.getByText("移除").closest("button");
  if (!btn) throw new Error("找不到移除按钮");
  return btn;
}

/** 在 antd Select 下拉中点击指定 title 的选项（打开下拉后调用）。 */
async function clickSelectOption(text: string) {
  await waitFor(() => {
    const dropdown = document.querySelector(
      ".ant-select-dropdown:not(.ant-select-dropdown-hidden)",
    ) as HTMLElement | null;
    const option = dropdown?.querySelector(
      `.ant-select-item-option[title="${text}"]`,
    ) as HTMLElement | null;
    expect(option).toBeTruthy();
    if (option) fireEvent.click(option);
  });
}

function fav(partial: Partial<FavoriteDetail> & { asset_id: string }): FavoriteDetail {
  return {
    asset_type: "METRIC",
    name: partial.asset_id,
    description: null,
    domain: null,
    status: "PUBLISHED",
    tier: null,
    is_pii: false,
    created_at: "2026-08-15T00:00:00",
    dead: false,
    ...partial,
  };
}

const MOCK_ITEMS: FavoriteDetail[] = [
  fav({ asset_type: "METRIC", asset_id: "GMV", name: "成交总额", domain: "finance", status: "PUBLISHED", tier: "T1", created_at: "2026-08-14T00:00:00" }),
  fav({ asset_type: "TABLE", asset_id: "dw.sales", name: "dw.sales", status: "CONFIDENTIAL", description: "销售明细表", created_at: "2026-08-15T00:00:00" }),
  fav({ asset_type: "TERM", asset_id: "TERM_AOV", name: "客单价", domain: "finance", status: "DRAFT", created_at: "2026-08-13T00:00:00" }),
  fav({ asset_type: "METRIC", asset_id: "GHOST", name: "GHOST", status: "UNKNOWN", dead: true, created_at: "2026-08-12T00:00:00" }),
];

beforeEach(() => {
  vi.clearAllMocks();
  mockedList.mockResolvedValue([]);
  mockedAdd.mockResolvedValue({ asset_type: "METRIC", asset_id: "GMV", pinned: true });
  mockedRemove.mockResolvedValue({ asset_type: "METRIC", asset_id: "GMV", pinned: false });
  mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 });
});

describe("我的收藏 - 多资产展示", () => {
  it("展示各资产类型 + 类型标签 + 概览统计", async () => {
    mockedList.mockResolvedValue(MOCK_ITEMS);
    renderPage();

    await screen.findByText("成交总额");
    // 名称 + 编码各出现一次（名称在 title，编码在描述）
    expect(screen.getAllByText("dw.sales").length).toBeGreaterThan(0);
    expect(screen.getByText("客单价")).toBeInTheDocument();
    // 资产类型标签（Segmented + 列表 Tag + 统计条均有，断言存在即可）
    expect(screen.getAllByText("指标").length).toBeGreaterThan(0);
    expect(screen.getAllByText("数据表").length).toBeGreaterThan(0);
    expect(screen.getAllByText("术语").length).toBeGreaterThan(0);
    // 概览统计：收藏总数 4
    const totalStat = screen.getByText("收藏总数").closest(".fav-stat-card");
    expect(totalStat).toHaveTextContent("4");
    // 分级与 PII 信息密度
    expect(screen.getByText("分级 T1")).toBeInTheDocument();
    // 收藏时间（多个收藏项各显示一次）
    expect(screen.getAllByText(/收藏于/).length).toBeGreaterThan(0);
  });

  it("失效收藏灰显并显示已失效", async () => {
    mockedList.mockResolvedValue(MOCK_ITEMS);
    renderPage();

    await screen.findByText("成交总额");
    const invalidItems = screen.getAllByText("已失效");
    expect(invalidItems.length).toBeGreaterThan(0);
  });

  it("数据表收藏显示敏感级标签", async () => {
    mockedList.mockResolvedValue([MOCK_ITEMS[1]]);
    renderPage();

    await screen.findByText("机密");
    expect(screen.getAllByText("dw.sales").length).toBeGreaterThan(0);
  });
});

describe("我的收藏 - 筛选", () => {
  it("Tab 切换只显示对应资产类型", async () => {
    mockedList.mockResolvedValue(MOCK_ITEMS);
    renderPage();
    await screen.findByText("成交总额");

    // 点击 Segmented 中的「数据表」选项
    fireEvent.click(screen.getByRole("radio", { name: /数据表/ }));
    expect(screen.getAllByText("dw.sales").length).toBeGreaterThan(0);
    expect(screen.queryByText("成交总额")).not.toBeInTheDocument();
  });

  it("关键词搜索过滤名称/编码", async () => {
    mockedList.mockResolvedValue(MOCK_ITEMS);
    renderPage();
    await screen.findByText("成交总额");

    fireEvent.change(screen.getByPlaceholderText("搜索名称/编码"), { target: { value: "AOV" } });
    expect(screen.getByText("客单价")).toBeInTheDocument();
    expect(screen.queryByText("成交总额")).not.toBeInTheDocument();
  });

  it("只看失效开关过滤出失效项", async () => {
    mockedList.mockResolvedValue(MOCK_ITEMS);
    renderPage();
    await screen.findByText("成交总额");

    fireEvent.click(screen.getByRole("button", { name: /只看失效/ }));
    expect(screen.getAllByText("GHOST").length).toBeGreaterThan(0);
    expect(screen.queryByText("成交总额")).not.toBeInTheDocument();
  });
});

describe("我的收藏 - 操作", () => {
  it("按指标名称搜索选择后添加收藏", async () => {
    mockedList.mockResolvedValue([]);
    mockedListMetrics.mockResolvedValue({
      items: [
        {
          id: 1,
          metric_code: "GMV",
          name: "成交总额",
          domain: "finance",
          type: "atomic",
          granularity: "day",
          unit: "CNY",
          currency: "CNY",
          aggregation: "SUM",
          time_semantics: "PERIOD",
          freshness: "T1",
          dw_layer: "DWD",
          metric_tier: "T1",
          serving_mode: "BATCH_ONLY",
          additivity: "ADDITIVE",
          non_additive_dimensions: null,
          definition_json: {},
          version: 1,
          row_version: 1,
          status: "PUBLISHED",
          owner_id: 1,
          backup_owner_id: null,
          approver_id: null,
          submitted_by: null,
          pii_flag: false,
          compliance_reviewed: true,
          term_id: null,
          effective_version: 1,
          consumption_guide: null,
          successor_code: null,
          deprecated_at: null,
          sunset_until: null,
          emergency_publish: false,
          emergency_reason: null,
          emergency_reviewed_at: null,
          gray_tenant_ids: null,
          pending_conflict: false,
          pending_conflict_detail: null,
          sla: null,
          pending_version: false,
          created_at: "2026-08-15T00:00:00",
          updated_at: "2026-08-15T00:00:00",
        },
      ],
      total: 1,
      page: 1,
      page_size: 50,
    });
    renderPage();

    // 打开添加下拉（首次打开即加载候选）
    const selector = document.querySelector(".fav-add-quick .ant-select-selector") as HTMLElement;
    fireEvent.mouseDown(selector);
    await clickSelectOption("成交总额 (GMV)");
    fireEvent.click(screen.getByRole("button", { name: /添\s*加/ }));

    await waitFor(() => expect(mockedAdd).toHaveBeenCalledWith("METRIC", "GMV"));
  });

  it("未选择指标点添加给出明确提示（不再静默无反应）", async () => {
    mockedList.mockResolvedValue([]);
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: /添\s*加/ }));

    await waitFor(() => expect(screen.findByText(/请先选择要收藏的指标/)).toBeTruthy());
    expect(mockedAdd).not.toHaveBeenCalled();
  });

  it("添加失败显示错误提示", async () => {
    mockedList.mockResolvedValue([]);
    mockedListMetrics.mockResolvedValue({
      items: [
        {
          id: 1,
          metric_code: "GMV",
          name: "成交总额",
          domain: "finance",
          type: "atomic",
          granularity: "day",
          unit: "CNY",
          currency: "CNY",
          aggregation: "SUM",
          time_semantics: "PERIOD",
          freshness: "T1",
          dw_layer: "DWD",
          metric_tier: "T1",
          serving_mode: "BATCH_ONLY",
          additivity: "ADDITIVE",
          non_additive_dimensions: null,
          definition_json: {},
          version: 1,
          row_version: 1,
          status: "PUBLISHED",
          owner_id: 1,
          backup_owner_id: null,
          approver_id: null,
          submitted_by: null,
          pii_flag: false,
          compliance_reviewed: true,
          term_id: null,
          effective_version: 1,
          consumption_guide: null,
          successor_code: null,
          deprecated_at: null,
          sunset_until: null,
          emergency_publish: false,
          emergency_reason: null,
          emergency_reviewed_at: null,
          gray_tenant_ids: null,
          pending_conflict: false,
          pending_conflict_detail: null,
          sla: null,
          pending_version: false,
          created_at: "2026-08-15T00:00:00",
          updated_at: "2026-08-15T00:00:00",
        },
      ],
      total: 1,
      page: 1,
      page_size: 50,
    });
    mockedAdd.mockRejectedValue(new UnisenseApiError("资产不存在: GHOST", "NOT_FOUND", 404, "t"));
    renderPage();

    const selector = document.querySelector(".fav-add-quick .ant-select-selector") as HTMLElement;
    fireEvent.mouseDown(selector);
    await clickSelectOption("成交总额 (GMV)");
    fireEvent.click(screen.getByRole("button", { name: /添\s*加/ }));

    await waitFor(() => expect(screen.findByText(/资产不存在/)).toBeTruthy());
  });

  it("移除收藏带资产类型调用 API", async () => {
    mockedList.mockResolvedValue([MOCK_ITEMS[1]]);
    renderPage();
    await screen.findByText("机密");

    fireEvent.click(removeBtn());
    await waitFor(() => expect(mockedRemove).toHaveBeenCalledWith("TABLE", "dw.sales"));
  });

  it("点击指标收藏跳转指标详情", async () => {
    mockedList.mockResolvedValue([MOCK_ITEMS[0]]);
    renderPage();
    await screen.findByText("成交总额");

    fireEvent.click(screen.getByText("成交总额"));
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/detail/GMV"));
  });

  it("点击数据表收藏跳转采集目录", async () => {
    mockedList.mockResolvedValue([MOCK_ITEMS[1]]);
    renderPage();
    await screen.findByText("机密");

    fireEvent.click(nameEl("dw.sales"));
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/catalogs?kw=dw.sales"));
  });

  it("空态显示去目录挑选引导按钮", async () => {
    mockedList.mockResolvedValue([]);
    renderPage();

    const goBtn = await screen.findByRole("button", { name: /去指标目录挑选收藏/ });
    fireEvent.click(goBtn);
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/catalog"));
  });
});

describe("我的收藏 - 统计卡点击切换 Tab", () => {
  it("点击「指标」统计卡切换到指标类型", async () => {
    mockedList.mockResolvedValue(MOCK_ITEMS);
    renderPage();
    await screen.findByText("成交总额");

    fireEvent.click(statCard("指标"));
    // 只剩指标（GMV / GHOST），数据表与术语隐藏
    expect(screen.getByText("成交总额")).toBeInTheDocument();
    expect(screen.getAllByText("GHOST").length).toBeGreaterThan(0);
    expect(screen.queryByText("客单价")).not.toBeInTheDocument();
    // 切到「指标」后，收藏总数卡不应再保持选中高亮（回归：fav-stat-total 曾无条件高亮）
    expect(statCard("收藏总数")).not.toHaveClass("active");
    expect(statCard("指标")).toHaveClass("active");
  });

  it("点击「数据表」统计卡切换到数据表类型", async () => {
    mockedList.mockResolvedValue(MOCK_ITEMS);
    renderPage();
    await screen.findByText("成交总额");

    fireEvent.click(statCard("数据表"));
    expect(screen.getAllByText("dw.sales").length).toBeGreaterThan(0);
    expect(screen.queryByText("成交总额")).not.toBeInTheDocument();
  });

  it("点击「已失效」统计卡只看失效项", async () => {
    mockedList.mockResolvedValue(MOCK_ITEMS);
    renderPage();
    await screen.findByText("成交总额");

    fireEvent.click(statCard("已失效"));
    expect(screen.getAllByText("GHOST").length).toBeGreaterThan(0);
    expect(screen.queryByText("成交总额")).not.toBeInTheDocument();
  });

  it("点击「收藏总数」统计卡回到全部", async () => {
    mockedList.mockResolvedValue(MOCK_ITEMS);
    renderPage();
    await screen.findByText("成交总额");

    fireEvent.click(statCard("数据表"));
    fireEvent.click(statCard("收藏总数"));
    expect(screen.getByText("成交总额")).toBeInTheDocument();
    expect(screen.getByText("客单价")).toBeInTheDocument();
  });
});

describe("我的收藏 - 卡片主体点击", () => {
  it("点击收藏卡片主体（非名称）跳转资产详情", async () => {
    mockedList.mockResolvedValue([MOCK_ITEMS[0]]);
    renderPage();
    await screen.findByText("成交总额");

    fireEvent.click(screen.getByText("成交总额").closest(".fav-card")!);
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/detail/GMV"));
  });

  it("点击卡片内「移除」不触发跳转", async () => {
    mockedList.mockResolvedValue([MOCK_ITEMS[1]]);
    renderPage();
    await screen.findByText("机密");

    fireEvent.click(removeBtn());
    await waitFor(() => expect(mockedRemove).toHaveBeenCalledWith("TABLE", "dw.sales"));
    // 未触发卡片整体跳转：仍停留在收藏页（标题在），而非 /detail
    expect(screen.getByText("我的收藏")).toBeInTheDocument();
  });
});
