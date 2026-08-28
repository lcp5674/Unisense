import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { App as AntApp } from "antd";
import { MeasureCatalogs } from "../pages/MeasureCatalogs";
import type { MeasureCatalog, MeasureSuggestResult } from "../types";

vi.mock("../api", () => ({
  listMeasureCatalogs: vi.fn(),
  createMeasureCatalog: vi.fn(),
  updateMeasureCatalog: vi.fn(),
  publishMeasureCatalog: vi.fn(),
  deprecateMeasureCatalog: vi.fn(),
  reactivateMeasureCatalog: vi.fn(),
  deleteMeasureCatalog: vi.fn(),
  restoreMeasureCatalog: vi.fn(),
  purgeMeasureCatalog: vi.fn(),
  submitMeasureCatalog: vi.fn(),
  approveMeasureCatalog: vi.fn(),
  rejectMeasureCatalog: vi.fn(),
  batchSubmitMeasures: vi.fn(),
  batchApproveMeasures: vi.fn(),
  batchRejectMeasures: vi.fn(),
  batchDeprecateMeasures: vi.fn(),
  batchReactivateMeasures: vi.fn(),
  batchDeleteMeasures: vi.fn(),
  fetchCurrentUser: vi.fn(),
  listDomainTree: vi.fn(),
  listDictItems: vi.fn(),
  listUsers: vi.fn(),
  autoSuggestMeasureCatalog: vi.fn(),
  inferMeasureSynonyms: vi.fn(),
  UnisenseApiError: class extends Error {},
}));

import {
  approveMeasureCatalog,
  autoSuggestMeasureCatalog,
  batchSubmitMeasures,
  createMeasureCatalog,
  deleteMeasureCatalog,
  fetchCurrentUser,
  inferMeasureSynonyms,
  listDictItems,
  listDomainTree,
  listMeasureCatalogs,
  listUsers,
  purgeMeasureCatalog,
  reactivateMeasureCatalog,
  rejectMeasureCatalog,
  restoreMeasureCatalog,
  submitMeasureCatalog,
} from "../api";
import { PermissionProvider } from "../hooks/usePermission";

const mockedList = vi.mocked(listMeasureCatalogs);
const mockedDomains = vi.mocked(listDomainTree);
const mockedDictItems = vi.mocked(listDictItems);
const mockedSuggest = vi.mocked(autoSuggestMeasureCatalog);
const mockedInferSynonyms = vi.mocked(inferMeasureSynonyms);
const mockedCreate = vi.mocked(createMeasureCatalog);
const mockedSubmit = vi.mocked(submitMeasureCatalog);
const mockedApprove = vi.mocked(approveMeasureCatalog);
const mockedReject = vi.mocked(rejectMeasureCatalog);
const mockedBatchSubmit = vi.mocked(batchSubmitMeasures);
const mockedCurrentUser = vi.mocked(fetchCurrentUser);
const mockedUsers = vi.mocked(listUsers);
const mockedReactivate = vi.mocked(reactivateMeasureCatalog);
const mockedDelete = vi.mocked(deleteMeasureCatalog);
const mockedRestore = vi.mocked(restoreMeasureCatalog);
const mockedPurge = vi.mocked(purgeMeasureCatalog);

const measure: MeasureCatalog = {
  id: 1,
  measure_code: "medical_fee_men_zhen_shou_fei",
  name: "门诊收费金额",
  description: null,
  measure_format: "AMOUNT",
  row_version: 1,
  default_unit: "CNY",
  default_decimal_places: 2,
  source_system: ["HIS"],
  synonyms: null,
  category: "FEE",
  stat_caliber: null,
  domain: "medical_fee",
  owner_id: 1,
  status: "DRAFT",
  created_at: "2026-08-01T00:00:00",
  updated_at: "2026-08-02T00:00:00",
};

const suggestRes: MeasureSuggestResult = {
  fields: {
    measure_code: {
      value: "medical_fee_men_zhen_shou_fei_amount",
      source: "rule",
      confidence: 0.9,
      reason: "由业务域与名称自动生成",
    },
    measure_format: { value: "AMOUNT", source: "rule", confidence: 0.85, reason: "命中收费语义" },
    default_unit: { value: "CNY", source: "rule", confidence: 0.9, reason: "金额联动" },
    default_decimal_places: { value: 2, source: "rule", confidence: 0.9, reason: "金额联动" },
    category: { value: "FEE", source: "rule", confidence: 0.8, reason: "费用类" },
    stat_caliber: { value: "收费明细按结算日期去重后求和", source: "llm", confidence: 0.7, reason: "AI 推断" },
    synonyms: { value: ["门诊收入"], source: "llm", confidence: 0.7, reason: "AI 推断" },
    source_system: { value: ["HIS"], source: "rule", confidence: 0.6, reason: "医疗域推断" },
    description: { value: "统计门诊收费总额，含药品/检查/检验费用", source: "llm", confidence: 0.7, reason: "AI 推断" },
    domain: { value: "medical_fee", source: "rule", confidence: 0.7, reason: "沿用输入" },
  },
};

function renderCatalogs() {
  return render(
    <MemoryRouter>
      <AntApp>
        <PermissionProvider
          user={
            {
              id: 1,
              username: "admin",
              display_name: "管理员",
              role: "platform_admin",
              domain: null,
              org_id: 1,
            } as never
          }
        >
          <MeasureCatalogs />
        </PermissionProvider>
      </AntApp>
    </MemoryRouter>,
  );
}

async function openCreateModal() {
  fireEvent.click(await screen.findByRole("button", { name: /新建逻辑度量/ }));
  const modal = await screen.findByRole("dialog");
  return modal;
}

// 度量分类字典种子（对齐 MeasureCategory 枚举，供 listDictItems mock）
const MOCK_CATEGORY_DICT = [
  { id: 1, dict_type: "measure_category", code: "FLOW", label: "流量类", sort_order: 0, status: "active", description: null, ref_count: 0, created_at: "", updated_at: "", extra: null },
  { id: 2, dict_type: "measure_category", code: "FEE", label: "费用类", sort_order: 1, status: "active", description: null, ref_count: 0, created_at: "", updated_at: "", extra: null },
  { id: 3, dict_type: "measure_category", code: "DRUG", label: "药品类", sort_order: 2, status: "active", description: null, ref_count: 0, created_at: "", updated_at: "", extra: null },
  { id: 4, dict_type: "measure_category", code: "MEDICAL_INSURANCE", label: "医保类", sort_order: 3, status: "active", description: null, ref_count: 0, created_at: "", updated_at: "", extra: null },
  { id: 5, dict_type: "measure_category", code: "EFFICIENCY", label: "效率类", sort_order: 4, status: "active", description: null, ref_count: 0, created_at: "", updated_at: "", extra: null },
  { id: 6, dict_type: "measure_category", code: "QUALITY", label: "质量类", sort_order: 5, status: "active", description: null, ref_count: 0, created_at: "", updated_at: "", extra: null },
  { id: 7, dict_type: "measure_category", code: "OTHER", label: "其他", sort_order: 6, status: "active", description: null, ref_count: 0, created_at: "", updated_at: "", extra: null },
];

describe("MeasureCatalogs 度量目录 AI 推断", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedList.mockResolvedValue({ items: [measure], total: 1, page: 1, page_size: 20 });
    mockedDictItems.mockResolvedValue(MOCK_CATEGORY_DICT);
    mockedDomains.mockResolvedValue([
      {
        id: 1,
        code: "medical_fee",
        name: "医疗费用",
        parent_id: null,
        level: 1,
        sort_order: 1,
        status: "active",
        metric_count: 0,
        children: [],
      },
    ]);
    mockedCurrentUser.mockResolvedValue({
      id: 1,
      username: "admin",
      display_name: "管理员",
      role: "platform_admin",
      domain: null,
      org_id: 1,
    } as never);
  });

  it("渲染度量列表（含度量分类列）", async () => {
    renderCatalogs();
    expect(await screen.findByText("门诊收费金额")).toBeInTheDocument();
    expect(screen.getByText("费用类")).toBeInTheDocument();
  });

  it("分类下拉从字典动态加载（listDictItems(measure_category)）", async () => {
    renderCatalogs();
    await openCreateModal();
    await waitFor(() => expect(mockedDictItems).toHaveBeenCalledWith("measure_category"));
  });

  it("新建弹窗展示「AI 推断」按钮，名称+描述推断后回填字段并标注来源", async () => {
    mockedSuggest.mockResolvedValue(suggestRes);
    renderCatalogs();
    const modal = await openCreateModal();

    // 填写名称与描述
    const nameInput = within(modal).getByLabelText("度量中文名");
    fireEvent.change(nameInput, { target: { value: "门诊收费金额" } });
    const descInput = within(modal).getByLabelText("描述");
    fireEvent.change(descInput, { target: { value: "门诊收费总额" } });

    fireEvent.click(within(modal).getByRole("button", { name: /AI 推断/ }));
    await waitFor(() =>
      expect(mockedSuggest).toHaveBeenCalledWith({
        name: "门诊收费金额",
        description: "门诊收费总额",
        domain: null,
      }),
    );

    // 回填断言
    await waitFor(() => {
      expect(within(modal).getByLabelText("逻辑度量编码（英文，缺省自动生成）")).toHaveValue(
        "medical_fee_men_zhen_shou_fei_amount",
      );
    });
    // 推断结果面板：AI 项（统计口径/同义词/描述）与规则项（编码/单位）来源标注
    expect((await screen.findAllByText(/收费明细按结算日期去重后求和/)).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/AI · 置信度 70%/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/规则 · 置信度 90%/).length).toBeGreaterThanOrEqual(1);
    // 描述词由 LLM 精炼并回填到表单（覆盖用户输入的原始描述）
    await waitFor(() => {
      expect(within(modal).getByLabelText("描述")).toHaveValue("统计门诊收费总额，含药品/检查/检验费用");
    });
  });

  it("提交携带推断回填的 category/stat_caliber", async () => {
    mockedSuggest.mockResolvedValue(suggestRes);
    mockedCreate.mockResolvedValue(measure);
    renderCatalogs();
    const modal = await openCreateModal();

    fireEvent.change(within(modal).getByLabelText("度量中文名"), {
      target: { value: "门诊收费金额" },
    });
    fireEvent.click(within(modal).getByRole("button", { name: /AI 推断/ }));
    await waitFor(() => expect(mockedSuggest).toHaveBeenCalled());

    fireEvent.click(within(modal).getByRole("button", { name: /确 定|确定|OK/ }));
    await waitFor(() =>
      expect(mockedCreate).toHaveBeenCalledWith(
        expect.objectContaining({
          name: "门诊收费金额",
          category: "FEE",
          stat_caliber: "收费明细按结算日期去重后求和",
          description: "统计门诊收费总额，含药品/检查/检验费用",
        }),
      ),
    );
  });

  it("未输入名称点「AI 推断」给出提示且不调用接口", async () => {
    renderCatalogs();
    const modal = await openCreateModal();
    fireEvent.click(within(modal).getByRole("button", { name: /AI 推断/ }));
    expect(await screen.findByText(/请先输入度量中文名/)).toBeInTheDocument();
    expect(mockedSuggest).not.toHaveBeenCalled();
  });

  it("AI 生成同义词：填写名称后点按钮 → 调 inferMeasureSynonyms 并合并回填到同义词标签", async () => {
    mockedInferSynonyms.mockResolvedValue({ synonyms: ["门诊收入", "诊费"] });
    renderCatalogs();
    const modal = await openCreateModal();

    fireEvent.change(within(modal).getByLabelText("度量中文名"), {
      target: { value: "门诊收费金额" },
    });
    fireEvent.click(within(modal).getByRole("button", { name: /AI 生成同义词/ }));

    await waitFor(() =>
      expect(mockedInferSynonyms).toHaveBeenCalledWith({
        name: "门诊收费金额",
        description: null,
      }),
    );
    // 生成结果合并回填为同义词标签（value 存于表单 store，展示为 antd tag）
    await waitFor(() => {
      expect(within(modal).getByText("门诊收入")).toBeInTheDocument();
    });
    expect(within(modal).getByText("诊费")).toBeInTheDocument();
  });

  it("AI 生成同义词：未填名称点按钮提示且不调用接口", async () => {
    renderCatalogs();
    const modal = await openCreateModal();
    fireEvent.click(within(modal).getByRole("button", { name: /AI 生成同义词/ }));
    expect(await screen.findByText(/请先输入度量中文名，再生成同义词/)).toBeInTheDocument();
    expect(mockedInferSynonyms).not.toHaveBeenCalled();
  });
});

describe("MeasureCatalogs 审核流（提交审核/通过/驳回）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedDictItems.mockResolvedValue(MOCK_CATEGORY_DICT);
    mockedDomains.mockResolvedValue([]);
    mockedUsers.mockResolvedValue([
      {
        id: 3,
        username: "doctor",
        display_name: "张医生",
        role: "domain_admin",
        domain: "medical_fee",
        status: "active",
      },
    ]);
    mockedCurrentUser.mockResolvedValue({
      id: 1,
      username: "admin",
      display_name: "管理员",
      role: "platform_admin",
      domain: null,
      org_id: 1,
    } as never);
  });

  it("DRAFT 度量显示「提交审核」，填写说明后调用 submitMeasureCatalog", async () => {
    mockedList.mockResolvedValue({ items: [measure], total: 1, page: 1, page_size: 20 });
    mockedSubmit.mockResolvedValue({ ...measure, status: "REVIEW" });
    renderCatalogs();

    fireEvent.click(await screen.findByRole("button", { name: /提交审核/ }));
    const modal = await screen.findByRole("dialog");
    fireEvent.change(within(modal).getByLabelText("提交说明"), {
      target: { value: "门诊收费口径已与业务对齐" },
    });
    fireEvent.click(within(modal).getByRole("button", { name: /确 定|确定|OK/ }));

    await waitFor(() =>
      expect(mockedSubmit).toHaveBeenCalledWith("medical_fee_men_zhen_shou_fei", {
        change_reason: "门诊收费口径已与业务对齐",
        reviewer_type: null,
        reviewer_id: null,
        reviewer_domain: null,
      }),
    );
    expect(await screen.findByText(/已提交审核/)).toBeInTheDocument();
  });

  it("REVIEW 度量（platform_admin 可审）审核通过并发布", async () => {
    const reviewRow = { ...measure, status: "REVIEW", submitted_by: 2 };
    mockedList.mockResolvedValue({ items: [reviewRow], total: 1, page: 1, page_size: 20 });
    mockedApprove.mockResolvedValue({ ...measure, status: "PUBLISHED" });
    renderCatalogs();

    fireEvent.click(await screen.findByRole("button", { name: "审核通过并发布" }));
    await waitFor(() =>
      expect(mockedApprove).toHaveBeenCalledWith("medical_fee_men_zhen_shou_fei", { comment: null }),
    );
    expect(await screen.findByText(/审核通过，已发布/)).toBeInTheDocument();
  });

  it("REVIEW 度量驳回：填写原因后调用 rejectMeasureCatalog", async () => {
    const reviewRow = { ...measure, status: "REVIEW", submitted_by: 2 };
    mockedList.mockResolvedValue({ items: [reviewRow], total: 1, page: 1, page_size: 20 });
    mockedReject.mockResolvedValue({ ...measure, status: "DRAFT" });
    renderCatalogs();

    fireEvent.click(await screen.findByRole("button", { name: "驳回该主数据" }));
    const modal = await screen.findByRole("dialog");
    fireEvent.change(within(modal).getByLabelText("驳回原因"), {
      target: { value: "统计口径与业务实际不符" },
    });
    fireEvent.click(within(modal).getByRole("button", { name: /确 定|确定|OK/ }));

    await waitFor(() =>
      expect(mockedReject).toHaveBeenCalledWith("medical_fee_men_zhen_shou_fei", {
        reason: "统计口径与业务实际不符",
      }),
    );
    expect(await screen.findByText(/已驳回，可修改后重新提交/)).toBeInTheDocument();
  });

  it("REVIEW 状态展示「审核中」且编辑按钮锁定", async () => {
    const reviewRow = { ...measure, status: "REVIEW", submitted_by: 2 };
    mockedList.mockResolvedValue({ items: [reviewRow], total: 1, page: 1, page_size: 20 });
    renderCatalogs();

    expect(await screen.findByText("审核中")).toBeInTheDocument();
    // 编辑按钮 disabled（审核中锁定）
    const editBtn = document.querySelector('button[disabled] .anticon-edit');
    expect(editBtn).toBeTruthy();
  });

  it("提交审核指定「指定用户」时评审用户为选项框，选择用户后提交 reviewer_id", async () => {
    mockedList.mockResolvedValue({ items: [measure], total: 1, page: 1, page_size: 20 });
    mockedSubmit.mockResolvedValue({ ...measure, status: "REVIEW" });
    renderCatalogs();

    fireEvent.click(await screen.findByRole("button", { name: /提交审核/ }));
    const modal = await screen.findByRole("dialog");

    // 评审指派选「指定用户」
    fireEvent.mouseDown(within(modal).getByRole("combobox"));
    fireEvent.click(await screen.findByTitle("指定用户"));

    // 评审用户渲染为选项框（含用户下拉），而非手动输入框
    await waitFor(() => expect(within(modal).getAllByRole("combobox")).toHaveLength(2));
    expect(within(modal).queryByPlaceholderText("如 5")).toBeNull();
    fireEvent.mouseDown(within(modal).getAllByRole("combobox")[1]);
    fireEvent.click(await screen.findByTitle("张医生（doctor）"));

    fireEvent.change(within(modal).getByLabelText("提交说明"), {
      target: { value: "门诊收费口径已与业务对齐" },
    });
    fireEvent.click(within(modal).getByRole("button", { name: /确 定|确定|OK/ }));

    await waitFor(() =>
      expect(mockedSubmit).toHaveBeenCalledWith("medical_fee_men_zhen_shou_fei", {
        change_reason: "门诊收费口径已与业务对齐",
        reviewer_type: "user",
        reviewer_id: 3,
        reviewer_domain: null,
      }),
    );
    expect(await screen.findByText(/已提交审核/)).toBeInTheDocument();
  });

  it("批量操作：勾选草稿逻辑度量 → 批量提交审核 → 调用 batchSubmitMeasures", async () => {
    mockedBatchSubmit.mockResolvedValue({
      results: [{ code: "medical_fee_men_zhen_shou_fei", ok: true, message: "" }],
      ok_count: 1,
      fail_count: 0,
    });
    renderCatalogs();
    await screen.findByText("门诊收费金额");
    // 勾选表头全选（measure 为 DRAFT，可批量提交审核）
    const selectAll = document.querySelector(".ant-table-selection-column input[type=checkbox]") as Element;
    fireEvent.click(selectAll);
    const batchBtn = screen.getByRole("button", { name: /批量操作/ }) as HTMLButtonElement;
    expect(batchBtn.disabled).toBe(false);
    fireEvent.click(batchBtn);
    fireEvent.click(await screen.findByText("批量提交审核（草稿）"));
    await screen.findByText(/确定批量提交审核选中的/);
    fireEvent.click(screen.getByRole("button", { name: "提交审核" }));
    await waitFor(() => {
      expect(mockedBatchSubmit).toHaveBeenCalledWith([
        {
          code: "medical_fee_men_zhen_shou_fei",
          change_reason: "批量提交逻辑度量审核",
          reviewer_id: null,
          reviewer_type: null,
          reviewer_domain: null,
        },
      ]);
    });
  });
});

describe("MeasureCatalogs 生命周期（重新启用/删除/回收站恢复）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedDictItems.mockResolvedValue(MOCK_CATEGORY_DICT);
    mockedDomains.mockResolvedValue([]);
    mockedUsers.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({
      id: 1,
      username: "admin",
      display_name: "管理员",
      role: "platform_admin",
      domain: null,
      org_id: 1,
    } as never);
  });

  it("DEPRECATED 度量显示「重新启用」和「删除」，点重新启用调用 reactivateMeasureCatalog", async () => {
    const deprecated = { ...measure, status: "DEPRECATED" as const };
    mockedList.mockResolvedValue({ items: [deprecated], total: 1, page: 1, page_size: 20 });
    mockedReactivate.mockResolvedValue({ ...deprecated, status: "DRAFT" });
    renderCatalogs();

    fireEvent.click(await screen.findByRole("button", { name: /重新启用/ }));
    // Popconfirm 确认
    fireEvent.click(await screen.findByRole("button", { name: /确 定|确定|OK/ }));

    await waitFor(() =>
      expect(mockedReactivate).toHaveBeenCalledWith("medical_fee_men_zhen_shou_fei"),
    );
    expect(await screen.findByText(/已重新启用/)).toBeInTheDocument();
  });

  it("DRAFT 度量点删除调用 deleteMeasureCatalog", async () => {
    mockedList.mockResolvedValue({ items: [measure], total: 1, page: 1, page_size: 20 });
    mockedDelete.mockResolvedValue(measure);
    renderCatalogs();

    fireEvent.click(await screen.findByRole("button", { name: /删除/ }));
    fireEvent.click(await screen.findByRole("button", { name: /确 定|确定|OK/ }));

    await waitFor(() =>
      expect(mockedDelete).toHaveBeenCalledWith("medical_fee_men_zhen_shou_fei"),
    );
    expect(await screen.findByText(/已删除/)).toBeInTheDocument();
  });

  it("回收站视图显示「恢复」按钮，点击调用 restoreMeasureCatalog", async () => {
    mockedList.mockResolvedValue({ items: [measure], total: 1, page: 1, page_size: 20 });
    mockedRestore.mockResolvedValue(measure);
    renderCatalogs();

    // 切换到回收站视图（antd Select placeholder 为文本节点，用 getByText 展开）
    fireEvent.mouseDown(screen.getByText("回收站"));
    fireEvent.click(await screen.findByTitle("回收站"));

    // 回收站视图下仅显示恢复按钮（编辑/审核等操作隐藏）
    await waitFor(() =>
      expect(mockedList).toHaveBeenCalledWith(
        expect.objectContaining({ deleted: true, status: undefined }),
      ),
    );
    fireEvent.click(await screen.findByRole("button", { name: /恢 复|恢复/ }));
    // Popconfirm 确认
    fireEvent.click(await screen.findByRole("button", { name: /确 定|确定|OK/ }));

    await waitFor(() =>
      expect(mockedRestore).toHaveBeenCalledWith("medical_fee_men_zhen_shou_fei"),
    );
    expect(await screen.findByText(/已恢复/)).toBeInTheDocument();
  });

  it("回收站视图（平台管理员）显示「彻底删除」，点击调用 purgeMeasureCatalog", async () => {
    mockedList.mockResolvedValue({ items: [measure], total: 1, page: 1, page_size: 20 });
    mockedPurge.mockResolvedValue({ measure_code: measure.measure_code });
    renderCatalogs();

    // 切换到回收站视图
    fireEvent.mouseDown(screen.getByText("回收站"));
    fireEvent.click(await screen.findByTitle("回收站"));

    // 平台管理员可见「彻底删除」按钮，点击后 Popconfirm 确认
    fireEvent.click(await screen.findByRole("button", { name: /彻底删除/ }));
    fireEvent.click(await screen.findByRole("button", { name: /确 定|确定|OK/ }));

    await waitFor(() =>
      expect(mockedPurge).toHaveBeenCalledWith("medical_fee_men_zhen_shou_fei"),
    );
    expect(await screen.findByText(/已彻底删除/)).toBeInTheDocument();
  });
});
