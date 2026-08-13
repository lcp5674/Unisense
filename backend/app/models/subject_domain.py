"""主题域模型（树形3层体系，对齐 TD §12 / spec FR-001~FR-004）。

主题域是指标资产的顶层分类维度，采用邻接表+物化路径实现树形结构。
每个域节点可配置默认值预设，注册该域指标时自动带入。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class SubjectDomain(Base, BaseModel):
    """主题域实体（树形3层体系）。

    Attributes:
        code: 域编码（唯一），小写字母开头+小写字母数字下划线。
        name: 显示名。
        parent_id: 父域ID（根域为null）。
        level: 层级（1=根/2=子/3=孙），最多3层。
        path: 物化路径（如"1.5.12"），加速子树查询。
        sort_order: 同级排序。
        status: 状态（active/inactive）。
        defaults_json: 域级默认值预设（granularity/unit/aggregation等）。
        description: 描述。
        owner_id: 域管理员ID。
    """

    __tablename__ = "subject_domain"

    code: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="域编码（唯一）"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="域显示名")
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("subject_domain.id", name="fk_domain_parent"),
        nullable=True,
        comment="父域ID（根域为null）",
    )
    level: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, comment="层级（1=根/2=子/3=孙）"
    )
    path: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="物化路径（如 1.5.12）"
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="同级排序"
    )
    status: Mapped[str] = mapped_column(
        Enum("active", "inactive", name="domain_status"),
        nullable=False,
        default="active",
        comment="状态",
    )
    defaults_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, comment="域级默认值预设"
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="描述"
    )
    owner_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="域管理员ID"
    )

    __table_args__ = (
        Index("idx_domain_code", "code"),
        Index("idx_domain_parent", "parent_id"),
        Index("idx_domain_path", "path"),
        Index("idx_domain_status", "status"),
    )
