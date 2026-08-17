/**
 * 细粒度权限管控前端基建（方案 A：自定义角色 + 按钮级权限点）。
 *
 * ``PermissionProvider`` 在登录后拉取 ``GET /me/permissions`` 快照（含
 * ``ui_actions``——该角色的全部 UI 权限点，默认基线 + ``role_permission`` 覆盖合并），
 * 通过 React Context 全局提供；``usePermission()`` 暴露 ``can/canAny/canAll``，
 * 供路由守卫（RequirePerm）、菜单显隐、页面 Tab 与按钮做**按权限点**的细粒度控制。
 *
 * 安全边界：本基建只控制**前端可见性/交互入口**；后端 API 层仍以
 * ``require_roles``（内置角色）与 PDP（资源级动作）做最终强制，二者不互相替代。
 * 快照未加载完成时 ``can`` 返回 ``true``（fail-open），避免首屏导航闪烁；
 * 后端强制保证无权限请求被拒绝。
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { fetchMyPermissions } from "../api";
import type { CurrentUser, PermissionSnapshot } from "../types";

export interface PermissionApi {
  /** 是否具备指定权限点（如 "metric:create" / "catalog:view"） */
  can: (perm: string) => boolean;
  /** 是否具备任一权限点 */
  canAny: (perms: string[]) => boolean;
  /** 是否具备全部权限点 */
  canAll: (perms: string[]) => boolean;
  snapshot: PermissionSnapshot | null;
  loading: boolean;
  /** 快照拉取是否失败（失败时 can() 仍 fail-open 放行、后端强制兜底） */
  error: boolean;
  /** 重新拉取权限快照（角色权限点被管理员调整后调用） */
  refresh: () => Promise<void>;
}

const PermissionContext = createContext<PermissionApi>({
  can: () => true,
  canAny: () => true,
  canAll: () => true,
  snapshot: null,
  loading: true,
  error: false,
  refresh: async () => undefined,
});

export function PermissionProvider({
  user,
  children,
}: {
  user: CurrentUser;
  children: ReactNode;
}) {
  const [snapshot, setSnapshot] = useState<PermissionSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const snap = await fetchMyPermissions();
      setSnapshot(snap);
      setError(false);
    } catch {
      // 快照拉取失败不阻断主流程：UI 兜底放行，后端强制仍生效
      setSnapshot(null);
      setError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    void refresh();
  }, [user?.id, refresh]);

  const value = useMemo<PermissionApi>(() => {
    const perms = new Set(snapshot?.ui_actions ?? []);
    return {
      can: (perm: string) => (snapshot === null ? true : perms.has(perm)),
      canAny: (permsList: string[]) =>
        snapshot === null ? true : permsList.some((p) => perms.has(p)),
      canAll: (permsList: string[]) =>
        snapshot === null ? true : permsList.every((p) => perms.has(p)),
      snapshot,
      loading,
      error,
      refresh,
    };
  }, [snapshot, loading, error, refresh]);

  return <PermissionContext.Provider value={value}>{children}</PermissionContext.Provider>;
}

export function usePermission(): PermissionApi {
  return useContext(PermissionContext);
}

/** 路由守卫：无权限点时重定向总览（替代按角色的 RequireRole）。 */
export function RequirePerm({ perm, children }: { perm: string; children: ReactNode }) {
  const { can } = usePermission();
  if (!can(perm)) {
    return <Navigate to="/dashboard" replace />;
  }
  return <>{children}</>;
}

/**
 * 页面级 view 权限点与路由路径映射（菜单/路由共用，保证一致）。
 * 键为路由 path，值为对应模块 view 权限点。
 */
export const ROUTE_PERM: Record<string, string> = {
  "/dashboard": "dashboard:view",
  "/todo": "todo:view",
  "/notifications": "notifications:view",
  "/favorites": "favorites:view",
  "/catalog": "catalog:view",
  "/compare": "compare:view",
  "/templates": "templates:view",
  "/create": "metric:create",
  "/metrics/review": "metric:review",
  "/assetmap": "assetmap:view",
  "/lineage": "lineage:view",
  "/review": "review:view",
  "/quality": "quality:view",
  "/dimensions": "dimensions:view",
  "/glossary": "glossary:view",
  "/query": "query:view",
  "/ai": "ai:view",
  "/feedback": "feedback:view",
  "/guide/:metricCode": "guide:view",
  "/data-sources": "data-sources:view",
  "/catalogs": "catalogs:view",
  "/collection-tasks": "collection-tasks:view",
  "/collection-history": "collection-history:view",
  "/domains": "domains:view",
  "/dicts": "dicts:view",
  "/sensitive-rules": "sensitive-rules:view",
  "/users": "users:view",
  "/organizations": "organizations:view",
  "/governance": "governance:view",
  "/audit": "audit:view",
  "/api-clients": "api-clients:view",
  "/system-config": "system-config:view",
  "/observability": "observability:view",
  "/tracking-stats": "tracking-stats:view",
};
