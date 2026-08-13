"""密钥轮换管理 API（SEC-02[P1]）。

管理员专用端点，需 platform_admin 角色。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import ApiResponse, ok
from app.core.key_rotation import get_key_rotation_manager, migrate_legacy_secrets
from app.db.mysql import get_db_session
from app.models.data_source import DataSource

router = APIRouter(prefix="/admin/keys", tags=["admin/keys"])


class RotateKeyRequest(BaseModel):
    new_raw_key: str = Field(..., min_length=16, description="新密钥材料（≥16字符）")


class MigrateRequest(BaseModel):
    dry_run: bool = Field(default=False, description="仅统计不实际迁移")


@router.post("/rotate", dependencies=[Depends(require_roles("platform_admin"))])
async def rotate_key(
    body: RotateKeyRequest,
    user: CurrentUser,
) -> ApiResponse[dict[str, object]]:
    """轮换密钥：旧活跃密钥移入解密列表，新密钥成为活跃密钥。"""
    manager = get_key_rotation_manager()
    manager.rotate_key(body.new_raw_key)
    metadata = manager.get_key_metadata()
    return ok(metadata)


@router.post("/migrate", dependencies=[Depends(require_roles("platform_admin"))])
async def migrate_secrets(
    body: MigrateRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db_session),
) -> ApiResponse[dict[str, object]]:
    """触发旧 SHA-256 加密数据迁移至当前 PBKDF2 活跃密钥。

    真实扫描 ``data_source.connection_config``（未软删）作为存量密文清单——
    此前 ``_list_func`` 恒返回空列表，迁移完全依赖运维脚本，API 为占位空壳。
    仅迁移活跃密钥无法解密的 legacy 密文（active 可解密者已迁移，跳过避免
    无谓重写）；``dry_run`` 只统计候选条数，不实际重加密。
    """
    result = await db.execute(
        select(DataSource.connection_config).where(DataSource.deleted_at.is_(None))
    )
    all_tokens = [t for t in result.scalars().all() if t]

    manager = get_key_rotation_manager()
    active = manager.active_fernet

    def _is_legacy(token: str) -> bool:
        try:
            active.decrypt(token.encode("utf-8"))
            return False
        except Exception:  # noqa: BLE001 - 非活跃密钥可解密的即为 legacy 候选
            return True

    legacy_tokens = [t for t in all_tokens if _is_legacy(t)]

    if body.dry_run:
        return ok(
            {
                "dry_run": 1,
                "candidate_count": len(legacy_tokens),
                "total_secret_rows": len(all_tokens),
            }
        )

    from app.core.secrets import SecretManager

    count = migrate_legacy_secrets(
        encrypt_func=lambda plaintext: SecretManager.encrypt(
            json.loads(plaintext.decode("utf-8"))
        ).encode("utf-8"),
        decrypt_func=lambda token: json.dumps(SecretManager.decrypt(token.decode("utf-8"))).encode(
            "utf-8"
        ),
        list_func=lambda: legacy_tokens,
    )
    return ok({"migrated": count, "total_secret_rows": len(all_tokens)})


@router.get("/status", dependencies=[Depends(require_roles("platform_admin"))])
async def key_status(
    user: CurrentUser,
) -> ApiResponse[dict[str, object]]:
    """获取当前密钥元数据（版本、创建时间、过期状态）。"""
    manager = get_key_rotation_manager()
    metadata = manager.get_key_metadata()
    return ok(metadata)
