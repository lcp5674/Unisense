import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { beforeEach, describe, expect, it, vi } from "vitest";
import LineageDpSync from "../pages/LineageDpSync";
import * as api from "../api";

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
});
