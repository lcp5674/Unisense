"""通知渠道投递测试（对齐 US5 / FR-10）。

覆盖钉钉 Webhook 发送、邮件 SMTP 发送、渠道路由逻辑。
使用 mock 隔离外部依赖（HTTP 调用、SMTP 连接）。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.notify import Notification, NotifyStatus
from app.services.notify.service import NotifyService


def _make_notification(
    channel: str = "console",
    template_code: str = "quality.anomaly",
    title: str = "测试通知",
    body: str | None = None,
) -> Notification:
    """创建测试用 Notification 对象。"""
    notif = Notification()
    notif.id = 1
    notif.subscriber_id = 100
    notif.channel = channel
    notif.template_code = template_code
    notif.title = title
    notif.body = body or json.dumps({"message": "test"})
    notif.payload = {"message": "test"}
    notif.status = NotifyStatus.PENDING.value
    return notif


class TestDingtalkDispatch:
    """钉钉 Webhook 投递测试。"""

    @pytest.mark.asyncio
    async def test_dingtalk_dispatch_success(self):
        """钉钉 Webhook 投递成功。"""
        notif = _make_notification(channel="dingtalk", template_code="quality.anomaly")
        svc = NotifyService.__new__(NotifyService)
        svc._session = AsyncMock()
        svc._repo = AsyncMock()
        svc._http_client = AsyncMock()

        # Mock httpx response
        mock_response = MagicMock()
        mock_response.status_code = 200
        svc._http_client.post = AsyncMock(return_value=mock_response)

        with patch("app.services.notify.service.settings") as mock_settings:
            mock_settings.notify_dingtalk_webhook = "https://oapi.dingtalk.com/robot/send?access_token=test"
            result = await svc._dispatch_dingtalk(notif)

        assert result is True
        svc._http_client.post.assert_called_once()
        call = svc._http_client.post.call_args
        # httpx.post(url, json=..., headers=...) 中 url 是位置参数
        url = call.args[0] if call.args else call.kwargs.get("url", "")
        assert "oapi.dingtalk.com" in url

    @pytest.mark.asyncio
    async def test_dingtalk_dispatch_no_webhook_configured(self):
        """未配置钉钉 Webhook 时返回 False。"""
        notif = _make_notification(channel="dingtalk")
        svc = NotifyService.__new__(NotifyService)
        svc._http_client = AsyncMock()

        with patch("app.services.notify.service.settings") as mock_settings:
            mock_settings.notify_dingtalk_webhook = ""
            result = await svc._dispatch_dingtalk(notif)

        assert result is False

    @pytest.mark.asyncio
    async def test_dingtalk_dispatch_http_error(self):
        """钉钉 Webhook HTTP 错误时返回 False。"""
        notif = _make_notification(channel="dingtalk")
        svc = NotifyService.__new__(NotifyService)
        svc._http_client = AsyncMock()

        mock_response = MagicMock()
        mock_response.status_code = 500
        svc._http_client.post = AsyncMock(return_value=mock_response)

        with patch("app.services.notify.service.settings") as mock_settings:
            mock_settings.notify_dingtalk_webhook = "https://oapi.dingtalk.com/robot/send?access_token=test"
            result = await svc._dispatch_dingtalk(notif)

        assert result is False

    @pytest.mark.asyncio
    async def test_dingtalk_quality_anomaly_template(self):
        """质量异常使用 markdown 告警模板。"""
        notif = _make_notification(
            channel="dingtalk",
            template_code="quality.anomaly",
            title="质量异常",
            body="检测到数据漂移",
        )
        svc = NotifyService.__new__(NotifyService)
        svc._http_client = AsyncMock()

        mock_response = MagicMock()
        mock_response.status_code = 200
        svc._http_client.post = AsyncMock(return_value=mock_response)

        with patch("app.services.notify.service.settings") as mock_settings:
            mock_settings.notify_dingtalk_webhook = "https://oapi.dingtalk.com/robot/send?access_token=test"
            await svc._dispatch_dingtalk(notif)

        call_kwargs = svc._http_client.post.call_args
        body = call_kwargs.kwargs.get("json", call_kwargs[1].get("json", {}))
        assert body["msgtype"] == "markdown"
        assert "质量异常告警" in body["markdown"]["title"]

    @pytest.mark.asyncio
    async def test_dingtalk_review_pending_template(self):
        """审核待办使用 markdown 待办模板。"""
        notif = _make_notification(
            channel="dingtalk",
            template_code="review.pending",
            title="审核待办",
        )
        svc = NotifyService.__new__(NotifyService)
        svc._http_client = AsyncMock()

        mock_response = MagicMock()
        mock_response.status_code = 200
        svc._http_client.post = AsyncMock(return_value=mock_response)

        with patch("app.services.notify.service.settings") as mock_settings:
            mock_settings.notify_dingtalk_webhook = "https://oapi.dingtalk.com/robot/send?access_token=test"
            await svc._dispatch_dingtalk(notif)

        call_kwargs = svc._http_client.post.call_args
        body = call_kwargs.kwargs.get("json", call_kwargs[1].get("json", {}))
        assert body["msgtype"] == "markdown"
        assert "审核待办" in body["markdown"]["title"]

    @pytest.mark.asyncio
    async def test_dingtalk_conflict_escalation_template(self):
        """冲突升级使用 markdown 紧急模板。"""
        notif = _make_notification(
            channel="dingtalk",
            template_code="conflict.escalate",
            title="冲突升级",
        )
        svc = NotifyService.__new__(NotifyService)
        svc._http_client = AsyncMock()

        mock_response = MagicMock()
        mock_response.status_code = 200
        svc._http_client.post = AsyncMock(return_value=mock_response)

        with patch("app.services.notify.service.settings") as mock_settings:
            mock_settings.notify_dingtalk_webhook = "https://oapi.dingtalk.com/robot/send?access_token=test"
            await svc._dispatch_dingtalk(notif)

        call_kwargs = svc._http_client.post.call_args
        body = call_kwargs.kwargs.get("json", call_kwargs[1].get("json", {}))
        assert body["msgtype"] == "markdown"
        assert "冲突升级" in body["markdown"]["title"]


class TestEmailDispatch:
    """邮件 SMTP 投递测试。"""

    @pytest.mark.asyncio
    async def test_email_dispatch_success(self):
        """邮件 SMTP 投递成功。"""
        notif = _make_notification(channel="email", template_code="review.pending")
        svc = NotifyService.__new__(NotifyService)
        svc._session = AsyncMock()
        svc._repo = AsyncMock()
        svc._http_client = AsyncMock()

        with (
            patch("app.services.notify.service.settings") as mock_settings,
            patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send,
        ):
            mock_settings.notify_smtp_host = "smtp.example.com"
            mock_settings.notify_smtp_port = 587
            mock_settings.notify_smtp_user = "noreply@unisense.local"
            mock_settings.notify_smtp_password = "test-password"

            result = await svc._dispatch_email(notif)

        assert result is True
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_email_dispatch_no_smtp_configured(self):
        """未配置 SMTP 主机时返回 False。"""
        notif = _make_notification(channel="email")
        svc = NotifyService.__new__(NotifyService)

        with patch("app.services.notify.service.settings") as mock_settings:
            mock_settings.notify_smtp_host = ""
            result = await svc._dispatch_email(notif)

        assert result is False

    @pytest.mark.asyncio
    async def test_email_dispatch_smtp_error(self):
        """SMTP 连接错误时返回 False。"""
        notif = _make_notification(channel="email")
        svc = NotifyService.__new__(NotifyService)

        with (
            patch("app.services.notify.service.settings") as mock_settings,
            patch(
                "aiosmtplib.send",
                new_callable=AsyncMock,
                side_effect=Exception("Connection refused"),
            ),
        ):
            mock_settings.notify_smtp_host = "smtp.example.com"
            mock_settings.notify_smtp_port = 587
            mock_settings.notify_smtp_user = "test"
            mock_settings.notify_smtp_password = "test"

            result = await svc._dispatch_email(notif)

        assert result is False


class TestChannelRouting:
    """渠道路由逻辑测试。"""

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_dingtalk(self):
        """dingtalk 渠道路由到 _dispatch_dingtalk。"""
        notif = _make_notification(channel="dingtalk")
        svc = NotifyService.__new__(NotifyService)
        svc._session = AsyncMock()
        svc._http_client = AsyncMock()

        with patch.object(
            svc, "_dispatch_dingtalk", new_callable=AsyncMock, return_value=True
        ) as mock:
            result = await svc._dispatch(notif, "dingtalk")

        assert result is True
        mock.assert_called_once_with(notif)

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_email(self):
        """email 渠道路由到 _dispatch_email。"""
        notif = _make_notification(channel="email")
        svc = NotifyService.__new__(NotifyService)

        with patch.object(
            svc, "_dispatch_email", new_callable=AsyncMock, return_value=True
        ) as mock:
            result = await svc._dispatch(notif, "email")

        assert result is True
        mock.assert_called_once_with(notif)

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_webhook(self):
        """webhook 渠道路由到 _dispatch_webhook。"""
        notif = _make_notification(channel="webhook")
        svc = NotifyService.__new__(NotifyService)
        svc._http_client = AsyncMock()

        with patch.object(
            svc, "_dispatch_webhook", new_callable=AsyncMock, return_value=True
        ) as mock:
            result = await svc._dispatch(notif, "webhook")

        assert result is True
        mock.assert_called_once_with(notif)

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_console(self):
        """console 渠道输出日志。"""
        notif = _make_notification(channel="console")
        svc = NotifyService.__new__(NotifyService)

        result = await svc._dispatch(notif, "console")
        assert result is True

    @pytest.mark.asyncio
    async def test_dispatch_unknown_channel(self):
        """未知渠道返回 False。"""
        notif = _make_notification(channel="unknown_channel")
        svc = NotifyService.__new__(NotifyService)

        result = await svc._dispatch(notif, "unknown_channel")
        assert result is False

    @pytest.mark.asyncio
    async def test_dispatch_exception_handling(self):
        """投递异常时返回 False（不抛出）。"""
        notif = _make_notification(channel="webhook")
        svc = NotifyService.__new__(NotifyService)

        with patch.object(
            svc, "_dispatch_webhook", new_callable=AsyncMock, side_effect=RuntimeError("boom")
        ):
            result = await svc._dispatch(notif, "webhook")

        assert result is False
