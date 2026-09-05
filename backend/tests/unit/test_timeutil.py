"""时区工具测试（schedule_tz / now_schedule / today_schedule）。

覆盖：默认 Asia/Shanghai、env 覆盖、返回 aware、上海日历日与 UTC 边界差 8h。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.core import timeutil as tu


def test_schedule_tz_defaults_shanghai() -> None:
    """默认业务时区为 Asia/Shanghai（UTC+8）。"""
    assert str(tu.schedule_tz()) == "Asia/Shanghai"
    dt = datetime(2026, 1, 1, tzinfo=tu.schedule_tz())
    assert dt.utcoffset() == timedelta(hours=8)


def test_schedule_tz_respects_env(monkeypatch) -> None:
    """settings.schedule_timezone 可覆盖为其它 IANA 时区。"""
    monkeypatch.setattr(tu.settings, "schedule_timezone", "Asia/Tokyo")
    assert str(tu.schedule_tz()) == "Asia/Tokyo"


def test_now_schedule_is_aware_shanghai(monkeypatch) -> None:
    """now_schedule 返回带上海时区的 aware datetime（与容器 UTC 解耦）。"""
    monkeypatch.setattr(tu.settings, "schedule_timezone", "Asia/Shanghai")
    now = tu.now_schedule()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(hours=8)
    # 与 UTC now 之差不超过 1 秒（同一时刻两种表示）
    assert abs((now - datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai"))).total_seconds()) < 1


def test_today_schedule_shanghai_day_boundary(monkeypatch) -> None:
    """上海日历日边界：UTC 前一日 16:30（= 上海次日 00:30）归次日。"""
    monkeypatch.setattr(tu.settings, "schedule_timezone", "Asia/Shanghai")
    # now_schedule() 返回当前上海时刻；当上海为 2026-09-06 00:30 时，
    # UTC 仍是 2026-09-05 16:30 → 上海日历日应为 09-06（比 UTC 日早 8h 换日）。
    shanghai_midnight_next = datetime(2026, 9, 6, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    with patch("app.core.timeutil.datetime") as m:
        m.now.return_value = shanghai_midnight_next
        assert tu.today_schedule() == date(2026, 9, 6)
