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
    listDataSourceTypes: vi.fn(),
    listDomainTree: vi.fn(),
    createDataSource: vi.fn(),
    updateDataSource: vi.fn(),
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
  listDomainTree,
  createDataSource,
  updateDataSource,
  testDataSourceConnection,
  checkDataSourceConnection,
  getSourceHealth,
  getSourceWatermark,
} from "../api";

const mockedList = vi.mocked(listDataSources);
const mockedTypes = vi.mocked(listDataSourceTypes);
const mockedDomains = vi.mocked(listDomainTree);
const mockedCreate = vi.mocked(createDataSource);
const mockedUpdate = vi.mocked(updateDataSource);
const mockedTest = vi.mocked(testDataSourceConnection);
const mockedCheck = vi.mocked(checkDataSourceConnection);
const mockedHealth = vi.mocked(getSourceHealth);
const mockedWatermark = vi.mocked(getSourceWatermark);

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
    mockedTypes.mockResolvedValue(TYPES);
    mockedDomains.mockResolvedValue(DOMAINS);
    mockedCreate.mockResolvedValue(source);
    mockedUpdate.mockResolvedValue(source);
    mockedTest.mockResolvedValue({ ok: true, source_type: "mysql", latency_ms: 12, error: null, detail: null });
    mockedCheck.mockResolvedValue({ ok: true, source_type: "mysql", latency_ms: 8, error: null, detail: null });
    mockedHealth.mockResolvedValue({ source_id: "mysql_finance", health_status: "healthy", last_collected_at: null, last_error: null, last_health_check: null, uptime_check: true });
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

  it("编辑数据源：预填现有值并调用更新接口（连接字段留空保持原配置）", async () => {
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
    // 未填写连接字段 → 不提交 connection_config（保持原配置）
    expect(payload.connection_config).toBeUndefined();
  });

  it("编辑时填写连接字段则随更新提交 connection_config", async () => {
    renderSources();
    await waitFor(() => {
      expect(screen.getByText("管理")).toBeTruthy();
    });
    fireEvent.click(screen.getByText("管理"));
    await screen.findByText(/数据源：财务库/);
    fireEvent.click(screen.getByText("编辑"));
    await screen.findByText(/编辑数据源：mysql_finance/);
    fireEvent.change(screen.getByPlaceholderText("127.0.0.1"), { target: { value: "10.0.0.2" } });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => {
      expect(mockedUpdate).toHaveBeenCalled();
    });
    const payload = mockedUpdate.mock.calls[0][1] as unknown as Record<string, unknown>;
    // 编辑弹窗预填默认端口 3306，连接字段填写后随更新提交
    expect(payload.connection_config).toEqual({ host: "10.0.0.2", port: 3306 });
  });
});
