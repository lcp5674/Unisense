// 时间中文展示工具 —— 强制上海时区（Asia/Shanghai, UTC+8），不随浏览器时区漂移。
// 后端统一以 UTC 落库（app/models/base.py TimestampMixin），MySQL DATETIME 返回无时区偏移的
// naive 值（Pydantic 序列化为无偏移 ISO 串），因此对无偏移字符串一律按 UTC 解析再换算为上海时区。
// 供可观测/运营等模块的时间列复用，避免 `new Date(v).toLocaleString("zh-CN")` 误用浏览器本地时区。

const SHANGHAI_TZ = "Asia/Shanghai";

/** 解析后端时间字符串为 Date；无时区偏移视为 UTC（后端落库为 UTC）。非法输入返回 null。 */
export function parseBackendTime(value: string | null | undefined): Date | null {
  if (!value) return null;
  const s = String(value).trim();
  if (!s) return null;
  const iso = /(Z|[+-]\d{2}:?\d{2})$/.test(s) ? s : `${s}Z`;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** 上海时区 HH:mm（24 小时制） */
function cnTimeOfDay(d: Date): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: SHANGHAI_TZ,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(d);
}

/** 上海时区 yyyy-MM-dd 键（用于"今天/昨天"日历日判断） */
function shanghaiDayKey(d: Date): string {
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: SHANGHAI_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(d);
  const p = (t: string) => parts.find((x) => x.type === t)?.value ?? "";
  return `${p("year")}-${p("month")}-${p("day")}`;
}

/** 上海时区绝对时间中文格式：2026年8月14日 14:30。非法输入返回 —。 */
export function formatCnTime(value: string | null | undefined): string {
  const d = parseBackendTime(value);
  if (!d) return "—";
  const date = new Intl.DateTimeFormat("zh-CN", {
    timeZone: SHANGHAI_TZ,
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(d);
  return `${date} ${cnTimeOfDay(d)}`;
}

/**
 * 上海时区相对时间中文描述：刚刚 / N 分钟前 / N 小时前 / 昨天 HH:mm / N 天前 / 更早回退绝对时间。
 * @param now 仅测试注入，默认当前时间。
 */
export function timeAgoCn(value: string | null | undefined, now: Date = new Date()): string {
  const d = parseBackendTime(value);
  if (!d) return "—";
  const diffMin = Math.floor((now.getTime() - d.getTime()) / 60_000);
  if (diffMin < 1) return "刚刚";
  if (diffMin < 60) return `${diffMin} 分钟前`;
  const diffHour = Math.floor(diffMin / 60);
  if (diffHour < 24) return `${diffHour} 小时前`;

  const targetKey = shanghaiDayKey(d);
  if (targetKey === shanghaiDayKey(new Date(now.getTime() - 86_400_000))) return `昨天 ${cnTimeOfDay(d)}`;
  if (diffHour < 24 * 7) return `${Math.floor(diffHour / 24)} 天前`;
  return formatCnTime(value);
}
