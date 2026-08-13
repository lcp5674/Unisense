/**
 * 中英术语字典前端单测（src/utils/zhEnDict.ts）。
 *
 * 与后端 `backend/tests/unit/test_zh_en_dict.py` 对齐：整段命中、贪心最长匹配、
 * 中文+ASCII 混合、未覆盖单字拼音兜底、非 CJK 合并。
 */

import { describe, expect, it } from "vitest";
import { zhToEn } from "../utils/zhEnDict";

describe("zhToEn", () => {
  it("整段命中（供应链 → supply_chain）", () => {
    expect(zhToEn("供应链")).toBe("supply_chain");
  });

  it("贪心最长匹配分词（销售订单 → sales_order）", () => {
    expect(zhToEn("销售订单")).toBe("sales_order");
  });

  it("中文+ASCII 混合（销售订单GMV → sales_order_gmv）", () => {
    expect(zhToEn("销售订单GMV")).toBe("sales_order_gmv");
  });

  it("未覆盖单字拼音兜底（钿 → dian）", () => {
    expect(zhToEn("钿")).toBe("dian");
  });

  it("覆盖词+未覆盖字混合（销售钿 → sales_dian）", () => {
    expect(zhToEn("销售钿")).toBe("sales_dian");
  });

  it("空串 → 空串", () => {
    expect(zhToEn("")).toBe("");
  });

  it("非 CJK 连续字符合并小写", () => {
    expect(zhToEn("123")).toBe("123");
  });
});
