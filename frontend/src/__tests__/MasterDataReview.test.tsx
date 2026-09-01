import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { MasterDataReview } from "../pages/MasterDataReview";
import type { CurrentUser, Dimension, MeasureCatalog, GlossaryTerm } from "../types";

vi.mock("../api", () => {
  class UnisenseApiError extends Error {
    code: string;
    traceId: string;
    status: number;
    constructor(message: string, code: string, status: number, traceId: string) {
      super(message);
      this.name = "UnisenseApiError";
      this.code = code;
      this.status = status;
      this.traceId = traceId;
    }
    get codeZh(): string {
      return this.code;
    }
  }
  return {
    listDimensions: vi.fn(),
    listMeasureCatalogs: vi.fn(),
    listTerms: vi.fn(),
    approveDimension: vi.fn(),
    rejectDimension: vi.fn(),
    approveMeasureCatalog: vi.fn(),
    rejectMeasureCatalog: vi.fn(),
    approveTerm: vi.fn(),
    rejectTerm: vi.fn(),
    fetchCurrentUser: vi.fn(),
    listUsers: vi.fn(),
  resolveUserNames: vi.fn().mockResolvedValue([]),
    listDomainTree: vi.fn(),
    UnisenseApiError,
  };
});

import {
  listDimensions,
  listMeasureCatalogs,
  listTerms,
  approveDimension,
  rejectDimension,
  approveMeasureCatalog,
  rejectMeasureCatalog,
  fetchCurrentUser,
  listUsers,
  listDomainTree,
} from "../api";

const mockedListDims = vi.mocked(listDimensions);
const mockedListMeasures = vi.mocked(listMeasureCatalogs);
const mockedListTerms = vi.mocked(listTerms);
const mockedApproveDim = vi.mocked(approveDimension);
const mockedRejectDim = vi.mocked(rejectDimension);
const mockedApproveMeasure = vi.mocked(approveMeasureCatalog);
const mockedRejectMeasure = vi.mocked(rejectMeasureCatalog);

const reviewer: CurrentUser = {
  id: 10,
  username: "reviewer_li",
  display_name: "李评审",
  role: "reviewer",
  domain: "outpatient",
  org_id: 1,
};

function dim(code: string, extra: Record<string, unknown> = {}): Dimension {
  return {
    id: 1,
    dim_code: code,
    name: `维度${code}`,
    domain: "outpatient",
    type: "enum",
    description: null,
    owner_id: 1,
    status: "REVIEW",
    created_at: "2026-08-01T00:00:00",
    updated_at: "2026-08-01T00:00:00",
    ...extra,
  } as unknown as Dimension;
}

function measure(code: string, extra: Record<string, unknown> = {}): MeasureCatalog {
  return {
    id: 1,
    measure_code: code,
    name: `度量${code}`,
    description: null,
    measure_format: "AMOUNT",
    default_unit: "元",
    default_decimal_places: 2,
    source_system: null,
    synonyms: null,
    category: "FEE",
    stat_caliber: null,
    domain: "outpatient",
    owner_id: 1,
    status: "REVIEW",
    created_at: "2026-08-01T00:00:00",
    updated_at: "2026-08-01T00:00:00",
    ...extra,
  } as unknown as MeasureCatalog;
}

function term(code: string, extra: Record<string, unknown> = {}): GlossaryTerm {
  return {
    id: 1,
    term_code: code,
    name: `术语${code}`,
    definition: "定义",
    domain: "outpatient",
    synonyms: [],
    boundary: null,
    status: "REVIEW",
    owner_id: 1,
    created_at: "2026-08-01T00:00:00",
    updated_at: "2026-08-01T00:00:00",
    ...extra,
  } as unknown as GlossaryTerm;
}

function renderReview() {
  return render(
    <MemoryRouter initialEntries={["/master-data/review"]}>
      <MasterDataReview />
    </MemoryRouter>,
  );
}

describe("MasterDataReview 统一主数据审批工作台", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedListDims.mockResolvedValue({ items: [], total: 0 });
    mockedListMeasures.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 });
    mockedListTerms.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 });
    vi.mocked(fetchCurrentUser).mockResolvedValue(reviewer);
    vi.mocked(listUsers).mockResolvedValue([
      { id: 10, username: "reviewer_li", display_name: "李评审", role: "reviewer" },
    ] as never);
    vi.mocked(listDomainTree).mockResolvedValue([{ code: "outpatient", name: "门诊", children: [] }] as never);
  });

  it("待我审：只显示当前用户可审的行（指定用户 + 域评审组），隐藏他人指派/无权限行", async () => {
    mockedListDims.mockResolvedValue({
      items: [
        dim("dim_a", { reviewer_type: "user", reviewer_id: 10 }),
        dim("dim_b", { reviewer_type: "user", reviewer_id: 99 }),
      ],
      total: 2,
    });
    mockedListMeasures.mockResolvedValue({
      items: [measure("meas_x", { reviewer_type: "domain", reviewer_domain: "outpatient" })],
      total: 1,
      page: 1,
      page_size: 200,
    });
    mockedListTerms.mockResolvedValue({
      items: [term("term_y", { reviewer_type: null })],
      total: 1,
      page: 1,
      page_size: 200,
    });

    renderReview();
    // 指定给我的维度 + 我所在域的评审组度量 → 显示
    expect(await screen.findByText("dim_a")).toBeTruthy();
    await screen.findByText("meas_x");
    // 他人指派的维度、未指派且 reviewer 无兜底权限的术语 → 隐藏
    expect(screen.queryByText("dim_b")).toBeNull();
    expect(screen.queryByText("term_y")).toBeNull();
    // 三模块聚合：类型标签齐全（Segmented 筛选选项与表格 Tag 均有类型词，用 getAllByText 断言存在）
    expect(screen.getAllByText("维度").length).toBeGreaterThan(0);
    expect(screen.getAllByText("逻辑度量").length).toBeGreaterThan(0);
  });

  it("待我审：调三模块 status=REVIEW 拉取（不传 reviewed_by）", async () => {
    renderReview();
    await waitFor(() => expect(mockedListDims).toHaveBeenCalled());
    expect(mockedListDims).toHaveBeenCalledWith(expect.objectContaining({ status: "REVIEW" }));
    expect(mockedListMeasures).toHaveBeenCalledWith(expect.objectContaining({ status: "REVIEW" }));
    expect(mockedListTerms).toHaveBeenCalledWith(expect.objectContaining({ status: "REVIEW" }));
    expect(mockedListDims.mock.calls[0][0]).not.toHaveProperty("reviewed_by");
  });

  it("我审过的：调三模块 reviewed_by=me，展示通过/驳回处理结果", async () => {
    mockedListDims.mockResolvedValue({
      items: [dim("dim_c", { approver_id: 10, reviewed_at: "2026-08-02T10:00:00" })],
      total: 1,
    });
    mockedListMeasures.mockResolvedValue({
      items: [
        measure("meas_d", {
          reject_reviewer_id: 10,
          rejected_at: "2026-08-03T11:00:00",
          reject_reason: "口径与业务不符",
        }),
      ],
      total: 1,
      page: 1,
      page_size: 200,
    });

    renderReview();
    // 切到「我审过的」
    fireEvent.click(screen.getByText("我审过的"));
    expect(await screen.findByText("dim_c")).toBeTruthy();
    await screen.findByText("meas_d");
    // 处理结果列：已通过 / 已驳回 + 原因
    expect(screen.getByText("已通过")).toBeTruthy();
    expect(screen.getByText("已驳回")).toBeTruthy();
    expect(screen.getByText("口径与业务不符")).toBeTruthy();
    // 参数：reviewed_by=当前用户
    expect(mockedListDims).toHaveBeenCalledWith(
      expect.objectContaining({ reviewed_by: 10 }),
    );
    expect(mockedListMeasures).toHaveBeenCalledWith(
      expect.objectContaining({ reviewed_by: 10 }),
    );
  });

  it("通过操作：点击行内通过按钮 → 调用对应模块 approve api 并刷新", async () => {
    mockedListDims.mockResolvedValue({
      items: [dim("dim_a", { reviewer_type: "user", reviewer_id: 10 })],
      total: 1,
    });
    renderReview();
    await screen.findByText("dim_a");
    fireEvent.click(screen.getByLabelText("审核通过并发布"));
    await waitFor(() => expect(mockedApproveDim).toHaveBeenCalledWith("dim_a", {}));
  });

  it("驳回操作：点击行内驳回 → 弹窗填原因 → 调用对应模块 reject api", async () => {
    mockedListDims.mockResolvedValue({
      items: [dim("dim_a", { reviewer_type: "user", reviewer_id: 10 })],
      total: 1,
    });
    renderReview();
    await screen.findByText("dim_a");
    fireEvent.click(screen.getByLabelText("驳回该主数据"));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByPlaceholderText(/如：定义与业务实际不符/), {
      target: { value: "定义与业务实际不符，请修改后重提" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: /OK|确\s*定/i }));
    await waitFor(() =>
      expect(mockedRejectDim).toHaveBeenCalledWith("dim_a", {
        reason: "定义与业务实际不符，请修改后重提",
      }),
    );
  });

  it("驳回操作按类型分发：度量走 approveMeasureCatalog/rejectMeasureCatalog", async () => {
    mockedListMeasures.mockResolvedValue({
      items: [measure("meas_x", { reviewer_type: "user", reviewer_id: 10 })],
      total: 1,
      page: 1,
      page_size: 200,
    });
    renderReview();
    await screen.findByText("meas_x");
    fireEvent.click(screen.getByLabelText("审核通过并发布"));
    await waitFor(() => expect(mockedApproveMeasure).toHaveBeenCalledWith("meas_x", {}));
    fireEvent.click(screen.getByLabelText("驳回该主数据"));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByPlaceholderText(/如：定义与业务实际不符/), {
      target: { value: "请补充统计口径后重提" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: /OK|确\s*定/i }));
    await waitFor(() =>
      expect(mockedRejectMeasure).toHaveBeenCalledWith("meas_x", {
        reason: "请补充统计口径后重提",
      }),
    );
  });

  it("类型筛选：默认全部聚合，切换维度/逻辑度量/术语只显示对应类型", async () => {
    mockedListDims.mockResolvedValue({
      items: [dim("dim_a", { reviewer_type: "user", reviewer_id: 10 })],
      total: 1,
    });
    mockedListMeasures.mockResolvedValue({
      items: [measure("meas_x", { reviewer_type: "user", reviewer_id: 10 })],
      total: 1,
      page: 1,
      page_size: 200,
    });
    mockedListTerms.mockResolvedValue({
      items: [term("term_y", { reviewer_type: "user", reviewer_id: 10 })],
      total: 1,
      page: 1,
      page_size: 200,
    });

    renderReview();
    // 默认「全部」：三类聚合显示
    expect(await screen.findByText("dim_a")).toBeTruthy();
    await screen.findByText("meas_x");
    await screen.findByText("term_y");

    // 切「维度」：只显示维度（Segmented 选项用 title 定位，避免与表格 Tag 文本冲突）
    fireEvent.click(screen.getByTitle("维度"));
    expect(screen.getByText("dim_a")).toBeTruthy();
    expect(screen.queryByText("meas_x")).toBeNull();
    expect(screen.queryByText("term_y")).toBeNull();

    // 切「逻辑度量」：只显示度量
    fireEvent.click(screen.getByTitle("逻辑度量"));
    expect(screen.queryByText("dim_a")).toBeNull();
    expect(screen.getByText("meas_x")).toBeTruthy();
    expect(screen.queryByText("term_y")).toBeNull();

    // 切「术语」：只显示术语
    fireEvent.click(screen.getByTitle("术语"));
    expect(screen.queryByText("dim_a")).toBeNull();
    expect(screen.queryByText("meas_x")).toBeNull();
    expect(screen.getByText("term_y")).toBeTruthy();

    // 切回「全部」：恢复聚合
    fireEvent.click(screen.getByTitle("全部"));
    expect(screen.getByText("dim_a")).toBeTruthy();
    expect(screen.getByText("meas_x")).toBeTruthy();
    expect(screen.getByText("term_y")).toBeTruthy();
  });
});
