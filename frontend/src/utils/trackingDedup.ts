// 同事件节流：同一事件 key 在窗口内重复触发仅放行一次。
// 用于防止自动化测试/快速刷新/异常循环刷爆统计埋点（TrackingProvider 使用）。
// 真实用户在同一对象上的秒级重复访问可忽略，10s 窗口不会造成统计失真。
// 工厂 + 注入 now，便于单元测试精确控制时间。

export interface TrackingDedup {
  shouldSend: (key: string, now: number) => boolean;
}

export function createTrackingDedup(windowMs: number): TrackingDedup {
  const lastSentAt = new Map<string, number>();
  return {
    shouldSend(key, now) {
      const last = lastSentAt.get(key);
      if (last === undefined) {
        lastSentAt.set(key, now);
        return true;
      }
      if (now - last < windowMs) return false;
      lastSentAt.set(key, now);
      return true;
    },
  };
}

export const trackingDedup = createTrackingDedup(10_000);

/** 事件去重键：event_type + target_type + target_id 共同标识同一事件。 */
export function dedupKey(eventType: string, targetId?: string, targetType?: string): string {
  return `${eventType}:${targetType ?? ""}:${targetId ?? ""}`;
}
