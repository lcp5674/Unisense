import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { SqlInferEval } from "../pages/SqlInferEval";
import type { SqlInferEvalData } from "../types";

vi.mock("../api", () => ({
  getSqlInferEval: vi.fn(),
  runSqlInferEval: vi.fn(),
  listEvalSamples: vi.fn(),
  createEvalSample: vi.fn(),
  updateEvalSample: vi.fn(),
  deleteEvalSample: vi.fn(),
  previewEvalSample: vi.fn(),
}));

import {
  createEvalSample,
  deleteEvalSample,
  getSqlInferEval,
  listEvalSamples,
  previewEvalSample,
  runSqlInferEval,
  updateEvalSample,
} from "../api";

const mockedGet = vi.mocked(getSqlInferEval);
const mockedRun = vi.mocked(runSqlInferEval);
const mockedListSamples = vi.mocked(listEvalSamples);
const mockedCreate = vi.mocked(createEvalSample);
const mockedUpdate = vi.mocked(updateEvalSample);
const mockedDelete = vi.mocked(deleteEvalSample);
const mockedPreview = vi.mocked(previewEvalSample);

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
          pred_measures: ["amount|SUM|alias:gmv"],
          pred_measures_detail: [
            { column: "amount", agg: "SUM", alias: "gmv", table: null, signature: "amount|SUM|alias:gmv" },
          ],
          pred_tables: ["ods.orders"],
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
          pred_measures: ["doctor_code|COUNT_DISTINCT|alias:current_month_active_doctor_cnt"],
          pred_measures_detail: [
            {
              column: "doctor_code",
              agg: "COUNT_DISTINCT",
              alias: "current_month_active_doctor_cnt",
              table: "wedw_dw.doctor_visit_agent_info_da",
              signature:
                "doctor_code|COUNT_DISTINCT|alias:current_month_active_doctor_cnt|table:wedw_dw.doctor_visit_agent_info_da",
            },
          ],
          pred_tables: ["wedw_dw.doctor_visit_agent_info_da"],
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
        expected_measures_detail: [
          { column: "amount", agg: "SUM", alias: "gmv", table: null },
        ],
        expected_tables: ["ods.orders"],
        expected_period: "day",
        source: "builtin",
      },
      {
        case_id: "doctor_active_month",
        dialect: "hive",
        note: "真实 ETL",
        sql: "INSERT OVERWRITE ...",
        expected_measures: [
          "doctor_code|COUNT_DISTINCT|alias:current_month_active_doctor_cnt",
          "doctor_code|COUNT_DISTINCT|alias:last_month_active_doctor_cnt",
        ],
        expected_measures_detail: [
          {
            column: "doctor_code",
            agg: "COUNT_DISTINCT",
            alias: "current_month_active_doctor_cnt",
            table: null,
          },
          {
            column: "doctor_code",
            agg: "COUNT_DISTINCT",
            alias: "last_month_active_doctor_cnt",
            table: null,
          },
        ],
        expected_tables: ["wedw_dw.doctor_visit_agent_info_da"],
        expected_period: "month",
        source: "builtin",
      },
    ],
  };
}

function renderPage() {
  return render(
    <App>
      <SqlInferEval />
    </App>,
  );
}

describe("SqlInferEval", () => {
  beforeEach(() => {
    mockedGet.mockReset();
    mockedRun.mockReset();
    mockedListSamples.mockReset();
    mockedCreate.mockReset();
    mockedUpdate.mockReset();
    mockedDelete.mockReset();
    mockedPreview.mockReset();
    mockedGet.mockResolvedValue(evalData());
    mockedListSamples.mockResolvedValue({ items: [], total: 0 });
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

  it("展开用例显示期望 vs 实际对照表（缺失标红 + 判定）", async () => {
    renderPage();
    // 点表格行展开图标（第二个：doctor_active_month 行）
    const expandIcons = await screen.findAllByRole("button", { name: /Expand row/ });
    fireEvent.click(expandIcons[1]);
    // 点内层 Collapse 面板「期望 vs 实际」
    fireEvent.click(await screen.findByText("期望 vs 实际（度量/表/周期）"));
    // 对照表维度行（度量/源表/周期）
    expect(await screen.findByText("度量")).toBeTruthy();
    expect(screen.getByText("源表")).toBeTruthy();
    expect(screen.getAllByText("周期").length).toBeGreaterThanOrEqual(1);
    // 缺失度量红标（期望有、实际未解析出）——结构化展示别名
    expect(await screen.findByText("as last_month_active_doctor_cnt")).toBeTruthy();
    // 实际解析结果完整展示：列名 + 聚合 Tag + 别名 + 源表 分开展示（期望/实际两列各一条）
    expect(screen.getAllByText("as current_month_active_doctor_cnt").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText("COUNT_DISTINCT").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText("doctor_code").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("← wedw_dw.doctor_visit_agent_info_da")).toBeTruthy();
    // 判定列显示差异统计（1 缺失 · 0 多余）
    expect(await screen.findByText("1 缺失 · 0 多余")).toBeTruthy();
    // 图例说明
    expect(screen.getByText("期望有、实际未解析出")).toBeTruthy();
  });

  it("匹配用例展示绿色判定与完整实际解析", async () => {
    renderPage();
    const expandIcons = await screen.findAllByRole("button", { name: /Expand row/ });
    fireEvent.click(expandIcons[0]); // gmv_daily（exact）
    fireEvent.click(await screen.findByText("期望 vs 实际（度量/表/周期）"));
    // 对账表三行判定均为绿色「匹配」
    expect((await screen.findAllByText("匹配")).length).toBeGreaterThanOrEqual(3);
    // 期望/实际度量完整展示（列名 + 聚合 Tag + 别名 结构化）
    expect(screen.getAllByText("as gmv").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText("SUM").length).toBeGreaterThanOrEqual(2);
    // 期望/实际源表完整展示
    expect(screen.getAllByText("ods.orders").length).toBeGreaterThanOrEqual(2);
  });

  it("加载失败展示错误提示", async () => {
    mockedGet.mockRejectedValue(new Error("评测数据加载失败"));
    renderPage();
    expect(await screen.findByText("评测数据加载失败")).toBeTruthy();
  });

  it("新增样本：填写 SQL 预览解析后创建", async () => {
    const user = userEvent.setup();
    mockedPreview.mockResolvedValue({
      measures: [{ column: "amount", agg: "SUM", alias: "gmv", table: "ods.orders" }],
      source_tables: ["ods.orders"],
      period: "day",
    });
    mockedCreate.mockResolvedValue({
      id: 10,
      case_id: "my_sample",
      dialect: "hive",
      sql: "SELECT SUM(amount) AS gmv FROM ods.orders",
      expected_measures: [{ column: "amount", agg: "SUM", alias: "gmv", table: null }],
      expected_tables: ["ods.orders"],
      expected_period: "day",
      note: "示例",
      enabled: true,
      is_builtin: false,
      created_by: 1,
    });
    renderPage();
    // 打开新增样本弹窗
    fireEvent.click(await screen.findByRole("button", { name: /新增样本/ }));
    expect(await screen.findByText("新增评测样本")).toBeTruthy();
    // 填 SQL（userEvent 完整事件序列）
    const sqlInput = screen.getByPlaceholderText(
      "粘贴完整 SQL 脚本（多语句 ETL / 方言写法）",
    );
    await user.clear(sqlInput);
    await user.type(sqlInput, "SELECT SUM(amount) AS gmv FROM ods.orders");
    // 填编码
    const caseIdInput = screen.getByPlaceholderText(
      "如 my_case（唯一，与内置基线不可重复）",
    );
    await user.type(caseIdInput, "my_sample");
    // 预览解析
    fireEvent.click(screen.getByRole("button", { name: /预览解析/ }));
    await waitFor(() => expect(mockedPreview).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/规则解析实际画像/)).toBeTruthy();
    // 保存创建
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1));
    expect(mockedCreate.mock.calls[0][0]).toMatchObject({
      case_id: "my_sample",
      expected_period: "day",
    });
    // 创建后刷新列表
    await waitFor(() => expect(mockedListSamples).toHaveBeenCalledTimes(2));
  });

  it("自定义样本行显示「自定义」标记并可删除", async () => {
    mockedListSamples.mockResolvedValue({
      items: [
        {
          id: 7,
          case_id: "my_sample",
          dialect: "hive",
          sql: "SELECT SUM(amount) AS gmv FROM ods.orders",
          expected_measures: [{ column: "amount", agg: "SUM", alias: "gmv", table: null }],
          expected_tables: ["ods.orders"],
          expected_period: "day",
          note: "示例",
          enabled: true,
          is_builtin: false,
          created_by: 1,
        },
      ],
      total: 1,
    });
    mockedDelete.mockResolvedValue({ sample_id: 7 });
    // dataset 需包含该自定义样本，才能在用例明细中匹配
    const data = evalData();
    data.dataset = [
      ...data.dataset,
      {
        case_id: "my_sample",
        dialect: "hive",
        note: "示例",
        sql: "SELECT SUM(amount) AS gmv FROM ods.orders",
        expected_measures: ["amount|SUM|alias:gmv"],
        expected_measures_detail: [{ column: "amount", agg: "SUM", alias: "gmv", table: null }],
        expected_tables: ["ods.orders"],
        expected_period: "day",
        source: "custom",
      },
    ];
    // report.cases 也补该自定义样本的评测结果
    data.report.cases = [
      ...data.report.cases,
      {
        case_id: "my_sample",
        dialect: "hive",
        exact: false,
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
    ];
    mockedGet.mockResolvedValue(data);
    renderPage();
    // 自定义 Tag
    expect(await screen.findByText("自定义")).toBeTruthy();
    // 删除（Popconfirm 确认）
    fireEvent.click(screen.getByRole("button", { name: /删\s*除/ }));
    fireEvent.click(await screen.findByRole("button", { name: /确 定|确定|OK/ }));
    await waitFor(() => expect(mockedDelete).toHaveBeenCalledTimes(1));
    expect(mockedDelete.mock.calls[0][0]).toBe(7);
  });
});
