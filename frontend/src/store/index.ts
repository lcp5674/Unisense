import { create } from "zustand";
import type { CurrentUser } from "../types";

// ---- Dashboard State ----
interface DashboardData {
  total_metrics: number;
  published_count: number;
  draft_count: number;
  deprecated_count: number;
  conflict_count: number;
  review_pending_count: number;
  avg_review_hours: number;
  pii_metric_count: number;
  quality_anomaly_count: number;
  top_domains: Array<{ domain: string; count: number }>;
}

// ---- Tracking State ----
interface TrackingEvent {
  event_type: string;
  target_id?: string;
  target_type?: string;
  context?: Record<string, unknown>;
  timestamp: number;
}

// ---- Store State ----
interface AppStore {
  // Auth
  user: CurrentUser | null;
  setUser: (user: CurrentUser | null) => void;

  // Dashboard
  dashboard: DashboardData | null;
  dashboardLoading: boolean;
  dashboardError: string | null;
  setDashboard: (data: DashboardData) => void;
  setDashboardLoading: (loading: boolean) => void;
  setDashboardError: (error: string | null) => void;

  // Tracking
  recentEvents: TrackingEvent[];
  addEvent: (event: TrackingEvent) => void;

  // Global error
  lastError: string | null;
  setLastError: (error: string | null) => void;
}

export const useAppStore = create<AppStore>((set) => ({
  // Auth
  user: null,
  setUser: (user) => set({ user }),

  // Dashboard
  dashboard: null,
  dashboardLoading: false,
  dashboardError: null,
  setDashboard: (data) => set({ dashboard: data, dashboardLoading: false, dashboardError: null }),
  setDashboardLoading: (loading) => set({ dashboardLoading: loading }),
  setDashboardError: (error) => set({ dashboardError: error, dashboardLoading: false }),

  // Tracking
  recentEvents: [],
  addEvent: (event) =>
    set((state) => ({
      recentEvents: [...state.recentEvents.slice(-99), event],
    })),

  // Global error
  lastError: null,
  setLastError: (error) => set({ lastError: error }),
}));
