/**
 * 全局用户名解析（跨组织中文名展示）。
 *
 * 背景：系统要求所有展示的用户名都是真实中文名（display_name||username），
 * 不允许出现「用户 #3」「未知用户」这类占位。而 /auth/users 多租户隔离
 * 只返回本组织用户，跨组织 owner/责任人/操作人无法从列表解析。
 *
 * 本模块是「订正逻辑」的单一事实来源：
 *  - 按已知 id 调用后端 /auth/users/by-ids（跨组织精确反查，不枚举目录）；
 *  - 模块级缓存完整 UserBrief（含 role/domain，供角色 Tag 展示），命中即返回；
 *  - 展示组件用 useUserNames(ids) 声明依赖，自动解析并随缓存更新重渲染；
 *  - 仅当用户记录确实不存在（已删除）才回退「未知用户」——这是数据缺失，
 *    不是权限可见性问题，与后端语义一致。
 */
import { useEffect, useMemo, useState } from "react";
import { resolveUserNames } from "../api";
import type { UserBrief } from "../types";

/** 模块级缓存：id → UserBrief（display_name/username/role/domain/status） */
const userCache = new Map<number, UserBrief>();

/** 正在拉取中的 id 集合（防并发重复请求） */
const inflight = new Set<number>();

/** 缓存写入（幂等合并） */
function remember(users: UserBrief[]) {
  for (const u of users) userCache.set(u.id, u);
}

/** 展示名：display_name 优先，回退 username */
export function nameOf(u: UserBrief | undefined): string | undefined {
  if (!u) return undefined;
  return u.display_name || u.username;
}

/**
 * 确保给定 id 的用户已解析。幂等：已缓存/已在途的跳过；
 * 拉取失败静默（组件以现有缓存兜底，不阻塞渲染）。
 */
export async function ensureUserNames(ids: Array<number | null | undefined>): Promise<void> {
  const missing = [...new Set(ids.filter((x): x is number => x != null && !userCache.has(x)))];
  if (!missing.length) return;
  const toFetch = missing.filter((id) => !inflight.has(id));
  if (!toFetch.length) return;
  toFetch.forEach((id) => inflight.add(id));
  try {
    const users = await resolveUserNames(toFetch);
    remember(users);
  } catch {
    // 网络/鉴权失败：保持未缓存，下次调用重试；调用方以现有缓存兜底
  } finally {
    toFetch.forEach((id) => inflight.delete(id));
  }
}

/** 同步读取缓存中的用户；未解析/不存在返回 undefined */
export function userBriefOf(id: number | null | undefined): UserBrief | undefined {
  if (id == null) return undefined;
  return userCache.get(id);
}

/** 同步读取缓存中的展示名；未解析/不存在返回 undefined */
export function userNameOf(id: number | null | undefined): string | undefined {
  return nameOf(userBriefOf(id));
}

/** 读取缓存中的展示名；未解析/不存在返回「未知用户」（仅用户记录缺失时出现） */
export function displayUserNameOf(id: number | null | undefined): string {
  return userNameOf(id) ?? "未知用户";
}

/**
 * React hook：声明依赖的用户 id 集合，自动解析并返回 id → UserBrief 映射。
 *
 * 用法：
 *   const userNames = useUserNames([metric.owner_id, metric.dw_developer_id]);
 *   ... userNames[metric.owner_id]?.display_name ?? "未知用户"
 *
 * 内部：ids 变化（去重）时触发 ensureUserNames，缓存更新后强制重渲染一次；
 * 纯展示组件无需自己维护 userMap state 与 listUsers 副作用。
 */
export function useUserNames(ids: Array<number | null | undefined>): Record<number, UserBrief> {
  const key = useMemo(
    () => [...new Set(ids.filter((x): x is number => x != null))].sort((a, b) => a - b).join(","),
    // ids 数组引用每次渲染都变，这里仅依赖「去重排序后的 key」避免循环
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [ids.map((x) => x ?? "").join(",")],
  );
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const target = key ? key.split(",").map(Number) : [];
    if (!target.length) return;
    let cancelled = false;
    ensureUserNames(target).then(() => {
      if (!cancelled) setTick((t) => t + 1);
    });
    return () => {
      cancelled = true;
    };
  }, [key]);

  const users = useMemo(() => {
    const m: Record<number, UserBrief> = {};
    if (key) {
      for (const id of key.split(",").map(Number)) {
        const u = userCache.get(id);
        if (u != null) m[id] = u;
      }
    }
    return m;
    // tick 变化时重算（缓存可能刚写入）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, tick]);

  return users;
}

/** 便捷：批量预热缓存（用于已有用户列表的场景，避免逐个 id 触发） */
export async function prewarmUserNames(users: UserBrief[]): Promise<void> {
  remember(users ?? []);
}
