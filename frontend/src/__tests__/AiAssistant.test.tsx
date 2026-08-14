import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { AiAssistant } from "../pages/AiAssistant";

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
    aiNl2Sql: vi.fn(),
    getLlmConfig: vi.fn(),
    saveLlmConfig: vi.fn(),
    testLlmConfig: vi.fn(),
    UnisenseApiError,
  };
});

import { aiNl2Sql } from "../api";

const mockNl2Sql = vi.mocked(aiNl2Sql);

describe("AiAssistant 自然语言查询", () => {
  beforeEach(() => {
    mockNl2Sql.mockReset();
  });

  it("生成 SQL：调用 aiNl2Sql 并展示生成的 SQL", async () => {
    mockNl2Sql.mockResolvedValue({
      sql: "SELECT ... FROM dwd_finance_order",
      safe: true,
      notes: ["命中指标 finance_revenue_sum_d"],
      method: "keyword",
      anchored: ["finance_revenue_sum_d"],
      params: {},
      execute: false,
    });
    render(<AiAssistant />);
    const textarea = screen.getByPlaceholderText(/如：最近 30 天 finance 域收入总额/) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "最近 30 天 finance 域收入总额，按日粒度" } });
    fireEvent.click(screen.getByText("生成 SQL"));
    await waitFor(() => {
      expect(mockNl2Sql).toHaveBeenCalledWith({
        nl_query: "最近 30 天 finance 域收入总额，按日粒度",
        metric_scope: null,
        execute: false,
      });
    });
    expect(await screen.findByText(/SELECT \.\.\./)).toBeTruthy();
    expect(screen.getByText(/关键词匹配/)).toBeTruthy();
  });

  it("空输入时提示，不调用接口", async () => {
    render(<AiAssistant />);
    fireEvent.click(screen.getByText("生成 SQL"));
    await waitFor(() => {
      expect(mockNl2Sql).not.toHaveBeenCalled();
    });
  });
});
