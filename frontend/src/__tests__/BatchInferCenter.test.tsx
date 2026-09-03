import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { BatchInferCenter } from "../components/BatchInferCenter";
import type { BatchInferTask } from "../api";

vi.mock("../api", () => ({
  listBatchInferTasks: vi.fn(),
  cancelBatchInferTask: vi.fn(),
}));

import { listBatchInferTasks } from "../api";

const mockedList = vi.mocked(listBatchInferTasks);

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

async function openCenter() {
  const bar = await screen.findByTestId("batch-infer-center-bar");
  fireEvent.click(bar);
}

describe("BatchInferCenter 取消状态展示", () => {
  beforeEach(() => {
    mockedList.mockReset();
  });

  it("取消已请求（cancel_requested=true 且 running）→ 浮条停止中 + 抽屉停止 Tag + 禁用按钮", async () => {
    mockedList.mockResolvedValue([
      makeTask({ id: 6, status: "running", cancel_requested: true }),
    ]);
    render(<BatchInferCenter />);
    // 浮条文案立即变化（无需开抽屉即可感知取消已生效）
    expect(await screen.findByText(/批量推断停止中/)).toBeTruthy();
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
    expect(await screen.findByText(/批量推断进行中/)).toBeTruthy();
    await openCenter();
    expect(screen.getByRole("button", { name: /取消任务/ })).toBeTruthy();
  });
});
