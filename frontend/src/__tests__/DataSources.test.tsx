import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { DataSources } from "../pages/DataSources";
import type { DataSource, SourceTypeInfo } from "../types";

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
    listDataSourceTypes: vi.fn(),
    createDataSource: vi.fn(),
    testDataSourceConnection: vi.fn(),
    checkDataSourceConnection: vi.fn(),
    collectSource: vi.fn(),
    scheduleSource: vi.fn(),
    getSourceHealth: vi.fn(),
    getSourceWatermark: vi.fn(),
    UnisenseApiError,
  };
});

import {
  listDataSources,
  listDataSourceTypes,
  createDataSource,
  testDataSourceConnection,
  checkDataSourceConnection,
  getSourceHealth,
  getSourceWatermark,
} from "../api";

const mockedList = vi.mocked(listDataSources);
const mockedTypes = vi.mocked(listDataSourceTypes);
const mockedCreate = vi.mocked(createDataSource);
const mockedTest = vi.mocked(testDataSourceConnection);
const mockedCheck = vi.mocked(checkDataSourceConnection);
const mockedHealth = vi.mocked(getSourceHealth);
const mockedWatermark = vi.mocked(getSourceWatermark);

const TYPES: SourceTypeInfo[] = [
  { source_type: "mysql", label: "MySQL", default_port: 3306, supports_database: true, supports_schema: false, description: "关系型数据库" },
  { source_type: "postgres", label: "PostgreSQL", default_port: 5432, supports_database: true, supports_schema: true, description: "关系型数据库" },
  { source_type: "kafka", label: "Kafka", default_port: 9092, supports_database: false, supports_schema: false, description: "消息队列" },
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
  render(<DataSources />);
  fireEvent.click(screen.getAllByText("新建数据源")[0]);
  await screen.findByText("选择数据源类型");
}

async function selectType(label: string) {
  fireEvent.mouseDown(screen.getByText("选择数据源类型"));
  await screen.findByText(label);
  fireEvent.click(screen.getByText(label));
}

function renderSources() {
  return render(<DataSources />);
}

describe("DataSources", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedList.mockResolvedValue([source]);
    mockedTypes.mockResolvedValue(TYPES);
    mockedCreate.mockResolvedValue(source);
    mockedTest.mockResolvedValue({ ok: true, source_type: "mysql", latency_ms: 12, error: null, detail: null });
    mockedCheck.mockResolvedValue({ ok: true, source_type: "mysql", latency_ms: 8, error: null, detail: null });
    mockedHealth.mockResolvedValue({ source_id: "mysql_finance", health_status: "healthy", last_collected_at: null, last_error: null, uptime_check: true });
    mockedWatermark.mockResolvedValue({ source_id: "mysql_finance", last_collected_at: null, mode: "FULL", scanned_count: 10, failed_count: 0 });
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
    fireEvent.change(screen.getByPlaceholderText("如 finance"), { target: { value: "finance" } });
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

  it("创建时省略 source_id（由后端自动生成）", async () => {
    await openCreateModal();
    fireEvent.change(screen.getByPlaceholderText("如 财务 MySQL"), { target: { value: "财务库" } });
    fireEvent.change(screen.getByPlaceholderText("如 finance"), { target: { value: "finance" } });
    fireEvent.change(screen.getByPlaceholderText("127.0.0.1"), { target: { value: "10.0.0.1" } });
    await selectType("MySQL（mysql）");
    fireEvent.click(screen.getByRole("button", { name: /创\s*建/ }));
    await waitFor(() => {
      expect(mockedCreate).toHaveBeenCalled();
    });
    const payload = mockedCreate.mock.calls[0][0] as unknown as Record<string, unknown>;
    expect(payload.source_id).toBeUndefined();
    expect(payload.source_type).toBe("mysql");
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
});
