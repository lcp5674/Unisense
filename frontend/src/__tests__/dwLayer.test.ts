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

  it("deriveDwLayerFromCatalogName：整库名形态 code（wedw_dwd）命中同库", () => {
    // 字典 code 配整库名 wedw_dwd/wedw_ods → 库名直接命中（此前拆段找 dwd 而 miss）
    expect(
      deriveDwLayerFromCatalogName("wedw_dwd.dw_order_df", ["wedw_ods", "wedw_dwd", "dws"]),
    ).toBe("wedw_dwd");
    expect(
      deriveDwLayerFromCatalogName("wedw_ods.ods_his", ["wedw_ods", "wedw_dwd"]),
    ).toBe("wedw_ods");
  });

  it("deriveDwLayerFromCatalogName：整库名 code 匹配带子库后缀的库", () => {
    expect(
      deriveDwLayerFromCatalogName("wedw_dwd_bak.dw_order_df", ["wedw_dwd"]),
    ).toBe("wedw_dwd");
  });

  it("deriveDwLayerFromCatalogName：整库名与分段码并存——整库名更具体优先", () => {
    const mixed = ["wedw_dwd", "dwd", "dws"];
    // 整库名形态库 → wedw_dwd（不落 dwd）
    expect(deriveDwLayerFromCatalogName("wedw_dwd.dw_order_df", mixed)).toBe("wedw_dwd");
    // 无整库名 code 的 wedw_dws 库 → 拆段命中 dws
    expect(deriveDwLayerFromCatalogName("wedw_dws.dw_ord_df", mixed)).toBe("dws");
    // 纯分段码库名（无 wedw_ 前缀）仍走拆段
    expect(deriveDwLayerFromCatalogName("dwd.plain_table", mixed)).toBe("dwd");
  });

  it("deriveDwLayerFromCatalogName：整库名形态大小写不敏感且不误伤其它库", () => {
    expect(deriveDwLayerFromCatalogName("WEDW_DWD.DW_ORDER_DF", ["WEDW_DWD"])).toBe("wedw_dwd");
    expect(deriveDwLayerFromCatalogName("other_db.some_table", ["wedw_dwd"])).toBeNull();
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
