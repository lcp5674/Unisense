"""特性开关管理 API（OPS-09: 特性开关框架）。

提供：
1. GET  /feature-flags          列出全部特性开关
2. PUT  /feature-flags/{name}   更新指定开关（启停/定向域/定向用户）

对齐 R&D-08：Redis Hash 存储 + 30s 内存缓存刷新（见 core/feature_flags.py）。
仅 platform_admin 可读写，读端点挂注入守卫（纵深防御）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, require_roles
from app.api.responses import ApiResponse, ok
from app.core.feature_flags import get_feature_flag_manager
from app.core.guard import guard_against_injection

router = APIRouter(prefix="/feature-flags", tags=["feature-flags"])

_ADMIN_ROLES = ("platform_admin",)
_ADMIN_DEPS = [Depends(require_roles(*_ADMIN_ROLES)), Depends(guard_against_injection)]


class FeatureFlagUpdate(BaseModel):
    """特性开关更新请求。"""

    enabled: bool | None = Field(default=None, description="是否启用")
    target_domains: list[str] | None = Field(default=None, description="定向启用域")
    target_users: list[int] | None = Field(default=None, description="定向启用用户")
    description: str | None = Field(default=None, description="开关说明")


@router.get("", dependencies=_ADMIN_DEPS, response_model=ApiResponse)
async def list_feature_flags(
    _user: CurrentUser,
) -> ApiResponse[Any]:
    """列出全部特性开关（含状态/定向配置）。"""
    manager = get_feature_flag_manager()
    flags = [f.to_dict() for f in manager.get_all_flags()]
    return ok(data={"items": flags, "total": len(flags)})


@router.put("/{name}", dependencies=_ADMIN_DEPS, response_model=ApiResponse)
async def update_feature_flag(
    name: str,
    body: FeatureFlagUpdate,
    _user: CurrentUser,
) -> ApiResponse[Any]:
    """更新指定特性开关。

    开关不存在返回 404；更新即时生效（内存缓存），Redis 持久化后跨副本同步。
    """
    from app.core.exceptions import NotFoundError

    manager = get_feature_flag_manager()
    flag = manager.update_flag(
        name,
        enabled=body.enabled,
        target_domains=body.target_domains,
        target_users=body.target_users,
        description=body.description,
    )
    if flag is None:
        raise NotFoundError(f"特性开关不存在: {name}", ctx={"name": name})
    return ok(data=flag.to_dict())
