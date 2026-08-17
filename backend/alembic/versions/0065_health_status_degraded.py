"""data_source.health_status 枚举扩充 degraded（健康状态机 DEGRADED 黄态）。

背景（PRD §4.13：ACTIVE → DEGRADED → UNAVAILABLE）：健康状态机
（CollectorService._evaluate_health_after_collect）在健康窗口失败率 ≥5% 时
产出 ``degraded`` 状态，但：
1. 数据库列自建表以来仅 ``('healthy','unhealthy','unknown')``；
2. 模型 Enum（data_source.py health_status）同样未含 degraded（SQLAlchemy
   Enum 默认不校验值，ORM 层放行）。

导致采集成功后的健康更新 ``UPDATE data_source SET health_status='degraded'``
被 MySQL 严格模式拒绝（DataError 1265 Data truncated），整批采集 FAILED——
只要数据源历史健康窗口失败率 >5%，每次采集必然复现（自我锁死）。

本迁移将 degraded 纳入枚举（置于末尾，保持既有值的存储序号不变，避免
存量行语义漂移）。downgrade 先将存量 degraded 归并为 unhealthy 再回缩枚举。

注：revision 挂 0064_notify_indexes（通知索引，非本域但为当前线性链后继）。
"""

from __future__ import annotations

from alembic import op

revision = "0065_health_status_degraded"
down_revision = "0064_notify_indexes"
branch_labels = None
depends_on = None

# 与模型 data_source.py health_status 列保持一致（含 degraded）
_HEALTH_STATUS_ENUM_DDL = (
    "ENUM('healthy','unhealthy','unknown','degraded') NOT NULL COMMENT '健康状态'"
)


def upgrade() -> None:
    op.execute(f"ALTER TABLE data_source MODIFY COLUMN health_status {_HEALTH_STATUS_ENUM_DDL}")


def downgrade() -> None:
    # 存量 degraded 归并为 unhealthy（最接近语义），避免回缩枚举时数据丢失
    op.execute("UPDATE data_source SET health_status='unhealthy' WHERE health_status='degraded'")
    op.execute(
        "ALTER TABLE data_source MODIFY COLUMN health_status "
        "ENUM('healthy','unhealthy','unknown') NOT NULL COMMENT '健康状态'"
    )
