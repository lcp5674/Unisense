import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ReviewWorkbench } from "../pages/ReviewWorkbench";
import type { ConflictListResponse, ConflictResponse, MetricCompareResult, RulingRecord } from "../types";

vi.mock("../api", () => ({
  listConflicts: vi.fn(),
  arbitrateConflict: vi.fn(),
  escalateConflict: vi.fn(),
  closeConflict: vi.fn(),
  compareMetrics: vi.fn(),
  listConflictRulings: vi.fn(),
}));
const trackMock = vi.fn();
vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: trackMock }),
}));

import {
  listConflicts,
  arbitrateConflict,
  closeConflict,
  compareMetrics,
  listConflictRulings,
} from "../api";
const mockedList = vi.mocked(listConflicts);
const mockedArbitrate = vi.mocked(arbitrateConflict);
const mockedClose = vi.mocked(closeConflict);
const mockedCompare = vi.mocked(compareMetrics);
const mockedRulings = vi.mocked(listConflictRulings);

const baseConflict = (over: Partial<Omit<ConflictResponse, "conflict_type">>): ConflictResponse => {
  const {
    conflict_id = "CF-A",
    type = "same_name_diff_def",
    status = "OPEN",
    similarity_score = 0.35,
    metric_a = null,
    metric_b = null,
    metric_codes = [],
    decision_json = null,
    candidate_metric_code = "sales_gmv_day",
    existing_metric_code = "sales_gmv_d",
    description = "同名但口径定义不一致",
    detected_at = "2026-08-10T10:00:00",
    ...rest
  } = over;
  return {
    conflict_id,
    type,
    conflict_type: "same_name_diff_def",
    status,
    similarity_score,
    metric_a,
    metric_b,
    metric_codes,
    decision_json,
    candidate_metric_code,
    existing_metric_code,
    description,
    detected_at,
    ...rest,
  };
};

const conflicts: ConflictResponse[] = [
  baseConflict({}),
  baseConflict({ conflict_id: "CF-B", type: "same_def_diff_name", status: "ESCALATED", similarity_score: 0.9 }),
  baseConflict({ conflict_id: "CF-C", type: "cross_domain_same_def", status: "RULED", similarity_score: 0.75 }),
];

const compareResult: MetricCompareResult = {
  metrics: ["sales_gmv_day", "sales_gmv_d"],
  fields: {
    granularity: { a: "day", b: "d", difference_level: "similar" },
    unit: { a: "元", b: "元", difference_level: "identical" },
    definition: { a: { expression: "sum(gmv)" }, b: { expression: "sum(amount)" }, difference_level: "different" },
    dependencies: { a: ["ods_order"], b: ["ods_order"], intersection: ["ods_order"], only_a: [], only_b: [], difference_level: "identical" },
  },
};

function renderWorkbench() {
  return render(
    <MemoryRouter>
      <ReviewWorkbench />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedList.mockResolvedValue({
    items: conflicts,
    total: conflicts.length,
    page: 1,
    page_size: 20,
  } as ConflictListResponse);
  mockedCompare.mockResolvedValue(compareResult);
  mockedArbitrate.mockResolvedValue(baseConflict({ status: "RULED" }));
  mockedRulings.mockResolvedValue([]);
});

describe("ReviewWorkbench 冲突仲裁", () => {
  it("加载并渲染冲突列表", async () => {
    renderWorkbench();
    await waitFor(() => expect(screen.getByText("CF-A")).toBeInTheDocument());
    expect(screen.getByText("CF-B")).toBeInTheDocument();
    expect(screen.getByText("同名不同义")).toBeInTheDocument();
  });

  it("操作列按状态差异化：OPEN 提供对比/仲裁/升级", async () => {
    renderWorkbench();
    await waitFor(() => expect(screen.getByText("CF-A")).toBeInTheDocument());
    const row = screen.getByText("CF-A").closest("tr") as HTMLElement;
    // antd 按钮两字中文自动插空格（"仲裁"→"仲 裁"），用 role+正则匹配
    expect(within(row).getByRole("button", { name: /仲\s*裁/ })).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: /升\s*级/ })).toBeInTheDocument();
    expect(within(row).getByText("对比")).toBeInTheDocument();
  });

  it("操作列：ESCALATED 仅仲裁不升级，RULED 提供关闭", async () => {
    renderWorkbench();
    await waitFor(() => expect(screen.getByText("CF-B")).toBeInTheDocument());
    const escalatedRow = screen.getByText("CF-B").closest("tr") as HTMLElement;
    expect(within(escalatedRow).getByRole("button", { name: /仲\s*裁/ })).toBeInTheDocument();
    expect(within(escalatedRow).queryByRole("button", { name: /升\s*级/ })).not.toBeInTheDocument();

    const ruledRow = screen.getByText("CF-C").closest("tr") as HTMLElement;
    expect(within(ruledRow).getByRole("button", { name: /关\s*闭/ })).toBeInTheDocument();
    expect(within(ruledRow).queryByRole("button", { name: /仲\s*裁/ })).not.toBeInTheDocument();
  });

  it("RULED 冲突点击关闭调用 closeConflict 并刷新", async () => {
    renderWorkbench();
    await waitFor(() => expect(screen.getByText("CF-C")).toBeInTheDocument());
    const ruledRow = screen.getByText("CF-C").closest("tr") as HTMLElement;
    fireEvent.click(within(ruledRow).getByRole("button", { name: /关\s*闭/ }));
    await waitFor(() =>
      expect(mockedClose).toHaveBeenCalledWith("CF-C"),
    );
    expect(mockedList).toHaveBeenCalledTimes(2); // 初始 + 关闭后刷新
  });

  it("仲裁弹窗展示差异对比与裁决方式，默认按类型给出首个决策", async () => {
    renderWorkbench();
    await waitFor(() => expect(screen.getByText("CF-A")).toBeInTheDocument());
    const row = screen.getByText("CF-A").closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: /仲\s*裁/ }));

    // 弹窗出现
    await waitFor(() => expect(screen.getByText(/仲裁冲突 CF-A/)).toBeInTheDocument());
    // 差异对比被加载展示
    await waitFor(() => expect(mockedCompare).toHaveBeenCalledWith("sales_gmv_day", "sales_gmv_d"));
    expect(screen.getByText("粒度")).toBeInTheDocument();
    // 按类型给出裁决选项（同名异义 → 采纳现有为权威为默认）
    expect(screen.getByText("采纳现有为权威")).toBeInTheDocument();
    expect(screen.getByText("保留差异（非真冲突）")).toBeInTheDocument();
  });

  it("提交仲裁以默认决策调用后端（choose_canonical + 现有编码）", async () => {
    renderWorkbench();
    await waitFor(() => expect(screen.getByText("CF-A")).toBeInTheDocument());
    const row = screen.getByText("CF-A").closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: /仲\s*裁/ }));
    await waitFor(() => expect(screen.getByText(/仲裁冲突 CF-A/)).toBeInTheDocument());

    fireEvent.click(screen.getByText("提交裁决"));
    await waitFor(() =>
      expect(mockedArbitrate).toHaveBeenCalledWith("CF-A", "choose_canonical", "sales_gmv_d"),
    );
    expect(trackMock).toHaveBeenCalledWith("review_arbitrate", "CF-A", "conflict");
  });

  it("只读对比弹窗展示候选 vs 现有差异", async () => {
    renderWorkbench();
    await waitFor(() => expect(screen.getByText("CF-A")).toBeInTheDocument());
    const row = screen.getByText("CF-A").closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByText("对比"));

    await waitFor(() => expect(screen.getByText(/差异对比 CF-A/)).toBeInTheDocument());
    await waitFor(() => expect(mockedCompare).toHaveBeenCalledWith("sales_gmv_day", "sales_gmv_d"));
    expect(screen.getByText("粒度")).toBeInTheDocument();
  });

  it("PII 冲突不提供仲裁，展示已转交治理标记", async () => {
    const withPii = [...conflicts, baseConflict({ conflict_id: "CF-P", type: "pii", status: "OPEN" })];
    mockedList.mockResolvedValue({
      items: withPii,
      total: withPii.length,
      page: 1,
      page_size: 20,
    } as ConflictListResponse);
    renderWorkbench();
    await waitFor(() => expect(screen.getByText("CF-P")).toBeInTheDocument());
    const row = screen.getByText("CF-P").closest("tr") as HTMLElement;
    expect(within(row).getByText("已转交治理")).toBeInTheDocument();
    expect(within(row).queryByText("仲裁")).not.toBeInTheDocument();
  });

  it("RULED/CLOSED 冲突提供裁决记录入口，展示历史知识库条目", async () => {
    const rulings: RulingRecord[] = [
      {
        id: 1,
        conflict_id: "CF-C",
        metric_codes: { a: "sales_gmv_day", b: "sales_gmv_d" },
        dispute_desc: "同名不同义",
        decision: "choose_canonical",
        reason: "现有口径更符合业务定义",
        arbitrator_id: 42,
        decided_at: "2026-08-12T10:00:00",
      },
    ];
    mockedRulings.mockResolvedValue(rulings);
    renderWorkbench();
    await waitFor(() => expect(screen.getByText("CF-C")).toBeInTheDocument());
    const ruledRow = screen.getByText("CF-C").closest("tr") as HTMLElement;
    fireEvent.click(within(ruledRow).getByRole("button", { name: /裁决记录/ }));

    await waitFor(() => expect(mockedRulings).toHaveBeenCalledWith("CF-C"));
    expect(screen.getByText(/裁决记录 CF-C/)).toBeInTheDocument();
    expect(screen.getByText("choose_canonical")).toBeInTheDocument();
    expect(screen.getByText("现有口径更符合业务定义")).toBeInTheDocument();
    expect(screen.getByText("2026年8月12日 18:00")).toBeInTheDocument();
  });

  it("无裁决记录时展示空态提示", async () => {
    mockedRulings.mockResolvedValue([]);
    renderWorkbench();
    await waitFor(() => expect(screen.getByText("CF-C")).toBeInTheDocument());
    const ruledRow = screen.getByText("CF-C").closest("tr") as HTMLElement;
    fireEvent.click(within(ruledRow).getByRole("button", { name: /裁决记录/ }));
    await waitFor(() => expect(screen.getByText(/暂无裁决记录/)).toBeInTheDocument());
  });

  it("提供统一的返回按钮（返回上一入口）", async () => {
    renderWorkbench();
    await waitFor(() => expect(screen.getByText("CF-A")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/lineage", "/review"]}>
        <Routes>
          <Route path="/lineage" element={<div>lineage-page</div>} />
          <Route path="/review" element={<ReviewWorkbench />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("CF-A")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("lineage-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    render(
      <MemoryRouter initialEntries={["/review"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/review" element={<ReviewWorkbench />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("CF-A")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("dashboard-page");
  });
});
