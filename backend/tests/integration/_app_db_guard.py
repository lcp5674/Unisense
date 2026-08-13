"""集成测试应用库保护（共享工具）。

根因：曾因 UNISENSE_INTEGRATION_DB_URL 未设置而回退到 UNISENSE_DB_URL（backend/.env
指向应用库 ``localhost:3307/unisense``），导致集成测试 DROP 重建整库、admin 账号丢失。
本模块解析 backend/.env 中应用库的 ``host:port + 库名``，供各集成测试在任何 DROP 前
调用（conftest 的 autouse fixture 亦调用）。

CI 不受影响：CI 无 .env，其 MySQL 服务库（同名 unisense）是每个 job 临时的可重建实例，
不是应用库；无 .env 时本保护自动失效。
"""

from __future__ import annotations

from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]  # backend/


def _split_authority(url: str) -> tuple[str, str] | None:
    """从 DB URL 提取 (host:port, db_name)；格式异常返回 None。"""
    db_name = url.split("?")[0].rsplit("/", 1)[1]
    authority = url.split("@")[-1].split("/", 1)[0]
    if not db_name or not authority:
        return None
    return authority, db_name


def _app_db_authority() -> tuple[str, str] | None:
    """读取 backend/.env 应用库的 (host:port, db_name)；无 .env 返回 None。"""
    env_file = _BACKEND_DIR / ".env"
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("UNISENSE_DB_URL="):
            url = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            return _split_authority(url)
    return None


# 模块加载时解析一次；无 .env（如 CI）则 _APP_DB 为 None，保护自动失效。
_APP_DB = _app_db_authority()


def assert_not_app_db(url: str) -> None:
    """断言目标库不是应用库；是则抛出 AssertionError（须在任何 DROP 前调用）。"""
    if _APP_DB is None:
        return
    target = _split_authority(url)
    if target is not None and target == _APP_DB:
        raise AssertionError(
            f"拒绝在集成测试中使用应用库 `{target[1]}@{target[0]}`："
            "UNISENSE_INTEGRATION_DB_URL 必须指向独立测试库（如 unisense_it），不得指向应用库"
        )
