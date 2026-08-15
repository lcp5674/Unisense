"""用户反馈模型（TD §12.10 可观测性 / FR-16）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class Feedback(Base, BaseModel):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="反馈人 ID", index=True
    )
    target_type: Mapped[str] = mapped_column(String(64), nullable=False, comment="反馈对象类型")
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="反馈对象 ID")
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="评分 1-5")
    comment: Mapped[str | None] = mapped_column(Text, nullable=True, comment="反馈内容")
    nps_score: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="NPS 评分 0-10", index=True
    )
    #: 反馈处理状态（pending/adopted/rejected/in_progress）——此前仅写进 comment
    #: 文本，状态不可查询/过滤，"反馈采纳闭环"未真正落地；现落库可筛。
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default="pending",
        comment="处理状态：pending/adopted/rejected/in_progress",
    )
    resolution_note: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="处理说明（resolver 填写）"
    )
    resolver_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="处理人 ID"
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="处理时间"
    )
