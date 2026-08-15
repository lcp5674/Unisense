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
    deprecateDimension: vi.fn(),
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
    listDomainTree: vi.fn(),
    listFavorites: vi.fn(),
    addFavorite: vi.fn(),
    removeFavorite: vi.fn(),
    UnisenseApiError,
  };
});

import { listDimensions, listMetrics, getDimension, updateDimension, bindMetricDimension, listDomainTree, listDimensionMembers, updateDimensionMember, deleteDimensionMember, listDimensionMetrics, listDimensionMappings, updateDimensionMapping, listReconciliations, listUsers, listFavorites } from "../api";

const mockedList = vi.mocked(listDimensions);
const mockedListFavorites = vi.mocked(listFavorites);

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
  // 详情抽屉/成员删除/映射编辑等新功能默认值（避免组件内 .then 到 undefined）
  vi.mocked(listDimensionMetrics).mockResolvedValue({ items: [], total: 0 });
  vi.mocked(listDimensionMappings).mockResolvedValue({ items: [], total: 0 });
  vi.mocked(listReconciliations).mockResolvedValue({ items: [], total: 0 });
});

describe("Dimensions 页面", () => {
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

    // 首查挂起，搜索框可用后输入关键词触发二次查询
    const searchInput = await screen.findByPlaceholderText("搜索维度编码 / 名称 / 描述");
    fireEvent.change(searchInput, { target: { value: "渠道" } });
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
    await user.click(within(row).getByRole("button", { name: "绑定指标" }));

    await waitFor(() => {
      expect(screen.getByText(/绑定指标 → dim_channel/)).toBeInTheDocument();
    });
    const dialog = screen.getByRole("dialog");
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
    await user.click(screen.getByRole("tab", { name: /成员管理/ }));
    // 选择维度（Tab 内唯一的 Select combobox）
    const dimSelect = await screen.findByRole("combobox");
    fireEvent.mouseDown(dimSelect);
    await user.click(await screen.findByText("dim_channel · 渠道"));

    // 打开新增成员，父级应为 Select（选项来自成员列表）
    await user.click(screen.getByRole("button", { name: /新增成员/ }));
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
    await user.click(screen.getByRole("tab", { name: /成员管理/ }));
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
        expect.objectContaining({
          dim_code: "dim_channel",
          member_code: "online",
          member_name: "线上（新）",
          status: "PUBLISHED",
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
    expect(screen.getByText(/PARTITION 分区/)).toBeInTheDocument();
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

    await user.click(screen.getByRole("tab", { name: /成员管理/ }));
    // 先选择维度（Tab 内唯一的 Select combobox），成员列表才会加载
    const dimSelect = await screen.findByRole("combobox");
    fireEvent.mouseDown(dimSelect);
    await user.click(await screen.findByText("dim_channel · 渠道"));
    await screen.findByText("华东");
    // Popconfirm 为 click 触发：点触发按钮 → 浮层出现 → 点「删除」确认
    await user.click(screen.getAllByRole("button", { name: /删\s*除/ })[0]);
    const desc = await screen.findByText(/级联删除整个子树/);
    const popconfirm = desc.closest(".ant-popover") as HTMLElement;
    await user.click(within(popconfirm).getByRole("button", { name: /删\s*除/ }));

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
