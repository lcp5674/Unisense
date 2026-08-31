import { describe, expect, it } from "vitest";
import { validateMetricCode, METRIC_CODE_PATTERN } from "../utils/metricCode";

describe("validateMetricCode", () => {
  it("留空/未定义放行（由后端自动生成）", () => {
    expect(validateMetricCode(undefined)).toBeNull();
    expect(validateMetricCode(null)).toBeNull();
    expect(validateMetricCode("")).toBeNull();
    expect(validateMetricCode("  ")).toBeNull();
  });

  it("合法 4 段编码通过", () => {
    expect(validateMetricCode("outpatient_fee_amount_day")).toBeNull();
    expect(validateMetricCode("e2e_apidoc_fee_day")).toBeNull();
    expect(validateMetricCode("uncategorized_doctor_activecnt_month")).toBeNull();
  });

  it("域段可含下划线（字面 >4 段，后 3 段无下划线则合并为域段）", () => {
    // online_consultation 域编码本身含下划线 → 字面 5 段但语义 4 段，放行
    expect(validateMetricCode("online_consultation_wy_imageconsultcntday_day")).toBeNull();
    // a_b_c_d_e：域=a_b、业务对象=c、度量=d、周期=e，放行
    expect(validateMetricCode("a_b_c_d_e")).toBeNull();
  });

  it("段数不足报错（含具体段数）", () => {
    expect(validateMetricCode("e2e_fee_day")).toBe("须符合 4 段格式（域_业务对象_度量_统计周期），当前 3 段");
    expect(validateMetricCode("e2e_fee")).toBe("须符合 4 段格式（域_业务对象_度量_统计周期），当前 2 段");
    expect(validateMetricCode("e2e-fee_day_1")).toBe("须符合 4 段格式（域_业务对象_度量_统计周期），当前 3 段");
  });

  it("段格式非法报错（大写/非法字符/后 3 段含下划线）", () => {
    expect(validateMetricCode("E2e_fee_day_1")).toBe("第 1 段（域）格式错误：须小写字母开头+小写字母数字下划线");
    expect(validateMetricCode("e2e_fee_day_ABC")).toBe("第 4 段（统计周期）格式错误：须小写字母开头+小写字母数字下划线");
    // 多段但后 3 段含大写/数字开头 → 拒
    expect(validateMetricCode("a_b_c_d_E")).toBe("后 3 段（业务对象_度量_统计周期）格式错误：须小写字母开头+小写字母数字（无下划线）");
    expect(validateMetricCode("a_b_c_d_1e")).toBe("后 3 段（业务对象_度量_统计周期）格式错误：须小写字母开头+小写字母数字（无下划线）");
  });

  it("正则与后端 METRIC_CODE_PATTERN 对齐", () => {
    expect(METRIC_CODE_PATTERN.test("outpatient_fee_amount_day")).toBe(true);
    expect(METRIC_CODE_PATTERN.test("tpl_gmv_daily")).toBe(false);
    expect(METRIC_CODE_PATTERN.test("e2e_fee")).toBe(false);
  });
});
