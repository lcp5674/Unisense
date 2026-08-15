/**
 * 中英术语字典前端单测（src/utils/zhEnDict.ts）。
 *
 * 与后端 `backend/tests/unit/test_zh_en_dict.py` 对齐：整段命中、贪心最长匹配、
 * 中文+ASCII 混合、未覆盖单字拼音兜底、非 CJK 合并。
 * slugifyCode 用例覆盖「编码自动生成」预览（主题域/字典项），含空 token 过滤。
 */

import { describe, expect, it, vi } from "vitest";
import { zhToEn, slugifyCode, resolveUniqueCode } from "../utils/zhEnDict";

// 模拟 pinyin-pro：仅对特殊字 "𠮷"（无字典命中且无拼音的极端场景）返回空串，
// 其余走真实实现——用于验证 slugifyCode 的空 token 过滤（复核观察③）。
vi.mock("pinyin-pro", async (importOriginal) => {
  const actual = await importOriginal<typeof import("pinyin-pro")>();
  return {
    ...actual,
    pinyin: (ch: string, opts: unknown) =>
      ch === "𠮷" ? "" : (actual.pinyin as (t: string, o?: unknown) => string)(ch, opts),
  };
});

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

describe("slugifyCode", () => {
  it("中文术语翻译（销售订单 → sales_order）", () => {
    expect(slugifyCode("销售订单")).toBe("sales_order");
  });

  it("ASCII + 中文混合（GMV销售订单 → gmv_sales_order）", () => {
    expect(slugifyCode("GMV销售订单")).toBe("gmv_sales_order");
  });

  it("纯标点/空白 → 空串（无可提取字符）", () => {
    expect(slugifyCode("!!!")).toBe("");
    expect(slugifyCode("  ")).toBe("");
    expect(slugifyCode("")).toBe("");
  });

  it("过滤空 token——无字典命中且无拼音的字不产生中段双下划线", () => {
    // "𠮷" 经 vi.mock 无拼音 → zhToEn 返回空串 → 中段空 token 应被过滤，
    // 对齐后端 codegen.slugify_code 的 `"_".join(t for t in tokens if t)`。
    expect(slugifyCode("A𠮷B")).toBe("a_b");
  });
});

describe("resolveUniqueCode", () => {
  it("无冲突返回 base", () => {
    expect(resolveUniqueCode("minute", ["daily", "weekly"])).toBe("minute");
  });

  it("冲突追加 _2 后缀", () => {
    expect(resolveUniqueCode("minute", ["daily", "minute"])).toBe("minute_2");
  });

  it("多级冲突自增（minute, minute_2 → minute_3）", () => {
    expect(resolveUniqueCode("minute", ["minute", "minute_2"])).toBe("minute_3");
  });

  it("超长 base 截断到 64（对齐后端 MAX_CODE_LEN）", () => {
    const long = "a".repeat(70);
    expect(resolveUniqueCode(long, []).length).toBe(64);
    // 截断后仍冲突 → base 截断 + _2 后缀（对齐 generate_unique_code 的
    // `base[: max_len - len(suffix)] + suffix`）
    expect(resolveUniqueCode(long, [long.slice(0, 64)])).toBe(`${"a".repeat(62)}_2`);
  });

  it("超上限回退 base（对齐后端 MAX_CODE_ATTEMPTS 防御）", () => {
    const used = new Set<string>(["x"]);
    for (let n = 2; n <= 100; n += 1) used.add(`x_${n}`);
    expect(resolveUniqueCode("x", used)).toBe("x");
  });
});
