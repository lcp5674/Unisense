import { describe, it, expect } from "vitest";
import { parseBackendTime, formatCnTime, timeAgoCn } from "../utils/timeCn";

describe("timeCn 时间中文展示（强制上海时区）", () => {
  describe("parseBackendTime", () => {
    it("无时区偏移的 naive 串按 UTC 解析（后端落库 UTC）", () => {
      const d = parseBackendTime("2026-08-14T02:30:00");
      expect(d?.toISOString()).toBe("2026-08-14T02:30:00.000Z");
    });

    it("带 Z / 偏移后缀的串原样解析", () => {
      expect(parseBackendTime("2026-08-14T02:30:00Z")?.toISOString()).toBe("2026-08-14T02:30:00.000Z");
      expect(parseBackendTime("2026-08-14T10:30:00+08:00")?.toISOString()).toBe("2026-08-14T02:30:00.000Z");
    });

    it("空值与非法输入返回 null", () => {
      expect(parseBackendTime(null)).toBeNull();
      expect(parseBackendTime(undefined)).toBeNull();
      expect(parseBackendTime("")).toBeNull();
      expect(parseBackendTime("not-a-date")).toBeNull();
    });
  });

  describe("formatCnTime", () => {
    it("UTC 时间换算为上海时区中文格式（UTC+8）", () => {
      expect(formatCnTime("2026-08-14T02:30:00")).toBe("2026年8月14日 10:30");
      expect(formatCnTime("2026-08-14T02:30:00Z")).toBe("2026年8月14日 10:30");
      expect(formatCnTime("2026-08-14T10:30:00+08:00")).toBe("2026年8月14日 10:30");
    });

    it("跨日换算：UTC 前一日深夜 → 上海次日凌晨", () => {
      expect(formatCnTime("2026-08-13T16:00:00")).toBe("2026年8月14日 00:00");
    });

    it("非法输入返回占位符 —", () => {
      expect(formatCnTime("garbage")).toBe("—");
      expect(formatCnTime(null)).toBe("—");
    });
  });

  describe("timeAgoCn", () => {
    // 固定"当前时间"以便确定性断言（上海 2026-08-14 12:00 = UTC 04:00）
    const now = new Date("2026-08-14T04:00:00Z");

    it("一分钟内显示刚刚", () => {
      expect(timeAgoCn("2026-08-14T03:59:30Z", now)).toBe("刚刚");
    });

    it("分钟 / 小时级显示相对描述", () => {
      expect(timeAgoCn("2026-08-14T03:55:00Z", now)).toBe("5 分钟前");
      expect(timeAgoCn("2026-08-14T01:00:00Z", now)).toBe("3 小时前");
    });

    it("上海日历日的昨天显示昨天 HH:mm（含跨时区边界）", () => {
      // 上海 8-14 12:00 的"昨天"= 上海 8-13；UTC 8-13 23:30 = 上海 8-14 07:30，仍是"今天"
      // 用上海 8-13 上午的值验证：UTC 8-13 01:00 = 上海 8-13 09:00
      expect(timeAgoCn("2026-08-13T01:00:00Z", now)).toBe("昨天 09:00");
    });

    it("一周内显示 N 天前，更早回退绝对时间", () => {
      expect(timeAgoCn("2026-08-11T04:00:00Z", now)).toBe("3 天前");
      expect(timeAgoCn("2026-07-01T04:00:00Z", now)).toBe("2026年7月1日 12:00");
    });

    it("非法输入返回占位符 —", () => {
      expect(timeAgoCn("garbage", now)).toBe("—");
    });
  });
});
