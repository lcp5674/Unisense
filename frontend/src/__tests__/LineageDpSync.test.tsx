import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
  getDpSyncScanStatus: vi.fn(),
  getDpSyncCurrentScan: vi.fn(),
  cancelDpSyncScan: vi.fn(),
  forceCancelDpSyncScan: vi.fn(),
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
    mockedApi.getDpSyncScanStatus.mockResolvedValue({
      task_id: 1,
      status: "success",
      progress: { stage: "done", total: 1, processed: 1, current_task_id: null },
      result: null,
    });
    mockedApi.getDpSyncCurrentScan.mockResolvedValue({ running: false });
    mockedApi.cancelDpSyncScan.mockResolvedValue({ cancelled: true });
    mockedApi.forceCancelDpSyncScan.mockResolvedValue({ cancelled: true });
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
      task_types: [
        { value: 1, label: "数据抽取（SQL 加工）", known: true, count: 100 },
        { value: 3, label: "Shell 任务", known: true, count: 18 },
        { value: 10, label: "DataX 同步任务", known: true, count: 31 },
      ],
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

  afterEach(() => {
    // 部分用例使用 fake timers——统一恢复，避免泄漏影响后续用例
    vi.useRealTimers();
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

  it("ops scan submits async task and surfaces failure error clearly", async () => {
    const user = userEvent.setup();
    mockedApi.scanDpSyncNow.mockResolvedValue({
      task_id: 1,
      status: "running",
      already_running: false,
    });
    mockedApi.getDpSyncScanStatus
      .mockResolvedValueOnce({
        task_id: 1,
        status: "running",
        progress: {
          stage: "parsing",
          total: 10,
          processed: 2,
          current_task_id: 101,
        },
        result: null,
      })
      .mockResolvedValueOnce({
        task_id: 1,
        status: "failed",
        error: "(pymysql.err.OperationalError) (2003, \"Can't connect\")",
        progress: { stage: "parsing", total: 10, processed: 2, current_task_id: 101 },
        result: { skipped: "failed", error: "(pymysql.err.OperationalError)" },
      });
    renderPage();
    await user.click(screen.getByText(/运\s*维/));
    await screen.findByText("运行记录");
    await user.click(screen.getByText(/立即全量扫描/));
    // 首帧（提交后立即轮询）：running → 实时进度展示
    await screen.findByText(/扫描中：解析节点脚本并写血缘（已处理 2 \/ 10 个任务）/);
    expect(screen.getByText(/当前任务 #101/)).toBeInTheDocument();
    // 推进 1.6s（轮询间隔 1.5s）：第二帧 failed → 明确异常提示（不再显示「扫描完成」）
    await new Promise((r) => setTimeout(r, 1600));
    await screen.findByText("扫描失败");
    expect(screen.getAllByText(/Can't connect/, { exact: false }).length).toBeGreaterThan(0);
    expect(mockedApi.scanDpSyncNow).toHaveBeenCalledTimes(1);
    expect(mockedApi.getDpSyncScanStatus).toHaveBeenCalledWith(1);
  });

  it("ops scan parsing text reflects current step type dynamically", async () => {
    const user = userEvent.setup();
    mockedApi.scanDpSyncNow.mockResolvedValue({
      task_id: 2,
      status: "running",
      already_running: false,
    });
    // DataX 节点类型 → parsing 文案应展示「正在解析 DataX 同步 节点并写血缘」
    mockedApi.getDpSyncScanStatus.mockResolvedValue({
      task_id: 2,
      status: "running",
      progress: {
        stage: "parsing",
        total: 10,
        processed: 3,
        current_task_id: 202,
        current_step_type: 2,
        current_step_label: "DataX 同步",
      },
      result: null,
    });
    renderPage();
    await user.click(screen.getByText(/运\s*维/));
    await screen.findByText("运行记录");
    await user.click(screen.getByText(/立即全量扫描/));
    await screen.findByText(/扫描中：正在解析 DataX 同步 节点并写血缘/);
    expect(screen.getByText(/已处理 3 \/ 10 个任务/)).toBeInTheDocument();
    // 无类型信息时回退静态文案（scanParsingText 兜底）
    mockedApi.getDpSyncScanStatus.mockResolvedValue({
      task_id: 2,
      status: "running",
      progress: {
        stage: "parsing",
        total: 10,
        processed: 4,
        current_task_id: 202,
      },
      result: null,
    });
    await waitFor(
      () =>
        expect(
          screen.getAllByText(/解析节点脚本并写血缘/).length
        ).toBeGreaterThan(0),
      { timeout: 3000 }
    );
  });

  it("ops scan shows cancel button and cancels running task", async () => {
    const user = userEvent.setup();
    mockedApi.scanDpSyncNow.mockResolvedValue({
      task_id: 7,
      status: "running",
      already_running: false,
    });
    mockedApi.getDpSyncScanStatus.mockResolvedValue({
      task_id: 7,
      status: "running",
      progress: { stage: "parsing", total: 5, processed: 1, current_task_id: 11 },
      result: null,
    });
    renderPage();
    await user.click(screen.getByText(/运\s*维/));
    await screen.findByText("运行记录");
    await user.click(screen.getByText(/立即全量扫描/));
    await screen.findByText(/扫描中/);
    await user.click(screen.getByText(/取\s*消\s*扫\s*描/));
    expect(mockedApi.cancelDpSyncScan).toHaveBeenCalledWith(7);
    // 两段式取消（B）：请求后按钮变「正在停止…」+ 明确等待文案（不再只是「已发送」）
    await waitFor(() =>
      expect(screen.getAllByText(/正在停止扫描：/).length).toBeGreaterThan(0)
    );
    expect(screen.getByText(/正在停止…/)).toBeInTheDocument();
  });

  it("ops scan shows force-terminate entry after cancel wait timeout", async () => {
    const user = userEvent.setup();
    mockedApi.scanDpSyncNow.mockResolvedValue({
      task_id: 11,
      status: "running",
      already_running: false,
    });
    // cancel_requested 且已超过等待上限（10s 前请求）→ 轮询应武装「强制终止」入口
    mockedApi.getDpSyncScanStatus.mockResolvedValue({
      task_id: 11,
      status: "running",
      cancel_requested: true,
      cancel_requested_at: new Date(Date.now() - 10000).toISOString(),
      progress: { stage: "parsing", total: 5, processed: 2, current_task_id: 11 },
      result: null,
    });
    renderPage();
    await user.click(screen.getByText(/运\s*维/));
    await screen.findByText("运行记录");
    await user.click(screen.getByText(/立即全量扫描/));
    await waitFor(() =>
      expect(screen.getAllByText(/^强制终止$/).length).toBeGreaterThan(0)
    );
    // 点击触发按钮（首个）打开确认弹窗 → 确认（Modal ok 按钮文本同为「强制终止」，取最后一个）
    await user.click(screen.getAllByText(/^强制终止$/)[0]);
    await waitFor(() =>
      expect(screen.getAllByText(/^强制终止$/).length).toBeGreaterThanOrEqual(2)
    );
    const btns = screen.getAllByText(/^强制终止$/);
    await user.click(btns[btns.length - 1]);
    await waitFor(() =>
      expect(mockedApi.forceCancelDpSyncScan).toHaveBeenCalledWith(11)
    );
  });

  it("ops scan already-running submit tracks existing task", async () => {
    mockedApi.scanDpSyncNow.mockResolvedValue({
      task_id: 9,
      status: "running",
      already_running: true,
    });
    mockedApi.getDpSyncScanStatus.mockResolvedValue({
      task_id: 9,
      status: "success",
      progress: { stage: "done", total: 3, processed: 3, current_task_id: null },
      result: { scanned_tasks: 3, scanned_steps: 5, parsed_ok: 3 },
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByText(/运\s*维/));
    await screen.findByText("运行记录");
    await user.click(screen.getByText(/立即全量扫描/));
    // 已有任务运行时：不重复提交，跟踪其进度并最终展示完成
    await waitFor(() =>
      expect(mockedApi.getDpSyncScanStatus).toHaveBeenCalledWith(9)
    );
    expect(mockedApi.scanDpSyncNow).toHaveBeenCalledTimes(1);
  });

  it("ops tab auto-resumes running scan progress when re-entered", async () => {
    const user = userEvent.setup();
    // 切走页面再回来：后端仍有运行中的手动扫描（OpsTab 挂载即自动接上轮询，
    // 无需重新点「立即扫描」）
    mockedApi.getDpSyncCurrentScan.mockResolvedValue({
      running: true,
      task_id: 5,
      status: "running",
      progress: { stage: "parsing", total: 20, processed: 8, current_task_id: 202 },
      result: null,
    });
    mockedApi.getDpSyncScanStatus.mockResolvedValue({
      task_id: 5,
      status: "running",
      progress: { stage: "parsing", total: 20, processed: 10, current_task_id: 202 },
      result: null,
    });
    renderPage();
    await user.click(screen.getByText(/运\s*维/));
    await screen.findByText("运行记录");
    // 未点「立即扫描」即自动恢复运行中任务进度
    await screen.findByText(/扫描中：解析节点脚本并写血缘/);
    expect(mockedApi.getDpSyncCurrentScan).toHaveBeenCalledTimes(1);
    expect(mockedApi.getDpSyncScanStatus).toHaveBeenCalledWith(5);
    expect(mockedApi.scanDpSyncNow).not.toHaveBeenCalled();
  });

  it("warns when selected step types include non-parseable (non-SQL) types", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/保\s*存/);
    // 打开「节点类型」下拉（页面 Select 顺序：数据源/任务类型/节点类型——取第三个）
    const selectors = document.querySelectorAll(".ant-select-selector");
    expect(selectors.length).toBeGreaterThanOrEqual(3);
    fireEvent.mouseDown(selectors[2]);
    await user.click(await screen.findByText(/2 = DataX 同步/));
    await screen.findByText(/所选节点类型包含无法解析为血缘的类型/);
    expect(screen.getAllByText(/DataX 同步/).length).toBeGreaterThan(0);
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
