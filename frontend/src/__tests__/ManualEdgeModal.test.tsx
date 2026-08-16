import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { ManualEdgeModal, MANUAL_NODE_TYPE_OPTIONS } from "../components/lineage/ManualEdgeModal";
import * as api from "../api";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    addManualLineageEdge: vi.fn(),
    lineageNodes: vi.fn(),
  };
});

const apiMock = vi.mocked(api);

async function typeTarget(value: string) {
  // 弹窗含 3 个 combobox（登记方向 Select / 目标节点 AutoComplete / 边类型 Select），
  // 目标节点为第 2 个；渲染在 Modal portal，需从 document 查询。
  const inputs = Array.from(
    document.querySelectorAll<HTMLInputElement>(".ant-select-selection-search-input"),
  );
  const target = inputs[1];
  if (!target) throw new Error("目标节点输入框未渲染");
  await act(async () => {
    fireEvent.change(target, { target: { value } });
  });
}

describe("ManualEdgeModal 手动登记血缘边", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.addManualLineageEdge.mockResolvedValue({
      edge: {
        id: 1,
        source_node: "",
        target_node: "",
        edge_type: "DERIVED_FROM",
        granularity: "L1",
        confidence: 1,
        provenance: "manual",
        pii_inherited: false,
      },
      created: true,
    });
    apiMock.lineageNodes.mockResolvedValue([]);
  });

  it("渲染节点类型与所需信息提示（产品语义：每类节点需哪些信息）", () => {
    render(
      <ManualEdgeModal open onClose={() => {}} baseNode="table:ods.orders" />,
    );
    expect(screen.getByText(/人工登记血缘边/)).toBeTruthy();
    // 六类节点类型提示齐全
    expect(MANUAL_NODE_TYPE_OPTIONS).toHaveLength(6);
    for (const o of MANUAL_NODE_TYPE_OPTIONS) {
      expect(screen.getByText(o.hint)).toBeTruthy();
      expect(screen.getByText(o.example)).toBeTruthy();
    }
  });

  it("添加下游：登记「当前节点 → 目标节点」", async () => {
    render(<ManualEdgeModal open onClose={() => {}} baseNode="table:ods.orders" />);
    await typeTarget("metric:gmv_total");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "登记血缘边" }));
    });
    await waitFor(() =>
      expect(apiMock.addManualLineageEdge).toHaveBeenCalledWith(
        expect.objectContaining({
          source_node: "table:ods.orders",
          target_node: "metric:gmv_total",
        }),
      ),
    );
  });

  it("添加上游：登记「目标节点 → 当前节点」，且边类型推断为 DERIVED_FROM", async () => {
    render(
      <ManualEdgeModal
        open
        onClose={() => {}}
        baseNode="metric:gmv_total"
        defaultDirection="upstream"
      />,
    );
    await typeTarget("table:ods.orders");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "登记血缘边" }));
    });
    await waitFor(() =>
      expect(apiMock.addManualLineageEdge).toHaveBeenCalledWith(
        expect.objectContaining({
          source_node: "table:ods.orders",
          target_node: "metric:gmv_total",
          edge_type: "DERIVED_FROM",
        }),
      ),
    );
  });

  it("登记成功触发 onSuccess 回调", async () => {
    const onSuccess = vi.fn();
    const onClose = vi.fn();
    render(
      <ManualEdgeModal
        open
        onClose={onClose}
        onSuccess={onSuccess}
        baseNode="dimension:store"
      />,
    );
    await typeTarget("metric:gmv_total");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "登记血缘边" }));
    });
    await waitFor(() => expect(onSuccess).toHaveBeenCalled());
    expect(onClose).toHaveBeenCalled();
  });
});
