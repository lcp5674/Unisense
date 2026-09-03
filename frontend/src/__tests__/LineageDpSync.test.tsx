import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LineageDpSync } from "../pages/LineageDpSync";
import * as api from "../api";
import type { DataSource } from "../types";

vi.mock("../api", () => ({
  getDpSyncConfig: vi.fn(),
  saveDpSyncConfig: vi.fn(),
  listDpTickets: vi.fn(),
  getDpTicket: vi.fn(),
  resolveDpTicket: vi.fn(),
  listDpSyncRuns: vi.fn(),
  getDpSyncWatermark: vi.fn(),
  resetDpSyncWatermark: vi.fn(),
  scanDpSyncNow: vi.fn(),
  listDataSources: vi.fn(),
  getDpSyncMeta: vi.fn(),
  previewDpSyncExclude: vi.fn(),
}));

const mockedApi = vi.mocked(api);

function renderPage() {
  return render(
    <App>
      <LineageDpSync />
    </App>
  );
}

describe("LineageDpSync", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedApi.getDpSyncConfig.mockResolvedValue({
      enabled: false,
      source_id: "mysql_uncategorized",
      poll_interval_minutes: 5,
      llm_enabled: true,
      resolve_memory_enabled: true,
      owner_backfill: "orphan_only",
    });
    mockedApi.listDpTickets.mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      page_size: 10,
    });
    mockedApi.listDpSyncRuns.mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      page_size: 10,
    });
    mockedApi.getDpSyncWatermark.mockResolvedValue({
      task: { last_scan_at: "2026-09-03T10:00:00" },
      step: null,
    });
    mockedApi.listDataSources.mockResolvedValue({
      items: [
        { source_id: "mysql_uncategorized", name: "dp", source_type: "mysql" },
        { source_id: "other_source", name: "其它源", source_type: "mysql" },
      ] as unknown as DataSource[],
      total: 2,
      page: 1,
      page_size: 200,
    });
    mockedApi.getDpSyncMeta.mockResolvedValue({
      task_types: [{ value: 1, label: "SQL 任务", known: true, count: 100 }],
      step_types: [
        { value: 2, label: "DataX 同步", known: true, count: 50 },
        { value: 7, label: "Hive/Spark SQL", known: true, count: 200 },
      ],
      exclude_defaults: ["(^|\\.)tmp_", "_bak$"],
      reachable: false,
      reason: "测试环境 dp 源不可达",
    });
    mockedApi.previewDpSyncExclude.mockResolvedValue({
      reachable: true,
      total: 4,
      matched: 2,
      samples: [
        { table: "wedw_dwd.tmp_x", pattern: "(^|\\.)tmp_" },
        { table: "wedw_ods.tbl_bak", pattern: "_bak$" },
      ],
      invalid_patterns: [],
      note: "预览范围为 dp 任务产出表",
    });
  });

  it("renders three tab entries and loads config", async () => {
    renderPage();
    expect(screen.getByText("dp 调度血缘同步")).toBeInTheDocument();
    await waitFor(() =>
      expect(mockedApi.getDpSyncConfig).toHaveBeenCalledTimes(1)
    );
    expect(screen.getAllByText("同步配置").length).toBeGreaterThan(0);
    expect(screen.getByText("待抉择")).toBeInTheDocument();
    expect(screen.getByText("运维")).toBeInTheDocument();
  });

  it("saves config with enabled toggle", async () => {
    const user = userEvent.setup();
    mockedApi.saveDpSyncConfig.mockResolvedValue({
      enabled: true,
      source_id: "mysql_uncategorized",
      poll_interval_minutes: 5,
      llm_enabled: true,
      resolve_memory_enabled: true,
      owner_backfill: "orphan_only",
    });
    renderPage();
    await screen.findByText(/保\s*存/);
    await user.click(screen.getByText(/保\s*存/));
    await waitFor(() =>
      expect(mockedApi.saveDpSyncConfig).toHaveBeenCalledTimes(1)
    );
    const payload = mockedApi.saveDpSyncConfig.mock.calls[0][0];
    expect(payload.poll_interval_minutes).toBe(5);
    expect(payload.source_id).toBe("mysql_uncategorized");
  });

  it("lists tickets in tickets tab and resolves", async () => {
    const user = userEvent.setup();
    mockedApi.listDpTickets.mockResolvedValue({
      items: [
        {
          id: 11,
          task_id: 1386,
          step_id: 5012,
          task_name: "转诊预约指标",
          out_table: "wedw_dwd.dp_dq_measure_df",
          sql_hash: "abc",
          status: "diverged",
          divergence_reason: "sqlglot 与 LLM 意见不一致",
        },
      ],
      total: 1,
      page: 1,
      page_size: 10,
    });
    mockedApi.getDpTicket.mockResolvedValue({
      id: 11,
      task_id: 1386,
      step_id: 5012,
      task_name: "转诊预约指标",
      out_table: "wedw_dwd.dp_dq_measure_df",
      sql_hash: "abc",
      status: "diverged",
      divergence_reason: "sqlglot 与 LLM 意见不一致",
      sql_text: "create table t as select 1",
      sqlglot_result: {
        table_edges: [{ source: "wedw_ods.a", target: "wedw_dwd.t" }],
        field_edges: [],
      },
    });
    mockedApi.resolveDpTicket.mockResolvedValue({
      ticket_id: 11,
      resolution: "accept_sqlglot",
    });
    renderPage();
    await user.click(screen.getByText("待抉择"));
    await screen.findByText("转诊预约指标");
    expect(mockedApi.listDpTickets).toHaveBeenCalled();
    // 打开详情抽屉 → 采纳 sqlglot
    await user.click(screen.getByText("转诊预约指标"));
    await screen.findByText("采纳 sqlglot");
    await user.click(screen.getByText("采纳 sqlglot"));
    await waitFor(() =>
      expect(mockedApi.resolveDpTicket).toHaveBeenCalledWith(11, {
        resolution: "accept_sqlglot",
      })
    );
  });

  it("renders ops tab with run log columns", async () => {
    const user = userEvent.setup();
    mockedApi.listDpSyncRuns.mockResolvedValue({
      items: [
        {
          id: 1,
          run_at: "2026-09-03T10:00:00",
          status: "success",
          scanned_tasks: 2,
          scanned_steps: 3,
          parsed_ok: 2,
          llm_confirmed: 0,
          diverged: 0,
          llm_fallback: 0,
          unparseable: 0,
          tickets_created: 0,
          tickets_resolved: 0,
          errors: 0,
          llm_calls: 0,
          duration_ms: 100,
        },
      ],
      total: 1,
      page: 1,
      page_size: 10,
    });
    renderPage();
    await user.click(screen.getByText(/运\s*维/));
    await screen.findByText("运行记录");
    expect(mockedApi.getDpSyncWatermark).toHaveBeenCalled();
  });

  it("source_id renders as data-source select and loads type/exclude meta", async () => {
    renderPage();
    // 数据源下拉：选中值显示 source_id · name（来自 listDataSources 选项）
    await screen.findByText("mysql_uncategorized · dp（mysql）");
    // 类型目录（meta）驱动 + 内置排除默认规则只读展示
    await screen.findByText(/内置默认排除（始终生效）/);
    expect(screen.getByText("(^|\\.)tmp_")).toBeInTheDocument();
    expect(mockedApi.listDataSources).toHaveBeenCalled();
    expect(mockedApi.getDpSyncMeta).toHaveBeenCalled();
  });

  it("clears task type filter to all (= empty array) on save", async () => {
    const user = userEvent.setup();
    mockedApi.saveDpSyncConfig.mockResolvedValue({
      enabled: false,
      source_id: "mysql_uncategorized",
      poll_interval_minutes: 5,
      task_type_filter: [],
      step_type_filter: [7],
      llm_enabled: true,
      resolve_memory_enabled: true,
      owner_backfill: "orphan_only",
    });
    renderPage();
    await screen.findByText(/保\s*存/);
    // 任务类型卡的「清空（=全部）」
    const clearLinks = screen.getAllByText("清空（=全部）");
    await user.click(clearLinks[0]);
    await user.click(screen.getByText(/保\s*存/));
    await waitFor(() =>
      expect(mockedApi.saveDpSyncConfig).toHaveBeenCalledTimes(1)
    );
    const payload = mockedApi.saveDpSyncConfig.mock.calls[0][0];
    expect(payload.task_type_filter).toEqual([]); // 空 = 全部任务类型
  });

  it("previews exclude regex hit count from dp source", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("校验并预览命中量");
    // 填一条正则再预览（默认空也允许：仅内置默认）
    const textarea = screen.getByPlaceholderText(/每行一条正则/);
    await user.type(textarea, "(^\\.)*tmp_");
    await user.click(screen.getByText("校验并预览命中量"));
    await screen.findByText(/命中 2 \/ 4 张任务产出表/);
    expect(mockedApi.previewDpSyncExclude).toHaveBeenCalledWith(
      expect.objectContaining({ source_id: "mysql_uncategorized" })
    );
  });
});
