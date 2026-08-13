"""QuickBI 嵌入票据服务单测（FR-12）。

覆盖：签发结构、票据签名/校验往返、篡改拒绝、过期拒绝、
未配置密钥降级（ExternalDependencyError）。
"""

from __future__ import annotations

import time

import pytest

from app.core.exceptions import ExternalDependencyError
from app.services.semantic.quickbi import QuickBiService


@pytest.fixture
def service() -> QuickBiService:
    svc = QuickBiService.__new__(QuickBiService)
    svc._sign_key = "test-sign-key"
    svc._embed_base = "https://quickbi.aliyun.com/embed"
    return svc


class TestIssueTicket:
    def test_issue_structure(self, service: QuickBiService) -> None:
        data = service.issue_ticket("report_a", dashboard_id="db1", params={"date": "2026-01-01"})
        assert "ticket" in data
        assert "embed_url" in data
        assert "expires_at" in data
        assert data["embed_url"] == "https://quickbi.aliyun.com/embed"
        assert data["expires_at"] > int(time.time())
        # 票据含 report_id 负载与签名（用 . 分隔）
        assert "." in data["ticket"]

    def test_issue_no_dashboard_default_params(self, service: QuickBiService) -> None:
        data = service.issue_ticket("report_b")
        body = QuickBiService.verify_ticket(data["ticket"], "test-sign-key")
        assert body is not None
        assert body["report_id"] == "report_b"
        assert body["dashboard_id"] is None
        assert body["params"] == {}

    def test_issue_binds_actor_claims(self, service: QuickBiService) -> None:
        """票据绑定签发者身份声明（user_id/role/domain），供网关按用户收敛。"""
        data = service.issue_ticket(
            "report_pii",
            actor={"user_id": 7, "role": "analyst", "domain": "sales"},
        )
        body = QuickBiService.verify_ticket(data["ticket"], "test-sign-key")
        assert body is not None
        assert body["actor"] == {"user_id": 7, "role": "analyst", "domain": "sales"}
        # 签名保证 actor 声明不可篡改：改 user_id 后保留原签名 → 校验失败
        import base64
        import json

        payload, sig = data["ticket"].rsplit(".", 1)
        forged = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        forged["actor"] = {"user_id": 999, "role": "platform_admin", "domain": "hr"}
        forged_payload = base64.urlsafe_b64encode(
            json.dumps(forged, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).decode("ascii")
        # 使用原始签名（针对原 payload），篡改后必然不匹配
        assert (
            QuickBiService.verify_ticket(f"{forged_payload}.{sig}", "test-sign-key") is None
        )

    def test_issue_actor_only_whitelisted_keys(self, service: QuickBiService) -> None:
        """actor 仅纳入白名单键，调用方无法用 actor 覆盖 exp/iat。"""
        data = service.issue_ticket(
            "report_a",
            actor={"user_id": 1, "role": "analyst", "exp": 0, "iat": 0},
        )
        body = QuickBiService.verify_ticket(data["ticket"], "test-sign-key")
        assert body is not None
        assert body["actor"] == {"user_id": 1, "role": "analyst"}
        assert body["exp"] > int(time.time())  # exp 未被 actor 覆盖

    def test_issue_default_embed_base(self) -> None:
        svc = QuickBiService.__new__(QuickBiService)
        svc._sign_key = "k"
        svc._embed_base = ""
        data = svc.issue_ticket("r")
        assert data["embed_url"] == "https://quickbi.aliyun.com/embed"

    def test_issue_disabled_raises(self) -> None:
        svc = QuickBiService.__new__(QuickBiService)
        svc._sign_key = ""
        svc._embed_base = ""
        assert svc.enabled is False
        with pytest.raises(ExternalDependencyError):
            svc.issue_ticket("r")


class TestVerifyTicket:
    def test_roundtrip(self, service: QuickBiService) -> None:
        data = service.issue_ticket("report_c", dashboard_id="db2")
        body = QuickBiService.verify_ticket(data["ticket"], "test-sign-key")
        assert body is not None
        assert body["report_id"] == "report_c"
        assert body["dashboard_id"] == "db2"
        assert body["v"] == "v1"

    def test_tampered_rejected(self, service: QuickBiService) -> None:
        data = service.issue_ticket("report_c")
        assert QuickBiService.verify_ticket(data["ticket"] + "x", "test-sign-key") is None

    def test_wrong_key_rejected(self, service: QuickBiService) -> None:
        data = service.issue_ticket("report_c")
        assert QuickBiService.verify_ticket(data["ticket"], "other-key") is None

    def test_malformed_rejected(self) -> None:
        assert QuickBiService.verify_ticket("not-a-ticket", "k") is None
        assert QuickBiService.verify_ticket("", "k") is None

    def test_expired_rejected(self, service: QuickBiService) -> None:
        data = service.issue_ticket("report_c", ttl=-10)  # 已过期
        assert QuickBiService.verify_ticket(data["ticket"], "test-sign-key") is None
