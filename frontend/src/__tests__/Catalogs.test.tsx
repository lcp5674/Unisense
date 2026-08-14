import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
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
    UnisenseApiError,
  };
});

import { listCatalogs, registerCatalog, listDataSources, listCatalogDatabases } from "../api";

const mockedList = vi.mocked(listCatalogs);
const mockedRegister = vi.mocked(registerCatalog);
const mockedSources = vi.mocked(listDataSources);
const mockedDatabases = vi.mocked(listCatalogDatabases);

const SOURCES: DataSource[] = [
  {
    source_id: "mysql_unisense",
    name: "Unisense MySQL",
    source_type: "mysql",
    domain: "sales",
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
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockedList.mockResolvedValue({ items: CATALOGS, total: 1, page: 1, page_size: 20 });
  mockedSources.mockResolvedValue({ items: SOURCES, total: 2, page: 1, page_size: 200 });
  mockedDatabases.mockResolvedValue(["unisense", "sales"]);
  mockedRegister.mockResolvedValue({ ...CATALOGS[0], sensitivity_level: "INTERNAL" } as DBCatalog);
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
      const lastCall = mockedList.mock.calls[mockedList.mock.calls.length - 1][0];
      expect(lastCall.database).toBe("unisense");
    });
  });
});
