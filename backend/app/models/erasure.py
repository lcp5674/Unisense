"""被遗忘权（数据主体删除请求 / DSR）领域模型（TD §4.15.7 / R7-09③）。

WORM 约束下审计行禁止物理删除；被遗忘权以**覆写脱敏**实现去标识化——
将命中数据主体的审计行 PII 字段（``ip`` / ``detail_json`` 中的个人标识）覆写为
``ANONYMIZED_<hash>``，并保留一条 ``action=PII_ANONYMIZED`` 审计留存。

本表为 DSR 执行台账，记录每次被遗忘权执行的主体、操作人、脱敏令牌与影响行数，
供合规复核与去标识化可追溯。本表本身不软删（合规留痕）。
"""

from __future__ import annotations

import enum

from sqlalchemy import BigInteger, Enum, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import TimestampMixin


class ErasureStatus(enum.StrEnum):
    """被遗忘权执行状态机。"""

    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ErasureRequest(Base, TimestampMixin):
    """被遗忘权（DSR）台账（TD §4.1，新表 ``erasure_request``）。"""

    __tablename__ = "erasure_request"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键 ID"
    )
    subject_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="数据主体（被遗忘）用户 ID"
    )
    requested_by: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="执行操作人（合规官/平台管理员）ID"
    )
    status: Mapped[ErasureStatus] = mapped_column(
        Enum(ErasureStatus, values_callable=lambda e: [str(m.value) for m in e]),
        nullable=False,
        default=ErasureStatus.PENDING,
        comment="执行状态",
    )
    token: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="脱敏令牌 ANONYMIZED_<hash>"
    )
    affected_rows: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, comment="被覆写脱敏的审计行数"
    )
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="执行事由")

    __table_args__ = (
        Index("idx_erasure_subject", "subject_user_id"),
        Index("idx_erasure_status", "status"),
    )
