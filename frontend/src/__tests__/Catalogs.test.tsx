import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { Catalogs } from "../pages/Catalogs";
import type { DBCatalog, DataSource } from "../types";

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
    listCatalogs: vi.fn(),
    registerCatalog: vi.fn(),
    bulkDeprecateCatalogs: vi.fn(),
    listDataSources: vi.fn(),
    listCatalogDatabases: vi.fn(),
    refreshCatalogEntity: vi.fn(),
    inferColumnDescription: vi.fn(),
    inferDescriptions: vi.fn(),
    updateColumnDescription: vi.fn(),
    fetchDescriptionCoverage: vi.fn(),
    listFavorites: vi.fn(),
    addFavorite: vi.fn(),
    removeFavorite: vi.fn(),
    UnisenseApiError,
  };
});

import { listCatalogs, registerCatalog, listDataSources, listCatalogDatabases, refreshCatalogEntity, fetchDescriptionCoverage, inferDescriptions, listFavorites } from "../api";

const mockedList = vi.mocked(listCatalogs);
const mockedRegister = vi.mocked(registerCatalog);
const mockedSources = vi.mocked(listDataSources);
const mockedDatabases = vi.mocked(listCatalogDatabases);
const mockedRefresh = vi.mocked(refreshCatalogEntity);
const mockedListFavorites = vi.mocked(listFavorites);

const SOURCES: DataSource[] = [
  {
    source_id: "mysql_unisense",
    name: "Unisense MySQL",
    source_type: "mysql",
    domain: "sales",
    enabled: true,
    cluster_id: null,
    coverage: 0.9,
    health_status: "healthy",
    connection_config_present: true,
    schedule_cron: null,
    collection_mode: "FULL",
    created_by: 1,
    created_at: "2026-08-13T00:00:00",
    updated_at: "2026-08-13T00:00:00",
  },
  {
    source_id: "hive_ods",
    name: "ODS Hive",
    source_type: "hive",
    domain: "finance",
    enabled: true,
    cluster_id: null,
    coverage: 0.5,
    health_status: "unknown",
    connection_config_present: true,
    schedule_cron: null,
    collection_mode: "FULL",
    created_by: 1,
    created_at: "2026-08-13T00:00:00",
    updated_at: "2026-08-13T00:00:00",
  },
];

const CATALOGS: DBCatalog[] = [
  {
    id: 1,
    source_id: "mysql_unisense",
    entity_name: "dwd_finance_order",
    entity_type: "TABLE",
    schema_def: {},
    etl_sql: null,
    sensitivity_level: "INTERNAL",
    owner_id: null,
    upstream_signature: "sig1",
    content_signature: null,
    schema_incomplete: false,
    domain: "sales",
    owner_name: "Alice",
    description: "订单事实表",
    updated_at: "2026-08-15T10:00:00",
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockedList.mockResolvedValue({ items: CATALOGS, total: 1, page: 1, page_size: 20 });
  mockedSources.mockResolvedValue({ items: SOURCES, total: 2, page: 1, page_size: 200 });
  mockedDatabases.mockResolvedValue(["unisense", "sales"]);
  mockedListFavorites.mockResolvedValue([]);
  mockedRegister.mockResolvedValue({ ...CATALOGS[0], sensitivity_level: "INTERNAL" } as DBCatalog);
  vi.mocked(fetchDescriptionCoverage).mockResolvedValue({
    total_tables: 10,
    tables_with_desc: 3,
    tables_missing_desc: 7,
    total_fields: 40,
    fields_with_desc: 16,
    fields_missing_desc: 24,
    per_table: [],
  });
});

describe("Catalogs 页面", () => {
  it("登记实体弹窗用数据源下拉代替手填 source_id，提交时 source_id 由选择自动填充", async () => {
    render(
      <MemoryRouter>
        <Catalogs />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getAllByText("登记实体")[0]);

    // 打开数据源下拉并选择（选项渲染在 portal 中，需先展开）
    fireEvent.mouseDown(screen.getByText("选择数据源（source_id 自动填充）"));
    fireEvent.click(await screen.findByText("Unisense MySQL（mysql_unisense）"));

    // 填写实体名并提交
    fireEvent.change(screen.getByLabelText("实体名"), { target: { value: "dwd_finance_order" } });
    fireEvent.click(screen.getByRole("button", { name: /登\s*记(?!实体)/ }));

    await waitFor(() => {
      expect(mockedRegister).toHaveBeenCalledWith("mysql_unisense", {
        entity_name: "dwd_finance_order",
        entity_type: "TABLE",
        schema_def: {},
        etl_sql: null,
      });
    });
  });

  it("未选择数据源时提交被表单校验拦截（必填提示）", async () => {
    render(
      <MemoryRouter>
        <Catalogs />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getAllByText("登记实体")[0]);
    fireEvent.click(screen.getByRole("button", { name: /登\s*记(?!实体)/ }));

    await waitFor(() => {
      expect(screen.getByText("请选择归属的数据源")).toBeTruthy();
    });
    expect(mockedRegister).not.toHaveBeenCalled();
  });

  it("列表展示采集目录实体（source_id 作为数据源列）", async () => {
    render(
      <MemoryRouter>
        <Catalogs />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText("dwd_finance_order")).toBeTruthy();
      expect(screen.getByText("mysql_unisense")).toBeTruthy();
    });
  });

  it("库名下拉随数据源联动，选择库名后按 database 过滤请求", async () => {
    render(
      <MemoryRouter>
        <Catalogs />
      </MemoryRouter>,
    );

    // 库名选项来自后端（含真实库名）
    await waitFor(() => {
      expect(mockedDatabases).toHaveBeenCalled();
    });
    // 选择库名 "unisense" → 触发带 database 参数的列表请求
    fireEvent.mouseDown(screen.getByText("全部库名"));
    const options = await screen.findAllByText("unisense");
    fireEvent.click(options[options.length - 1]);
    await waitFor(() => {
      const calls = mockedList.mock.calls;
      const lastCall = calls.length > 0 ? calls[calls.length - 1][0] : undefined;
      expect(lastCall?.database).toBe("unisense");
    });
  });

  it("从数据源详情 ?source_id=xxx 直达：所有查询都携带 source_id 过滤（避免全量首查竞态覆盖）", async () => {
    render(
      <MemoryRouter initialEntries={["/catalogs?source_id=mysql_unisense"]}>
        <Catalogs />
      </MemoryRouter>,
    );

    await screen.findByText("共 1 条");
    const calls = mockedList.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    // 任何一次查询都不得丢失 URL 带来的 source_id 过滤
    for (const c of calls) {
      expect(c[0]).toMatchObject({ source_id: "mysql_unisense" });
    }
  });

  it("从全局搜索 ?kw=xxx 直达：所有查询都携带关键词过滤", async () => {
    render(
      <MemoryRouter initialEntries={["/catalogs?kw=order"]}>
        <Catalogs />
      </MemoryRouter>,
    );

    await screen.findByText("共 1 条");
    const calls = mockedList.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) {
      expect(c[0]).toMatchObject({ keyword: "order" });
    }
  });

  it("从总览仪表 ?sensitivity=xxx 直达：所有查询都携带敏感级别过滤（资产卡片下钻）", async () => {
    render(
      <MemoryRouter initialEntries={["/catalogs?sensitivity=PII"]}>
        <Catalogs />
      </MemoryRouter>,
    );

    await screen.findByText("共 1 条");
    const calls = mockedList.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) {
      expect(c[0]).toMatchObject({ sensitivity_level: "PII" });
    }
  });

  it("防竞态：迟到的首查响应不覆盖最新筛选结果", async () => {
    type CatalogListResponse = { items: DBCatalog[]; total: number; page: number; page_size: number };
    let resolveFull!: (v: CatalogListResponse) => void;
    const fullPromise = new Promise<CatalogListResponse>((r) => {
      resolveFull = r;
    });
    // 首查（挂起）；随后切换源状态触发二次查询立即返回 2；兜底返回 8
    mockedList.mockImplementationOnce(() => fullPromise);
    mockedList.mockResolvedValueOnce({ items: CATALOGS, total: 2, page: 1, page_size: 20 });
    mockedList.mockResolvedValue({ items: [], total: 8, page: 1, page_size: 20 });

    render(
      <MemoryRouter initialEntries={["/catalogs?source_id=mysql_unisense"]}>
        <Catalogs />
      </MemoryRouter>,
    );

    await screen.findByText("全部源状态");
    fireEvent.mouseDown(screen.getByText("全部源状态"));
    const deletedOption = await screen.findByText("已删除源");
    fireEvent.click(deletedOption);

    await screen.findByText("共 2 条");

    // 迟到的首查此刻才返回：若被应用会覆盖筛选结果（total 变 8）
    resolveFull({ items: [], total: 8, page: 1, page_size: 20 });
    await screen.findByText("共 2 条");
    expect(mockedList).toHaveBeenCalledWith(expect.objectContaining({ source_status: "deleted" }));
  });

  it("切换每页条数后按新 page_size 重新请求（不固化为 20 条/页）", async () => {
    const { container } = render(
      <MemoryRouter>
        <Catalogs />
      </MemoryRouter>,
    );

    // 初始请求固定 page_size=20
    await waitFor(() => {
      expect(mockedList).toHaveBeenCalledWith(expect.objectContaining({ page_size: 20 }));
    });

    // 打开每页条数选择器（antd size changer 显式开启，total=1 也可见）
    const sizeChanger = container.querySelector(".ant-pagination-options .ant-select-selector");
    expect(sizeChanger).toBeTruthy();
    fireEvent.mouseDown(sizeChanger!);

    // 选择「50」（测试环境无 ConfigProvider，选项文本为 antd 默认 en_US 的 "50 / page"）
    const option = await screen.findByRole("option", { name: /50/ });
    fireEvent.click(option);

    // 重新请求携带新的 page_size
    await waitFor(() => {
      const calls = mockedList.mock.calls;
      const lastCall = calls.length > 0 ? calls[calls.length - 1][0] : undefined;
      expect(lastCall?.page_size).toBe(50);
    });
  });

  it("字段详情抽屉兼容 schema_json 字段名（历史/旧接口返回），正确展示字段", async () => {
    const legacyCatalog = {
      ...CATALOGS[0],
      schema_def: undefined as unknown as Record<string, unknown>,
      schema_json: {
        columns: [
          { name: "order_id", type: "bigint", comment: "订单ID", nullable: true },
          { name: "amount", type: "decimal", comment: "", nullable: true },
        ],
      },
    } as unknown as DBCatalog;
    mockedList.mockResolvedValueOnce({ items: [legacyCatalog], total: 1, page: 1, page_size: 20 });
    mockedList.mockResolvedValue({ items: [legacyCatalog], total: 1, page: 1, page_size: 20 });

    render(
      <MemoryRouter>
        <Catalogs />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByText("字段详情"));
    await waitFor(() => {
      expect(screen.getByText("order_id")).toBeTruthy();
      expect(screen.getByText("amount")).toBeTruthy();
      // 空态提示不应出现（字段已解析出来）
      expect(screen.queryByText(/暂无字段信息/)).toBeNull();
    });
  });

  it("采集该表按钮触发单实体刷新并回填最新字段", async () => {
    // 首次列表：实体无字段（schema 空）→ 抽屉显示空态
    const emptySchema = { ...CATALOGS[0], schema_def: {} };
    mockedList.mockResolvedValueOnce({ items: [emptySchema], total: 1, page: 1, page_size: 20 });
    // 刷新后重拉：实体带字段
    const withCols = {
      ...CATALOGS[0],
      schema_def: {
        columns: [
          { name: "order_id", type: "bigint", nullable: true },
          { name: "amount", type: "decimal", nullable: true },
        ],
      },
    } as DBCatalog;
    mockedList.mockResolvedValue({ items: [withCols], total: 1, page: 1, page_size: 20 });
    mockedRefresh.mockResolvedValue({
      source_id: "mysql_unisense",
      entity_name: "dwd_finance_order",
      sensitivity_level: "INTERNAL",
      drifted: false,
      columns: 2,
    });

    render(
      <MemoryRouter>
        <Catalogs />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByText("字段详情"));
    expect(screen.getByText(/暂无字段信息/)).toBeTruthy();

    fireEvent.click(screen.getByText("采集该表"));
    await waitFor(() => {
      expect(mockedRefresh).toHaveBeenCalledWith("mysql_unisense", "dwd_finance_order");
    });
    // 抽屉回填最新字段
    await waitFor(() => {
      expect(screen.getByText("order_id")).toBeTruthy();
    });
    expect(screen.queryByText(/暂无字段信息/)).toBeNull();
  });

  it("头部展示描述缺失统计卡（字段覆盖率/缺失字段/缺表描述/表总数）", async () => {
    render(
      <MemoryRouter>
        <Catalogs />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText("字段描述覆盖率")).toBeTruthy();
      expect(screen.getByText("缺失字段数")).toBeTruthy();
      expect(screen.getByText("缺表描述")).toBeTruthy();
      expect(screen.getByText("目录表总数")).toBeTruthy();
    });
    // 覆盖率 40 字段有 16 描述 → 副标题展示 16 / 40 字段有描述
    expect(screen.getByText("16 / 40 字段有描述")).toBeTruthy();
    expect(screen.getByText("3 / 10 表已补全")).toBeTruthy();
    expect(fetchDescriptionCoverage).toHaveBeenCalled();
  });

  it("提供统一的返回按钮（返回上一入口）", async () => {
    render(
      <MemoryRouter initialEntries={["/catalogs"]}>
        <Catalogs />
      </MemoryRouter>,
    );
    await screen.findByText("dwd_finance_order");
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/lineage", "/catalogs"]}>
        <Routes>
          <Route path="/lineage" element={<div>lineage-page</div>} />
          <Route path="/catalogs" element={<Catalogs />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("dwd_finance_order");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("lineage-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    render(
      <MemoryRouter initialEntries={["/catalogs"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/catalogs" element={<Catalogs />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("dwd_finance_order");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("dashboard-page");
  });

  it("批量推断进行中退出再进：模块级 Map 拦截，不重发请求", async () => {
    const withCols = {
      ...CATALOGS[0],
      schema_def: {
        columns: [
          { name: "order_id", type: "bigint", comment: "" },
          { name: "amount", type: "decimal", comment: "" },
        ],
      },
    } as DBCatalog;
    mockedList.mockResolvedValue({ items: [withCols], total: 1, page: 1, page_size: 20 });

    type InferBatchResult = Awaited<ReturnType<typeof inferDescriptions>>;
    let resolveInfer!: (v: InferBatchResult) => void;
    const pending = new Promise<InferBatchResult>((r) => {
      resolveInfer = r;
    });
    vi.mocked(inferDescriptions).mockReturnValue(pending);

    const first = render(
      <MemoryRouter>
        <Catalogs />
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByText("字段详情"));
    fireEvent.click(screen.getByRole("button", { name: /批量推断缺失描述/ }));
    await waitFor(() => expect(inferDescriptions).toHaveBeenCalledTimes(1));

    // 模拟退出页面：卸载组件（模块级 inferInflight 不随组件卸载重置）
    first.unmount();

    // 重新进入：再次触发批量推断，应被模块级 Map 拦截，不重发请求
    render(
      <MemoryRouter>
        <Catalogs />
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByText("字段详情"));
    fireEvent.click(screen.getByRole("button", { name: /批量推断缺失描述/ }));
    expect(inferDescriptions).toHaveBeenCalledTimes(1);

    // 完成时清理：Map 条目移除后可再次触发
    resolveInfer({ inferred: [], skipped: [], failed: [] });
    await waitFor(() => {});
    fireEvent.click(screen.getByRole("button", { name: /批量推断缺失描述/ }));
    await waitFor(() => expect(inferDescriptions).toHaveBeenCalledTimes(2));
  });

  it("来自变更追踪（?from=变更追踪）时返回按钮显示来源并精确返回 /assetmap?tab=changes", async () => {
    function LocDisplay() {
      const loc = useLocation();
      return <div data-testid="loc">{loc.pathname + loc.search}</div>;
    }
    render(
      <MemoryRouter initialEntries={["/catalogs?from=变更追踪&kw=ods_order"]}>
        <LocDisplay />
        <Routes>
          <Route path="/catalogs" element={<Catalogs />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("返回变更追踪")).toBeInTheDocument());
    fireEvent.click(screen.getByText("返回变更追踪"));
    await waitFor(() => expect(screen.getByTestId("loc").textContent).toBe("/assetmap?tab=changes"));
  });

  it("?focus= 目标行高亮显示（突出定位的表）", async () => {
    render(
      <MemoryRouter initialEntries={["/catalogs?focus=dwd_finance_order"]}>
        <Catalogs />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("dwd_finance_order")).toBeInTheDocument());
    const row = screen.getByText("dwd_finance_order").closest("tr");
    expect(row?.getAttribute("style") ?? "").toContain("background");
  });

  it("列表展示字段数与描述覆盖率（产品化布局信息丰富度）", async () => {
    const withCols = {
      ...CATALOGS[0],
      schema_def: {
        columns: [
          { name: "order_id", type: "bigint", comment: "订单ID" },
          { name: "amount", type: "decimal", comment: "" },
          { name: "qty", type: "int", description: "LLM 推断描述" },
        ],
      },
    } as DBCatalog;
    mockedList.mockResolvedValue({ items: [withCols], total: 1, page: 1, page_size: 20 });

    render(
      <MemoryRouter>
        <Catalogs />
      </MemoryRouter>,
    );

    // 3 字段：order_id 有 comment、qty 有 description → 2 已描述（67%）
    await screen.findByText("3 字段 · 2 已描述（67%）");
  });

  it("业务域与最近更新列展示治理信号（产品化布局信息丰富度）", async () => {
    render(
      <MemoryRouter>
        <Catalogs />
      </MemoryRouter>,
    );
    await screen.findByText("dwd_finance_order");
    // 业务域 Tag（来自 data_source 继承回填）
    expect(screen.getByText("sales")).toBeTruthy();
    // 最近更新列显示相对时间（2026-08-15T10:00 距当前为 N 小时前/分钟前）
    expect(screen.getByText(/刚刚|分钟前|小时前|天前/)).toBeTruthy();
  });

  it("列设置可开关列显示：取消「最近更新」后该列隐藏，实体列固定保留", async () => {
    render(
      <MemoryRouter>
        <Catalogs />
      </MemoryRouter>,
    );
    await screen.findByText("dwd_finance_order");
    // 默认最近更新列可见
    expect(screen.getByText(/刚刚|分钟前|小时前|天前/)).toBeTruthy();

    // 打开「列设置」下拉，取消「最近更新」
    fireEvent.click(screen.getByText("列设置"));
    fireEvent.click(screen.getByLabelText("最近更新"));

    await waitFor(() => {
      expect(screen.queryByText(/刚刚|分钟前|小时前|天前/)).toBeNull();
    });
    // 实体/数据源列固定保留（不可关）
    expect(screen.getByText("dwd_finance_order")).toBeTruthy();
    expect(screen.getByText("mysql_unisense")).toBeTruthy();
  });
});
