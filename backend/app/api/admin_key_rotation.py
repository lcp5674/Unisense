"""密钥轮换管理 API（SEC-02[P1]）。

管理员专用端点，需 platform_admin 角色。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, require_roles
from app.api.responses import ApiResponse, ok
from app.core.key_rotation import get_key_rotation_manager, migrate_legacy_secrets

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
) -> ApiResponse[dict[str, object]]:
    """触发旧 SHA-256 加密数据迁移至当前 PBKDF2 活跃密钥。"""
    if body.dry_run:
        manager = get_key_rotation_manager()
        metadata = manager.get_key_metadata()
        return ok({"dry_run": 1, "decrypt_key_count": metadata["decrypt_key_count"]})

    from app.core.secrets import SecretManager

    def _list_func() -> list[str]:
        # 目前密钥迁移的存量密文清单由运维脚本提供（db_catalog.config 全量扫描）；
        # API 层留空列表占位，实际迁移入口见 docs/runbooks/key-rotation.md
        return []

    count = migrate_legacy_secrets(
        encrypt_func=lambda plaintext: SecretManager.encrypt(
            json.loads(plaintext.decode("utf-8"))
        ).encode("utf-8"),
        decrypt_func=lambda token: json.dumps(
            SecretManager.decrypt(token.decode("utf-8"))
        ).encode("utf-8"),
        list_func=_list_func,
    )
    return ok({"migrated": count})


@router.get("/status", dependencies=[Depends(require_roles("platform_admin"))])
async def key_status(
    user: CurrentUser,
) -> ApiResponse[dict[str, object]]:
    """获取当前密钥元数据（版本、创建时间、过期状态）。"""
    manager = get_key_rotation_manager()
    metadata = manager.get_key_metadata()
    return ok(metadata)
