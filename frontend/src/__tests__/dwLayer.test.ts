import { describe, expect, it } from "vitest";
import {
  deriveDwLayerFromCatalogName,
  dwLayerStroke,
} from "../utils/dwLayer";

describe("dwLayer 分层派生与取色——大小写不敏感", () => {
  it("deriveDwLayerFromCatalogName：表名/库名大小写任意均命中字典码", () => {
    // 表名大写 + 库段 dwd
    expect(deriveDwLayerFromCatalogName("WEDW_DWD.DW_ORDER_DF", ["dwd", "dim"])).toBe("dwd");
    // 库名大写带 dim、字典码大写
    expect(deriveDwLayerFromCatalogName("WEDW_DIM.HOSPITAL_DA", ["DIM", "dwd"])).toBe("dim");
    // 表名大写前缀 ods（库名不带层）
    expect(deriveDwLayerFromCatalogName("wedw.ODS_HIS_REGISTER", ["ods"])).toBe("ods");
    // 字典码混合大小写
    expect(deriveDwLayerFromCatalogName("wedw_dwd.dw_order_df", ["DWD", "Dim"])).toBe("dwd");
    // 未收录 → 未分层
    expect(deriveDwLayerFromCatalogName("WEDW_TMP.TMP_X", ["dwd"])).toBeNull();
  });

  it("dwLayerStroke：大小写归一化——大写层码命中标准色而非扩展色板", () => {
    const standard = dwLayerStroke("dws");
    // 标准层大写/混合大小写与全小写取色一致（命中 DW_LAYER_STANDARD_COLORS）
    expect(dwLayerStroke("DWS")).toBe(standard);
    expect(dwLayerStroke("Dws")).toBe(standard);
    // 标准色确认（dws = #6a1b9a 汇总层）
    expect(standard).toBe("#6a1b9a");
    // 扩展层大小写一致
    expect(dwLayerStroke("DIM")).toBe(dwLayerStroke("dim"));
  });
});
