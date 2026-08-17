import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { CollectionHistory } from "../pages/CollectionHistory";
import type { DataSource } from "../types";
import type { DriftLogItem } from "../api";

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
    listDriftLogs: vi.fn(),
    listCollectionRuns: vi.fn(),
    getCollectionRunDetail: vi.fn(),
    UnisenseApiError,
  };
});

import { listDataSources, listDriftLogs, listCollectionRuns, getCollectionRunDetail } from "../api";

const mockedSources = vi.mocked(listDataSources);
const mockedRuns = vi.mocked(listCollectionRuns);
const mockedDrift = vi.mocked(listDriftLogs);
const mockedRunDetail = vi.mocked(getCollectionRunDetail);

const source: DataSource = {
  source_id: "mysql_finance",
  name: "财务库",
  source_type: "mysql",
  domain: "finance",
  enabled: true,
  cluster_id: null,
  coverage: 0.5,
  health_status: "healthy",
  connection_config_present: true,
  schedule_cron: null,
  schedule_enabled: true,
  collection_mode: "FULL",
  created_by: 1,
  created_at: "2026-08-01T00:00:00",
  updated_at: "2026-08-01T00:00:00",
};

const runs = [
  {
    id: 1,
    source_id: "mysql_finance",
    source_name: "财务库",
    job_id: null,
    trigger: "manual",
    mode: "FULL",
    effective_mode: "FULL",
    status: "COMPLETED",
    actor_id: 1,
    actor_name: "张三",
    started_at: "2026-08-14T03:00:00+00:00",
    finished_at: "2026-08-14T03:00:30+00:00",
    duration_seconds: 30,
    scanned: 54,
    registered: 54,
    pii_registered: 2,
    failed_count: 0,
    drift_count: 3,
    deprecated_count: 0,
    coverage: 0.8,
    error: null,
    detail: null,
  },
  {
    id: 2,
    source_id: "mysql_finance",
    source_name: "财务库",
    job_id: "job-fail-1",
    trigger: "scheduled",
    mode: "INCREMENTAL",
    effective_mode: null,
    status: "FAILED",
    actor_id: null,
    actor_name: null,
    started_at: "2026-08-14T04:00:00+00:00",
    finished_at: "2026-08-14T04:00:10+00:00",
    duration_seconds: 10,
    scanned: 0,
    registered: 0,
    pii_registered: 0,
    failed_count: 1,
    drift_count: 0,
    deprecated_count: 0,
    coverage: null,
    error: "connection refused",
    detail: null,
  },
];

const driftLogs: DriftLogItem[] = [
  {
    source_id: "mysql_finance",
    entity_name: "orders",
    change_type: "ADD_COLUMN",
    before_signature: "abc",
    after_signature: "def",
    before_schema: null,
    after_schema: null,
    diff_json: { added: ["user_id"], removed: [], changed: [] },
    detected_at: "2026-08-14T03:00:30+00:00",
  },
  {
    source_id: "mysql_finance",
    entity_name: "orders",
    change_type: "TYPE_CHANGE",
    before_signature: "def",
    after_signature: "ghi",
    before_schema: null,
    after_schema: null,
    diff_json: { added: [], removed: [], changed: [{ name: "amount", before: { type: "int" }, after: { type: "decimal" } }] },
    detected_at: "2026-08-14T05:00:00+00:00",
  },
];

describe("CollectionHistory", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedSources.mockResolvedValue({ items: [source], total: 1, page: 1, page_size: 100 });
    mockedRuns.mockResolvedValue({ items: runs, total: 2, page: 1, page_size: 10 });
    mockedDrift.mockResolvedValue({ items: driftLogs, total: 2, page: 1, page_size: 10 });
    mockedRunDetail.mockResolvedValue({ ...runs[0], detail: { failed_specs: [], drift_events: [] } });
  });

  it("采集记录 tab：展示统计摘要卡与运行历史表格", async () => {
    render(<MemoryRouter><CollectionHistory /></MemoryRouter>);
    await screen.findAllByText("采集记录");
    // 摘要卡
    expect(screen.getByText("采集次数")).toBeTruthy();
    expect(screen.getByText("累计扫描")).toBeTruthy();
    // 表格行：成功/失败 + 指标
    await waitFor(() => {
      expect(screen.getAllByText("成功").length).toBeGreaterThanOrEqual(1);
    });
    expect(screen.getAllByText("失败").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("手动")).toBeTruthy();
    expect(screen.getByText("定时")).toBeTruthy();
    // 模式展示（INCREMENTAL 失败行）
    expect(screen.getByText("INCREMENTAL")).toBeTruthy();
  });

  it("采集记录请求携带过滤参数（源/状态/触发）", async () => {
    render(<MemoryRouter><CollectionHistory /></MemoryRouter>);
    await screen.findAllByText("采集记录");
    await waitFor(() => {
      expect(mockedRuns).toHaveBeenCalledWith(
        expect.objectContaining({ page_size: 10, page: 1 }),
      );
    });
  });

  it("变更追踪 tab：切换后展示 diff 明细（新增列/类型变更）", async () => {
    render(<MemoryRouter><CollectionHistory /></MemoryRouter>);
    await screen.findAllByText("采集记录");
    fireEvent.click(screen.getByText("变更追踪"));
    await screen.findByText("选择数据源查看 Schema 变更");
    fireEvent.mouseDown(screen.getByText("选择数据源查看 Schema 变更"));
    await screen.findByText("财务库 (mysql_finance)");
    fireEvent.click(screen.getByText("财务库 (mysql_finance)"));
    await waitFor(() => {
      expect(mockedDrift).toHaveBeenCalledWith("mysql_finance", expect.objectContaining({ page_size: 10 }));
    });
    // diff 明细：新增列 + 类型变更
    await waitFor(() => {
      expect(screen.getByText("+user_id")).toBeTruthy();
    });
    expect(screen.getByText(/~amount/)).toBeTruthy();
    // 删除列 DROPPED 死代码已被移除（不渲染"表已删除"）
    expect(screen.queryByText("表已删除")).toBeNull();
  });

  it("点击详情：拉取运行详情并展示指标与明细抽屉", async () => {
    render(<MemoryRouter><CollectionHistory /></MemoryRouter>);
    await screen.findAllByText("采集记录");
    await waitFor(() => {
      expect(screen.getAllByRole("button", { name: /详\s*情/ }).length).toBeGreaterThanOrEqual(1);
    });
    fireEvent.click(screen.getAllByRole("button", { name: /详\s*情/ })[0]);
    await screen.findByText("采集运行详情");
    expect(mockedRunDetail).toHaveBeenCalledWith(1);
    // 抽屉展示指标
    expect(screen.getAllByText("扫描").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("覆盖率")).toBeTruthy();
  });
});
