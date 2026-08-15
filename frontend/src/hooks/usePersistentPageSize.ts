import { useCallback, useState } from "react";

/** 分页器每页条数可选项 */
export const PAGE_SIZE_OPTIONS = [10, 20, 50, 100] as const;

function readStored(key: string, fallback: number): number {
  try {
    const raw = localStorage.getItem(key);
    const n = raw ? Number.parseInt(raw, 10) : Number.NaN;
    return Number.isFinite(n) && n > 0 ? n : fallback;
  } catch {
    return fallback;
  }
}

/**
 * 每页条数 + 持久化。
 *
 * 抽屉/列表表格的分页器过去固定 pageSize（20 条/页）且无每页条数切换控件；
 * 本 hook 让用户可切换每页条数，并按 storageKey 持久化到 localStorage——
 * 切换 Tab / 重开抽屉 / 刷新页面后仍保持用户上一次的选择，不再重置回默认值。
 */
export function usePersistentPageSize(storageKey: string, fallback = 20) {
  const [pageSize, setPageSize] = useState(() => readStored(storageKey, fallback));

  const onShowSizeChange = useCallback(
    (_current: number, size: number) => {
      setPageSize(size);
      try {
        localStorage.setItem(storageKey, String(size));
      } catch {
        // 忽略 localStorage 不可用（隐私模式等）
      }
    },
    [storageKey],
  );

  return { pageSize, onShowSizeChange };
}
