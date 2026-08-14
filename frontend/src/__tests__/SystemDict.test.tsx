import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { SystemDict } from "../pages/SystemDict";
import type { SystemDictItem } from "../types";

vi.mock("../api", () => ({
  listDictTypes: vi.fn(),
  listAllDictItems: vi.fn(),
  createDictItem: vi.fn(),
  updateDictItem: vi.fn(),
  deactivateDictItem: vi.fn(),
  activateDictItem: vi.fn(),
  deleteDictItem: vi.fn(),
}));

import { listDictTypes, listAllDictItems } from "../api";
const mockedTypes = vi.mocked(listDictTypes);
const mockedItems = vi.mocked(listAllDictItems);

const ITEMS: SystemDictItem[] = [
  {
    id: 1,
    dict_type: "granularity",
    code: "daily",
    label: "日",
    sort_order: 1,
    status: "active",
    description: null,
    ref_count: 3,
    created_at: "2026-08-13T00:00:00",
    updated_at: "2026-08-13T00:00:00",
  },
  {
    id: 2,
    dict_type: "granularity",
    code: "weekly",
    label: "周",
    sort_order: 2,
    status: "inactive",
    description: null,
    ref_count: 0,
    created_at: "2026-08-13T00:00:00",
    updated_at: "2026-08-13T00:00:00",
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockedTypes.mockResolvedValue(["granularity"]);
  mockedItems.mockResolvedValue(ITEMS);
});

function renderDict(initialEntry = "/dicts") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <SystemDict />
    </MemoryRouter>,
  );
}

describe("SystemDict 页面", () => {
  it("渲染参照数据项列表", async () => {
    renderDict();
    await screen.findByText("日");
    expect(screen.getByText("周")).toBeInTheDocument();
    expect(mockedItems).toHaveBeenCalledWith("granularity");
  });

  it("从总览仪表 ?status=active 直达：仅展示启用项", async () => {
    renderDict("/dicts?status=active");
    await screen.findByText("日");
    // 周（inactive）被客户端状态筛选隐藏
    expect(screen.queryByText("周")).not.toBeInTheDocument();
  });

  it("从总览仪表 ?status=inactive 直达：仅展示停用项", async () => {
    renderDict("/dicts?status=inactive");
    await waitFor(() => expect(screen.getByText("周")).toBeInTheDocument());
    expect(screen.queryByText("日")).not.toBeInTheDocument();
  });

  it("状态筛选下拉可切换过滤", async () => {
    renderDict();
    await screen.findByText("日");
    fireEvent.mouseDown(screen.getByRole("combobox"));
    // antd 下拉选项为 .ant-select-item-option（可见文本「启用/停用」），
    // 与隐藏原生 select 的 option 值（active/inactive）区分开
    await new Promise((r) => setTimeout(r, 100));
    const optionEls = Array.from(document.querySelectorAll<HTMLElement>(".ant-select-item-option"));
    const inactiveOpt = optionEls.find((o) => o.textContent?.includes("停用"));
    expect(inactiveOpt).toBeTruthy();
    fireEvent.click(inactiveOpt!);
    await waitFor(() => expect(screen.getByText("周")).toBeInTheDocument());
    expect(screen.queryByText("日")).not.toBeInTheDocument();
  });

  it("提供统一的返回按钮（返回上一入口）", async () => {
    renderDict();
    await screen.findByText("日");
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/lineage", "/dicts"]}>
        <Routes>
          <Route path="/lineage" element={<div>lineage-page</div>} />
          <Route path="/dicts" element={<SystemDict />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("日");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("lineage-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    render(
      <MemoryRouter initialEntries={["/dicts"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/dicts" element={<SystemDict />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("日");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("dashboard-page");
  });
});
