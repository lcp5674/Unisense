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

  it("放宽：不强制 4 段式——短编码（1~3 段）与多段编码均放行", () => {
    // 2026-09 放宽：通用标识符即可注册，4 段式仅为系统自动生成的命名建议
    expect(validateMetricCode("sales")).toBeNull();
    expect(validateMetricCode("fin_gmv")).toBeNull();
    expect(validateMetricCode("e2e_fee_day")).toBeNull();
    expect(validateMetricCode("online_consultation_wy_imageconsultcntday_day")).toBeNull();
    expect(validateMetricCode("a_b_c_d_e")).toBeNull();
  });

  it("非法字符/形态报错（大写/数字开头/非法字符/下划线卫生）", () => {
    expect(validateMetricCode("E2e_fee_day_1")).toBe("编码须以小写字母开头，仅含小写字母、数字和下划线");
    expect(validateMetricCode("e2e_fee_day_ABC")).toBe("编码须以小写字母开头，仅含小写字母、数字和下划线");
    expect(validateMetricCode("2sales_gmv_day")).toBe("编码须以小写字母开头，仅含小写字母、数字和下划线");
    expect(validateMetricCode("e2e-fee_day_1")).toBe("编码须以小写字母开头，仅含小写字母、数字和下划线");
    expect(validateMetricCode("_sales")).toBe("编码须以小写字母开头，仅含小写字母、数字和下划线");
    expect(validateMetricCode("sales_")).toBe("编码不能以下划线开头/结尾，且不允许连续下划线");
    expect(validateMetricCode("sales__gmv")).toBe("编码不能以下划线开头/结尾，且不允许连续下划线");
  });

  it("正则与历史 4 段式形态参考保持不变（仅作展示参考，校验已放宽）", () => {
    expect(METRIC_CODE_PATTERN.test("outpatient_fee_amount_day")).toBe(true);
    expect(METRIC_CODE_PATTERN.test("tpl_gmv_daily")).toBe(false);
  });
});
