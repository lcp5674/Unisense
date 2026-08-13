"""SEC-04 日期上下文守卫回归测试。"""

from app.core.guard import _is_suspicious


def test_date_not_blocked():
    assert not _is_suspicious("2024-01-01")
    assert not _is_suspicious("WHERE dt = '2024-12-31'")
