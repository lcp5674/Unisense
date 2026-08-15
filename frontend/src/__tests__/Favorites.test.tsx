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
    UnisenseApiError,
  };
});

vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: vi.fn() }),
}));

import { listFavoriteDetails, addFavorite, removeFavorite, UnisenseApiError } from "../api";

const mockedList = vi.mocked(listFavoriteDetails);
const mockedAdd = vi.mocked(addFavorite);
const mockedRemove = vi.mocked(removeFavorite);

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
  it("添加默认按指标类型调用 API", async () => {
    mockedList.mockResolvedValue([]);
    renderPage();

    const input = screen.getByPlaceholderText(/知道编码/);
    fireEvent.change(input, { target: { value: "GMV" } });
    fireEvent.click(screen.getByRole("button", { name: /添\s*加/ }));

    await waitFor(() => expect(mockedAdd).toHaveBeenCalledWith("METRIC", "GMV"));
  });

  it("添加不存在的资产显示错误提示", async () => {
    mockedList.mockResolvedValue([]);
    mockedAdd.mockRejectedValue(new UnisenseApiError("资产不存在: GHOST", "NOT_FOUND", 404, "t"));
    renderPage();

    const input = screen.getByPlaceholderText(/知道编码/);
    fireEvent.change(input, { target: { value: "GHOST" } });
    fireEvent.click(screen.getByRole("button", { name: /添\s*加/ }));

    await waitFor(() => expect(screen.findByText(/资产不存在/)).toBeTruthy());
  });

  it("移除收藏带资产类型调用 API", async () => {
    mockedList.mockResolvedValue([MOCK_ITEMS[1]]);
    renderPage();
    await screen.findByText("机密");

    fireEvent.click(screen.getByRole("button", { name: /移除/ }));
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
