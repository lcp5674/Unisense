"""审计日志 WORM 触发器（S3 审查修复：DB 层强制未归档审计行不可删/核心字段不可改）。

背景：``models/audit.py`` 声明「MySQL 触发器强制 WORM」，但全量迁移从未建触发器——
具备 DB 权限者可物理删除/篡改审计行，合规留痕完全依赖 MinIO 冷存哈希链。
本迁移补齐两枚触发器（对齐声明与实现）：

- ``trg_audit_log_no_delete_unarchived``（BEFORE DELETE）：未归档（archived=FALSE）
  行禁止删除。归档搬迁是唯一合法删除路径，且归档任务仅删除**已归档**行
  （MinIO 冷存已含完整记录 + SHA-256 哈希链），不受影响。
- ``trg_audit_log_no_update_core``（BEFORE UPDATE）：未归档行禁止修改核心字段
  （actor/action/entity/detail/ip/trace_id/pii_access）；仅允许 archived 状态翻转
  （归档任务标记 archived=True 用），已归档行的任何修改亦不受限（冷存为准）。

幂等：CREATE TRIGGER 由 alembic 版本表保证只执行一次；downgrade DROP TRIGGER。
MySQL 8.0 下 op.execute 直接执行 CREATE TRIGGER（含 BEGIN...END）即可，无需 DELIMITER。
"""

from __future__ import annotations

from alembic import op

revision = "0122_audit_worm_triggers"
down_revision = "0121_dimension_snapshot_audit_columns"
branch_labels = None
depends_on = None

_DELETE_TRIGGER = "trg_audit_log_no_delete_unarchived"
_UPDATE_TRIGGER = "trg_audit_log_no_update_core"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TRIGGER {_DELETE_TRIGGER}
        BEFORE DELETE ON audit_log
        FOR EACH ROW
        BEGIN
            IF OLD.archived = FALSE THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'audit_log is WORM: unarchived rows cannot be deleted';
            END IF;
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_UPDATE_TRIGGER}
        BEFORE UPDATE ON audit_log
        FOR EACH ROW
        BEGIN
            IF OLD.archived = FALSE AND (
                NEW.actor_id <> OLD.actor_id
                OR NEW.action <> OLD.action
                OR NEW.entity_type <> OLD.entity_type
                OR NEW.entity_id <> OLD.entity_id
                OR IFNULL(CAST(NEW.ip AS CHAR), '') <> IFNULL(CAST(OLD.ip AS CHAR), '')
                OR IFNULL(CAST(NEW.trace_id AS CHAR), '') <> IFNULL(CAST(OLD.trace_id AS CHAR), '')
                OR NEW.pii_access <> OLD.pii_access
                OR IFNULL(CAST(NEW.detail_json AS CHAR), '') <> IFNULL(CAST(OLD.detail_json AS CHAR), '')
            ) THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'audit_log is WORM: core fields cannot be updated';
            END IF;
        END
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_DELETE_TRIGGER}")
    op.execute(f"DROP TRIGGER IF EXISTS {_UPDATE_TRIGGER}")
