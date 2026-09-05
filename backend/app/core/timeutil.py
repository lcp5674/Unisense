"""时区工具：业务钟点/日历日统一走可配置时区（默认 Asia/Shanghai）。

系统约定（TD §4.1 / models.base.TimestampMixin）：
- **存储与绝对时刻比较一律 UTC**（ORM default=datetime.now(UTC)，MySQL DATETIME 存 UTC 墙钟）；
- **业务钟点/自然日语义（cron 触发、日配额换日、统计日聚、废弃生效日）**由本模块
  提供 ``schedule_tz()`` / ``now_schedule()`` / ``today_schedule()`` 统一判定，
  基准来自 ``settings.schedule_timezone``（默认 Asia/Shanghai），与容器时区（UTC）解耦，
  避免「配置 0 13 * * * 实际按 UTC 13 点触发」的 8 小时错位。

    Examples:
        >>> from app.core.timeutil import now_schedule, today_schedule
        >>> now_schedule().tzinfo  # doctest: +SKIP
        zoneinfo.ZoneInfo(key='Asia/Shanghai')
        >>> today_schedule()  # 上海日历日（YYYY-MM-DD 的 date）
"""

from __future__ import annotations

from datetime import date, datetime, tzinfo
from zoneinfo import ZoneInfo

from app.core.config import settings

#: 时区解析缓存（settings 惰性单例，多 worker 各自解析一次即可复用）。
_tz_cache: dict[str, tzinfo] = {}


def schedule_tz() -> tzinfo:
    """返回业务调度时区（默认 Asia/Shanghai），带缓存。

    Returns:
        IANA 时区对象。
    """
    name = settings.schedule_timezone or "Asia/Shanghai"
    tz = _tz_cache.get(name)
    if tz is None:
        tz = ZoneInfo(name)
        _tz_cache[name] = tz
    return tz


def now_schedule() -> datetime:
    """当前时刻（带业务时区，aware）。

    Returns:
        ``datetime``（aware，Asia/Shanghai）。
    """
    return datetime.now(schedule_tz())


def today_schedule() -> date:
    """当前业务自然日（上海日历日）。

    Returns:
        仅含年/月/日的 ``date``。
    """
    return now_schedule().date()
