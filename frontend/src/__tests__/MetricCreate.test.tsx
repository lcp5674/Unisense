import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { App as AntApp } from "antd";
import { MetricCreate } from "../pages/MetricCreate";

// 批量注册依赖后端 POST /metric-definitions/batch-register（对齐 FR-030）
vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    listDomainTree: vi.fn(),
    listDictItems: vi.fn(),
    listCatalogs: vi.fn(),
    batchRegisterMetrics: vi.fn(),
  };
});

import { listDomainTree, listDictItems, listCatalogs, batchRegisterMetrics } from "../api";
import type { DBCatalog, SubjectDomainTreeNode } from "../types";

const mockedTree = vi.mocked(listDomainTree);
const mockedDict = vi.mocked(listDictItems);
const mockedCatalogs = vi.mocked(listCatalogs);
const mockedBatch = vi.mocked(batchRegisterMetrics);

/** 构造完整 DBCatalog（源表搜索 mock 用），仅 entity_name/source_name 参与渲染。 */
function makeCatalog(entityName: string): DBCatalog {
  return {
    source_id: "src_test",
    entity_name: entityName,
    entity_type: "TABLE",
    schema_def: {},
    etl_sql: null,
    sensitivity_level: "L2",
    owner_id: null,
    upstream_signature: "",
    content_signature: null,
    schema_incomplete: true,
    source_name: null,
  };
}

const TREE: SubjectDomainTreeNode[] = [
  {
    id: 1,
    code: "sales",
    name: "销售",
    parent_id: null,
    level: 1,
    sort_order: 0,
    status: "active",
    metric_count: 3,
    children: [
      {
        id: 2,
        code: "sales_order",
        name: "订单",
        parent_id: 1,
        level: 2,
        sort_order: 0,
        status: "active",
        metric_count: 1,
        children: [],
      },
    ],
  },
  {
    id: 3,
    code: "finance",
    name: "财务",
    parent_id: null,
    level: 1,
    sort_order: 1,
    status: "active",
    metric_count: 0,
    children: [],
  },
];

/** 组件依赖 AntApp.useApp() 的 message，渲染时需包 <App> 提供真实 context；页面用 useNavigate 需配路由。 */
function renderPage() {
  return render(
    <AntApp>
      <MemoryRouter initialEntries={["/create"]}>
        <Routes>
          <Route path="/create" element={<MetricCreate />} />
          <Route path="/detail/:code" element={<div>detail</div>} />
        </Routes>
      </MemoryRouter>
    </AntApp>,
  );
}

/** 打开批量注册弹窗（点击页面右上角「批量注册指标」按钮）。 */
async function openBatchModal() {
  fireEvent.click(await screen.findByText("批量注册指标"));
  const modal = document.querySelector(".ant-modal") as HTMLElement;
  expect(modal).toBeTruthy();
  return modal;
}

/**
 * 在可见下拉中点击指定选项：antd 虚拟列表会渲染同名包裹节点，直接 getByText 会命中多个，
 * 必须点击 .ant-select-item-option 本体（title=选项文本）才能触发选中。
 */
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

/** 批量表单公共填充：选域（默认「销售 (sales)」）+ 源表搜索选中 + 填写度量列。 */
async function fillBatchForm(modal: HTMLElement, measureColumns: string) {
  fireEvent.mouseDown(within(modal).getByText("选择所属业务域（须为 active 域）"));
  await clickSelectOption("销售 (sales)");

  // 源表 Select showSearch：id 与主表单重复，需限定在弹窗内查询搜索输入
  const srcInput = modal.querySelector('input[id="source_table"]') as HTMLInputElement;
  fireEvent.mouseDown(srcInput);
  fireEvent.change(srcInput, { target: { value: "dwd" } });
  await clickSelectOption("dwd.sales_detail");

  fireEvent.change(within(modal).getByLabelText("度量列"), { target: { value: measureColumns } });
}

describe("MetricCreate 批量注册指标", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedTree.mockResolvedValue(TREE);
    mockedDict.mockResolvedValue([]);
    mockedCatalogs.mockResolvedValue({
      items: [makeCatalog("dwd.sales_detail")],
      total: 1,
      page: 1,
      page_size: 20,
    });
  });

  it("点击「批量注册指标」打开弹窗，展示批量表单", async () => {
    renderPage();
    const modal = await openBatchModal();
    expect(within(modal).getByText("批量注册指标")).toBeTruthy();
    expect(within(modal).getByText("业务域")).toBeTruthy();
    expect(within(modal).getByText("源表名")).toBeTruthy();
    expect(within(modal).getByText("度量列")).toBeTruthy();
    expect(within(modal).getByText("提交批量注册")).toBeTruthy();
  });

  it("提交批量注册携带正确 payload（源表/度量列/域/LLM 预填）", async () => {
    mockedBatch.mockResolvedValue({
      batch_id: "batch_abc123",
      candidates: [
        { metric_code: "sales_gmv_day", status: "DRAFT", validation_errors: null },
        { metric_code: "sales_order_cnt_day", status: "DRAFT", validation_errors: null },
      ],
    });
    renderPage();
    const modal = await openBatchModal();

    await fillBatchForm(modal, "gmv\norder_cnt");
    fireEvent.click(within(modal).getByText("提交批量注册"));

    await waitFor(() => {
      expect(mockedBatch).toHaveBeenCalledWith({
        source_table: "dwd.sales_detail",
        measure_columns: ["gmv", "order_cnt"],
        domain: "sales",
        llm_prefill: true,
        dimension_mapping: undefined,
      });
    });

    // 结果区展示批次号与成功明细
    await waitFor(() => {
      expect(screen.getByText(/batch_abc123/)).toBeTruthy();
      expect(screen.getByText("批量注册完成：成功 2 / 失败 0")).toBeTruthy();
      expect(screen.getByText("sales_gmv_day")).toBeTruthy();
      expect(screen.getByText("sales_order_cnt_day")).toBeTruthy();
    });
  });

  it("部分失败时展示失败原因明细", async () => {
    mockedBatch.mockResolvedValue({
      batch_id: "batch_fail1",
      candidates: [
        { metric_code: "sales_gmv_day", status: "DRAFT", validation_errors: null },
        { metric_code: "sales_dup_day", status: "VALIDATION_ERROR", validation_errors: "指标编码已存在" },
      ],
    });
    renderPage();
    const modal = await openBatchModal();

    await fillBatchForm(modal, "gmv\ndup");
    fireEvent.click(within(modal).getByText("提交批量注册"));

    await waitFor(() => {
      expect(mockedBatch).toHaveBeenCalled();
    });

    // 部分失败：警告色提示 + 失败原因列展示
    await waitFor(() => {
      expect(screen.getByText("批量注册完成：成功 1 / 失败 1")).toBeTruthy();
      expect(screen.getByText("校验失败")).toBeTruthy();
      expect(screen.getByText("指标编码已存在")).toBeTruthy();
    });
  });

  it("度量列为空时提示且不发请求", async () => {
    renderPage();
    const modal = await openBatchModal();

    // 只填空白行（合法域/源表但无度量列 → 前端拦截不发请求）
    await fillBatchForm(modal, "  \n  ");
    fireEvent.click(within(modal).getByText("提交批量注册"));

    await waitFor(() => {
      expect(screen.getByText("请至少填写一个度量列")).toBeTruthy();
    });
    expect(mockedBatch).not.toHaveBeenCalled();
  });
});
