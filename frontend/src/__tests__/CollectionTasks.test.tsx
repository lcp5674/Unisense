import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { CollectionTasks } from "../pages/CollectionTasks";
import type { CollectionJob, DataSource } from "../types";

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
    listCollectionJobs: vi.fn(),
    listDataSources: vi.fn(),
    UnisenseApiError,
  };
});

import { listCollectionJobs, listDataSources } from "../api";

const mockedJobs = vi.mocked(listCollectionJobs);
const mockedSources = vi.mocked(listDataSources);

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
  collection_mode: "FULL",
  created_by: 1,
  created_at: "2026-08-01T00:00:00",
  updated_at: "2026-08-01T00:00:00",
};

const jobs: CollectionJob[] = [
  {
    job_id: "collect:mysql_finance:abc123",
    source_id: "mysql_finance",
    actor_id: 1,
    status: "COMPLETED",
    detail: { scanned: 54, registered: 54, mode: "FULL" },
    created_at: "2026-08-14T03:00:00+00:00",
    kind: "manual",
  },
  {
    job_id: "collect:sched:mysql_finance:1752000000",
    source_id: "mysql_finance",
    actor_id: 1,
    status: "QUEUED",
    detail: { mode: "FULL" },
    created_at: "2026-08-14T03:30:00+00:00",
    kind: "scheduled",
  },
];

describe("CollectionTasks", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedJobs.mockResolvedValue(jobs);
    mockedSources.mockResolvedValue({ items: [source], total: 1, page: 1, page_size: 100 });
  });

  it("展示任务列表：手动/定时标记 + 创建时间", async () => {
    render(<MemoryRouter><CollectionTasks /></MemoryRouter>);
    await waitFor(() => {
      expect(screen.getByText("采集任务中心")).toBeTruthy();
    });
    // 手动/定时标记列
    expect(screen.getByText("手动")).toBeTruthy();
    expect(screen.getByText("定时")).toBeTruthy();
    // 创建时间已回显（不再是 "—"）
    expect(screen.getAllByText(/2026\/8\/14/).length).toBeGreaterThanOrEqual(2);
    // 任务 ID 均展示
    expect(screen.getByText("collect:mysql_finance:abc123")).toBeTruthy();
  });

  it("按数据源筛选任务：切换筛选后按 source_id 请求", async () => {
    render(<MemoryRouter><CollectionTasks /></MemoryRouter>);
    await waitFor(() => {
      expect(screen.getByText("按数据源筛选")).toBeTruthy();
    });
    fireEvent.mouseDown(screen.getByText("按数据源筛选"));
    await screen.findByText("财务库（mysql_finance）");
    fireEvent.click(screen.getByText("财务库（mysql_finance）"));
    await waitFor(() => {
      expect(mockedJobs).toHaveBeenCalledWith({ limit: 50, source_id: "mysql_finance" });
    });
  });

  it("从总览仪表 ?status=RUNNING 直达：请求携带任务状态过滤（资产卡片下钻）", async () => {
    render(
      <MemoryRouter initialEntries={["/collection-tasks?status=RUNNING"]}>
        <CollectionTasks />
      </MemoryRouter>,
    );
    await screen.findByText("采集任务中心");
    await waitFor(() => {
      expect(mockedJobs).toHaveBeenCalledWith(
        expect.objectContaining({ status: "RUNNING", limit: 50 }),
      );
    });
  });
});
