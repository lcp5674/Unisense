import { describe, expect, it } from "vitest";
import { computeDagrePositions } from "../components/assetmap/dagreLayout";

/** 便捷构造：节点 id + 可选渲染尺寸（默认 10，模拟 G6 antv-dagre defaultNodeSize）。 */
function nodesOf(ids: string[]): { id: string; size: number }[] {
  return ids.map((id) => ({ id, size: 10 }));
}

describe("computeDagrePositions（dagre 分层坐标，Web Worker/同步兜底共用）", () => {
  it("简单链 a→b→c：TB 方向 y 递增、同链 x 对齐", () => {
    const pos = computeDagrePositions(
      nodesOf(["a", "b", "c"]),
      [
        { source: "a", target: "b" },
        { source: "b", target: "c" },
      ],
      { rankdir: "TB", nodesep: 40, ranksep: 36, align: "DL" },
    );
    expect(pos.size).toBe(3);
    expect(pos.get("a")!.y).toBeLessThan(pos.get("b")!.y);
    expect(pos.get("b")!.y).toBeLessThan(pos.get("c")!.y);
    // 单链 dagre 会把三节点排在各自层中央，x 应一致（同一垂直线）
    expect(pos.get("a")!.x).toBeCloseTo(pos.get("c")!.x, 5);
  });

  it("分叉 a→b 与 a→c：b/c 同层（y 相近）、x 分开", () => {
    const pos = computeDagrePositions(
      nodesOf(["a", "b", "c"]),
      [
        { source: "a", target: "b" },
        { source: "a", target: "c" },
      ],
      { rankdir: "TB", nodesep: 40, ranksep: 36, align: "DL" },
    );
    expect(Math.abs(pos.get("b")!.y - pos.get("c")!.y)).toBeLessThan(1e-6);
    expect(pos.get("b")!.x).not.toBeCloseTo(pos.get("c")!.x, 3);
    expect(pos.get("a")!.y).toBeLessThan(pos.get("b")!.y);
  });

  it("LR 方向：x 递增（非 y）", () => {
    const pos = computeDagrePositions(
      nodesOf(["a", "b"]),
      [{ source: "a", target: "b" }],
      { rankdir: "LR", nodesep: 50, ranksep: 50, align: "UL" },
    );
    expect(pos.get("a")!.x).toBeLessThan(pos.get("b")!.x);
    expect(pos.get("a")!.y).toBeCloseTo(pos.get("b")!.y, 5);
  });

  it("真环 a↔b + c：不崩且全部节点有坐标（acyclicer greedy 翻转环边强制分层）", () => {
    const pos = computeDagrePositions(
      nodesOf(["a", "b", "c"]),
      [
        { source: "a", target: "b" },
        { source: "b", target: "a" },
        { source: "a", target: "c" },
      ],
      { rankdir: "TB", nodesep: 40, ranksep: 36, align: "DL" },
    );
    for (const id of ["a", "b", "c"]) {
      expect(pos.get(id)).toBeDefined();
      expect(Number.isFinite(pos.get(id)!.x)).toBe(true);
      expect(Number.isFinite(pos.get(id)!.y)).toBe(true);
    }
  });

  it("泳道锚点（size=1）与普通节点混合：小盒节点不异常", () => {
    const pos = computeDagrePositions(
      [
        { id: "__lane_ods__", size: 1 },
        { id: "table:o", size: [24, 14] },
        { id: "metric:m", size: 16 },
      ],
      [
        { source: "__lane_ods__", target: "table:o" },
        { source: "table:o", target: "metric:m" },
      ],
      { rankdir: "TB", nodesep: 40, ranksep: 36, align: "DL" },
    );
    expect(pos.size).toBe(3);
    expect(pos.get("__lane_ods__")!.y).toBeLessThan(pos.get("table:o")!.y);
  });

  it("孤立节点（无边）不崩且返回坐标", () => {
    const pos = computeDagrePositions(nodesOf(["lonely"]), [], {
      rankdir: "TB",
      nodesep: 40,
      ranksep: 36,
      align: "DL",
    });
    expect(pos.get("lonely")).toBeDefined();
    expect(Number.isFinite(pos.get("lonely")!.x)).toBe(true);
  });

  it("端点在节点集之外的边被防御性跳过（不抛错）", () => {
    const pos = computeDagrePositions(
      nodesOf(["a"]),
      [
        { source: "a", target: "ghost" },
        { source: "ghost", target: "a" },
      ],
      { rankdir: "TB", nodesep: 40, ranksep: 36, align: "DL" },
    );
    expect(pos.size).toBe(1);
  });

  it("大盒（折行长标签）使同层节点水平间距拉开（nodesep 语义生效）", () => {
    // 两颗同层节点，盒子越宽 → dagre 布局后 x 间距越大（节点盒含 2×sep padding）
    const wide = computeDagrePositions(
      [
        { id: "a", size: [200, 20] },
        { id: "b", size: [200, 20] },
      ],
      [],
      { rankdir: "TB", nodesep: 40, ranksep: 36, align: "DL" },
    );
    const narrow = computeDagrePositions(nodesOf(["a", "b"]), [], {
      rankdir: "TB",
      nodesep: 40,
      ranksep: 36,
      align: "DL",
    });
    const gapWide = Math.abs(wide.get("a")!.x - wide.get("b")!.x);
    const gapNarrow = Math.abs(narrow.get("a")!.x - narrow.get("b")!.x);
    expect(gapWide).toBeGreaterThan(gapNarrow);
  });
});
