import { useEffect, useState } from "react";

/**
 * 防抖值 hook：输入变化后延迟 delayMs 才更新返回值。
 * 用于远程检索场景——用户连续输入时不触发请求，停顿后才发起，
 * 避免每敲一个字符都打后端接口。
 */
export function useDebouncedValue<T>(value: T, delayMs = 350): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);

  return debounced;
}
