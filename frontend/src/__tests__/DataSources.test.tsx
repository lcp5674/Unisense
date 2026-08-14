import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { DataSources } from "../pages/DataSources";
import type { DataSource, SourceTypeInfo, SubjectDomainTreeNode } from "../types";

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
    listDataSources: vi.fn(),
    getDataSource: vi.fn(),
    listDataSourceTypes: vi.fn(),
    listDomainTree: vi.fn(),
    createDataSource: vi.fn(),
    updateDataSource: vi.fn(),
    testDataSourceConnection: vi.fn(),
    checkDataSourceConnection: vi.fn(),
    collectSourceNow: vi.fn(),
    streamCollectionJob: vi.fn(),
    getCollectionJob: vi.fn(),
    listDataSourceDatabases: vi.fn(),
    scheduleSource: vi.fn(),
    getSourceHealth: vi.fn(),
    getSourceWatermark: vi.fn(),
    listDriftLogs: vi.fn(),
    UnisenseApiError,
  };
});

import {
  listDataSources,
  getDataSource,
  listDataSourceTypes,
  listDomainTree,
  createDataSource,
  updateDataSource,
  testDataSourceConnection,
  checkDataSourceConnection,
  getSourceHealth,
  getSourceWatermark,
  listDriftLogs,
  collectSourceNow,
  streamCollectionJob,
  getCollectionJob,
  listDataSourceDatabases,
} from "../api";

const mockedList = vi.mocked(listDataSources);
const mockedGet = vi.mocked(getDataSource);
const mockedTypes = vi.mocked(listDataSourceTypes);
const mockedDomains = vi.mocked(listDomainTree);
const mockedCreate = vi.mocked(createDataSource);
const mockedUpdate = vi.mocked(updateDataSource);
const mockedTest = vi.mocked(testDataSourceConnection);
const mockedCheck = vi.mocked(checkDataSourceConnection);
const mockedHealth = vi.mocked(getSourceHealth);
const mockedWatermark = vi.mocked(getSourceWatermark);
const mockedListDriftLogs = vi.mocked(listDriftLogs);
const mockedCollectNow = vi.mocked(collectSourceNow);
const mockedStream = vi.mocked(streamCollectionJob);
const mockedGetJob = vi.mocked(getCollectionJob);
const mockedListDatabases = vi.mocked(listDataSourceDatabases);

const TYPES: SourceTypeInfo[] = [
  { source_type: "mysql", label: "MySQL", default_port: 3306, supports_database: true, supports_schema: false, description: "关系型数据库" },
  { source_type: "postgres", label: "PostgreSQL", default_port: 5432, supports_database: true, supports_schema: true, description: "关系型数据库" },
  { source_type: "spark", label: "Spark", default_port: 10000, supports_database: true, supports_schema: false, description: "Spark SQL（Thrift Server）" },
  { source_type: "kafka", label: "Kafka", default_port: 9092, supports_database: false, supports_schema: false, description: "消息队列" },
];

const DOMAINS: SubjectDomainTreeNode[] = [
  { id: 1, code: "finance", name: "财务", parent_id: null, level: 1, sort_order: 0, status: "active", metric_count: 0, children: [] },
  { id: 2, code: "marketing", name: "营销", parent_id: null, level: 1, sort_order: 1, status: "active", metric_count: 0, children: [] },
];

const source: DataSource = {
  source_id: "mysql_finance",
  name: "财务库",
  source_type: "mysql",
  domain: "finance",
  cluster_id: null,
  coverage: 0.5,
  health_status: "healthy",
  connection_config_present: true,
  schedule_cron: null,
  collection_mode: "FULL",
  created_by: 1,
  created_at: "2026-08-01T00:00:00",
  updated_at: "2026-08-01T00:00:00",
};

async function openCreateModal() {
  render(<MemoryRouter><DataSources /></MemoryRouter>);
  fireEvent.click(screen.getAllByText("新建数据源")[0]);
  await screen.findByText("选择数据源类型");
}

async function selectType(label: string) {
  fireEvent.mouseDown(screen.getByText("选择数据源类型"));
  await screen.findByText(label);
  fireEvent.click(screen.getByText(label));
}

async function selectDomain(label: string) {
  fireEvent.mouseDown(screen.getByText("从主题域选择"));
  await screen.findByText(label);
  fireEvent.click(screen.getByText(label));
}

function renderSources() {
  return render(<MemoryRouter><DataSources /></MemoryRouter>);
}

describe("DataSources", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // P1-1: 列表返回分页结构 {items, total, page, page_size}
    mockedList.mockResolvedValue({ items: [source], total: 1, page: 1, page_size: 20 });
    // 详情（编辑回显）：返回带明文连接配置的详情
    mockedGet.mockResolvedValue({ ...source, connection_config: { host: "10.0.0.1", port: 3306, database: "finance", user: "root", password: "secret" } });
    mockedTypes.mockResolvedValue(TYPES);
    mockedDomains.mockResolvedValue(DOMAINS);
    mockedCreate.mockResolvedValue(source);
    mockedUpdate.mockResolvedValue(source);
    mockedTest.mockResolvedValue({ ok: true, source_type: "mysql", latency_ms: 12, error: null, detail: null });
    mockedCheck.mockResolvedValue({ ok: true, source_type: "mysql", latency_ms: 8, error: null, detail: null });
    mockedHealth.mockResolvedValue({ source_id: "mysql_finance", health_status: "healthy", last_collected_at: null, last_error: null, last_health_check: null, uptime_check: true });
    mockedWatermark.mockResolvedValue({ source_id: "mysql_finance", last_collected_at: null, mode: "FULL", scanned_count: 10, failed_count: 0 });
    mockedListDriftLogs.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 10 });
    mockedCollectNow.mockResolvedValue({ job_id: "job-1", status: "QUEUED", mode: "FULL" });
    mockedStream.mockReturnValue(() => {});
    mockedGetJob.mockResolvedValue({ job_id: "job-1", status: "COMPLETED", detail: {} });
    mockedListDatabases.mockResolvedValue({ databases: ["finance", "orders"], source_type: "mysql" });
  });

  it("动态拉取类型元信息并展示中文标签", async () => {
    renderSources();
    await waitFor(() => {
      expect(screen.getByText("mysql_finance")).toBeTruthy();
    });
    expect(mockedTypes).toHaveBeenCalled();
    expect(screen.getByText("MySQL")).toBeTruthy();
  });

  it("创建弹窗显示系统自动生成的 Source ID 预览", async () => {
    await openCreateModal();
    await selectType("MySQL（mysql）");
    await selectDomain("财务（finance）");
    await waitFor(() => {
      expect(screen.getByText(/Source ID 将由系统自动生成/)).toBeTruthy();
    });
    expect(screen.getByText(/Source ID 将由系统自动生成：mysql_finance/)).toBeTruthy();
  });

  it("测试连接按钮调用后端并提示成功", async () => {
    await openCreateModal();
    fireEvent.change(screen.getByPlaceholderText("127.0.0.1"), { target: { value: "10.0.0.1" } });
    await selectType("MySQL（mysql）");
    fireEvent.click(screen.getByText("测试连接"));
    await waitFor(() => {
      expect(mockedTest).toHaveBeenCalled();
    });
    expect(mockedTest.mock.calls[0][0].connection_config.host).toBe("10.0.0.1");
    expect(screen.getAllByText(/连接成功/).length).toBeGreaterThan(0);
  });

  it("测试连接通过后自动枚举数据库并显示目标库选择", async () => {
    await openCreateModal();
    fireEvent.change(screen.getByPlaceholderText("127.0.0.1"), { target: { value: "10.0.0.1" } });
    await selectType("MySQL（mysql）");
    fireEvent.click(screen.getByText("测试连接"));
    await waitFor(() => {
      expect(mockedListDatabases).toHaveBeenCalled();
    });
    expect(mockedListDatabases.mock.calls[0][0].connection_config.host).toBe("10.0.0.1");
    // 连接通过后 database 字段变为选择框（枚举结果渲染为选项）
    await screen.findByText(/Database（选择目标库）/);
    fireEvent.mouseDown(screen.getByText("全部库（默认）"));
    await screen.findByRole("option", { name: "finance" });
    expect(screen.getByRole("option", { name: "orders" })).toBeTruthy();
  });

  it("立即采集走异步 collect-now + SSE 实时进度并展示结果明细", async () => {
    renderSources();
    await waitFor(() => {
      expect(screen.getByText("管理")).toBeTruthy();
    });
    fireEvent.click(screen.getByText("管理"));
    await screen.findByText(/数据源：财务库/);
    fireEvent.click(screen.getByText("立即采集"));
    await waitFor(() => {
      expect(mockedCollectNow).toHaveBeenCalledWith("mysql_finance", "FULL");
    });
    expect(mockedStream).toHaveBeenCalledWith("job-1", expect.any(Object));
    // 模拟 SSE 进度事件 + 终态事件
    const handlers = mockedStream.mock.calls[0][1] as {
      onProgress?: (s: unknown, p: Record<string, unknown> | null) => void;
      onDone?: (s: { status: string; detail?: Record<string, unknown> | null }) => void;
    };
    handlers.onProgress?.(null, {
      phase: "registering",
      index: 1,
      total: 2,
      messages: ["注册 1/2：users"],
    });
    handlers.onDone?.({
      status: "COMPLETED",
      detail: {
        scanned: 2,
        registered: 2,
        pii_registered: 1,
        failed_count: 0,
        coverage: 1,
        mode: "FULL",
        drift_count: 0,
        drift_events: [],
        deprecated_count: 0,
        entities: [
          { entity_name: "users", sensitivity_level: "PII", drifted: false, change_type: null },
          { entity_name: "orders", sensitivity_level: "INTERNAL", drifted: false, change_type: null },
        ],
      },
    });
    await screen.findByText("采集完成：扫描 2 · 注册 2 · PII 1");
    expect(screen.getByText("本次采集到的表（2）")).toBeTruthy();
  });

  it("创建时省略 source_id（由后端自动生成）", async () => {
    await openCreateModal();
    fireEvent.change(screen.getByPlaceholderText("如 财务 MySQL"), { target: { value: "财务库" } });
    await selectDomain("财务（finance）");
    fireEvent.change(screen.getByPlaceholderText("127.0.0.1"), { target: { value: "10.0.0.1" } });
    await selectType("MySQL（mysql）");
    fireEvent.click(screen.getByRole("button", { name: /创\s*建/ }));
    await waitFor(() => {
      expect(mockedCreate).toHaveBeenCalled();
    });
    const payload = mockedCreate.mock.calls[0][0] as unknown as Record<string, unknown>;
    expect(payload.source_id).toBeUndefined();
    expect(payload.source_type).toBe("mysql");
    expect(payload.domain).toBe("finance");
    expect((payload.connection_config as Record<string, unknown>).database).toBeUndefined();
  });

  it("详情弹窗提供实时探活（测试连接）", async () => {
    renderSources();
    await waitFor(() => {
      expect(screen.getByText("管理")).toBeTruthy();
    });
    fireEvent.click(screen.getByText("管理"));
    await screen.findByText(/数据源：财务库/);
    fireEvent.click(screen.getByText("测试连接"));
    await waitFor(() => {
      expect(mockedCheck).toHaveBeenCalledWith("mysql_finance");
    });
    expect(screen.getAllByText(/连接正常/).length).toBeGreaterThan(0);
  });

  it("编辑数据源：回显明文连接配置，仅改名不提交 connection_config", async () => {
    renderSources();
    await waitFor(() => {
      expect(screen.getByText("管理")).toBeTruthy();
    });
    fireEvent.click(screen.getByText("管理"));
    await screen.findByText(/数据源：财务库/);
    fireEvent.click(screen.getByText("编辑"));
    // 编辑弹窗预填现有值
    await screen.findByText(/编辑数据源：mysql_finance/);
    expect(screen.getByDisplayValue("财务库")).toBeTruthy();
    // 明文连接配置回显（host/port/database/user/password 均已预填）
    await waitFor(() => {
      expect(screen.getByDisplayValue("10.0.0.1")).toBeTruthy();
      expect(screen.getByDisplayValue("secret")).toBeTruthy();
    });
    // 修改名称并保存
    fireEvent.change(screen.getByDisplayValue("财务库"), { target: { value: "财务库-新" } });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => {
      expect(mockedUpdate).toHaveBeenCalled();
    });
    const [sourceId, payload] = mockedUpdate.mock.calls[0] as unknown as [string, Record<string, unknown>];
    expect(sourceId).toBe("mysql_finance");
    expect(payload.name).toBe("财务库-新");
    expect(payload.domain).toBe("finance");
    // 未修改连接字段 → 不提交 connection_config（保持原配置，避免重置健康状态）
    expect(payload.connection_config).toBeUndefined();
  });

  it("编辑时修改连接字段则随更新提交完整 connection_config（含回显字段）", async () => {
    renderSources();
    await waitFor(() => {
      expect(screen.getByText("管理")).toBeTruthy();
    });
    fireEvent.click(screen.getByText("管理"));
    await screen.findByText(/数据源：财务库/);
    fireEvent.click(screen.getByText("编辑"));
    await screen.findByText(/编辑数据源：mysql_finance/);
    // 等待明文回显完成后修改 Host
    await waitFor(() => {
      expect(screen.getByDisplayValue("10.0.0.1")).toBeTruthy();
    });
    fireEvent.change(screen.getByDisplayValue("10.0.0.1"), { target: { value: "10.0.0.2" } });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => {
      expect(mockedUpdate).toHaveBeenCalled();
    });
    const payload = mockedUpdate.mock.calls[0][1] as unknown as Record<string, unknown>;
    // 修改 Host 后提交完整配置：改动的 host + 回显的其余字段
    expect(payload.connection_config).toEqual({
      host: "10.0.0.2",
      port: 3306,
      database: "finance",
      user: "root",
      password: "secret",
    });
  });

  it("编辑修改连接配置保存后，引导「立即重新采集」", async () => {
    renderSources();
    await waitFor(() => {
      expect(screen.getByText("管理")).toBeTruthy();
    });
    fireEvent.click(screen.getByText("管理"));
    await screen.findByText(/数据源：财务库/);
    fireEvent.click(screen.getByText("编辑"));
    await screen.findByText(/编辑数据源：mysql_finance/);
    await waitFor(() => {
      expect(screen.getByDisplayValue("10.0.0.1")).toBeTruthy();
    });
    fireEvent.change(screen.getByDisplayValue("10.0.0.1"), { target: { value: "10.0.0.9" } });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    // 保存成功且连接配置变更 → 出现重新采集引导确认框
    await screen.findByText("连接配置已变更");
    fireEvent.click(screen.getByText("立即重新采集"));
    await waitFor(() => {
      expect(mockedCollectNow).toHaveBeenCalledWith("mysql_finance", "FULL");
    });
  });
});
