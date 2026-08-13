"""Review regression tests for consume / ai / notify industrial-grade gaps.

Each test documents a concrete defect found during review. When run against the
current (pre-fix) code, a subset intentionally FAILS — that failure is the
evidence for the corresponding finding. Once the proposed fix is applied, the
same test asserts the fixed (correct) behaviour.

Defects covered (see review report):
- D1: ai.service.ask(execute=True) builds a NEW OLAPExecutor per call (HTTP
      connection-pool leak), bypassing the shared connectivity pool.
- D2: ai.service._is_unsafe uses a substring blocklist that is bypassable
      (UNION/**/SELECT falls through) on LLM-generated, un-parameterized SQL.
- D3: notify.service._dispatch_email falls back to a placeholder recipient and
      still returns True (marked SENT) when the subscriber has no email.
- D4: notify repository/svc list_notifications is unbounded (no LIMIT).
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.ai.service import AiService
from app.services.notify.repository import NotifyRepository
from app.services.notify.service import NotifyService

# ---------------------------------------------------------------------------
# D1 — ai: OLAPExecutor per-call instantiation (connection-pool leak)
# ---------------------------------------------------------------------------


def _enabled_llm() -> MagicMock:
    llm = MagicMock()
    llm.enabled = True
    # returns a valid, non-empty SELECT so nl2sql resolves to a safe result
    llm.chat = AsyncMock(return_value={"content": "SELECT 1 FROM unified_metric"})
    return llm


async def _ai_svc(vocab: set[str]) -> tuple[AiService, MagicMock]:
    db = MagicMock()
    svc = AiService(db, llm=_enabled_llm())
    repo = MagicMock()
    repo.vocabulary = AsyncMock(return_value=vocab)
    svc._repo = repo  # noqa: SLF001
    return svc, repo


async def test_d1_ask_reuses_shared_olap_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two ask(execute=True) calls must reuse ONE executor (documented defect D1).

    consume.service keeps a process-wide singleton precisely to reuse the HTTP
    connection pool ("避免每请求新建客户端泄漏"). ai.ask bypasses it and news an
    OLAPExecutor() on every call, never closing it -> unbounded connection pools
    / file-descriptor growth under repeated NL2SQL+execute traffic.
    """
    svc, _repo = await _ai_svc({"gmv"})

    constructors: list[int] = []

    class _FakeExecutor:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            constructors.append(1)

        async def execute(self, sql: str, params: dict[str, Any] | None) -> SimpleNamespace:
            return SimpleNamespace(rows=[], total=0, elapsed_ms=1.0)

    monkeypatch.setattr("app.services.consume.olap_executor.OLAPExecutor", _FakeExecutor)

    await svc.ask("查看 gmv 趋势", execute=True)
    await svc.ask("查看 gmv 趋势", execute=True)

    # A shared connection-pool singleton must be created exactly once.
    assert len(constructors) == 1, (
        f"OLAPExecutor constructed {len(constructors)} times across 2 asks; "
        "each construction allocates a fresh 20-connection httpx pool "
        "(app/services/ai/service.py:262) that is never closed."
    )


# ---------------------------------------------------------------------------
# D2 — ai: LLM-generated SQL not parameterized and blocklist is bypassable
# ---------------------------------------------------------------------------


async def test_d2_rejects_union_comment_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Comment-spliced UNION slips past _is_unsafe (documented defect D2)."""
    bypass_sql = "SELECT a FROM unified_metric UNION/**/SELECT secret FROM app_config"
    monkeypatch.setattr(
        "app.services.ai.service.AiService._generate_sql_with_llm",
        AsyncMock(return_value=bypass_sql),
    )
    monkeypatch.setattr(
        "app.services.ai.service.AiService._generate_sql_with_keywords",
        AsyncMock(return_value=("", {})),
    )

    svc, _repo = await _ai_svc({"gmv"})
    result = await svc.nl2sql("给我全部的敏感配置")
    # UNION/**/SELECT bypasses the substring blocklist ('union select' is not a
    # substring of 'union/**/select') and returns safe=True with raw SQL that
    # would be executed verbatim against OLAP.
    assert result["safe"] is False, (
        "LLM-generated SQL containing UNION/**/SELECT passed the _is_unsafe "
        "substring check and is marked safe (app/services/ai/service.py:67-70, "
        "124-129). No table/column whitelist constrains generated SQL."
    )


# ---------------------------------------------------------------------------
# D3 — notify: placeholder email recipient falsely marked SENT
# ---------------------------------------------------------------------------


class _FakeSmtp(ModuleType):
    class SMTPException(Exception):  # noqa: N818 - must match aiosmtplib's public API
        pass

    @staticmethod
    async def send(*args: Any, **kwargs: Any) -> tuple[int, bytes]:
        return (250, b"ok")


async def test_d3_email_missing_recipient_not_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subscriber without an email must NOT be 'delivered' to a placeholder."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "notify_smtp_host", "smtp.test.local")
    monkeypatch.setattr(settings, "notify_smtp_port", 587)
    monkeypatch.setattr(settings, "notify_smtp_user", "no-reply@unisense.local")
    monkeypatch.setattr(settings, "notify_smtp_password", "pw")
    monkeypatch.setitem(sys.modules, "aiosmtplib", _FakeSmtp("aiosmtplib"))

    db = MagicMock()
    svc = NotifyService(db)
    repo = MagicMock()
    # Subscriber has NO registered email -> recipient resolution returns None.
    repo.get_user_email = AsyncMock(return_value=None)
    svc._repo = repo  # noqa: SLF001

    notif = SimpleNamespace(
        id=1,
        subscriber_id=999,
        title="测试通知",
        body="body",
        template_code="quality.anomaly",
    )
    ok = await svc._dispatch_email(notif)  # noqa: SLF001
    # Missing recipient -> must not be counted as delivered to placeholder.
    assert ok is False, (
        "email dispatch returned True for a subscriber with no registered email; "
        "_dispatch_email falls back to smtp_user/admin@unisense.local "
        "(app/services/notify/service.py:370) and publish_event marks the "
        "notification SENT (line 134) although the real recipient never received it."
    )


# ---------------------------------------------------------------------------
# D4 — notify: list_notifications unbounded (no pagination)
# ---------------------------------------------------------------------------


async def test_d4_list_notifications_is_bounded() -> None:
    """list_notifications must cap rows to avoid unbounded materialization."""
    captured: dict[str, Any] = {}

    class _Scalars:
        def all(self) -> list[Any]:
            return []

    class _Exec:
        def scalars(self) -> _Scalars:
            return _Scalars()

    class _FakeSession:
        async def execute(self, stmt: Any) -> _Exec:
            captured["stmt"] = stmt
            return _Exec()

    repo = NotifyRepository(_FakeSession())
    await repo.list_notifications(1, None)

    stmt = captured["stmt"]
    limit = getattr(stmt, "_limit", None)
    assert limit is not None and limit > 0, (
        "NotifyRepository.list_notifications issues an unbounded SELECT "
        "(app/services/notify/repository.py:26-32): a high-volume subscriber's "
        "notification inbox is fully materialized on every read -> unbounded "
        "memory/response (documented defect D4)."
    )
