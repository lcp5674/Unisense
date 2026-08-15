import { describe, expect, it, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { usePersistentPageSize, PAGE_SIZE_OPTIONS } from "../hooks/usePersistentPageSize";

describe("usePersistentPageSize", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("默认返回 fallback 条数（20）", () => {
    const { result } = renderHook(() => usePersistentPageSize("t.pageSize"));
    expect(result.current.pageSize).toBe(20);
  });

  it("localStorage 有值时优先读取", () => {
    localStorage.setItem("t.pageSize", "50");
    const { result } = renderHook(() => usePersistentPageSize("t.pageSize"));
    expect(result.current.pageSize).toBe(50);
  });

  it("切换条数后写入 localStorage 并更新 state", () => {
    const { result } = renderHook(() => usePersistentPageSize("t.pageSize"));
    act(() => {
      result.current.onShowSizeChange(1, 100);
    });
    expect(result.current.pageSize).toBe(100);
    expect(localStorage.getItem("t.pageSize")).toBe("100");
  });

  it("localStorage 存了非法值（0/负数/NaN）时回退默认", () => {
    localStorage.setItem("t.pageSize", "0");
    const a = renderHook(() => usePersistentPageSize("t.pageSize"));
    expect(a.result.current.pageSize).toBe(20);

    localStorage.setItem("t.pageSize", "abc");
    const b = renderHook(() => usePersistentPageSize("t.pageSize"));
    expect(b.result.current.pageSize).toBe(20);
  });

  it("不同 storageKey 互不干扰", () => {
    localStorage.setItem("a.pageSize", "10");
    const a = renderHook(() => usePersistentPageSize("a.pageSize"));
    const b = renderHook(() => usePersistentPageSize("b.pageSize"));
    expect(a.result.current.pageSize).toBe(10);
    expect(b.result.current.pageSize).toBe(20);
  });

  it("导出每页条数可选项（10/20/50/100）", () => {
    expect([...PAGE_SIZE_OPTIONS]).toEqual([10, 20, 50, 100]);
  });
});
