import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
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
    getCollectionJob: vi.fn(),
    collectSourceNow: vi.fn(),
    listDataSources: vi.fn(),
    UnisenseApiError,
  };
});

import { listCollectionJobs, getCollectionJob, collectSourceNow, listDataSources } from "../api";

const mockedJobs = vi.mocked(listCollectionJobs);
const mockedGetJob = vi.mocked(getCollectionJob);
const mockedCollectNow = vi.mocked(collectSourceNow);
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
    mockedJobs.mockResolvedValue({ items: jobs, total: jobs.length, page: 1, page_size: 10 });
    mockedGetJob.mockResolvedValue(jobs[0]);
    mockedCollectNow.mockResolvedValue({ job_id: "job-retry-1", status: "QUEUED" });
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
    // 创建时间已回显（上海时区中文格式，不再是 "—"）
    expect(screen.getAllByText(/2026年8月14日/).length).toBeGreaterThanOrEqual(2);
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
      expect(mockedJobs).toHaveBeenCalledWith({
        limit: 10,
        offset: 0,
        source_id: "mysql_finance",
      });
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
        expect.objectContaining({ status: "RUNNING", limit: 10, offset: 0 }),
      );
    });
  });

  it("提供统一的返回按钮（返回上一入口）", async () => {
    render(<MemoryRouter><CollectionTasks /></MemoryRouter>);
    await screen.findByText("采集任务中心");
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/lineage", "/collection-tasks"]}>
        <Routes>
          <Route path="/lineage" element={<div>lineage-page</div>} />
          <Route path="/collection-tasks" element={<CollectionTasks />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("采集任务中心");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("lineage-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    render(
      <MemoryRouter initialEntries={["/collection-tasks"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/collection-tasks" element={<CollectionTasks />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("采集任务中心");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("dashboard-page");
  });

  it("点击详情：拉取最新任务状态并展示完整信息抽屉", async () => {
    render(<MemoryRouter><CollectionTasks /></MemoryRouter>);
    await screen.findByText("采集任务中心");
    fireEvent.click(screen.getAllByRole("button", { name: /详\s*情/ })[0]);
    await screen.findByText("采集任务详情");
    expect(mockedGetJob).toHaveBeenCalledWith("collect:mysql_finance:abc123");
    // 抽屉展示任务 ID（表格行 + 抽屉两处）与状态（两处）
    expect(screen.getAllByText("collect:mysql_finance:abc123").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText("已完成").length).toBeGreaterThanOrEqual(2);
  });

  it("失败任务可重试：调用 collect-now 重新投递并刷新列表", async () => {
    const failedJob: CollectionJob = {
      ...jobs[0],
      job_id: "collect:mysql_finance:failed1",
      status: "FAILED",
      detail: { error: "connection refused", source_id: "mysql_finance" },
    };
    mockedJobs.mockResolvedValue({ items: [failedJob], total: 1, page: 1, page_size: 10 });
    render(<MemoryRouter><CollectionTasks /></MemoryRouter>);
    await screen.findByText("采集任务中心");
    fireEvent.click(screen.getAllByRole("button", { name: /重\s*试/ })[0]);
    await waitFor(() => {
      expect(mockedCollectNow).toHaveBeenCalledWith("mysql_finance");
    });
  });
});
