import { createContext, useContext, type ReactNode } from "react";
import { trackEvent } from "../api";
import type { CurrentUser } from "../types";
import { useAppStore } from "../store";
import { dedupKey, trackingDedup } from "../utils/trackingDedup";

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

    const event = {
      event_type: eventType,
      target_id: targetId,
      target_type: targetType,
      context,
      timestamp: Date.now(),
    };

    // Store locally
    addEvent(event);

    // Fire-and-forget to backend
    trackEvent({
      event_type: eventType,
      target_id: targetId,
      target_type: targetType,
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
