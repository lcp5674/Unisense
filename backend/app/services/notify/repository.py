"""通知服务 Repository（TD §12.9 / FR-16 / FR-17）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notify import EventLog, Notification, NotifyStatus, SubscriptionPref
from app.models.user import User, UserRole

# 待处理类事件（TD §12.9 通知闭环：接收者需要采取行动，而非仅被告知）。
# 供「仅看待处理」筛选与前端「需处理」语义标记使用；集中维护避免散落。
TODO_EVENT_TYPES = frozenset(
    {
        "metric.submitted",
        "metric.rename_required",
        "metric.health_critical",
        "metric.gray_recycled",
        # 数据源 DROP：Owner 需处理（恢复或确认退役），7 天处理期超期由每日巡检升级提醒
        "metric.source_dropped",
        "conflict_open",
        "conflict_escalated",
        "conflict_reopened",
        "pii_conflict",
        "pii.review_pending",
        "quality.anomaly",
        "reconciliation.alert",
        "escalation.triggered",
        "audit.capacity_warning",
        "catalog_schema_drifted",
        "collect.degraded",
        "collect.failed",
        "catalog.connection_failed",
        "grant.expiring_soon",
        "grant.expired",
        "dict.unknown_pending",
    }
)

# 对象级聚焦（Task D）：按 payload 中常见业务对象键精确过滤。
# 覆盖指标编码/冲突编号/反馈编号/数据源名/表名等，供「只看某个业务对象」场景。
OBJECT_KEY_FIELDS = (
    "metric_code",
    "conflict_id",
    "feedback_id",
    "source_id",
    "source_name",
    "table_name",
    "catalog_id",
    "grant_id",
)


class NotifyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_event(self, obj: EventLog) -> EventLog:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def save_notification(self, obj: Notification) -> Notification:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def list_notifications(
        self,
        subscriber_id: int,
        status: str | None,
        limit: int = 200,
    ) -> list[Notification]:
        """列出订阅者通知；强制行数上限，防高活跃订阅者收件箱全量物化（D4）。"""
        stmt = (
            select(Notification)
            .where(Notification.subscriber_id == subscriber_id)
            .order_by(Notification.id.desc())
            .limit(limit)
        )
        if status:
            stmt = stmt.where(Notification.status == status)
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_notifications_page(
        self,
        subscriber_id: int,
        status: str | None,
        read_state: str | None = None,
        template_code: str | None = None,
        todo_only: bool = False,
        days: int | None = None,
        page: int = 1,
        page_size: int = 20,
        object_key: str | None = None,
    ) -> tuple[list[Notification], int]:
        """订阅者通知分页查询，返回 ``(items, total)``。

        page/page_size 由 API 层做边界约束（page>=1、page_size<=200），
        此处按 offset/limit 精确切页；total 供前端分页器计算总页数。

        筛选能力（产品化收件箱）：
        - read_state: unread（未读）/ read（已读）
        - template_code: 按消息类型精确过滤
        - todo_only: 仅待处理类事件（TODO_EVENT_TYPES），且排除已标记处理的
        - days: 近 N 天（created_at >= now - N 天）
        - object_key: 对象级聚焦——匹配 payload 常见业务对象键（指标编码/冲突编号等）
        """
        base = select(Notification).where(Notification.subscriber_id == subscriber_id)
        if status:
            base = base.where(Notification.status == status)
        if read_state == "unread":
            base = base.where(Notification.read_at.is_(None))
        elif read_state == "read":
            base = base.where(Notification.read_at.is_not(None))
        if template_code:
            base = base.where(Notification.template_code == template_code)
        if todo_only:
            # 仅待处理：TODO 事件集 + 尚未标记「已处理」（handled_at 为空）——闭环后不再打扰
            base = base.where(
                Notification.template_code.in_(TODO_EVENT_TYPES),
                Notification.handled_at.is_(None),
            )
        if days is not None and days > 0:
            since = datetime.now(UTC) - timedelta(days=days)
            base = base.where(Notification.created_at >= since)
        if object_key:
            # 对象级聚焦：精确匹配 payload 中任意业务对象键；数字 key 亦回退匹配 ref_id
            conds = [
                func.json_unquote(func.json_extract(Notification.payload, f"$.{k}"))
                == object_key
                for k in OBJECT_KEY_FIELDS
            ]
            if object_key.isdigit():
                conds.append(Notification.ref_id == int(object_key))
            base = base.where(or_(*conds))
        total_stmt = select(func.count()).select_from(base.subquery())
        total = int((await self._session.execute(total_stmt)).scalar_one() or 0)
        offset = max(page - 1, 0) * page_size
        stmt = base.order_by(Notification.id.desc()).offset(offset).limit(page_size)
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows), total

    async def find_recent_notification(
        self,
        subscriber_id: int,
        template_code: str,
        window_seconds: int,
    ) -> Notification | None:
        """查询订阅者近 N 秒内是否已有同类型通知（去重防风暴）。

        采集降级等高频事件在窗口内反复触发时，避免逐条刷屏：返回最近一条供发布方
        决定跳过（计数合并到已有通知），而非创建新通知。
        """
        since = datetime.now(UTC) - timedelta(seconds=window_seconds)
        stmt = (
            select(Notification)
            .where(
                Notification.subscriber_id == subscriber_id,
                Notification.template_code == template_code,
                Notification.created_at >= since,
                Notification.handled_at.is_(None),
            )
            .order_by(Notification.id.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_recent_notification_by_object(
        self,
        subscriber_id: int,
        template_code: str,
        object_key: str,
        window_seconds: int,
    ) -> Notification | None:
        """查询订阅者近 N 秒内是否已有同「业务对象」的同类型未处理通知（对象级去重）。

        与 ``find_recent_notification`` 的区别：除 ``(user, event_type)`` 外还按
        payload 中的业务对象键（metric_code/conflict_id/table_name 等）精确匹配——
        同一指标 10 分钟内重复提交只保留一条；不同指标各自成条（不再互相挤掉）。
        """
        since = datetime.now(UTC) - timedelta(seconds=window_seconds)
        conds = [
            func.json_unquote(func.json_extract(Notification.payload, f"$.{k}")) == object_key
            for k in OBJECT_KEY_FIELDS
        ]
        stmt = (
            select(Notification)
            .where(
                Notification.subscriber_id == subscriber_id,
                Notification.template_code == template_code,
                Notification.created_at >= since,
                Notification.handled_at.is_(None),
                or_(*conds),
            )
            .order_by(Notification.id.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_domain_admins(self, domain: str) -> list[int]:
        """查询指定域的全部启用 domain_admin 用户 ID（主角色或 user_role 扩展角色）。

        域归属以 ``User.domain`` 为准（user_role 表无 domain 列，多角色域管理员
        仍按主记录域过滤）；供事件相关性收敛（同域治理兜底接收本域指标事件）。
        """
        stmt = (
            select(User.id)
            .where(
                or_(
                    and_(User.role == "domain_admin", User.domain == domain),
                    User.role_items.any(
                        UserRole.role == "domain_admin", User.domain == domain
                    ),
                ),
                User.status == "active",
            )
            .order_by(User.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def count_unread(self, subscriber_id: int) -> int:
        """订阅者未读通知总数（全局角标用，精确计数而非列表近似）。

        与 ``list_notifications_page`` 的未读口径一致（``read_at IS NULL``），
        供 Header 角标 / 通知中心入口展示，避免前端拉列表近似导致 >100 条时不准。
        """
        stmt = (
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.subscriber_id == subscriber_id,
                Notification.read_at.is_(None),
            )
        )
        return int((await self._session.execute(stmt)).scalar_one() or 0)

    async def mark_all_read(self, subscriber_id: int) -> int:
        """将订阅者全部未读通知置为已读，返回更新条数。"""
        now = datetime.now(UTC)
        stmt = (
            update(Notification)
            .where(Notification.subscriber_id == subscriber_id, Notification.read_at.is_(None))
            .values(read_at=now)
        )
        res = await self._session.execute(stmt)
        return int(res.rowcount or 0)

    async def delete_notification(self, obj: Notification) -> None:
        await self._session.delete(obj)

    async def delete_all(self, subscriber_id: int) -> int:
        """删除订阅者全部通知（收件箱清空），返回删除条数。"""
        stmt = delete(Notification).where(Notification.subscriber_id == subscriber_id)
        res = await self._session.execute(stmt)
        return int(res.rowcount or 0)

    async def get_notification(self, notif_id: int) -> Notification | None:
        stmt = select(Notification).where(Notification.id == notif_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_subscription(
        self, user_id: int, channel: str, event_type: str
    ) -> SubscriptionPref | None:
        stmt = select(SubscriptionPref).where(
            SubscriptionPref.user_id == user_id,
            SubscriptionPref.channel == channel,
            SubscriptionPref.event_type == event_type,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_asset_subscription(
        self, user_id: int, channel: str, asset_type: str, asset_id: str
    ) -> SubscriptionPref | None:
        """按资产维度幂等查找订阅（P2 资产订阅：用户×渠道×资产唯一）。"""
        stmt = select(SubscriptionPref).where(
            SubscriptionPref.user_id == user_id,
            SubscriptionPref.channel == channel,
            SubscriptionPref.asset_type == asset_type,
            SubscriptionPref.asset_id == asset_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_subscriptions(self, user_id: int) -> list[SubscriptionPref]:
        stmt = select(SubscriptionPref).where(SubscriptionPref.user_id == user_id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_enabled_subscriptions(self, event_type: str) -> list[SubscriptionPref]:
        stmt = select(SubscriptionPref).where(
            SubscriptionPref.event_type == event_type,
            SubscriptionPref.enabled.is_(True),
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_asset_subscribers(
        self, asset_type: str, asset_id: str
    ) -> list[SubscriptionPref]:
        """按资产维度查找启用订阅者（P2 资产订阅：关注某指标/源表的用户）。"""
        stmt = select(SubscriptionPref).where(
            SubscriptionPref.asset_type == asset_type,
            SubscriptionPref.asset_id == asset_id,
            SubscriptionPref.enabled.is_(True),
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def save_subscription(self, obj: SubscriptionPref) -> SubscriptionPref:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def get_user_email(self, user_id: int) -> str | None:
        """按用户 ID 解析收件邮箱（订阅人为邮件投递真实收件人）。

        缺失或邮箱为空时返回 None，由调用方降级到配置的发件人/占位地址。
        """
        stmt = select(User.email).where(User.id == user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_user_display_name(self, user_id: int) -> str | None:
        """按用户 ID 解析展示姓名（操作人快照用）：display_name 优先，回落 username。

        用户不存在/已删除时返回 None——通知仍是历史记录，前端回落显示 ID 或隐藏。
        """
        stmt = select(User.display_name, User.username).where(User.id == user_id)
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        display_name, username = row
        return display_name or username

    async def list_admin_ids(self) -> list[int]:
        """查询全部启用的 platform_admin 用户 ID（字典未收录值待收录通知的收件人）。

        方案 A 多角色：主角色或 user_role 扩展角色为 platform_admin 均计入。
        """
        stmt = (
            select(User.id)
            .where(
                or_(
                    User.role == "platform_admin",
                    User.role_items.any(UserRole.role == "platform_admin"),
                ),
                User.status == "active",
            )
            .order_by(User.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def find_recent_notification_by_value_key(
        self,
        subscriber_id: int,
        template_code: str,
        value_key: str,
        window_seconds: int,
    ) -> Notification | None:
        """查询订阅者近 N 秒内是否已有同「字典值键」的未处理通知（精确去重）。

        字典未收录值通知按 ``(dict_type:value)`` 指纹去重：同一未收录值反复提交
        时不重复打扰管理员（payload 落 ``value_key`` 字段供检索）。
        """
        since = datetime.now(UTC) - timedelta(seconds=window_seconds)
        stmt = (
            select(Notification)
            .where(
                Notification.subscriber_id == subscriber_id,
                Notification.template_code == template_code,
                Notification.created_at >= since,
                Notification.handled_at.is_(None),
                Notification.payload["value_key"].as_string() == value_key,
            )
            .order_by(Notification.id.desc())
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def list_event_logs(self, event_type: str | None, limit: int) -> list[EventLog]:
        stmt = select(EventLog)
        if event_type:
            stmt = stmt.where(EventLog.event_type == event_type)
        rows = (
            (await self._session.execute(stmt.order_by(EventLog.id.desc()).limit(limit)))
            .scalars()
            .all()
        )
        return list(rows)

    async def purge_old_notifications(self, cutoff: datetime) -> int:
        """物理删除已读/已办结且非 FAILED 的过期通知（未读与待重试保留）。

        返回删除条数。通知为"已读即完成使命"的临时消息，超期后物理清理以控制
        存储增长；未读（用户未看）与 FAILED（待重试）永不删除。
        """
        stmt = delete(Notification).where(
            Notification.created_at < cutoff,
            Notification.status != NotifyStatus.FAILED.value,
            or_(
                Notification.read_at.is_not(None),
                Notification.handled_at.is_not(None),
            ),
        )
        res = await self._session.execute(stmt)
        return int(res.rowcount or 0)

    async def purge_old_event_logs(self, cutoff: datetime) -> int:
        """物理删除超过保留期的事件日志（审计性质的业务事件流留痕）。

        与 ``audit_log`` 独立——audit_log 有专门的 MinIO 归档机制，此处事件日志
        仅服务于通知投递与问题排查，超期直接清理。
        """
        stmt = delete(EventLog).where(EventLog.created_at < cutoff)
        res = await self._session.execute(stmt)
        return int(res.rowcount or 0)

    async def commit(self) -> None:
        await self._session.commit()
