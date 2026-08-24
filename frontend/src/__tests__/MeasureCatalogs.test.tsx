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
  listDomainTree: vi.fn(),
  autoSuggestMeasureCatalog: vi.fn(),
  UnisenseApiError: class extends Error {},
}));

import {
  autoSuggestMeasureCatalog,
  createMeasureCatalog,
  listDomainTree,
  listMeasureCatalogs,
} from "../api";
import { PermissionProvider } from "../hooks/usePermission";

const mockedList = vi.mocked(listMeasureCatalogs);
const mockedDomains = vi.mocked(listDomainTree);
const mockedSuggest = vi.mocked(autoSuggestMeasureCatalog);
const mockedCreate = vi.mocked(createMeasureCatalog);

const measure: MeasureCatalog = {
  id: 1,
  measure_code: "medical_fee_men_zhen_shou_fei",
  name: "门诊收费金额",
  description: null,
  measure_format: "AMOUNT",
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

describe("MeasureCatalogs 度量目录 AI 推断", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedList.mockResolvedValue({ items: [measure], total: 1, page: 1, page_size: 20 });
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
  });

  it("渲染度量列表（含度量分类列）", async () => {
    renderCatalogs();
    expect(await screen.findByText("门诊收费金额")).toBeInTheDocument();
    expect(screen.getByText("费用类")).toBeInTheDocument();
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
    // 推断结果面板：AI 项（统计口径/同义词）与规则项（编码/单位）来源标注
    expect((await screen.findAllByText(/收费明细按结算日期去重后求和/)).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/AI · 置信度 70%/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/规则 · 置信度 90%/).length).toBeGreaterThanOrEqual(1);
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
});
