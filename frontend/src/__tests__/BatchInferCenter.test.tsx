import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import {
  BatchInferCenter,
  notifyBatchInferActivity,
} from "../components/BatchInferCenter";
import type { BatchInferTask, DpTicketRetryTask } from "../api";

vi.mock("../api", () => ({
  listBatchInferTasks: vi.fn(),
  cancelBatchInferTask: vi.fn(),
  listDpRetryTasks: vi.fn(),
  cancelDpRetryTask: vi.fn(),
}));

import { listBatchInferTasks, listDpRetryTasks } from "../api";

const mockedList = vi.mocked(listBatchInferTasks);
const mockedDpList = vi.mocked(listDpRetryTasks);

function makeTask(over: Partial<BatchInferTask>): BatchInferTask {
  return {
    id: 6,
    actor_id: 1,
    actor_name: "admin",
    status: "running",
    total: 1,
    done: 0,
    failed: 0,
    cancelled: 0,
    added_total: 0,
    concurrency: 2,
    cancel_requested: false,
    error: null,
    tasks: [{ catalog_id: 1, entity_name: "t1", missing_fields: 5, needs_table_desc: false }],
    progress: [
      {
        catalog_id: 1,
        entity_name: "t1",
        status: "running",
        summary: "",
      },
    ],
    created_at: "2026-09-03T08:00:00",
    ...over,
  };
}

function makeDpTask(over: Partial<DpTicketRetryTask>): DpTicketRetryTask {
  return {
    id: 30,
    actor_id: 1,
    actor_name: "admin",
    status: "running",
    total: 1,
    done: 0,
    failed: 0,
    cancelled: 0,
    counts: { auto_resolved: 0, refreshed: 0, kept: 0, failed: 0 },
    cancel_requested: false,
    error: null,
    progress: [
      {
        ticket_id: 21,
        task_name: "t_a",
        out_table: "db.o1",
        status: "running",
        action: null,
        summary: "",
      },
    ],
    created_at: "2026-09-03T08:00:00",
    ...over,
  };
}

async function openCenter() {
  const bar = await screen.findByTestId("batch-infer-center-bar");
  fireEvent.click(bar);
}

describe("BatchInferCenter 取消状态展示", () => {
  beforeEach(() => {
    mockedList.mockReset();
    mockedDpList.mockReset();
    mockedDpList.mockResolvedValue([]);
  });

  it("取消已请求（cancel_requested=true 且 running）→ 浮条停止中 + 抽屉停止 Tag + 禁用按钮", async () => {
    mockedList.mockResolvedValue([
      makeTask({ id: 6, status: "running", cancel_requested: true }),
    ]);
    render(<BatchInferCenter />);
    // 浮条文案立即变化（无需开抽屉即可感知取消已生效）
    const bar = await screen.findByTestId("batch-infer-center-bar");
    expect(bar.textContent).toMatch(/批量推断\s*停止中/);
    await openCenter();
    expect(screen.getByText(/停止中（当前表结束后取消）/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /已请求取消/ })).toBeDisabled();
    // 不再出现可再次点击的「取消任务」
    expect(screen.queryByRole("button", { name: /取消任务/ })).toBeNull();
  });

  it("进行中且未请求取消 → 仍显示可点「取消任务」", async () => {
    mockedList.mockResolvedValue([
      makeTask({ id: 7, status: "running", cancel_requested: false }),
    ]);
    render(<BatchInferCenter />);
    const bar = await screen.findByTestId("batch-infer-center-bar");
    expect(bar.textContent).toMatch(/批量推断\s*进行中/);
    await openCenter();
    expect(screen.getByRole("button", { name: /取消任务/ })).toBeTruthy();
  });
});

describe("BatchInferCenter 状态机轮询（无任务零请求 / 有任务立即感知）", () => {
  beforeEach(() => {
    mockedList.mockReset();
    mockedDpList.mockReset();
    mockedDpList.mockResolvedValue([]);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("无任务：挂载探测一次后保持零请求（不启动轮询）", async () => {
    vi.useFakeTimers();
    mockedList.mockResolvedValue([]);
    render(<BatchInferCenter />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(mockedList).toHaveBeenCalledTimes(1); // 仅挂载探测
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });
    expect(mockedList).toHaveBeenCalledTimes(1); // 8s 后仍零请求
  });

  it("有运行中任务：启动 4s 轮询持续拉取", async () => {
    vi.useFakeTimers();
    mockedList.mockResolvedValue([makeTask({ id: 6, status: "running" })]);
    render(<BatchInferCenter />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(mockedList).toHaveBeenCalledTimes(1); // 挂载探测
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(mockedList).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(mockedList).toHaveBeenCalledTimes(3);
  });

  it("任务完成停止轮询；事件唤醒立即刷新并恢复轮询", async () => {
    vi.useFakeTimers();
    mockedList.mockResolvedValueOnce([]); // 挂载探测：无任务
    render(<BatchInferCenter />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(mockedList).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });
    expect(mockedList).toHaveBeenCalledTimes(1); // 空闲零请求

    // 事件唤醒（DescriptionCoveragePanel 提交任务后触发）
    mockedList.mockResolvedValue([makeTask({ id: 9, status: "running" })]);
    await act(async () => {
      notifyBatchInferActivity();
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(mockedList).toHaveBeenCalledTimes(2); // 事件触发立即刷新
    // 恢复 4s 轮询
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(mockedList).toHaveBeenCalledTimes(3);
  });

  it("任务秒级失败（提交事件带 id，首次刷新即终态）→ 仍弹完成摘要而非无声消失", async () => {
    vi.useFakeTimers();
    // 挂载探测：无任务（任务中心零请求空闲）
    mockedList.mockResolvedValueOnce([]);
    render(<BatchInferCenter />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(mockedList).toHaveBeenCalledTimes(1);

    // 用户提交任务 → DescriptionCoveragePanel 触发 notifyBatchInferActivity(taskId)
    // 但任务极快失败（LLM 不可达 → 秒级 completed/failed）——事件唤醒的首次刷新就拉到终态
    mockedList.mockResolvedValue([
      makeTask({
        id: 16,
        status: "completed",
        total: 1,
        done: 0,
        failed: 1,
        cancelled: 0,
        added_total: 0,
        progress: [{ catalog_id: 75, entity_name: "t75", status: "error", summary: "LLM 推断失败" }],
      }),
    ]);
    await act(async () => {
      notifyBatchInferActivity(16);
      await vi.advanceTimersByTimeAsync(0);
    });

    // 无进行中浮条（任务已终态），但完成摘要必须出现——用户能立即看到「失败 1 表」而非什么都看不到
    expect(screen.queryByText(/批量推断进行中/)).toBeNull();
    expect(screen.getByTestId("batch-infer-done-bar")).toBeTruthy();
    expect(screen.getByText(/批量推断 #16 完成/)).toBeTruthy();
    expect(screen.getByText(/失败 1 表/)).toBeTruthy();
  });

  it("任务进入终态后显示完成摘要浮条（成功/失败/取消/新增统计），停留后自动消失", async () => {
    vi.useFakeTimers();
    // 挂载探测：任务 running
    mockedList.mockResolvedValueOnce([makeTask({ id: 6, status: "running" })]);
    render(<BatchInferCenter />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const bar = screen.getByTestId("batch-infer-center-bar");
    expect(bar.textContent).toMatch(/批量推断\s*进行中/);

    // 下一轮轮询：任务 completed（带聚合统计）
    mockedList.mockResolvedValue([
      makeTask({
        id: 6,
        status: "completed",
        total: 4,
        done: 2,
        failed: 1,
        cancelled: 1,
        added_total: 7,
        progress: [
          { catalog_id: 1, entity_name: "t1", status: "done", summary: "字段描述已生成", added: 5 },
          { catalog_id: 2, entity_name: "t2", status: "done", summary: "表描述已生成", added: 2 },
          { catalog_id: 3, entity_name: "t3", status: "error", summary: "LLM 推断失败" },
          { catalog_id: 4, entity_name: "t4", status: "cancelled", summary: "已取消" },
        ],
      }),
    ]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });

    // 进行中浮条消失 → 完成摘要浮条出现（不再无声消失）
    expect(screen.queryByText(/批量推断进行中/)).toBeNull();
    expect(screen.getByTestId("batch-infer-done-bar")).toBeTruthy();
    expect(screen.getByText(/批量推断 #6 完成/)).toBeTruthy();
    expect(screen.getByText(/成功 2 表/)).toBeTruthy();
    expect(screen.getByText(/失败 1 表/)).toBeTruthy();
    expect(screen.getByText(/取消 1 表/)).toBeTruthy();
    expect(screen.getByText(/新增 7 处/)).toBeTruthy();

    // 停留 FINISHED_NOTICE_MS(10s) 后自动消失
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(screen.queryByTestId("batch-infer-done-bar")).toBeNull();
  });

  it("任务全部成功（无失败/取消/新增）→ 摘要浮条不含失败/取消/新增段", async () => {
    vi.useFakeTimers();
    mockedList.mockResolvedValueOnce([makeTask({ id: 7, status: "running" })]);
    render(<BatchInferCenter />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    mockedList.mockResolvedValue([
      makeTask({ id: 7, status: "completed", total: 2, done: 2, failed: 0, cancelled: 0, added_total: 0 }),
    ]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(screen.getByTestId("batch-infer-done-bar")).toBeTruthy();
    expect(screen.getByText(/成功 2 表/)).toBeTruthy();
    expect(screen.queryByText(/失败 0 表/)).toBeNull();
    expect(screen.queryByText(/取消 0 表/)).toBeNull();
    expect(screen.queryByText(/新增 0 处/)).toBeNull();
  });
});

describe("BatchInferCenter dp 待抉择 LLM 重试双源", () => {
  beforeEach(() => {
    mockedList.mockReset();
    mockedDpList.mockReset();
    mockedDpList.mockResolvedValue([]);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("dp 任务提交事件(kind=dp)+秒级终态 → dp 完成摘要出现（自动采纳/失败计数）", async () => {
    vi.useFakeTimers();
    // 挂载探测：两源均无任务（零请求空闲）
    mockedList.mockResolvedValueOnce([]);
    mockedDpList.mockResolvedValueOnce([]);
    render(<BatchInferCenter />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(mockedDpList).toHaveBeenCalledTimes(1);

    // LineageDpSync 提交 dp 重试 → notifyBatchInferActivity(taskId, "dp")；
    // 任务极快终态（completed）——事件唤醒首次刷新即终态 → dp 完成摘要必须出现
    mockedDpList.mockResolvedValue([
      makeDpTask({
        id: 30,
        status: "completed",
        total: 1,
        done: 0,
        failed: 1,
        cancelled: 0,
        counts: { auto_resolved: 0, refreshed: 0, kept: 0, failed: 1 },
        progress: [
          {
            ticket_id: 21,
            task_name: "t_a",
            out_table: "db.o1",
            status: "error",
            action: "failed",
            summary: "LLM 异常",
          },
        ],
      }),
    ]);
    await act(async () => {
      notifyBatchInferActivity(30, "dp");
      await vi.advanceTimersByTimeAsync(0);
    });
    // catalog 无任务 → 无进行中浮条；dp 完成摘要出现
    expect(screen.queryByText(/LLM 任务进行中/)).toBeNull();
    expect(screen.getByTestId("batch-infer-done-bar")).toBeTruthy();
    expect(screen.getByText(/LLM 重试 #30 完成/)).toBeTruthy();
    expect(screen.getByText(/自动采纳 0/)).toBeTruthy();
    expect(screen.getByText(/失败 1/)).toBeTruthy();
  });

  it("dp running 任务单独存在时也启动轮询（双源任一 running 即轮询）", async () => {
    vi.useFakeTimers();
    mockedList.mockResolvedValueOnce([]); // catalog 无任务
    mockedDpList.mockResolvedValueOnce([makeDpTask({ id: 31, status: "running" })]);
    render(<BatchInferCenter />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const bar = screen.getByTestId("batch-infer-center-bar");
    expect(bar.textContent).toMatch(/LLM 重试\s*进行中/);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(mockedDpList).toHaveBeenCalledTimes(2); // 轮询持续拉 dp 源
  });
});
