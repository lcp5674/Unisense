/**
 * 数仓分层（dw_layer）字典驱动派生与配色（前端共享）。
 *
 * 后端以 ``system_dict`` 的 ``dw_layer`` 字典为唯一事实源，在图谱读路径按节点
 * ``库.表`` 名实时派生分层（见 ``services/system_dict/layers.py``）；主图（AssetGraph）
 * 直接消费后端下发的 ``dw_layer``，治理中心等拿不到下发字段的小视图用本模块的
 * ``deriveDwLayerFromCatalogName`` 按**同一匹配规则**本地派生——管理员补录/停用
 * 字典项后，各处分层口径自动一致，无需改前端硬编码白名单。
 */

//: 标准数仓分层配色（与后端字典码小写 key 对应；AssetGraph 主图泳道共用）。
export const DW_LAYER_STANDARD_COLORS: Record<string, string> = {
  ods: "#2e7d32", // 操作数据层（绿）
  dwd: "#1565c0", // 明细数据层（蓝）
  dws: "#6a1b9a", // 汇总数据层（紫）
  ads: "#ef6c00", // 应用数据层（橙）
  dm: "#00695c", // 数据集市（青）
};

//: 字典扩展层（非标准 5 层）兜底色板：按层名 hash 稳定取色。
export const DW_LAYER_EXTRA_PALETTE = [
  "#00838f", "#5d4037", "#c2185b", "#455a64", "#7cb342",
  "#8e24aa", "#00897b", "#f9a825", "#6d4c41", "#3949ab",
];

/** 分层配色：标准层取标准色；字典扩展层按层名 hash 从兜底色板稳定取色（同层跨渲染一致）。 */
export function dwLayerStroke(layer: string): string {
  const known = DW_LAYER_STANDARD_COLORS[layer];
  if (known) return known;
  let h = 0;
  for (let i = 0; i < layer.length; i += 1) h = (h * 31 + layer.charCodeAt(i)) >>> 0;
  return DW_LAYER_EXTRA_PALETTE[h % DW_LAYER_EXTRA_PALETTE.length];
}

/**
 * 按 ``库.表`` 名派生数仓分层码（小写），命中字典 active 码才返回（与后端
 * ``derive_dw_layer_from_catalog_name`` 同规则，保证治理中心与主图口径一致）：
 *
 * 1. 库名（``.`` 前段）按 ``_`` 拆段，**从右往左**找首个命中分层码的段
 *    （``wedw_dwd.xxx`` → ``dwd``；``wedw_dim.dim_x`` 且字典含 ``dim`` → ``dim``）。
 * 2. 库名未命中时回退表名前缀（``ods_``/``dwd.`` 等）。
 * 3. 都不命中返回 ``null``（保持「未分层表」语义）。
 *
 * ``activeCodes`` 为 dw_layer 字典的 active 编码集合（小写，可含管理员补录的
 * dim/mid/st/tmp 等扩展层）；传入空集/未提供时返回 ``null``（调用方归未分层）。
 */
export function deriveDwLayerFromCatalogName(
  entityName: string,
  activeCodes: Iterable<string> | null | undefined,
): string | null {
  if (!activeCodes) return null;
  const codes = new Set<string>();
  for (const c of activeCodes) {
    if (c) codes.add(String(c).toLowerCase());
  }
  if (!entityName || codes.size === 0) return null;
  const full = entityName.trim().toLowerCase();
  if (!full) return null;
  const [dbPart, tablePart] = full.split(".");
  for (const seg of [...dbPart.split("_").filter(Boolean)].reverse()) {
    if (codes.has(seg)) return seg;
  }
  if (tablePart) {
    for (const code of codes) {
      if (tablePart.startsWith(`${code}_`) || tablePart.startsWith(`${code}.`)) return code;
    }
  }
  return null;
}
