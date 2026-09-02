"""参照数据初始化标记（seed_marker 表）。

背景：部署自举（scripts/bootstrap.py）把「首次初始化」收敛为启动期自动执行，
但**参照数据（维度/术语/业务主题域）与幂等播种不同**——迭代重建容器时若再次
执行 seed_medical_reference，其「成员指纹不一致 → 删旧重灌」语义会覆盖业务在
运行期对参照数据的修改。故参照数据 seed 需「只初始化一次」的持久标记：
标记**存 DB 而非 Redis**（Redis 随容器重建丢失，标记必须在迭代重建后仍存在）。

表结构：
- name：标记名（如 ``medical_reference``），主键；
- version：脚本数据版本（记录用，迭代脚本时可人工 bump 触发重灌）；
- seeded_at：首次初始化完成时间；
- detail：执行摘要（JSON，audit 用）。

语义：
- 存在 → 参照数据已初始化，bootstrap 跳过（迭代重建不重灌）；
- 缺失 → 首次部署，bootstrap 执行 seed 后写入；
- 强制重灌：``UNISENSE_SEED_REFERENCE_FORCE=1`` 或删行/``--force``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base


class SeedMarker(Base):
    """一次性初始化标记（seed_marker）。"""

    __tablename__ = "seed_marker"

    name: Mapped[str] = mapped_column(String(64), primary_key=True, comment="标记名")
    version: Mapped[str] = mapped_column(String(32), nullable=False, comment="数据版本")
    seeded_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=sa_func.now(),
        comment="初始化完成时间",
    )
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, comment="执行摘要")
