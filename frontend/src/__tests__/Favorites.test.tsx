import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { Favorites } from "../pages/Favorites";

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
  return <div data-testid="path">{loc.pathname}</div>;
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

beforeEach(() => {
  vi.clearAllMocks();
  mockedList.mockResolvedValue([]);
  mockedAdd.mockResolvedValue({ metric_code: "GMV", pinned: true });
  mockedRemove.mockResolvedValue({ metric_code: "GMV", pinned: false });
});

describe("我的收藏 - 列表展示", () => {
  it("聚合详情一次展示名称、域、状态", async () => {
    mockedList.mockResolvedValue([
      { metric_code: "GMV", name: "成交总额", domain: "finance", status: "PUBLISHED" },
      { metric_code: "AOV", name: "客单价", domain: "finance", status: "DRAFT" },
    ]);

    renderPage();

    await screen.findByText("成交总额");
    expect(screen.getByText("客单价")).toBeInTheDocument();
    // 名称下方展示编码 Tag
    expect(screen.getAllByText("GMV").length).toBeGreaterThan(0);
  });

  it("失效收藏（UNKNOWN）灰显并提示已失效", async () => {
    mockedList.mockResolvedValue([
      { metric_code: "GMV", name: "成交总额", domain: "finance", status: "PUBLISHED" },
      { metric_code: "GHOST", name: "GHOST", domain: null, status: "UNKNOWN" },
    ]);

    renderPage();

    await screen.findByText("成交总额");
    const ghostItem = screen.getByText(/已失效/);
    expect(ghostItem).toBeInTheDocument();
  });
});

describe("我的收藏 - 操作", () => {
  it("添加收藏调用 API 并刷新列表", async () => {
    mockedList.mockResolvedValue([]);
    renderPage();

    const input = screen.getByPlaceholderText(/指标编码/);
    fireEvent.change(input, { target: { value: "GMV" } });
    fireEvent.click(screen.getByRole("button", { name: /添加收藏/ }));

    await waitFor(() => expect(mockedAdd).toHaveBeenCalledWith("GMV"));
    // 刷新后重新拉取详情
    expect(mockedList).toHaveBeenCalledTimes(2);
  });

  it("添加不存在的指标显示错误提示", async () => {
    mockedList.mockResolvedValue([]);
    mockedAdd.mockRejectedValue(new UnisenseApiError("指标不存在: GHOST", "NOT_FOUND", 404, "t"));
    renderPage();

    const input = screen.getByPlaceholderText(/指标编码/);
    fireEvent.change(input, { target: { value: "GHOST" } });
    fireEvent.click(screen.getByRole("button", { name: /添加收藏/ }));

    await waitFor(() => expect(screen.findByText(/指标不存在/)).toBeTruthy());
  });

  it("移除收藏调用 API 并刷新", async () => {
    mockedList.mockResolvedValue([
      { metric_code: "GMV", name: "成交总额", domain: "finance", status: "PUBLISHED" },
    ]);
    renderPage();
    await screen.findByText("成交总额");

    fireEvent.click(screen.getByRole("button", { name: /移除/ }));
    await waitFor(() => expect(mockedRemove).toHaveBeenCalledWith("GMV"));
    expect(mockedList).toHaveBeenCalledTimes(2);
  });

  it("点击收藏项跳转指标详情", async () => {
    mockedList.mockResolvedValue([
      { metric_code: "GMV", name: "成交总额", domain: "finance", status: "PUBLISHED" },
    ]);
    renderPage();
    await screen.findByText("成交总额");

    fireEvent.click(screen.getByText("成交总额"));
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/detail/GMV"));
  });
});
