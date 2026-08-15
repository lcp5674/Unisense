import { createContext, useContext, type ReactNode } from "react";
import { trackEvent } from "../api";
import type { CurrentUser } from "../types";
import { useAppStore } from "../store";
import { dedupKey, trackingDedup } from "../utils/trackingDedup";

// 字段长度上限（对齐后端 TrackEventRequest schema 与 tracking_event 表列长度）：
// 超长 target_id（如血缘节点 id ``table:库.表``）会让后端 pydantic 校验失败返回 422，
// 埋点是 fire-and-forget 统计，不应因输入长而失败，故发送前截断。
const _MAX_EVENT_TYPE = 32;
const _MAX_TARGET_ID = 36;
const _MAX_TARGET_TYPE = 32;

interface TrackingContextValue {
  track: (
    eventType: string,
    targetId?: string,
    targetType?: string,
    context?: Record<string, unknown>,
  ) => void;
}

const TrackingContext = createContext<TrackingContextValue>({
  track: () => {},
});

interface TrackingProviderProps {
  user: CurrentUser;
  children: ReactNode;
}

export function TrackingProvider({ user, children }: TrackingProviderProps) {
  const addEvent = useAppStore((s) => s.addEvent);

  function track(
    eventType: string,
    targetId?: string,
    targetType?: string,
    context?: Record<string, unknown>,
  ) {
    // 同事件节流：窗口内重复触发直接丢弃，防止测试/快速刷新刷爆统计
    if (!trackingDedup.shouldSend(dedupKey(eventType, targetId, targetType), Date.now())) return;

    // 长度钳制：后端 schema/DB 列有上限，超长值直接截断（埋点统计不需完整原文）
    const eventTypeClipped = eventType.slice(0, _MAX_EVENT_TYPE);
    const targetIdClipped = targetId?.slice(0, _MAX_TARGET_ID);
    const targetTypeClipped = targetType?.slice(0, _MAX_TARGET_TYPE);

    const event = {
      event_type: eventTypeClipped,
      target_id: targetIdClipped,
      target_type: targetTypeClipped,
      context,
      timestamp: Date.now(),
    };

    // Store locally
    addEvent(event);

    // Fire-and-forget to backend
    trackEvent({
      event_type: eventTypeClipped,
      target_id: targetIdClipped,
      target_type: targetTypeClipped,
      context: { ...context, actor_id: user.id, actor_role: user.role },
    }).catch(() => {
      // Silently ignore tracking errors
    });
  }

  return (
    <TrackingContext.Provider value={{ track }}>
      {children}
    </TrackingContext.Provider>
  );
}

export function useTrackingContext() {
  return useContext(TrackingContext);
}
