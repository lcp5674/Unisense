"""用户反馈模型（TD §12.10 可观测性 / FR-16）。"""

from __future__ import annotations

from sqlalchemy import BigInteger, Integer, String, Text
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
