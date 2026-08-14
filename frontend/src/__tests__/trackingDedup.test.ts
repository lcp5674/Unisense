import { describe, it, expect } from "vitest";
import { createTrackingDedup, dedupKey } from "../utils/trackingDedup";

describe("trackingDedup 同事件节流", () => {
  it("窗口内首次放行，重复触发丢弃", () => {
    const dedup = createTrackingDedup(10_000);
    expect(dedup.shouldSend("k", 1000)).toBe(true);
    expect(dedup.shouldSend("k", 5000)).toBe(false);
    expect(dedup.shouldSend("k", 10_999)).toBe(false);
  });

  it("窗口过后再次放行", () => {
    const dedup = createTrackingDedup(10_000);
    dedup.shouldSend("k", 0);
    expect(dedup.shouldSend("k", 10_000)).toBe(true); // 恰好在窗口边界
    expect(dedup.shouldSend("k", 15_000)).toBe(false); // 又进入新窗口
    dedup.shouldSend("k", 25_000);
    expect(dedup.shouldSend("k", 35_000)).toBe(true);
  });

  it("不同 key 互不影响", () => {
    const dedup = createTrackingDedup(10_000);
    expect(dedup.shouldSend("a", 0)).toBe(true);
    expect(dedup.shouldSend("b", 0)).toBe(true);
    expect(dedup.shouldSend("a", 5000)).toBe(false);
    expect(dedup.shouldSend("b", 5000)).toBe(false);
    expect(dedup.shouldSend("c", 5000)).toBe(true);
  });

  it("dedupKey 区分 event_type 与 target", () => {
    expect(dedupKey("view", undefined, "dashboard")).toBe("view:dashboard:");
    expect(dedupKey("view", "m1", "metric")).toBe("view:metric:m1");
    expect(dedupKey("view", "m1", "metric")).not.toBe(dedupKey("view", "m2", "metric"));
    expect(dedupKey("view", "m1", "metric")).not.toBe(dedupKey("favorite", "m1", "metric"));
  });
});
