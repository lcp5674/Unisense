"""审计 WORM 触发器放行 erasure 匿名化（被遗忘权 R7-09③ 与 WORM 的合规例外）。

背景：``0122_audit_worm_triggers`` 将 ``ip`` / ``detail_json`` 一并列为不可改 core，
但 erasure 流程（``governance.service.execute_erasure``）恰需把命中主体的审计行
``ip`` 与 ``detail_json`` 个人标识**覆写为 ANONYMIZED_ 令牌**实现去标识化——
两者冲突导致 ``POST /erasure`` 500（WORM: core fields cannot be updated）。

本迁移重建 ``trg_audit_log_no_update_core``：

- 其余 core 字段（actor_id/action/entity_type/entity_id/trace_id/pii_access）仍禁改；
- ``ip`` / ``detail_json`` 允许**且仅允许**被改写为含 ``ANONYMIZED_`` 令牌的形式
  （erasure 白名单：变更后值必须是令牌形式才放行，杜绝随意篡改审计内容）。

幂等：由 alembic 版本表保证只执行一次；downgrade 恢复 0122 原版触发器。
"""

from __future__ import annotations

from alembic import op

revision = "0131_audit_worm_allow_erasure"
down_revision = "0130_seed_marker"
branch_labels = None
depends_on = None

_DELETE_TRIGGER = "trg_audit_log_no_delete_unarchived"
_UPDATE_TRIGGER = "trg_audit_log_no_update_core"


def _create_update_trigger(erasure_exception: bool) -> None:
    """重建更新触发器。``erasure_exception=True`` 时放行 ip/detail_json 的匿名化改写。"""
    if erasure_exception:
        core_check = """
            IF OLD.archived = FALSE AND (
                NEW.actor_id <> OLD.actor_id
                OR NEW.action <> OLD.action
                OR NEW.entity_type <> OLD.entity_type
                OR NEW.entity_id <> OLD.entity_id
                OR NEW.trace_id <> OLD.trace_id
                OR NEW.pii_access <> OLD.pii_access
                OR (
                    IFNULL(CAST(NEW.ip AS CHAR), '') <> IFNULL(CAST(OLD.ip AS CHAR), '')
                    AND CAST(NEW.ip AS CHAR) NOT LIKE 'ANONYMIZED\\_%'
                )
                OR (
                    IFNULL(CAST(NEW.detail_json AS CHAR), '') <> IFNULL(CAST(OLD.detail_json AS CHAR), '')
                    AND CAST(NEW.detail_json AS CHAR) NOT LIKE '%ANONYMIZED\\_%'
                )
            ) THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'audit_log is WORM: core fields cannot be updated';
            END IF;"""
    else:
        core_check = """
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
            END IF;"""
    op.execute(
        f"""
        CREATE TRIGGER {_UPDATE_TRIGGER}
        BEFORE UPDATE ON audit_log
        FOR EACH ROW
        BEGIN
            {core_check}
        END
        """
    )


def upgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_UPDATE_TRIGGER}")
    _create_update_trigger(erasure_exception=True)


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_UPDATE_TRIGGER}")
    _create_update_trigger(erasure_exception=False)
