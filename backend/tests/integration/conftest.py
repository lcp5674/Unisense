"""集成测试共享保护：autouse 守卫拒绝应用库。

对所有集成测试在 setup 阶段（任何 DROP 之前）统一校验目标库非应用库，防止
UNISENSE_INTEGRATION_DB_URL / UNISENSE_DB_URL 误指向应用库导致整库被重建。
逻辑与 _app_db_guard.assert_not_app_db 一致；CI（无 .env，临时可重建库）不受影响。
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest
from _app_db_guard import assert_not_app_db


@pytest.fixture(autouse=True)
def _guard_app_db() -> Generator[None, None, None]:
    url = os.getenv("UNISENSE_INTEGRATION_DB_URL") or os.getenv("UNISENSE_DB_URL")
    if url:
        assert_not_app_db(url)
    yield
