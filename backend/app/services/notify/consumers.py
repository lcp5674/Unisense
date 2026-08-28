"""业务事件 → 通知闭环消费者注册（TD §5.5）。

单一事实来源：API（``app/main.py``）与 arq worker（``collector/worker.py``）
共用本模块，保证「手动触发」与「后台任务」（定时采集/质量巡检/审计归档/
冲突 SLA 升级）触发的事件都能进入通知闭环，两侧行为对称——此前 worker
进程从不注册消费者，worker 侧事件双链路（本地订阅 + Redis 发布）全丢（C1）。

事件经 EventBus 本地订阅者消费，写入 notify 的 EventLog 并按订阅扇出投递
（Webhook/钉钉/SMTP/console）。一致性模型：API 与 worker **各进程处理
本进程发布的事件**（本地订阅者），不依赖 Redis 跨进程再分发——这使后台
任务事件即使 API 短暂不可用也不会丢失（C1/C2）。Redis 广播仅作 best-effort
冗余，供未来可选的跨进程下游消费。
"""

from __future__ import annotations

from typing import Any

from app.core.eventbus import EventBus, get_eventbus

#: 业务事件类型（metric/quality/conflict/governance）→ 通知闭环订阅集合（TD §5.5）
#: 必须与各服务 EventBus 实际发布的事件类型完全一致，否则事件永不进入通知闭环：
#:   metric 发布 metric.created/submitted/approved/rejected/deprecated/promoted/
#:     rolled_back/emergency_published/health_critical（services/semantic/service.py）
#:   conflict 发布 conflict_open/conflict_ruled/conflict_escalated/pii_conflict
#:   （services/conflict/service.py）
#:   governance 发布 grant.*/classification.*/pii.*（services/governance/*）
#:   quality 发布 quality.anomaly/reconciliation.alert/benchmark.imported
#:   （services/quality/*）
BUSINESS_EVENT_TYPES: tuple[str, ...] = (
    "metric.created",
    "metric.submitted",
    "metric.resubmitted",
    "metric.approved",
    "metric.gray_published",
    "metric.rejected",
    "metric.deprecated",
    "metric.promoted",
    "metric.rolled_back",
    "metric.emergency_published",
    "metric.health_critical",
    # 指标重新启用（semantic/service.py reactivate_metric 发布）：DEPRECATED 复活为 DRAFT 后
    # 通知相关方可重新送审（C1 第七轮：此前发布但不在订阅集合 → 事件永不落 EventLog/扇出）
    "metric.reactivated",
    # 紧急发布补审完成（P1-6）：complete_emergency_review 写 emergency_reviewed_at 后发布，
    # 定向通知补审执行人/指标 Owner（TD §12.3 紧急发布闭环）
    "metric.emergency_reviewed",
    # 数据源 DROP → 下游指标置 DATA_SOURCE_DROPPED（P1-4）：定向通知指标 Owner 去处理
    # （恢复/确认退役，7 天处理期见 TodoCenter 待办与每日超期巡检）
    "metric.source_dropped",
    # DSD 源恢复/误报 → 指标回 PUBLISHED：通知 Owner 确认与消费方
    "metric.source_recovered",
    # 灰度超期强制回收（P1-7）：check_experimental_expiry 每日巡检触发
    "metric.gray_recycled",
    # 冲突仲裁「保留差异+指定一方改名」→ 定向通知指标 Owner 去详情页改名（TD §12.4）
    "metric.rename_required",
    # PENDING_VERSION 确认期创建 → 定向通知消费方（Owner/备份 Owner）去「版本历史」确认（TD §12.3）
    "metric.breaking_change_pending",
    # PENDING_VERSION 全部确认/超时接受转正 → 定向通知消费方新口径已生效（TD §12.3）
    "metric.breaking_change_promoted",
    # 冲突仲裁「选权威」→ 定向通知落败方指标 Owner：指标已废弃（DEPRECATED）或
    # 已作废（软删），后继=胜方（TD §12.4）
    "metric.voided",
    "quality.anomaly",
    "reconciliation.alert",
    "benchmark.imported",
    "conflict_open",
    "conflict_ruled",
    "conflict_escalated",
    # 悬空冲突强制关闭（conflict/service.py force_close 发布，管理员处置留痕）
    "conflict_forced_closed",
    # 升级超时强提醒（conflict/sla_tasks.py remind_stale_escalated 定向通知管理员）
    "conflict_escalation_overdue",
    "pii_conflict",
    "grant.granted",
    "grant.revoked",
    "grant.expired",
    "pii.reviewed",
    "pii.propagated",
    "classification.changed",
    "classification.done",
    "escalation.triggered",
    # observability / audit（走 EventBus 的可接入业务事件，TD §5.5）
    "feedback.status_updated",
    "nps.submitted",
    "audit.capacity_warning",
    # 采集/血缘断链修复：collector/lineage 双发 EventBus 的目录血缘事件（TD §5.5）
    "catalog_registered",
    "catalog_schema_drifted",
    "lineage_parsed",
    "lineage_ingested",
    # DDL 变更事件化（lineage/service.py 经 notify_user 定向通知受影响资产 Owner，
    # 同时发布本事件供通知中心记录/订阅扇出——表/列重命名、DROP 的下游治理闭环）
    "lineage.ddl_changed",
    # 血缘变更影响（semantic/service.py 变更指标血缘时发布，标题映射见 notify/service.py）
    "lineage.change_impacted",
    # 指标血缘注册失败（metrics.py / semantic/service.py best-effort 路径，C7：
    # 血缘静默缺失不再无声——运维/管理员订阅感知，可补注册修复）
    "lineage.metric_register_failed",
    # 采集定向通知（collector/service.py 经 notify_user 直发源 Owner，模板注册）
    "catalog.deprecated",
    "collect.degraded",
    "collect.failed",
    "catalog.connection_failed",
    # 核心依赖降级（core/degradation.py 已发布 EventBus，供 notify 消费告警）
    "degradation.state_changed",
    # 冲突重开（conflict/service.py 经 _safe_publish 发布，原仅存于失效旧 HTTP 通道）
    "conflict_reopened",
    # 账号安全/组织（users.py/organizations.py 经 notify_user 定向通知，模板注册）
    "user.created",
    "user.status_changed",
    "user.password_reset",
    "org.status_changed",
    # 授权到期提醒 / PII 复核待办（定向通知，模板注册）
    "grant.expiring_soon",
    "pii.review_pending",
    # 表增长超阈值（data_retention.py 巡检发布，P11：此前无订阅 → 事件发出即消失的死信告警）
    "storage.table_oversized",
    # T7/T8（审查修复）：后台任务失败 / 缓存失效失败 → 告警事件进通知闭环
    "system.task_failed",
    "system.cache_invalidate_failed",
)


def register_notify_event_consumers(bus: EventBus | None = None) -> None:
    """注册业务事件 → 通知闭环消费者（best-effort，异常不阻断业务主流程）。

    Args:
        bus: 目标 EventBus 实例；默认使用进程级单例（``get_eventbus()``）。
    """
    from app.db.mysql import async_session_factory
    from app.services.notify.service import NotifyService

    async def _consume(event: dict[str, Any]) -> None:
        async with async_session_factory() as session:
            await NotifyService(session).handle_business_event(event)

    bus = bus or get_eventbus()
    for event_type in BUSINESS_EVENT_TYPES:
        bus.subscribe(event_type, _consume)
