import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { App as AntApp } from "antd";
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

import { listDictTypes, listAllDictItems, createDictItem } from "../api";
const mockedTypes = vi.mocked(listDictTypes);
const mockedItems = vi.mocked(listAllDictItems);
const mockedCreate = vi.mocked(createDictItem);

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
    <AntApp>
      <MemoryRouter initialEntries={[initialEntry]}>
        <SystemDict />
      </MemoryRouter>
    </AntApp>,
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

  it("新增弹窗：输入显示名后自动生成英文编码预览（分钟 → minute）", async () => {
    renderDict();
    await screen.findByText("日");
    fireEvent.click(screen.getByRole("button", { name: /新增参照数据项/ }));
    // 初始（未输入显示名）：与后端一致回退 item
    expect(screen.getByTestId("dict-code-preview")).toHaveValue("item");
    const labelInput = await screen.findByPlaceholderText("如 人民币元");
    fireEvent.change(labelInput, { target: { value: "分钟" } });
    await waitFor(() => expect(screen.getByTestId("dict-code-preview")).toHaveValue("minute"));
  });

  it("新增弹窗：编码与已有项冲突时预览自动追加序号（分钟 → minute_2）", async () => {
    // 当前类型下已存在 code=minute 的项（非软删）→ 预览应显示后端将生成的 minute_2
    mockedItems.mockResolvedValue([...ITEMS, { ...ITEMS[0], id: 3, code: "minute", label: "分钟（已存在）" }]);
    renderDict();
    await screen.findByText("日");
    fireEvent.click(screen.getByRole("button", { name: /新增参照数据项/ }));
    const labelInput = await screen.findByPlaceholderText("如 人民币元");
    fireEvent.change(labelInput, { target: { value: "分钟" } });
    await waitFor(() => expect(screen.getByTestId("dict-code-preview")).toHaveValue("minute_2"));
  });

  it("新增提交不传 code，由后端按显示名自动生成", async () => {
    mockedCreate.mockResolvedValue({} as any);
    renderDict();
    await screen.findByText("日");
    fireEvent.click(screen.getByRole("button", { name: /新增参照数据项/ }));
    const labelInput = await screen.findByPlaceholderText("如 人民币元");
    fireEvent.change(labelInput, { target: { value: "人民币元" } });
    // Modal 未包 ConfigProvider，antd 默认英文 locale → 直接点主按钮提交
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => expect(mockedCreate).toHaveBeenCalled());
    const callArg = mockedCreate.mock.calls[0][1] as { code?: string; label: string };
    expect(callArg.label).toBe("人民币元");
    expect(callArg.code).toBeUndefined();
  });

  it("新增弹窗打开时静默刷新项列表（缩小并发滞后窗口）", async () => {
    renderDict();
    await screen.findByText("日");
    const callsBefore = mockedItems.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: /新增参照数据项/ }));
    // openCreate 触发一次静默刷新（不置 loading），供编码预览基于最新项重算
    await waitFor(() => expect(mockedItems.mock.calls.length).toBeGreaterThan(callsBefore));
  });

  it("同名编码超上限：预览切换为「需手动指定」并可输入编码透传提交", async () => {
    // base=item 的编码链被全部占用（item、item_2 … item_100 共 101 个）→
    // resolveUniqueCode 超上限回退 base（item 仍被占用）→ 需手动指定编码
    const exhausted: SystemDictItem[] = [{ ...ITEMS[0], id: 3, code: "item" }];
    for (let n = 2; n <= 100; n += 1) {
      exhausted.push({ ...ITEMS[0], id: 100 + n, code: `item_${n}` });
    }
    mockedItems.mockResolvedValue(exhausted);
    mockedCreate.mockResolvedValue({} as any);
    renderDict();
    // 101 项大列表渲染较慢，直接等「新增」按钮（不遍历表格行）
    fireEvent.click(await screen.findByRole("button", { name: /新增参照数据项/ }));
    // 纯标点显示名 → slugifyCode 回退 item → 超上限 → 切换为手动指定
    const labelInput = await screen.findByPlaceholderText("如 人民币元");
    fireEvent.change(labelInput, { target: { value: "!!!" } });
    expect(await screen.findByTestId("dict-code-manual")).toBeTruthy();
    expect(screen.queryByTestId("dict-code-preview")).toBeNull();
    expect(screen.getByText("需手动指定")).toBeTruthy();
    // 手动输入编码并提交 → code 随表单透传（后端不再自动生成）
    fireEvent.change(screen.getByTestId("dict-code-manual"), { target: { value: "item_101" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => expect(mockedCreate).toHaveBeenCalled());
    const arg = mockedCreate.mock.calls[0][1] as { code?: string; label: string };
    expect(arg.code).toBe("item_101");
    // 101 项大列表渲染 + 全量并行下耗时较长，单独放宽超时
  }, 15000);

  it("超上限手动编码后改显示名不再冲突：切回自动生成且不残留旧 code", async () => {
    // 构造超上限场景（base=item 编码链被全部占用）→ 输入手动 code
    const exhausted: SystemDictItem[] = [{ ...ITEMS[0], id: 3, code: "item" }];
    for (let n = 2; n <= 100; n += 1) {
      exhausted.push({ ...ITEMS[0], id: 100 + n, code: `item_${n}` });
    }
    mockedItems.mockResolvedValue(exhausted);
    mockedCreate.mockResolvedValue({} as any);
    renderDict();
    fireEvent.click(await screen.findByRole("button", { name: /新增参照数据项/ }));
    const labelInput = await screen.findByPlaceholderText("如 人民币元");
    fireEvent.change(labelInput, { target: { value: "!!!" } });
    fireEvent.change(await screen.findByTestId("dict-code-manual"), { target: { value: "item_101" } });
    // 改显示名为「分钟」→ base=minute（不在 used）→ 切回自动生成预览
    fireEvent.change(labelInput, { target: { value: "分钟" } });
    await waitFor(() => expect(screen.getByTestId("dict-code-preview")).toHaveValue("minute"));
    expect(screen.queryByTestId("dict-code-manual")).toBeNull();
    // preserve=false：字段卸载时值被清除 → 提交不残留旧 code
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => expect(mockedCreate).toHaveBeenCalled());
    const arg = mockedCreate.mock.calls[0][1] as { code?: string; label: string };
    expect(arg.code).toBeUndefined();
    // 101 项大列表渲染 + 全量并行下耗时较长，单独放宽超时
  }, 15000);
});
