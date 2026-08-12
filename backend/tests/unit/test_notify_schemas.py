"""notify Schemas 校验测试（对齐 TD §12.9 / FR-16 / FR-17）。

回归：channel 大小写漂移曾导致幂等查询 miss + _dispatch 命中失败，
SubscriptionUpsert 现统一规范化到 NotifyChannel 枚举 value。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.notify.schemas import SubscriptionUpsert


class TestSubscriptionChannelNormalization:
    def test_lowercase_webhook_normalized_to_enum_value(self) -> None:
        sub = SubscriptionUpsert(
            user_id=1, channel="webhook", event_type="conflict.escalate", enabled=True
        )
        assert sub.channel == "WEBHOOK"

    def test_uppercase_webhook_preserved(self) -> None:
        sub = SubscriptionUpsert(user_id=1, channel="WEBHOOK", event_type="conflict.escalate")
        assert sub.channel == "WEBHOOK"

    def test_console_stays_lowercase(self) -> None:
        sub = SubscriptionUpsert(user_id=1, channel="console", event_type="quality.anomaly")
        assert sub.channel == "console"

    def test_uppercase_console_normalized_to_value(self) -> None:
        sub = SubscriptionUpsert(user_id=1, channel="CONSOLE", event_type="quality.anomaly")
        assert sub.channel == "console"

    def test_email_normalized(self) -> None:
        sub = SubscriptionUpsert(user_id=1, channel="email", event_type="q.refresh")
        assert sub.channel == "EMAIL"

    def test_dingtalk_normalized(self) -> None:
        sub = SubscriptionUpsert(user_id=1, channel="dingtalk", event_type="q.refresh")
        assert sub.channel == "DINGTALK"

    def test_invalid_channel_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SubscriptionUpsert(user_id=1, channel="slack", event_type="q.refresh")
