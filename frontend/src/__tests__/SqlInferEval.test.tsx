import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { SqlInferEval } from "../pages/SqlInferEval";
import type { SqlInferEvalData } from "../types";

vi.mock("../api", () => ({
  getSqlInferEval: vi.fn(),
  runSqlInferEval: vi.fn(),
}));

import { getSqlInferEval, runSqlInferEval } from "../api";

const mockedGet = vi.mocked(getSqlInferEval);
const mockedRun = vi.mocked(runSqlInferEval);

function evalData(): SqlInferEvalData {
  return {
    report: {
      total: 9,
      exact_count: 8,
      exact_rate: 0.8889,
      measure_precision: 0.8889,
      measure_recall: 0.8889,
      table_precision: 1,
      table_recall: 1,
      period_match_rate: 1,
      cases: [
        {
          case_id: "gmv_daily",
          dialect: "hive",
          exact: true,
          measure_precision: 1,
          measure_recall: 1,
          table_precision: 1,
          table_recall: 1,
          period_match: true,
          extra_measures: [],
          missing_measures: [],
          extra_tables: [],
          missing_tables: [],
          pred_period: "day",
          expected_period: "day",
        },
        {
          case_id: "doctor_active_month",
          dialect: "hive",
          exact: false,
          measure_precision: 1,
          measure_recall: 0.5,
          table_precision: 1,
          table_recall: 1,
          period_match: true,
          extra_measures: [],
          missing_measures: ["doctor_code|COUNT_DISTINCT|alias:last_month_active_doctor_cnt"],
          extra_tables: [],
          missing_tables: [],
          pred_period: "month",
          expected_period: "month",
        },
      ],
    },
    history: [
      {
        id: 1,
        ran_at: "2026-08-26T10:00:00Z",
        total: 9,
        exact_count: 8,
        exact_rate: 0.8889,
        measure_precision: 0.8889,
        measure_recall: 0.8889,
        table_precision: 1,
        table_recall: 1,
        period_match_rate: 1,
        elapsed_ms: 12,
        actor_id: 1,
      },
    ],
    latest_run: {
      id: 1,
      ran_at: "2026-08-26T10:00:00Z",
      total: 9,
      exact_count: 8,
      exact_rate: 0.8889,
      measure_precision: 0.8889,
      measure_recall: 0.8889,
      table_precision: 1,
      table_recall: 1,
      period_match_rate: 1,
      elapsed_ms: 12,
      actor_id: 1,
    },
    latest_run_cases: [],
    dataset: [
      {
        case_id: "gmv_daily",
        dialect: "hive",
        note: "日 GMV + 去重买家数",
        sql: "SELECT SUM(amount) AS gmv FROM ods.orders",
        expected_measures: ["amount|SUM|alias:gmv"],
        expected_tables: ["ods.orders"],
        expected_period: "day",
      },
      {
        case_id: "doctor_active_month",
        dialect: "hive",
        note: "真实 ETL",
        sql: "INSERT OVERWRITE ...",
        expected_measures: ["doctor_code|COUNT_DISTINCT|alias:current_month_active_doctor_cnt"],
        expected_tables: ["wedw_dw.doctor_visit_agent_info_da"],
        expected_period: "month",
      },
    ],
  };
}

function renderPage() {
  return render(<SqlInferEval />);
}

describe("SqlInferEval", () => {
  beforeEach(() => {
    mockedGet.mockReset();
    mockedRun.mockReset();
    mockedGet.mockResolvedValue(evalData());
  });

  it("渲染成功率指标与逐样本明细", async () => {
    renderPage();
    expect(await screen.findByText("SQL 智能推断解析成功率")).toBeTruthy();
    // 端到端完全匹配率：8/9
    expect(await screen.findByText("8/9 用例 度量+表+周期全等")).toBeTruthy();
    // 指标卡
    expect(screen.getByText("度量级召回率")).toBeTruthy();
    expect(screen.getByText("度量级精确率")).toBeTruthy();
    expect(screen.getByText("周期匹配率")).toBeTruthy();
    // 历史记录
    expect(await screen.findByText("成功率历史趋势")).toBeTruthy();
    // 用例通过/失败徽标
    expect(screen.getByText("通过")).toBeTruthy();
    expect(screen.getByText("失败")).toBeTruthy();
  });

  it("点「运行评测并记录」触发 runSqlInferEval 并刷新", async () => {
    mockedRun.mockResolvedValue({
      run_id: 2,
      report: evalData().report,
    });
    renderPage();
    const btn = await screen.findByRole("button", { name: /运行评测并记录/ });
    fireEvent.click(btn);
    await waitFor(() => expect(mockedRun).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(mockedGet).toHaveBeenCalledTimes(2));
  });

  it("展开用例显示期望 vs 实际明细（缺失度量标注）", async () => {
    renderPage();
    // 点表格行展开图标（第二个：doctor_active_month 行）
    const expandIcons = await screen.findAllByRole("button", { name: /Expand row/ });
    fireEvent.click(expandIcons[1]);
    // 点内层 Collapse 面板「期望 vs 实际」
    const detailPanel = await screen.findByText("期望 vs 实际（度量/表/周期）");
    fireEvent.click(detailPanel);
    // 展开区显示缺失度量标注
    expect(
      await screen.findByText(
        "缺失度量：doctor_code|COUNT_DISTINCT|alias:last_month_active_doctor_cnt",
      ),
    ).toBeTruthy();
  });

  it("加载失败展示错误提示", async () => {
    mockedGet.mockRejectedValue(new Error("评测数据加载失败"));
    renderPage();
    expect(await screen.findByText("评测数据加载失败")).toBeTruthy();
  });
});
