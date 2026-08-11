"""术语库模型。

对齐 TD §4.1 term 表和 PRD 4.6 术语库（FR-08）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class Term(Base, BaseModel):
    """术语库实体。

    业务概念标准层，提供指标引用的标准定义。

    Attributes:
        term_code: 术语编码（唯一）。
        name: 术语名称。
        definition: 术语定义。
        domain: 所属域。
        synonyms: 同义词列表（JSON）。
        boundary: 边界说明（可空）。
        status: 术语状态。
        owner_id: Owner ID。
    """

    __tablename__ = "term"

    term_code: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="术语编码（唯一）"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="术语名称")
    definition: Mapped[str] = mapped_column(Text, nullable=False, comment="术语定义")
    domain: Mapped[str] = mapped_column(String(64), nullable=False, comment="所属域")
    synonyms: Mapped[list[Any]] = mapped_column(JSON, nullable=False, comment="同义词列表")
    boundary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="边界说明")
    status: Mapped[str] = mapped_column(
        Enum("DRAFT", "PUBLISHED", "DEPRECATED", name="term_status_enum"),
        nullable=False,
        default="DRAFT",
        comment="术语状态",
    )
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", name="fk_term_owner"), nullable=False, comment="Owner ID"
    )

    __table_args__ = (
        Index("idx_term_domain", "domain"),
        Index("idx_term_status", "status"),
    )
