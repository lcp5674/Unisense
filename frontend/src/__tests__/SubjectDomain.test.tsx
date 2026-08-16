import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { App as AntApp } from "antd";
import { SubjectDomain, previewDomainCode } from "../pages/SubjectDomain";
import type { SubjectDomainTreeNode } from "../types";

vi.mock("../api", () => ({
  listDomainTree: vi.fn(),
  getDomain: vi.fn(),
  createDomain: vi.fn(),
  updateDomain: vi.fn(),
  deactivateDomain: vi.fn(),
  activateDomain: vi.fn(),
  deleteDomain: vi.fn(),
  getDomainDefaults: vi.fn(),
  updateDomainDefaults: vi.fn(),
  listDictItems: vi.fn(),
}));

import { listDomainTree, createDomain, getDomain, getDomainDefaults, updateDomain, listDictItems } from "../api";

const mockedList = vi.mocked(listDomainTree);
const mockedCreate = vi.mocked(createDomain);
const mockedGet = vi.mocked(getDomain);
const mockedDefaults = vi.mocked(getDomainDefaults);
const mockedUpdate = vi.mocked(updateDomain);
const mockedDictItems = vi.mocked(listDictItems);

/** 组件依赖 AntApp.useApp() 的 message/modal，渲染时需包 <App> 提供真实 context；组件用 useNavigate 需配路由。 */
function renderPage() {
  return render(
    <AntApp>
      <MemoryRouter>
        <SubjectDomain />
      </MemoryRouter>
    </AntApp>,
  );
}

const TREE: SubjectDomainTreeNode[] = [
  {
    id: 1, code: "sales", name: "销售", parent_id: null, level: 1, sort_order: 0,
    status: "active", metric_count: 3,
    children: [
      { id: 2, code: "sales_order", name: "订单", parent_id: 1, level: 2, sort_order: 0, status: "active", metric_count: 1, children: [] },
    ],
  },
  { id: 3, code: "finance", name: "财务", parent_id: null, level: 1, sort_order: 1, status: "active", metric_count: 0, children: [] },
];

describe("previewDomainCode", () => {
  it("ASCII 显示名 → 小写下划线 slug", () => {
    expect(previewDomainCode("Sales Platform")).toBe("sales_platform");
  });

  it("纯中文根域 → 英文 slug", () => {
    expect(previewDomainCode("销售")).toBe("sales");
  });

  it("子域带父域前缀", () => {
    expect(previewDomainCode("Order", "sales")).toBe("sales_order");
  });

  it("子域纯中文 → 英文并带父域前缀", () => {
    expect(previewDomainCode("订单", "sales")).toBe("sales_order");
  });

  it("中英混合 → 英文与 ASCII 用下划线连接", () => {
    expect(previewDomainCode("销售订单GMV")).toBe("sales_order_gmv");
  });

  it("含特殊符号折叠为下划线", () => {
    expect(previewDomainCode("Order Data & Stats!", "sales")).toBe("sales_order_data_stats");
  });
});

describe("SubjectDomain 页面", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedList.mockResolvedValue(TREE);
    mockedCreate.mockResolvedValue({ id: 4, code: "risk", name: "风控", parent_id: null, level: 1, path: "4", sort_order: 0, status: "active", defaults_json: {}, description: null, owner_id: 1, metric_count: 0, created_at: "", updated_at: "" });
    mockedGet.mockResolvedValue({ id: 1, code: "sales", name: "销售", parent_id: null, level: 1, path: "1", sort_order: 0, status: "active", defaults_json: {}, description: null, owner_id: 1, metric_count: 3, created_at: "", updated_at: "" });
    mockedDefaults.mockResolvedValue({});
    // 域默认值字典：默认返回空（下拉回退输入框），不影响既有测试
    mockedDictItems.mockResolvedValue([]);
  });

  it("渲染域树与「新建根域」按钮", async () => {
    renderPage();
    expect(await screen.findByText("销售")).toBeTruthy();
    expect(screen.getAllByText("新建根域").length).toBeGreaterThan(0);
    expect(screen.getByText("订单")).toBeTruthy();
    // 每个节点带「新建子域」按钮
    expect(screen.getByLabelText("新建子域-销售")).toBeTruthy();
    expect(screen.getByLabelText("新建子域-订单")).toBeTruthy();
  });

  it("新建弹窗：编码预览随显示名实时变化（空名兜底 domain，中文转英文）", async () => {
    renderPage();
    await screen.findByText("销售");
    fireEvent.click(screen.getAllByText("新建根域")[0]);

    const input = await screen.findByPlaceholderText("如 销售");
    const preview = screen.getByTestId("domain-code-preview") as HTMLInputElement;
    // 初始为空 → domain
    expect(preview.value).toBe("domain");

    fireEvent.change(input, { target: { value: "Risk Control" } });
    expect((screen.getByTestId("domain-code-preview") as HTMLInputElement).value).toBe("risk_control");

    // 纯中文 → 英文
    fireEvent.change(input, { target: { value: "销售" } });
    expect((screen.getByTestId("domain-code-preview") as HTMLInputElement).value).toBe("sales");
  });

  it("新建弹窗：选父域后编码预览带父域前缀", async () => {
    renderPage();
    await screen.findByText("销售");
    fireEvent.click(screen.getAllByText("新建根域")[0]);
    await screen.findByPlaceholderText("如 销售");

    // 打开上级域下拉并选择「销售」
    fireEvent.mouseDown(screen.getByRole("combobox"));
    const option = await screen.findByTitle("销售");
    fireEvent.click(option);

    fireEvent.change(screen.getByPlaceholderText("如 销售"), { target: { value: "Order" } });
    expect((screen.getByTestId("domain-code-preview") as HTMLInputElement).value).toBe("sales_order");
  });

  it("提交不传 code（由后端自动生成）", async () => {
    renderPage();
    await screen.findByText("销售");
    fireEvent.click(screen.getAllByText("新建根域")[0]);

    fireEvent.change(await screen.findByPlaceholderText("如 销售"), { target: { value: "Risk" } });
    // Modal 未包 ConfigProvider，antd 默认英文 locale → OK 按钮文本为 "OK"
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);

    await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1));
    const arg = mockedCreate.mock.calls[0][0];
    expect(arg.code).toBeUndefined();
    expect(arg.name).toBe("Risk");
    expect(arg.parent_id).toBeNull();
    // PLAT-2：owner_id 不随前端下发，后端以认证身份为准
    expect(arg.owner_id).toBeUndefined();
  });

  it("点击树节点「新建子域」打开弹窗且父域已预选", async () => {
    renderPage();
    await screen.findByText("销售");

    fireEvent.click(screen.getByLabelText("新建子域-销售"));
    expect(await screen.findByText("在「销售」下新建子域")).toBeTruthy();

    fireEvent.change(screen.getByPlaceholderText("如 销售"), { target: { value: "Order" } });
    expect((screen.getByTestId("domain-code-preview") as HTMLInputElement).value).toBe("sales_order");
  });

  it("创建根域与已存在同名域冲突：显示警告并拦截提交", async () => {
    renderPage();
    await screen.findByText("销售");
    fireEvent.click(screen.getAllByText("新建根域")[0]);

    fireEvent.change(await screen.findByPlaceholderText("如 销售"), { target: { value: "销售" } });
    // 同父域（根域）已存在「销售」（sales）
    expect(await screen.findByTestId("create-dup-warning")).toBeTruthy();
    expect(screen.getByTestId("create-dup-warning").textContent).toContain("销售");
    expect(screen.getByTestId("create-dup-warning").textContent).toContain("sales");

    // 提交被拦截：createDomain 不被调用
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await new Promise((r) => setTimeout(r, 50));
    expect(mockedCreate).not.toHaveBeenCalled();
  });

  it("不同父域下同名不触发冲突警告", async () => {
    renderPage();
    await screen.findByText("销售");
    // 在「销售」下新建名为「财务」的子域——根域已有「财务」(finance)，但不同父域应放行
    fireEvent.click(screen.getByLabelText("新建子域-销售"));

    fireEvent.change(screen.getByPlaceholderText("如 销售"), { target: { value: "财务" } });
    expect(screen.queryByTestId("create-dup-warning")).toBeNull();
  });

  it("编辑改名撞同父域同名：显示警告并拦截提交", async () => {
    renderPage();
    await screen.findByText("销售");
    // 选中「销售」节点 → 打开编辑弹窗
    fireEvent.click(screen.getByText("销售"));
    fireEvent.click(await screen.findByText("编辑"));

    // 编辑弹窗 name 预填「销售」，改为「财务」（根域已存在 finance/财务）
    const editInput = await screen.findByDisplayValue("销售");
    fireEvent.change(editInput, { target: { value: "财务" } });
    expect(await screen.findByTestId("edit-dup-warning")).toBeTruthy();
    expect(screen.getByTestId("edit-dup-warning").textContent).toContain("财务");

    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await new Promise((r) => setTimeout(r, 50));
    expect(mockedUpdate).not.toHaveBeenCalled();
  });

  it("编辑保持原名（仅改描述）不触发同名警告", async () => {
    renderPage();
    await screen.findByText("销售");
    fireEvent.click(screen.getByText("销售"));
    fireEvent.click(await screen.findByText("编辑"));

    const editInput = await screen.findByDisplayValue("销售");
    fireEvent.change(editInput, { target: { value: "销售" } });
    expect(screen.queryByTestId("edit-dup-warning")).toBeNull();
  });

  it("提供统一的返回按钮（返回上一入口）", async () => {
    renderPage();
    await screen.findByText("销售");
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <AntApp>
        <MemoryRouter initialEntries={["/search", "/domains"]}>
          <Routes>
            <Route path="/search" element={<div>search-page</div>} />
            <Route path="/domains" element={<SubjectDomain />} />
          </Routes>
        </MemoryRouter>
      </AntApp>,
    );
    await screen.findByText("销售");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("search-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    render(
      <AntApp>
        <MemoryRouter initialEntries={["/domains"]}>
          <Routes>
            <Route path="/dashboard" element={<div>dashboard-page</div>} />
            <Route path="/domains" element={<SubjectDomain />} />
          </Routes>
        </MemoryRouter>
      </AntApp>,
    );
    await screen.findByText("销售");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("dashboard-page");
  });

  it("域默认值弹窗：字典字段渲染下拉选项而非自由文本（对齐字典枚举，避免手输非法值）", async () => {
    // 粒度字典返回「日（day）」「月（month）」等选项
    mockedDictItems.mockImplementation((dictType: string) =>
      Promise.resolve(
        dictType === "granularity"
          ? [
              { id: 1, dict_type: "granularity", code: "day", label: "日", sort_order: 0, status: "active", description: null, ref_count: 0, created_at: "", updated_at: "" },
              { id: 2, dict_type: "granularity", code: "month", label: "月", sort_order: 1, status: "active", description: null, ref_count: 0, created_at: "", updated_at: "" },
            ]
          : [],
      ),
    );
    renderPage();
    // 点击树节点「销售」→ 加载详情卡（默认值按钮在详情卡内）
    fireEvent.click(await screen.findByText("销售"));
    await screen.findByRole("button", { name: /默\s*认\s*值/ });
    // 打开默认值弹窗（详情卡内「默认值」按钮）
    fireEvent.click(screen.getByRole("button", { name: /默\s*认\s*值/ }));
    await screen.findByText("配置域默认值");
    // 粒度字段应渲染为 Select（含「日（day）」选项），而非 Input
    const granularitySelect = document.querySelector(".ant-select");
    expect(granularitySelect).toBeTruthy();
    // 确认字典项以「日（day）」标签出现（仅当下拉展开时可见；此处验证 Select 已渲染而非 Input）
    expect(document.querySelectorAll(".ant-select").length).toBeGreaterThan(0);
    expect(document.querySelector("input[placeholder*='默认粒度值']")).toBeNull();
  });
});
