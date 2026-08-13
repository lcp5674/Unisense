import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
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
}));

import { listDomainTree, createDomain, getDomain, getDomainDefaults } from "../api";

const mockedList = vi.mocked(listDomainTree);
const mockedCreate = vi.mocked(createDomain);
const mockedGet = vi.mocked(getDomain);
const mockedDefaults = vi.mocked(getDomainDefaults);

/** 组件依赖 AntApp.useApp() 的 message/modal，渲染时需包 <App> 提供真实 context。 */
function renderPage() {
  return render(
    <AntApp>
      <SubjectDomain />
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

  it("纯中文根域 → domain 兜底", () => {
    expect(previewDomainCode("销售")).toBe("domain");
  });

  it("子域带父域前缀", () => {
    expect(previewDomainCode("Order", "sales")).toBe("sales_order");
  });

  it("子域纯中文 → 父域_sub 兜底", () => {
    expect(previewDomainCode("订单", "sales")).toBe("sales_sub");
  });

  it("含特殊符号折叠为下划线", () => {
    expect(previewDomainCode("Order Data & Stats!", "sales")).toBe("sales_order_data_stats");
  });
});

describe("SubjectDomain 页面", () => {
  beforeEach(() => {
    mockedList.mockResolvedValue(TREE);
    mockedCreate.mockResolvedValue({ id: 4, code: "risk", name: "风控", parent_id: null, level: 1, path: "4", sort_order: 0, status: "active", defaults_json: {}, description: null, owner_id: 1, metric_count: 0, created_at: "", updated_at: "" });
    mockedGet.mockResolvedValue({ id: 1, code: "sales", name: "销售", parent_id: null, level: 1, path: "1", sort_order: 0, status: "active", defaults_json: {}, description: null, owner_id: 1, metric_count: 3, created_at: "", updated_at: "" });
    mockedDefaults.mockResolvedValue({});
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

  it("新建弹窗：编码预览随显示名实时变化，纯中文兜底 domain", async () => {
    renderPage();
    await screen.findByText("销售");
    fireEvent.click(screen.getAllByText("新建根域")[0]);

    const input = await screen.findByPlaceholderText("如 销售");
    const preview = screen.getByTestId("domain-code-preview") as HTMLInputElement;
    // 初始为空 → domain
    expect(preview.value).toBe("domain");

    fireEvent.change(input, { target: { value: "Risk Control" } });
    expect((screen.getByTestId("domain-code-preview") as HTMLInputElement).value).toBe("risk_control");
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
});
