import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useNavigate, Routes, Route } from "react-router-dom";
import { Dimensions } from "../pages/Dimensions";
import type { Dimension } from "../types";

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
    listDimensions: vi.fn(),
    createDimension: vi.fn(),
    getDimension: vi.fn(),
    updateDimension: vi.fn(),
    publishDimension: vi.fn(),
    submitDimension: vi.fn(),
    approveDimension: vi.fn(),
    rejectDimension: vi.fn(),
    deprecateDimension: vi.fn(),
    reactivateDimension: vi.fn(),
    deleteDimension: vi.fn(),
    restoreDimension: vi.fn(),
    batchSubmitDimensions: vi.fn(),
    batchApproveDimensions: vi.fn(),
    batchRejectDimensions: vi.fn(),
    batchDeprecateDimensions: vi.fn(),
    bindMetricDimension: vi.fn(),
    listMetricDimensions: vi.fn(),
    listDimensionMappings: vi.fn(),
    createDimensionMapping: vi.fn(),
    updateDimensionMapping: vi.fn(),
    deleteDimensionMapping: vi.fn(),
    listReconciliations: vi.fn(),
    submitReconciliation: vi.fn(),
    reviewReconciliation: vi.fn(),
    listDimensionMembers: vi.fn(),
    createDimensionMember: vi.fn(),
    updateDimensionMember: vi.fn(),
    deleteDimensionMember: vi.fn(),
    listDimensionMetrics: vi.fn(),
    listMetrics: vi.fn(),
    listUsers: vi.fn(),
  resolveUserNames: vi.fn().mockResolvedValue([]),
    listDomainTree: vi.fn(),
    listFavorites: vi.fn(),
    addFavorite: vi.fn(),
    removeFavorite: vi.fn(),
    listDataSources: vi.fn(),
    previewColumnValues: vi.fn(),
    listSourceTables: vi.fn(),
    listSourceDatabases: vi.fn(),
    listSourceColumns: vi.fn(),
    bindDimensionReference: vi.fn(),
    refreshDimensionSnapshot: vi.fn(),
    listDimensionSnapshots: vi.fn(),
    getDimensionSnapshotLatestRun: vi.fn(),
    batchPublishDimensionMembers: vi.fn(),
    batchDeprecateDimensionMembers: vi.fn(),
    batchDeleteDimensionMembers: vi.fn(),
    createDimensionMappingValue: vi.fn(),
    listDimensionMappingValues: vi.fn(),
    deleteDimensionMappingValue: vi.fn(),
    getMappingCoverage: vi.fn(),
    translateDimensionValues: vi.fn(),
    fetchCurrentUser: vi.fn(),
    UnisenseApiError,
  };
});

import { listDimensions, listMetrics, getDimension, updateDimension, bindMetricDimension, listDomainTree, listDimensionMembers, updateDimensionMember, deleteDimensionMember, listDimensionMetrics, listDimensionMappings, updateDimensionMapping, listReconciliations, listUsers, listFavorites, listDataSources, previewColumnValues, listSourceTables, listSourceDatabases, listSourceColumns, fetchCurrentUser, submitDimension, approveDimension, rejectDimension, batchSubmitDimensions, batchDeprecateDimensions, reactivateDimension, deleteDimension, restoreDimension, getDimensionSnapshotLatestRun, batchPublishDimensionMembers } from "../api";

const mockedList = vi.mocked(listDimensions);
const mockedListFavorites = vi.mocked(listFavorites);
const mockedSubmitDim = vi.mocked(submitDimension);
const mockedApproveDim = vi.mocked(approveDimension);
const mockedRejectDim = vi.mocked(rejectDimension);
const mockedBatchSubmitDim = vi.mocked(batchSubmitDimensions);
const mockedBatchDeprecateDim = vi.mocked(batchDeprecateDimensions);
const mockedReactivateDim = vi.mocked(reactivateDimension);
const mockedDeleteDim = vi.mocked(deleteDimension);
const mockedRestoreDim = vi.mocked(restoreDimension);

const DIMS: Dimension[] = [
  {
    id: 1,
    dim_code: "dim_channel",
    name: "渠道",
    domain: "finance",
    type: "SCD1",
    description: "渠道维度",
    owner_id: 1,
    status: "PUBLISHED",
    created_at: "2026-08-01T00:00:00",
    updated_at: "2026-08-01T00:00:00",
  },
  {
    id: 2,
    dim_code: "dim_region",
    name: "区域",
    domain: "finance",
    type: "SCD1",
    description: "区域维度",
    owner_id: 1,
    status: "DRAFT",
    row_version: 2,
    created_at: "2026-08-01T00:00:00",
    updated_at: "2026-08-01T00:00:00",
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockedList.mockResolvedValue({ items: DIMS, total: 2 });
  mockedListFavorites.mockResolvedValue([]);
  // 维度列表 Tab 挂载即拉取指标候选（绑定指标下拉），默认返回空列表
  vi.mocked(listMetrics).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 });
  // 业务域树（新建/编辑维度业务域选项框）：finance → 财务域
  vi.mocked(listDomainTree).mockResolvedValue([
    { id: 1, code: "finance", name: "财务域", parent_id: null, level: 1, sort_order: 0, status: "ACTIVE", metric_count: 0, children: [] },
  ]);
  // 成员列表（默认成员下拉/父级选择），默认空
  vi.mocked(listDimensionMembers).mockResolvedValue({ items: [], total: 0 });
  // 用户选择器（维度 Owner 下拉），默认空
  vi.mocked(listUsers).mockResolvedValue([]);
  // 当前用户（对账复核需治理角色）：默认平台管理员，使复核按钮可用
  vi.mocked(fetchCurrentUser).mockResolvedValue({
    id: 1,
    username: "admin",
    display_name: "管理员",
    role: "platform_admin",
    domain: null,
    org_id: 1,
  });
  vi.mocked(listDataSources).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
  vi.mocked(previewColumnValues).mockResolvedValue({ values: [], total: 0, truncated: false });
  vi.mocked(listSourceTables).mockResolvedValue({ tables: [] });
  vi.mocked(listSourceDatabases).mockResolvedValue({ databases: [] });
  vi.mocked(listSourceColumns).mockResolvedValue({ columns: [] });
  // 详情抽屉/成员删除/映射编辑等新功能默认值（避免组件内 .then 到 undefined）
  vi.mocked(listDimensionMetrics).mockResolvedValue({ items: [], total: 0 });
  vi.mocked(listDimensionMappings).mockResolvedValue({ items: [], total: 0 });
  vi.mocked(listReconciliations).mockResolvedValue({ items: [], total: 0 });
});

describe("Dimensions 页面", () => {
  it("绑定指标候选：listMetrics 请求 page_size 不超过后端上限 100（避免 422）", async () => {
    render(
      <MemoryRouter initialEntries={["/dimensions"]}>
        <Dimensions />
      </MemoryRouter>,
    );

    await screen.findByText("dim_channel");
    const calls = vi.mocked(listMetrics).mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) {
      expect(c[0]?.page_size).toBeLessThanOrEqual(100);
    }
  });

  it("从全局搜索 ?kw=xxx 直达：所有查询都携带关键词过滤（避免全量首查竞态覆盖）", async () => {
    render(
      <MemoryRouter initialEntries={["/dimensions?kw=渠道"]}>
        <Dimensions />
      </MemoryRouter>,
    );

    await screen.findByText("dim_channel");
    const calls = mockedList.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) {
      expect(c[0]).toMatchObject({ keyword: "渠道" });
    }
  });

  it("从总览仪表 ?status=xxx 直达：所有查询都携带状态过滤（资产卡片下钻）", async () => {
    render(
      <MemoryRouter initialEntries={["/dimensions?status=PUBLISHED"]}>
        <Dimensions />
      </MemoryRouter>,
    );

    await screen.findByText("dim_channel");
    const calls = mockedList.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) {
      expect(c[0]).toMatchObject({ status: "PUBLISHED" });
    }
  });

  it("从总览仪表 Owner 责任分布 ?owner_id= 直达：所有查询都携带责任人过滤", async () => {
    render(
      <MemoryRouter initialEntries={["/dimensions?owner_id=2"]}>
        <Dimensions />
      </MemoryRouter>,
    );

    await screen.findByText("dim_channel");
    const calls = mockedList.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) {
      expect(c[0]).toMatchObject({ owner_id: 2 });
    }
  });

  it("URL 直达时搜索框预填关键词（?kw=）", async () => {
    render(
      <MemoryRouter initialEntries={["/dimensions?kw=渠道"]}>
        <Dimensions />
      </MemoryRouter>,
    );

    const input = await screen.findByPlaceholderText("搜索维度编码 / 名称 / 描述");
    expect((input as HTMLInputElement).value).toBe("渠道");  });

  it("防竞态：迟到的首查响应不覆盖最新筛选结果", async () => {
    type DimListResponse = { items: Dimension[]; total: number };
    let resolveFull!: (v: DimListResponse) => void;
    const fullPromise = new Promise<DimListResponse>((r) => {
      resolveFull = r;
    });
    // 首查（挂起）；随后输入关键词触发二次查询立即返回 1 条；兜底返回全量 2 条
    mockedList.mockImplementationOnce(() => fullPromise);
    mockedList.mockResolvedValueOnce({ items: [DIMS[0]], total: 1 });
    mockedList.mockResolvedValue({ items: DIMS, total: 2 });

    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );

    // 首查挂起，搜索框可用后输入关键词并回车（惰性搜索：确认才触发二次查询）
    const searchInput = await screen.findByPlaceholderText("搜索维度编码 / 名称 / 描述");
    fireEvent.change(searchInput, { target: { value: "渠道" } });
    fireEvent.keyDown(searchInput, { key: "Enter", code: "Enter" });
    await screen.findByText("dim_channel");

    // 迟到的首查此刻才返回：若被应用会覆盖筛选结果（dim_region 也会出现）
    resolveFull({ items: DIMS, total: 2 });
    // 先给 React 处理迟到响应的时间，再断言未被覆盖（避免 waitFor 在更新前假绿）
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText("dim_region")).toBeNull();
    expect(mockedList).toHaveBeenCalledWith(expect.objectContaining({ keyword: "渠道" }));
  });

  it("SPA 内 URL 关键词变化时重新按新关键词查询", async () => {
    function JumpBtn() {
      const navigate = useNavigate();
      return <button onClick={() => navigate("/dimensions?kw=区域")}>跳到区域</button>;
    }
    render(
      <MemoryRouter initialEntries={["/dimensions?kw=渠道"]}>
        <JumpBtn />
        <Dimensions />
      </MemoryRouter>,
    );

    await screen.findByText("dim_channel");
    fireEvent.click(screen.getByText("跳到区域"));
    await waitFor(() => {
      expect(mockedList).toHaveBeenCalledWith(expect.objectContaining({ keyword: "区域" }));
    });
  });

  it("编辑维度：点击编辑打开预填表单，保存调用 updateDimension 并刷新列表", async () => {
    const user = userEvent.setup();
    vi.mocked(getDimension).mockResolvedValue(DIMS[1]);
    vi.mocked(updateDimension).mockResolvedValue({ ...DIMS[1], name: "区域（新）" });
    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );

    await screen.findByText("dim_region");
    const row = screen.getByText("dim_region").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /编\s*辑/ }));

    // Modal 打开且从详情端点拉取最新值预填
    await waitFor(() => {
      expect(screen.getByText(/编辑维度：dim_region/)).toBeInTheDocument();
      expect(getDimension).toHaveBeenCalledWith("dim_region");
    });
    const nameInput = screen.getByLabelText("名称") as HTMLInputElement;
    expect(nameInput.value).toBe("区域");

    await user.clear(nameInput);
    await user.type(nameInput, "区域（新）");
    await user.click(screen.getByRole("button", { name: /保\s*存/ }));

    await waitFor(() => {
      expect(updateDimension).toHaveBeenCalledWith(
        "dim_region",
        expect.objectContaining({ name: "区域（新）", domain: "finance", type: "SCD1" }),
        2, // row_version 乐观锁透传（他人已改则后端 409）
      );
    });
    // 保存成功后重新拉取列表
    await waitFor(() => {
      expect(mockedList.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
  });

  it("绑定指标：选择指标后调用 bindMetricDimension", async () => {
    const user = userEvent.setup();
    vi.mocked(listMetrics).mockResolvedValue({
      items: [{ id: 10, metric_code: "sales_gmv_day", name: "GMV" } as any],
      total: 1,
      page: 1,
      page_size: 200,
    });
    vi.mocked(bindMetricDimension).mockResolvedValue({
      id: 1,
      metric_id: 10,
      dim_code: "dim_channel",
      role: "filter",
      default_member: null,
    });
    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );

    await screen.findByText("dim_channel");
    const row = screen.getByText("dim_channel").closest("tr") as HTMLElement;
    // 绑定指标收进「更多」下拉：展开 → 点菜单项
    fireEvent.click(within(row).getByRole("button", { name: /更\s*多/ }));
    await user.click(await screen.findByRole("menuitem", { name: /绑定指标/ }));

    await waitFor(() => {
      expect(screen.getByText(/绑定指标 → dim_channel/)).toBeInTheDocument();
    });
    // 页面可能同时存在多个 Modal（审核等），精确取绑定指标弹窗
    const dialog = (await screen.findAllByRole("dialog")).find((d) =>
      within(d).queryByText(/绑定指标 → dim_channel/),
    ) as HTMLElement;
    const metricItem = within(dialog).getByText("指标").closest(".ant-form-item") as HTMLElement;
    fireEvent.mouseDown(within(metricItem).getByRole("combobox"));
    await user.click(await screen.findByText("sales_gmv_day · GMV"));
    await user.click(within(dialog).getByRole("button", { name: /绑\s*定/ }));

    await waitFor(() => {
      expect(bindMetricDimension).toHaveBeenCalledWith(
        expect.objectContaining({ metric_id: 10, dim_code: "dim_channel", role: "FILTER" }),
      );
    });
  });

  it("成员管理：父级选择为选项框，选择父级后路径自动推测预览", async () => {
    const user = userEvent.setup();
    vi.mocked(listDimensionMembers).mockResolvedValue({
      items: [
        {
          id: 1,
          dim_code: "dim_channel",
          member_code: "online",
          member_name: "线上",
          parent_code: null,
          path: "/online",
          attributes: null,
          status: "PUBLISHED",
          created_at: "2026-08-01T00:00:00",
        },
      ],
      total: 1,
    });
    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );

    // 切到「成员管理」Tab
    await user.click(screen.getByRole("tab", { name: /维度值管理/ }));
    // 选择维度（Tab 内唯一的 Select combobox）
    const dimSelect = await screen.findByRole("combobox");
    fireEvent.mouseDown(dimSelect);
    await user.click(await screen.findByText("dim_channel · 渠道"));

    // 打开新增值，父级应为 Select（选项来自成员列表）
    await user.click(screen.getByRole("button", { name: /新增值/ }));
    const dialog = screen.getByRole("dialog");
    const parentItem = within(dialog).getByText("父级编码").closest(".ant-form-item") as HTMLElement;
    fireEvent.mouseDown(within(parentItem).getByRole("combobox"));
    await user.click(await screen.findByText("/online（线上）"));
    // 路径自动推测预览：选择 online 父级 + 尚未填 member_code → 显示 /online/{member_code}
    await waitFor(() => {
      expect(within(dialog).getByText(/层级路径将自动生成/)).toBeInTheDocument();
      expect(within(dialog).getByText("/online/{member_code}")).toBeInTheDocument();
    });
  });

  it("成员管理：编辑成员调用 updateDimensionMember 并提交父级/状态", async () => {
    const user = userEvent.setup();
    const member = {
      id: 1,
      dim_code: "dim_channel",
      member_code: "online",
      member_name: "线上",
      parent_code: null,
      path: "/online",
      attributes: null,
      status: "PUBLISHED",
      created_at: "2026-08-01T00:00:00",
    };
    vi.mocked(listDimensionMembers).mockResolvedValue({ items: [member], total: 1 });
    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("tab", { name: /维度值管理/ }));
    const dimSelect = await screen.findByRole("combobox");
    fireEvent.mouseDown(dimSelect);
    await user.click(await screen.findByText("dim_channel · 渠道"));

    await screen.findByText("online");
    const row = screen.getByText("online").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /编\s*辑/ }));
    await waitFor(() => {
      expect(screen.getByText(/编辑成员：online/)).toBeInTheDocument();
    });
    const nameInput = screen.getByLabelText("成员名称") as HTMLInputElement;
    expect(nameInput.value).toBe("线上");
    await user.clear(nameInput);
    await user.type(nameInput, "线上（新）");
    await user.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => {
      expect(updateDimensionMember).toHaveBeenCalledWith(
        "dim_channel",
        "online",
        expect.objectContaining({
          member_name: "线上（新）",
          status: "PUBLISHED",
        }),
      );
    });
  });

  it("成员管理：DRAFT 成员编辑显示编码输入框并提交新码", async () => {
    const user = userEvent.setup();
    const member = {
      id: 1,
      dim_code: "dim_channel",
      member_code: "online_typo",
      member_name: "线上",
      parent_code: null,
      path: "/online_typo",
      attributes: null,
      status: "DRAFT",
      created_at: "2026-08-01T00:00:00",
    };
    vi.mocked(listDimensionMembers).mockResolvedValue({ items: [member], total: 1 });
    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("tab", { name: /维度值管理/ }));
    const dimSelect = await screen.findByRole("combobox");
    fireEvent.mouseDown(dimSelect);
    await user.click(await screen.findByText("dim_channel · 渠道"));

    await screen.findByText("online_typo");
    const row = screen.getByText("online_typo").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /编\s*辑/ }));
    await waitFor(() => {
      expect(screen.getByText(/编辑成员：online_typo/)).toBeInTheDocument();
    });
    // DRAFT 成员显示编码输入框
    const codeInput = screen.getByLabelText("成员编码") as HTMLInputElement;
    expect(codeInput.value).toBe("online_typo");
    await user.clear(codeInput);
    await user.type(codeInput, "online");
    await user.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => {
      expect(updateDimensionMember).toHaveBeenCalledWith(
        "dim_channel",
        "online_typo",
        expect.objectContaining({
          member_code: "online",
          member_name: "线上",
          status: "DRAFT",
        }),
      );
    });
  });

  it("维度映射：源/目标维度为选项框，选项来自维度列表", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("tab", { name: /维度映射/ }));
    await user.click(await screen.findByRole("button", { name: /新建映射/ }));
    const dialog = screen.getByRole("dialog");
    const sourceItem = within(dialog).getByText("源维度").closest(".ant-form-item") as HTMLElement;
    fireEvent.mouseDown(within(sourceItem).getByRole("combobox"));
    // 维度列表已加载 → 下拉含 dim_channel · 渠道
    await user.click(await screen.findByText("dim_channel · 渠道"));
    expect(within(sourceItem).getByText("dim_channel · 渠道")).toBeInTheDocument();
  });

  it("详情抽屉：点击详情并行拉取绑定指标/成员/映射并展示", async () => {
    const user = userEvent.setup();
    vi.mocked(listDimensionMetrics).mockResolvedValue({
      items: [{ metric_id: 7, metric_code: "sales_gmv", metric_name: "成交额", role: "PARTITION", default_member: "all", metric_status: "PUBLISHED" }],
      total: 1,
    });
    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );

    await screen.findByText("dim_channel");
    await user.click(screen.getAllByRole("button", { name: /详\s*情/ })[0]);

    await waitFor(() => {
      expect(listDimensionMetrics).toHaveBeenCalledWith("dim_channel");
    });
    expect(await screen.findByText("sales_gmv")).toBeInTheDocument();
    expect(screen.getByText(/分区/)).toBeInTheDocument();
    expect(screen.getByText(/成交额/)).toBeInTheDocument();
  });

  it("成员管理：删除成员经 Popconfirm 确认后调用 deleteDimensionMember", async () => {
    const user = userEvent.setup();
    vi.mocked(listDimensions).mockResolvedValue({ items: DIMS, total: 2 });
    vi.mocked(listDimensionMembers).mockResolvedValue({
      items: [
        { id: 1, dim_code: "dim_channel", member_code: "m1", member_name: "华东", parent_code: null, path: "/m1", attributes: null, status: "PUBLISHED", created_at: "2026-01-01T00:00:00Z" },
      ],
      total: 1,
    });
    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("tab", { name: /维度值管理/ }));
    // 先选择维度（Tab 内唯一的 Select combobox），成员列表才会加载
    const dimSelect = await screen.findByRole("combobox");
    fireEvent.mouseDown(dimSelect);
    await user.click(await screen.findByText("dim_channel · 渠道"));
    await screen.findByText("华东");
    // 删除收进「更多」下拉：展开 → 点菜单项 → Modal.confirm 确认
    // （取最后一个「更多」= 成员行内，避开维度列表等其他行）
    const moreBtns = screen.getAllByRole("button", { name: /更\s*多/ });
    await user.click(moreBtns[moreBtns.length - 1]);
    await user.click(await screen.findByText("删除"));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /确\s*认|确定|OK/ }));

    await waitFor(() => {
      expect(deleteDimensionMember).toHaveBeenCalledWith("dim_channel", "m1");
    });
  });

  it("维度映射：点击编辑预填映射类型/表达式，保存调用 updateDimensionMapping", async () => {
    const user = userEvent.setup();
    vi.mocked(listDimensionMappings).mockResolvedValue({
      items: [
        { id: 9, source_dim_code: "dim_channel", target_dim_code: "dim_region", mapping_type: "EQUIVALENT", expression: "a=b", created_by: 1, created_at: "2026-01-01T00:00:00Z" },
      ],
      total: 1,
    });
    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("tab", { name: /维度映射/ }));
    await user.click(await screen.findByRole("button", { name: /编\s*辑/ }));

    // 编辑 Modal 预填当前值
    const dialog = screen.getByRole("dialog");
    await waitFor(() => {
      expect(within(dialog).getByText(/编辑维度映射：dim_channel/)).toBeInTheDocument();
    });
    await user.click(within(dialog).getByRole("button", { name: /保\s*存/ }));

    await waitFor(() => {
      expect(updateDimensionMapping).toHaveBeenCalledWith(9, expect.objectContaining({ mapping_type: "EQUIVALENT" }));
    });
  });

  it("维度映射 Tab：服务端分页——首屏 page=1 展示 total，翻页后重新请求 page=2", async () => {
    const user = userEvent.setup();
    vi.mocked(listDimensionMappings).mockResolvedValue({
      items: Array.from({ length: 25 }, (_, i) => ({
        id: i + 1,
        source_dim_code: `dim_src_${i}`,
        target_dim_code: "dim_region",
        mapping_type: "EQUIVALENT",
        expression: null,
        created_by: 1,
        created_at: "2026-01-01T00:00:00Z",
      })),
      total: 25,
    });
    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("tab", { name: /维度映射/ }));
    await screen.findByText("dim_src_0");

    // 首屏：携带 page=1 + pageSize（服务端分页），并展示 total
    const firstCalls = vi.mocked(listDimensionMappings).mock.calls;
    expect(firstCalls[firstCalls.length - 1][1]).toBe(1);
    expect(firstCalls[firstCalls.length - 1][2]).toBe(20);
    expect(screen.getByText("共 25 条")).toBeInTheDocument();

    // 翻到第 2 页：重新请求 page=2，表格展示第 2 页数据（而非前端只切已拉取数据）
    fireEvent.click(screen.getByTitle("2"));
    await screen.findByText("dim_src_20");
    await waitFor(() => {
      const calls = vi.mocked(listDimensionMappings).mock.calls;
      expect(calls[calls.length - 1][1]).toBe(2);
    });
  });

  it("对账 Tab：指标列展示 metric_code · metric_name（非 #id）", async () => {
    const user = userEvent.setup();
    vi.mocked(listReconciliations).mockResolvedValue({
      items: [
        { id: 1, metric_id: 7, metric_code: "sales_gmv", metric_name: "成交额", dim_code: "dim_channel", expected_expr: "a", actual_expr: "b", diff_summary: null, status: "PENDING", reviewed_by: null, reviewed_at: null, created_at: "2026-08-01T00:00:00" },
      ],
      total: 1,
    });
    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("tab", { name: /对账/ }));
    await screen.findByText(/sales_gmv/);
    expect(screen.getByText("sales_gmv · 成交额")).toBeInTheDocument();
    // 不应再显示裸 #id
    expect(screen.queryByText("#7")).not.toBeInTheDocument();
  });

  it("对账 Tab：服务端分页——首屏 page=1 展示 total，翻页后重新请求 page=2", async () => {
    const user = userEvent.setup();
    vi.mocked(listReconciliations).mockResolvedValue({
      items: Array.from({ length: 25 }, (_, i) => ({
        id: i + 1,
        metric_id: 7,
        metric_code: `sales_gmv_${i}`,
        metric_name: "成交额",
        dim_code: "dim_channel",
        expected_expr: "a",
        actual_expr: "b",
        diff_summary: null,
        status: "PENDING",
        reviewed_by: null,
        reviewed_at: null,
        created_at: "2026-08-01T00:00:00",
      })),
      total: 25,
    });
    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("tab", { name: /对账/ }));
    await screen.findByText(/sales_gmv_0/);

    // 首屏：携带 page=1 + pageSize（服务端分页），并展示 total
    const firstCalls = vi.mocked(listReconciliations).mock.calls;
    expect(firstCalls[firstCalls.length - 1][1]).toBe(1);
    expect(firstCalls[firstCalls.length - 1][2]).toBe(20);
    expect(screen.getByText("共 25 条")).toBeInTheDocument();

    // 翻到第 2 页：重新请求 page=2，表格展示第 2 页数据
    fireEvent.click(screen.getByTitle("2"));
    await screen.findByText(/sales_gmv_20/);
    await waitFor(() => {
      const calls = vi.mocked(listReconciliations).mock.calls;
      expect(calls[calls.length - 1][1]).toBe(2);
    });
  });

  it("对账复核权限对齐后端 _GOV_DEPS：非治理角色（metric_owner）复核按钮禁用", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchCurrentUser).mockResolvedValue({
      id: 5,
      username: "owner",
      display_name: "指标Owner",
      role: "metric_owner",
      domain: "finance",
      org_id: 1,
    });
    vi.mocked(listReconciliations).mockResolvedValue({
      items: [
        { id: 2, metric_id: 7, metric_code: "sales_gmv", metric_name: "成交额", dim_code: "dim_channel", expected_expr: "a", actual_expr: "b", diff_summary: null, status: "PENDING", reviewed_by: null, reviewed_at: null, created_at: "2026-08-01T00:00:00" },
      ],
      total: 1,
    });
    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("tab", { name: /对账/ }));
    await screen.findByText(/sales_gmv/);
    // metric_owner 无治理角色：复核按钮（通过/驳回）均应禁用
    const approveBtn = screen.getAllByRole("button", { name: /通\s*过/ })[0];
    expect((approveBtn as HTMLButtonElement).disabled).toBe(true);
    const rejectBtn = screen.getAllByRole("button", { name: /驳\s*回/ })[0];
    expect((rejectBtn as HTMLButtonElement).disabled).toBe(true);
  });

  it("提供统一的返回按钮（返回上一入口）", async () => {
    render(
      <MemoryRouter initialEntries={["/dimensions"]}>
        <Dimensions />
      </MemoryRouter>,
    );
    await screen.findByText("dim_channel");
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/lineage", "/dimensions"]}>
        <Routes>
          <Route path="/lineage" element={<div>lineage-page</div>} />
          <Route path="/dimensions" element={<Dimensions />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("dim_channel");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("lineage-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    render(
      <MemoryRouter initialEntries={["/dimensions"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/dimensions" element={<Dimensions />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("dim_channel");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("dashboard-page");
  });
});

describe("Dimensions 审核流（提交审核/通过/驳回，复用主数据审核组件）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedList.mockResolvedValue({ items: DIMS, total: 2 });
    mockedListFavorites.mockResolvedValue([]);
    vi.mocked(listMetrics).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 });
    vi.mocked(listDomainTree).mockResolvedValue([
      { id: 1, code: "finance", name: "财务域", parent_id: null, level: 1, sort_order: 0, status: "ACTIVE", metric_count: 0, children: [] },
    ]);
    vi.mocked(listDimensionMembers).mockResolvedValue({ items: [], total: 0 });
    vi.mocked(listUsers).mockResolvedValue([
      {
        id: 7,
        username: "nurse",
        display_name: "王护士",
        role: "domain_admin",
        domain: "outpatient",
        status: "active",
      },
    ]);
    vi.mocked(fetchCurrentUser).mockResolvedValue({
      id: 1, username: "admin", display_name: "管理员", role: "platform_admin", domain: "finance", org_id: 1,
    } as never);
    vi.mocked(listDataSources).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    vi.mocked(previewColumnValues).mockResolvedValue({ values: [], total: 0, truncated: false });
    vi.mocked(listDimensionMetrics).mockResolvedValue({ items: [], total: 0 });
    vi.mocked(listDimensionMappings).mockResolvedValue({ items: [], total: 0 });
    vi.mocked(listReconciliations).mockResolvedValue({ items: [], total: 0 });
  });

  it("DRAFT 维度显示「提交审核」，填写说明后调用 submitDimension（进 REVIEW）", async () => {
    mockedSubmitDim.mockResolvedValue({ ...DIMS[1], status: "REVIEW" } as never);
    render(
      <MemoryRouter initialEntries={["/dimensions"]}>
        <Dimensions />
      </MemoryRouter>,
    );
    await screen.findByText("dim_region");
    fireEvent.click(screen.getAllByRole("button", { name: /提交审核/ })[0]);
    const modal = await screen.findByRole("dialog");
    fireEvent.change(within(modal).getByLabelText("提交说明"), {
      target: { value: "区域维度定义已完善，申请发布" },
    });
    fireEvent.click(within(modal).getByRole("button", { name: /确 定|确定|OK/ }));

    await waitFor(() =>
      expect(mockedSubmitDim).toHaveBeenCalledWith("dim_region", {
        change_reason: "区域维度定义已完善，申请发布",
        reviewer_type: null,
        reviewer_id: null,
        reviewer_domain: null,
      }),
    );
    expect(await screen.findByText(/已提交审核/)).toBeInTheDocument();
  });

  it("REVIEW 维度（platform_admin 可审）审核通过并发布", async () => {
    const reviewRow = { ...DIMS[0], status: "REVIEW", submitted_by: 2 };
    mockedList.mockResolvedValue({ items: [reviewRow], total: 1 });
    mockedApproveDim.mockResolvedValue({ ...DIMS[0], status: "PUBLISHED" } as never);
    render(
      <MemoryRouter initialEntries={["/dimensions"]}>
        <Dimensions />
      </MemoryRouter>,
    );
    await screen.findByText("dim_channel");
    fireEvent.click(await screen.findByRole("button", { name: "审核通过并发布" }));
    await waitFor(() => expect(mockedApproveDim).toHaveBeenCalledWith("dim_channel", { comment: null }));
    expect(await screen.findByText(/审核通过，已发布/)).toBeInTheDocument();
  });

  it("REVIEW 维度驳回：填写原因后调用 rejectDimension，状态回 DRAFT", async () => {
    const reviewRow = { ...DIMS[0], status: "REVIEW", submitted_by: 2 };
    mockedList.mockResolvedValue({ items: [reviewRow], total: 1 });
    mockedRejectDim.mockResolvedValue({ ...DIMS[0], status: "DRAFT" } as never);
    render(
      <MemoryRouter initialEntries={["/dimensions"]}>
        <Dimensions />
      </MemoryRouter>,
    );
    await screen.findByText("dim_channel");
    fireEvent.click(await screen.findByRole("button", { name: "驳回该主数据" }));
    const modal = await screen.findByRole("dialog");
    fireEvent.change(within(modal).getByLabelText("驳回原因"), {
      target: { value: "缺少层级说明，请补充后再提交" },
    });
    fireEvent.click(within(modal).getByRole("button", { name: /确 定|确定|OK/ }));

    await waitFor(() =>
      expect(mockedRejectDim).toHaveBeenCalledWith("dim_channel", {
        reason: "缺少层级说明，请补充后再提交",
      }),
    );
    expect(await screen.findByText(/已驳回，可修改后重新提交/)).toBeInTheDocument();
  });

  it("提交审核指定「域评审组」时评审域为选项框，选择业务域后提交 reviewer_domain", async () => {
    mockedSubmitDim.mockResolvedValue({ ...DIMS[1], status: "REVIEW" } as never);
    render(
      <MemoryRouter initialEntries={["/dimensions"]}>
        <Dimensions />
      </MemoryRouter>,
    );
    await screen.findByText("dim_region");
    fireEvent.click(screen.getAllByRole("button", { name: /提交审核/ })[0]);
    const modal = await screen.findByRole("dialog");

    // 评审指派选「指定域评审组」
    fireEvent.mouseDown(within(modal).getByRole("combobox"));
    fireEvent.click(await screen.findByTitle("指定域评审组"));

    // 评审域渲染为选项框（含域下拉），而非手动输入框
    await waitFor(() => expect(within(modal).getAllByRole("combobox")).toHaveLength(2));
    expect(within(modal).queryByPlaceholderText("如 outpatient")).toBeNull();
    fireEvent.mouseDown(within(modal).getAllByRole("combobox")[1]);
    fireEvent.click(await screen.findByTitle("财务域（finance）"));

    fireEvent.change(within(modal).getByLabelText("提交说明"), {
      target: { value: "区域维度定义已完善，申请发布" },
    });
    fireEvent.click(within(modal).getByRole("button", { name: /确 定|确定|OK/ }));

    await waitFor(() =>
      expect(mockedSubmitDim).toHaveBeenCalledWith("dim_region", {
        change_reason: "区域维度定义已完善，申请发布",
        reviewer_type: "domain",
        reviewer_id: null,
        reviewer_domain: "finance",
      }),
    );
    expect(await screen.findByText(/已提交审核/)).toBeInTheDocument();
  });

  it("提交审核指定「指定用户」时评审用户为选项框，选择用户后提交 reviewer_id", async () => {
    mockedSubmitDim.mockResolvedValue({ ...DIMS[1], status: "REVIEW" } as never);
    render(
      <MemoryRouter initialEntries={["/dimensions"]}>
        <Dimensions />
      </MemoryRouter>,
    );
    await screen.findByText("dim_region");
    fireEvent.click(screen.getAllByRole("button", { name: /提交审核/ })[0]);
    const modal = await screen.findByRole("dialog");

    // 评审指派选「指定用户」
    fireEvent.mouseDown(within(modal).getByRole("combobox"));
    fireEvent.click(await screen.findByTitle("指定用户"));

    // 评审用户渲染为选项框（含用户下拉），而非手动输入框
    await waitFor(() => expect(within(modal).getAllByRole("combobox")).toHaveLength(2));
    expect(within(modal).queryByPlaceholderText("如 5")).toBeNull();
    fireEvent.mouseDown(within(modal).getAllByRole("combobox")[1]);
    fireEvent.click(await screen.findByTitle("王护士（nurse）"));

    fireEvent.change(within(modal).getByLabelText("提交说明"), {
      target: { value: "区域维度定义已完善，申请发布" },
    });
    fireEvent.click(within(modal).getByRole("button", { name: /确 定|确定|OK/ }));

    await waitFor(() =>
      expect(mockedSubmitDim).toHaveBeenCalledWith("dim_region", {
        change_reason: "区域维度定义已完善，申请发布",
        reviewer_type: "user",
        reviewer_id: 7,
        reviewer_domain: null,
      }),
    );
    expect(await screen.findByText(/已提交审核/)).toBeInTheDocument();
  });

  it("批量操作：勾选草稿维度 → 批量提交审核 → 调用 batchSubmitDimensions", async () => {
    mockedBatchSubmitDim.mockResolvedValue({
      results: [{ code: "dim_region", ok: true, message: "" }],
      ok_count: 1,
      fail_count: 0,
    });
    render(
      <MemoryRouter initialEntries={["/dimensions"]}>
        <Dimensions />
      </MemoryRouter>,
    );
    await screen.findByText("dim_channel");
    // 勾选「区域」（DRAFT 行）：[0] 表头全选、[1] 渠道、[2] 区域
    fireEvent.click(screen.getAllByRole("checkbox")[2]);
    const batchBtn = screen.getByRole("button", { name: /批量操作/ }) as HTMLButtonElement;
    expect(batchBtn.disabled).toBe(false);
    fireEvent.click(batchBtn);
    fireEvent.click(await screen.findByText("批量提交审核（草稿）"));
    await screen.findByText(/确定批量提交审核选中的/);
    fireEvent.click(screen.getByRole("button", { name: "提交审核" }));
    await waitFor(() => {
      expect(mockedBatchSubmitDim).toHaveBeenCalledWith([
        {
          code: "dim_region",
          change_reason: "批量提交维度审核",
          reviewer_id: null,
          reviewer_type: null,
          reviewer_domain: null,
        },
      ]);
    });
  });

  it("批量废弃：勾选已发布维度 → 确认弹窗 → 调用 batchDeprecateDimensions", async () => {
    mockedBatchDeprecateDim.mockResolvedValue({
      results: [{ code: "dim_channel", ok: true, message: "" }],
      ok_count: 1,
      fail_count: 0,
    });
    render(
      <MemoryRouter initialEntries={["/dimensions"]}>
        <Dimensions />
      </MemoryRouter>,
    );
    await screen.findByText("dim_channel");
    // 勾选「渠道」（PUBLISHED 行）
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    const batchBtn = screen.getByRole("button", { name: /批量操作/ }) as HTMLButtonElement;
    fireEvent.click(batchBtn);
    fireEvent.click(await screen.findByText("批量废弃（已发布）"));
    await screen.findByText(/确定批量废弃选中的/);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "废 弃" }));
    await waitFor(() => {
      expect(mockedBatchDeprecateDim).toHaveBeenCalledWith(["dim_channel"]);
    });
  });
});

describe("Dimensions 生命周期（重新启用/删除/回收站恢复）", () => {
  const deprecatedDim: Dimension = {
    ...DIMS[0],
    dim_code: "dim_old",
    name: "旧维度",
    status: "DEPRECATED",
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mockedListFavorites.mockResolvedValue([]);
    vi.mocked(listMetrics).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 });
    vi.mocked(listDomainTree).mockResolvedValue([
      { id: 1, code: "finance", name: "财务域", parent_id: null, level: 1, sort_order: 0, status: "ACTIVE", metric_count: 0, children: [] },
    ]);
    vi.mocked(listDimensionMembers).mockResolvedValue({ items: [], total: 0 });
    vi.mocked(listUsers).mockResolvedValue([]);
    vi.mocked(fetchCurrentUser).mockResolvedValue({
      id: 1,
      username: "admin",
      display_name: "管理员",
      role: "platform_admin",
      domain: null,
      org_id: 1,
    } as never);
  });

  it("DEPRECATED 维度显示「重新启用」与「删除」，点重新启用调用 reactivateDimension", async () => {
    mockedList.mockResolvedValue({ items: [deprecatedDim], total: 1 });
    mockedReactivateDim.mockResolvedValue({ ...deprecatedDim, status: "DRAFT" });
    render(
      <MemoryRouter initialEntries={["/dimensions"]}>
        <Dimensions />
      </MemoryRouter>,
    );
    await screen.findByText("旧维度");
    // 重新启用收进「更多」下拉：展开 → 点菜单项 → Modal.confirm 确认
    fireEvent.click(screen.getByRole("button", { name: /更\s*多/ }));
    fireEvent.click(await screen.findByText("重新启用"));
    fireEvent.click(await screen.findByRole("button", { name: /确\s*认|确定|OK/ }));
    await waitFor(() => expect(mockedReactivateDim).toHaveBeenCalledWith("dim_old"));
    expect(await screen.findByText(/已重新启用/)).toBeInTheDocument();
  });

  it("DRAFT 维度点删除调用 deleteDimension", async () => {
    mockedList.mockResolvedValue({ items: [DIMS[1]], total: 1 });
    mockedDeleteDim.mockResolvedValue(DIMS[1]);
    render(
      <MemoryRouter initialEntries={["/dimensions"]}>
        <Dimensions />
      </MemoryRouter>,
    );
    await screen.findByText("区域");
    // 删除收进「更多」下拉：展开 → 点菜单项 → Modal.confirm 确认
    fireEvent.click(screen.getByRole("button", { name: /更\s*多/ }));
    fireEvent.click(await screen.findByText("删除"));
    fireEvent.click(await screen.findByRole("button", { name: /确\s*认|确定|OK/ }));
    await waitFor(() => expect(mockedDeleteDim).toHaveBeenCalledWith("dim_region"));
    expect(await screen.findByText(/已删除/)).toBeInTheDocument();
  });

  it("回收站视图显示「恢复」按钮，点击调用 restoreDimension", async () => {
    mockedList.mockResolvedValue({ items: [DIMS[1]], total: 1 });
    mockedRestoreDim.mockResolvedValue(DIMS[1]);
    render(
      <MemoryRouter initialEntries={["/dimensions"]}>
        <Dimensions />
      </MemoryRouter>,
    );
    await screen.findByText("区域");
    // 切换到回收站视图（antd Select placeholder 为文本节点，用 getByText 展开）
    fireEvent.mouseDown(screen.getByText("回收站"));
    fireEvent.click(await screen.findByTitle("回收站"));
    await waitFor(() =>
      expect(mockedList).toHaveBeenCalledWith(expect.objectContaining({ deleted: true })),
    );
    fireEvent.click(await screen.findByRole("button", { name: /恢 复|恢复/ }));
    fireEvent.click(await screen.findByRole("button", { name: /确 定|确定|OK/ }));
    await waitFor(() => expect(mockedRestoreDim).toHaveBeenCalledWith("dim_region"));
    expect(await screen.findByText(/已恢复/)).toBeInTheDocument();
  });
});

describe("Dimensions 引用型维度（SNAPSHOT）与成员批量操作", () => {
  beforeEach(() => {
    vi.mocked(listDimensions).mockResolvedValue({
      items: [
        {
          id: 1,
          dim_code: "dim_customer",
          name: "客户",
          domain: "sales",
          type: "SCD2",
          description: null,
          owner_id: 1,
          status: "PUBLISHED",
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
          sync_mode: "snapshot",
          source_id: "s1",
          source_table: "dwd.dim_customer",
          source_column: "customer_id",
          refresh_interval_hours: 24,
          last_snapshot_at: "2026-01-01T00:00:00Z",
        },
      ],
      total: 1,
    } as never);
    vi.mocked(listDimensionMembers).mockResolvedValue({ items: [], total: 0 } as never);
    vi.mocked(listDataSources).mockResolvedValue({ items: [], total: 0 } as never);
    vi.mocked(getDimensionSnapshotLatestRun).mockResolvedValue({
      id: 1,
      dim_code: "dim_customer",
      snapshot_at: "2026-01-01T00:00:00Z",
      status: "SUCCESS",
      total_count: 100,
      added_count: 3,
      removed_count: 1,
      null_count: 5,
      null_rate: 0.05,
      added_sample: ["c1", "c2", "c3"],
      removed_sample: ["old"],
      error_msg: null,
      duration_ms: 1200,
      created_at: "2026-01-01T00:00:00Z",
    } as never);
  });

  it("引用型维度：选择后展示绑定来源与快照数据质量摘要（值总数/新增/消失/空值率）", async () => {
    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );
    await userEvent.click(await screen.findByRole("tab", { name: /维度值管理/ }));
    const dimSelect = await screen.findByRole("combobox");
    fireEvent.mouseDown(dimSelect);
    await userEvent.click(await screen.findByText("dim_customer · 客户"));

    // 引用型面板：绑定来源 + 刷新快照按钮 + 质量摘要
    expect(await screen.findByText(/dwd\.dim_customer\.customer_id/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /刷新快照/ })).toBeInTheDocument();
    expect(screen.getByText(/值总数/)).toBeInTheDocument();
    expect(screen.getByText("100")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    // 消失样本 + 空值率 5.00%
    expect(screen.getByText("old")).toBeInTheDocument();
    expect(screen.getByText("5.00%")).toBeInTheDocument();
  });

  it("批量发布：勾选成员后调用 batchPublishDimensionMembers", async () => {
    vi.mocked(listDimensionMembers).mockResolvedValue({
      items: [
        { id: 1, dim_code: "dim_customer", member_code: "c1", member_name: "客户一", parent_code: null, path: null, attributes: null, status: "DRAFT", created_at: "2026-01-01T00:00:00Z" },
        { id: 2, dim_code: "dim_customer", member_code: "c2", member_name: "客户二", parent_code: null, path: null, attributes: null, status: "DRAFT", created_at: "2026-01-01T00:00:00Z" },
      ],
      total: 2,
    } as never);
    vi.mocked(batchPublishDimensionMembers).mockResolvedValue({
      published: 2,
      skipped: 0,
      failed: [],
    } as never);

    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );
    await userEvent.click(await screen.findByRole("tab", { name: /维度值管理/ }));
    const dimSelect = await screen.findByRole("combobox");
    fireEvent.mouseDown(dimSelect);
    await userEvent.click(await screen.findByText("dim_customer · 客户"));
    await screen.findByText("客户一");

    // 勾选两行（antd rowSelection：首个 checkbox 为表头全选，点击即全选 c1/c2）
    const checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[0]);
    await waitFor(() => expect(screen.getByRole("button", { name: /批量发布/ })).toBeEnabled());

    await userEvent.click(screen.getByRole("button", { name: /批量发布/ }));
    await waitFor(() =>
      expect(batchPublishDimensionMembers).toHaveBeenCalledWith("dim_customer", ["c1", "c2"]),
    );
    expect(await screen.findByText(/已发布 2 个/)).toBeInTheDocument();
  });

  it("从表自动获取：选数据源后表为选项框、选表后列为选项框（源库元数据列举）", async () => {
    vi.mocked(listDataSources).mockResolvedValue({
      items: [{ source_id: "s1", name: "MySQL", source_type: "mysql" }],
      total: 1,
      page: 1,
      page_size: 100,
    } as never);
    vi.mocked(listSourceTables).mockResolvedValue({
      tables: [
        { database: "dwd", table: "dim_customer", name: "dwd.dim_customer" },
        { database: "dwd", table: "orders", name: "dwd.orders" },
      ],
    } as never);
    vi.mocked(listSourceColumns).mockResolvedValue({
      columns: [
        { name: "customer_id", data_type: "bigint", comment: null },
        { name: "customer_name", data_type: "varchar", comment: null },
      ],
    } as never);

    vi.mocked(listSourceDatabases).mockResolvedValue({ databases: ["dwd"] } as never);
    vi.mocked(listSourceTables).mockResolvedValue({
      tables: [
        { database: "dwd", table: "orders", name: "dwd.orders" },
        { database: "dwd", table: "dim_customer", name: "dwd.dim_customer" },
      ],
    } as never);
    vi.mocked(listSourceColumns).mockResolvedValue({
      columns: [
        { name: "customer_id", data_type: "bigint", comment: null },
        { name: "customer_name", data_type: "varchar", comment: null },
      ],
    } as never);

    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );
    await userEvent.click(await screen.findByRole("tab", { name: /维度值管理/ }));
    const dimSelect = await screen.findByRole("combobox");
    fireEvent.mouseDown(dimSelect);
    await userEvent.click(await screen.findByText("dim_customer · 客户"));
    await userEvent.click(await screen.findByRole("button", { name: /从表自动获取/ }));

    // 弹窗内：数据源 → 目标库（轻量）→ 表选项框（选库后仅枚举该库表）
    const dialog = await screen.findByRole("dialog");
    let combos = within(dialog).getAllByRole("combobox");
    fireEvent.mouseDown(combos[0]);
    await userEvent.click(await screen.findByText("MySQL（s1）"));
    await waitFor(() => expect(listSourceDatabases).toHaveBeenCalledWith("s1"));
    // 选目标库 dwd → 仅枚举该库表（级联，不再全量 26s）
    fireEvent.mouseDown(within(dialog).getAllByRole("combobox")[1]);
    await userEvent.click(await screen.findByTitle("dwd"));
    await waitFor(() => expect(listSourceTables).toHaveBeenCalledWith("s1", ["dwd"]));
    // 表名下拉（combos[2]）：dwd.dim_customer 出现在选项中
    fireEvent.mouseDown(within(dialog).getAllByRole("combobox")[2]);
    // 点击 .ant-select-item-option 本体（title=选项文本）才能触发选中（antd 虚拟列表多副本）
    await waitFor(() => {
      const dropdown = document.querySelector(
        ".ant-select-dropdown:not(.ant-select-dropdown-hidden)",
      ) as HTMLElement | null;
      const option = dropdown?.querySelector(
        '.ant-select-item-option[title="dwd.dim_customer"]',
      ) as HTMLElement | null;
      expect(option).toBeTruthy();
      if (option) fireEvent.click(option);
    });

    // 选表后 → 列选项框（经 listSourceColumns 列举，显示 列名 (类型)）
    await waitFor(() =>
      expect(listSourceColumns).toHaveBeenCalledWith("s1", "dwd.dim_customer"),
    );
    combos = within(dialog).getAllByRole("combobox");
    fireEvent.mouseDown(combos[3]);
    await waitFor(() => {
      const dropdown = document.querySelector(
        ".ant-select-dropdown:not(.ant-select-dropdown-hidden)",
      ) as HTMLElement | null;
      expect(
        dropdown?.querySelector('.ant-select-item-option[title="customer_id (bigint)"]'),
      ).toBeTruthy();
      expect(
        dropdown?.querySelector('.ant-select-item-option[title="customer_name (varchar)"]'),
      ).toBeTruthy();
    });
  });

  it("绑定引用型：打开预填已绑定的源表列并预加载表/列选项", async () => {
    vi.mocked(listDataSources).mockResolvedValue({
      items: [{ source_id: "s1", name: "MySQL", source_type: "mysql" }],
      total: 1,
      page: 1,
      page_size: 100,
    } as never);
    vi.mocked(listSourceTables).mockResolvedValue({
      tables: [{ database: "dwd", table: "dim_customer", name: "dwd.dim_customer" }],
    } as never);
    vi.mocked(listSourceColumns).mockResolvedValue({
      columns: [{ name: "customer_id", data_type: "bigint", comment: null }],
    } as never);

    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );
    await userEvent.click(await screen.findByRole("tab", { name: /维度值管理/ }));
    const dimSelect = await screen.findByRole("combobox");
    fireEvent.mouseDown(dimSelect);
    await userEvent.click(await screen.findByText("dim_customer · 客户"));
    await userEvent.click(await screen.findByRole("button", { name: /重新绑定表列/ }));

    // 打开即预加载：库列表 + 按绑定源表拆库仅枚举该库表 + 预加载列
    await waitFor(() => expect(listSourceDatabases).toHaveBeenCalledWith("s1"));
    await waitFor(() => expect(listSourceTables).toHaveBeenCalledWith("s1", ["dwd"]));
    await waitFor(() =>
      expect(listSourceColumns).toHaveBeenCalledWith("s1", "dwd.dim_customer"),
    );
    expect(await screen.findByText("绑定引用型值来源 → dim_customer")).toBeInTheDocument();
  });
});
