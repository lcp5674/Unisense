// 通知未读数变更事件总线：通知中心内「标记已读/全部已读/删除/清空」等操作后广播，
// Layout 顶栏角标监听该事件实时刷新未读数——同页操作不触发路由变化，需事件驱动跨组件同步。
export const NOTIF_CHANGED_EVENT = "unisense:notif-changed";

export function notifyNotifChanged(): void {
  window.dispatchEvent(new CustomEvent(NOTIF_CHANGED_EVENT));
}

/** 订阅通知变更事件，返回取消订阅函数（供 useEffect 清理） */
export function onNotifChanged(handler: () => void): () => void {
  window.addEventListener(NOTIF_CHANGED_EVENT, handler);
  return () => window.removeEventListener(NOTIF_CHANGED_EVENT, handler);
}
