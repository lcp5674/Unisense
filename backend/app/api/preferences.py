"""用户偏好 API — 按用户持久化 UI 偏好（侧边栏折叠等）。

对齐 TD §5.4 user_preference 表（user_id + preference_key + JSON value）。
所有操作仅作用于当前登录用户：user_id 一律取自认证身份，不存在跨用户读写。

端点（挂载前缀 /api/v1）：
- GET    /api/v1/me/preferences             查询当前用户全部偏好
- PUT    /api/v1/me/preferences/{key}       upsert 单个偏好（存在即覆盖）
- DELETE /api/v1/me/preferences/{key}       软删除单个偏好（幂等）
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.api.responses import ApiResponse, ok
from app.db.mysql import get_db_session
from app.models.consume import UserPreference

router = APIRouter(prefix="/me", tags=["preferences"])


# ---- Schemas ----

class PreferenceItem(BaseModel):
    """单条偏好（key + JSON 值）。"""

    key: str
    value: Any


class PreferenceListResponse(BaseModel):
    """偏好列表响应。"""

    items: list[PreferenceItem]
    total: int


class PreferenceUpdate(BaseModel):
    """偏好写入请求体（值可为任意 JSON 标量/结构）。"""

    value: Any


# ---- Helpers ----

async def _get_preference(db: AsyncSession, user_id: int, key: str) -> UserPreference | None:
    """按用户与键查询未删除偏好。"""
    stmt = select(UserPreference).where(
        UserPreference.user_id == user_id,
        UserPreference.preference_key == key,
        UserPreference.deleted_at.is_(None),
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _upsert(db: AsyncSession, user_id: int, key: str, value: Any) -> UserPreference:
    """写入偏好：激活行覆盖；软删除行恢复；否则插入。

    软删除行仍占用唯一键 (user_id, key)，必须恢复而非插入，否则触发唯一约束冲突。
    并发首次写入的唯一键冲突时回退为覆盖激活行。
    """
    pref = await _get_preference(db, user_id, key)
    if pref is None:
        # 软删除行占用唯一键：恢复它（清 deleted_at 后覆盖），避免 INSERT 冲突
        stmt = select(UserPreference).where(
            UserPreference.user_id == user_id,
            UserPreference.preference_key == key,
        )
        soft_deleted = (await db.execute(stmt)).scalar_one_or_none()
        if soft_deleted is not None:
            soft_deleted.deleted_at = None
            pref = soft_deleted
        else:
            pref = UserPreference(user_id=user_id, preference_key=key)
            db.add(pref)
    pref.preference_value = value
    try:
        await db.commit()
    except IntegrityError:
        # 并发首次写入同一 (user_id, key)：唯一约束冲突，回退为覆盖激活行
        await db.rollback()
        pref = await _get_preference(db, user_id, key)
        if pref is None:
            raise
        pref.preference_value = value
        await db.commit()
    return pref


# ---- Endpoints ----

@router.get("/preferences", response_model=ApiResponse[PreferenceListResponse])
async def list_preferences(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
) -> ApiResponse[PreferenceListResponse]:
    """查询当前用户全部偏好（按键排序）。"""
    stmt = (
        select(UserPreference)
        .where(
            UserPreference.user_id == user.id,
            UserPreference.deleted_at.is_(None),
        )
        .order_by(UserPreference.preference_key)
    )
    rows = (await db.execute(stmt)).scalars().all()
    items = [PreferenceItem(key=r.preference_key, value=r.preference_value) for r in rows]
    return ok(PreferenceListResponse(items=items, total=len(items)))


@router.put("/preferences/{key}", response_model=ApiResponse[PreferenceItem])
async def upsert_preference(
    key: Annotated[str, Path(min_length=1, max_length=64, description="偏好键")],
    body: PreferenceUpdate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
) -> ApiResponse[PreferenceItem]:
    """写入（新增或覆盖）当前用户单个偏好。"""
    pref = await _upsert(db, user.id, key, body.value)
    return ok(PreferenceItem(key=key, value=pref.preference_value))


@router.delete("/preferences/{key}", response_model=ApiResponse[PreferenceItem])
async def delete_preference(
    key: Annotated[str, Path(min_length=1, max_length=64, description="偏好键")],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
) -> ApiResponse[PreferenceItem]:
    """软删除当前用户单个偏好（幂等：不存在返回 value=null）。"""
    pref = await _get_preference(db, user.id, key)
    if pref is not None:
        pref.deleted_at = datetime.now(UTC)
        await db.commit()
        return ok(PreferenceItem(key=key, value=pref.preference_value))
    return ok(PreferenceItem(key=key, value=None))
