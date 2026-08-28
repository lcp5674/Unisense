"""审计归档模型（P2: US13 审计归档 + PII 血缘传播）。

AuditArchiveLog: 归档元数据记录。
AuditLog.archived: 标记是否已归档。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import TimestampMixin


class AuditArchiveLog(Base, TimestampMixin):
    """审计归档日志：记录每次归档操作的元数据。

    Attributes:
        archive_date: 归档日期。
        rows_archived: 本次归档行数。
        s3_key: MinIO/S3 对象键。
        s3_size_bytes: 归档文件大小（字节）。
        status: 归档状态（PENDING/SUCCESS/FAILED）。
        completed_at: 归档完成时间。
    """

    __tablename__ = "audit_archive_log"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键 ID"
    )
    archive_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="归档日期",
    )
    rows_archived: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, comment="本次归档行数"
    )
    s3_key: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="MinIO/S3 对象键"
    )
    s3_size_bytes: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="归档文件大小（字节）"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING", comment="归档状态 PENDING/SUCCESS/FAILED"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="错误信息")
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="归档完成时间"
    )
    # S18（审查修复）：归档文件 sha256 哈希链——content_sha256 为本文件哈希，
    # prev_sha256 为上一成功归档文件哈希，串成链；MinIO 无 object-lock 时，
    # 篡改任一文件会破坏链（独立介质核对 prev_sha256 可发现）。
    content_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="本归档文件 sha256"
    )
    prev_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="上一归档文件 sha256（哈希链）"
    )

    __table_args__ = (
        Index("idx_audit_archive_date", "archive_date"),
        Index("idx_audit_archive_status", "status"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "archive_date": self.archive_date,
            "rows_archived": self.rows_archived,
            "s3_key": self.s3_key,
            "s3_size_bytes": self.s3_size_bytes,
            "status": self.status,
            "error_message": self.error_message,
            "completed_at": self.completed_at,
            "content_sha256": self.content_sha256,
            "prev_sha256": self.prev_sha256,
            "created_at": self.created_at,
        }
