"""SQLAlchemy 2.0 ORM 基类与公共 Mixin。

对齐:
    - TD §4.1 MySQL 核心表规范
    - DEV_GUIDE §8a.4 数据库对象命名（表名单数 snake_case，主键 id BIGINT）
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime
from sqlalchemy.orm import Mapped, mapped_column

# assetmap T-2 / 平台级敏感字段黑名单：序列化时强制剔除。
# 注：sensitivity_level 非凭据，是资产地图核心展示字段（FR-18），不在此黑名单，
# 否则 to_dict() 默认剔除会导致前端敏感度列永远空白。
_SENSITIVE_FIELDS = frozenset({
    "connection_config",
    "password",
    "secret",
    "token",
    "credential",
    "etl_sql",
    "schema_json",
})


class TimestampMixin:
    """时间戳 Mixin，提供 ``created_at`` 和 ``updated_at`` 字段。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="创建时间（UTC）",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        comment="更新时间（UTC）",
    )


class SoftDeleteMixin:
    """软删除 Mixin，提供 ``deleted_at`` 字段。

    WORM 表（如 audit_log）不继承此 Mixin。
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        comment="软删除时间（UTC），NULL 表示未删除",
    )


class BaseModel(TimestampMixin, SoftDeleteMixin):
    """所有业务模型的公共基类。

    继承 TimestampMixin + SoftDeleteMixin。
    须配合 ``app.db.mysql.Base`` 使用（通过 ``__table__`` 声明）。

    Attributes:
        id: 主键，BIGINT AUTO_INCREMENT。
        created_at: 创建时间（UTC）。
        updated_at: 更新时间（UTC）。
        deleted_at: 软删除时间（UTC），NULL 表示未删除。
    """

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        comment="主键 ID",
    )

    def to_dict(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        """序列化为字典；默认剔除敏感字段（连接配置/密码/密钥等）。

        assetmap T-2: 防止 data_source.connection_config 等凭据随资产地图接口外泄。
        """
        data: dict[str, Any] = {}
        table = getattr(self, "__table__", None)
        if table is None:
            return data
        for col in table.columns:
            if not include_sensitive and col.name in _SENSITIVE_FIELDS:
                continue
            data[col.name] = getattr(self, col.name)
        return data
