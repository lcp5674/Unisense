"""consume 消费快照双通道鉴权测试（TD §12.6 / FR-12）。

快照端点（GET /consume/metrics/{code}/snapshots）需同时服务两类调用方：
- 消费方（外部接入方 X-Api-Key / 平台内 consume Bearer）：接入方校验 + 限流。
- 内部登录用户（指标详情 UI）：用户 JWT 只读展示，不经过接入方限流。
本文件覆盖双通道的通过/拒绝路径。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.core.exceptions import AuthError, BusinessError
from app.main import app


def _session() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    s.commit = AsyncMock()
    s.rollback = AsyncMock()
    s.flush = AsyncMock()
    s.refresh = MagicMock()
    s.execute = MagicMock()
    return s


def _snap() -> dict:
    return {
        "id": 1,
        "metric_code": "M1",
        "version": 1,
        "dims": {},
        "date_range": "last_30d",
        "value_json": {"value": 1.0},
        "quality_flag": "GOOD",
        "generated_at": "2026-08-13T00:00:00",
        "generated_by": "QUERY",
    }


@pytest.fixture
def db_override() -> AsyncIterator[None]:
    session = _session()

    async def fake_db() -> AsyncIterator[MagicMock]:
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    yield
    app.dependency_overrides.clear()


@pytest.fixture
async def client(db_override: AsyncIterator[None]) -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_snapshots_without_credentials_401(client: httpx.AsyncClient) -> None:
    """无任何凭证 → 401（双通道均未命中）。"""
    resp = await client.get("/api/v1/consume/metrics/M1/snapshots")
    assert resp.status_code == 401


async def test_snapshots_accepts_internal_user_jwt(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """内部登录用户 JWT → 200（指标详情 UI 只读展示）。"""
    import app.api.consume as consume_api

    user = MagicMock(id=1, role="metric_owner", domain="sales")
    monkeypatch.setattr(consume_api, "get_current_user", AsyncMock(return_value=user))
    with patch.object(
        consume_api.ConsumeService,
        "list_snapshots",
        new=AsyncMock(return_value=[_snap()]),
    ):
        resp = await client.get(
            "/api/v1/consume/metrics/M1/snapshots",
            headers={"Authorization": "Bearer some-user-jwt"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "OK"
    assert body["data"][0]["metric_code"] == "M1"


async def test_snapshots_accepts_api_key(client: httpx.AsyncClient) -> None:
    """外部消费方 X-Api-Key → 200（接入方鉴权 + 限流）。"""
    import app.api.consume as consume_api

    api_client = MagicMock(id=1, client_id="app_test", status="ACTIVE")
    with (
        patch.object(
            consume_api.ConsumeService,
            "authenticate_client",
            new=AsyncMock(return_value=api_client),
        ),
        patch.object(consume_api.ConsumeService, "check_rate_limit", new=AsyncMock()),
        patch.object(
            consume_api.ConsumeService,
            "list_snapshots",
            new=AsyncMock(return_value=[_snap()]),
        ),
    ):
        resp = await client.get(
            "/api/v1/consume/metrics/M1/snapshots",
            headers={"X-Api-Key": "app_test:secret"},
        )
    assert resp.status_code == 200
    assert resp.json()["data"][0]["metric_code"] == "M1"


async def test_snapshots_accepts_consume_bearer(client: httpx.AsyncClient) -> None:
    """平台内 consume Bearer（QueryWorkspace 调试）→ 200。"""
    import app.api.consume as consume_api

    api_client = MagicMock(id=1, client_id="app_test", status="ACTIVE")
    with (
        patch.object(
            consume_api.ConsumeService,
            "authenticate_consume_token",
            new=AsyncMock(return_value=api_client),
        ),
        patch.object(consume_api.ConsumeService, "check_rate_limit", new=AsyncMock()),
        patch.object(
            consume_api.ConsumeService,
            "list_snapshots",
            new=AsyncMock(return_value=[_snap()]),
        ),
    ):
        resp = await client.get(
            "/api/v1/consume/metrics/M1/snapshots",
            headers={"Authorization": "Bearer some-consume-token"},
        )
    assert resp.status_code == 200


async def test_snapshots_invalid_api_key_without_user_401(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """带无效 X-Api-Key 且无有效用户 token → 401（两通道均失败）。"""
    import app.api.consume as consume_api

    monkeypatch.setattr(
        consume_api,
        "get_current_user",
        AsyncMock(side_effect=AuthError("Token 无效", error_code="AUTH_TOKEN_INVALID")),
    )
    with patch.object(
        consume_api.ConsumeService,
        "authenticate_client",
        new=AsyncMock(
            side_effect=BusinessError(
                "X-Api-Key 格式应为 client_id:secret", error_code="AUTH_APIKEY_INVALID"
            )
        ),
    ):
        resp = await client.get(
            "/api/v1/consume/metrics/M1/snapshots",
            headers={"X-Api-Key": "dev-semantic-key"},
        )
    assert resp.status_code == 401


async def test_snapshots_invalid_api_key_falls_back_to_user_200(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无效 X-Api-Key + 有效用户 JWT → 回落用户通道 200（前端全局 X-Api-Key 头场景）。"""
    import app.api.consume as consume_api

    user = MagicMock(id=1, role="metric_owner", domain="sales")
    monkeypatch.setattr(consume_api, "get_current_user", AsyncMock(return_value=user))
    with (
        patch.object(
            consume_api.ConsumeService,
            "authenticate_client",
            new=AsyncMock(
                side_effect=BusinessError(
                    "X-Api-Key 格式应为 client_id:secret", error_code="AUTH_APIKEY_INVALID"
                )
            ),
        ),
        patch.object(
            consume_api.ConsumeService,
            "list_snapshots",
            new=AsyncMock(return_value=[_snap()]),
        ),
    ):
        resp = await client.get(
            "/api/v1/consume/metrics/M1/snapshots",
            headers={
                "X-Api-Key": "dev-semantic-key",
                "Authorization": "Bearer some-user-jwt",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["data"][0]["metric_code"] == "M1"


async def test_snapshots_invalid_both_channels_401(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """consume Bearer 与用户 JWT 均无效 → 401。"""
    import app.api.consume as consume_api

    monkeypatch.setattr(
        consume_api,
        "get_current_user",
        AsyncMock(side_effect=AuthError("Token 无效", error_code="AUTH_TOKEN_INVALID")),
    )
    with patch.object(
        consume_api.ConsumeService,
        "authenticate_consume_token",
        new=AsyncMock(
            side_effect=BusinessError("消费令牌无效", error_code="AUTH_APIKEY_INVALID")
        ),
    ):
        resp = await client.get(
            "/api/v1/consume/metrics/M1/snapshots",
            headers={"Authorization": "Bearer junk-token"},
        )
    assert resp.status_code == 401
