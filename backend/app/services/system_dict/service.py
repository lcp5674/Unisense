"""系统字典业务逻辑层。"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessError, ConflictError, NotFoundError
from app.models.system_dict import SystemDict
from app.services.notify.service import NotifyService
from app.services.system_dict.repository import SystemDictRepository
from app.services.system_dict.schemas import (
    DictBatchItem,
    DictBatchResult,
    DictItemCreate,
    DictItemUpdate,
)

logger = structlog.get_logger("unisense.system_dict.service")

# 字典未收录值通知去重窗口（秒）：同一未收录值在窗口内对同一管理员只通知一次，
# 防止反复保存同一脏值刷屏（对齐 notify 服务 _DEDUP_WINDOW_SECONDS 的治理口径）。
_NOTIFY_DEDUP_WINDOW_SECONDS = 60


def _reference_targets() -> dict[str, list[tuple[type, str, bool]]]:
    """字典类型 → 引用该编码的业务表列注册表（改码同步与引用计数的单一事实源）。

    返回 ``dict_type -> [(Model, column_name, is_enum), ...]``；``is_enum``
    表示该列是 MySQL ENUM——改码新值必须在值域内（运行时从表结构
    ``Column.type.enums`` 取，避免手工维护副本漂移），否则改码被拒绝。
    """
    from app.models.measure_catalog import MeasureCatalog
    from app.models.metric import Metric
    from app.models.metric_mount import MetricMount
    from app.models.metric_template import MetricTemplate

    return {
        "granularity": [
            (Metric, "granularity", False),
            (MetricMount, "granularity", False),
            (MetricTemplate, "granularity", False),
        ],
        "unit": [(Metric, "unit", False)],
        "currency": [(Metric, "currency", False)],
        "aggregation": [(Metric, "aggregation", True)],
        "time_semantics": [(Metric, "time_semantics", True)],
        "freshness": [(Metric, "freshness", True)],
        "dw_layer": [(Metric, "dw_layer", True)],
        "metric_type": [(Metric, "type", True)],
        "metric_tier": [(Metric, "metric_tier", True)],
        "serving_mode": [(Metric, "serving_mode", True)],
        "additivity": [(Metric, "additivity", True)],
        "measure_category": [(MeasureCatalog, "category", False)],
        "measure_format": [(MeasureCatalog, "measure_format", True)],
    }


async def _sync_json_references(db: Any, dict_type: str, old: str, new: str) -> int:
    """同步 JSON 列内嵌的字典值（SubjectDomain/MetricTemplate 的 defaults_json）。

    字段 → dict_type 映射复用 subject_domain service 的
    ``_DEFAULT_FIELD_DICT_TYPES``（延迟 import 防循环依赖）；逐行读改写
    （域/模板均为个位数量级），返回同步的行数。
    """
    from sqlalchemy import select

    from app.models.metric_template import MetricTemplate
    from app.models.subject_domain import SubjectDomain
    from app.services.subject_domain.service import _DEFAULT_FIELD_DICT_TYPES

    fields = [f for f, dt in _DEFAULT_FIELD_DICT_TYPES.items() if dt == dict_type]
    if not fields:
        return 0
    changed = 0
    for model in (SubjectDomain, MetricTemplate):
        result = await db.execute(select(model))
        for row in result.scalars().all():
            payload = dict(row.defaults_json or {})
            dirty = False
            for field in fields:
                # JSON 内嵌字典值大小写不敏感比较（与 SQL 引用同步口径一致）
                if str(payload.get(field) or "").lower() == (old or "").lower():
                    payload[field] = new
                    dirty = True
            if dirty:
                row.defaults_json = payload
                changed += 1
    await db.flush()
    return changed


class SystemDictService:
    """系统字典服务。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._repo = SystemDictRepository(db)

    async def list_by_type(
        self,
        dict_type: str,
        status: str | None = "active",
    ) -> list[SystemDict]:
        """获取某类型字典项列表（默认仅 active）。"""
        return await self._repo.list_by_type(dict_type, status)

    async def list_all_by_type(self, dict_type: str) -> list[SystemDict]:
        """获取某类型全部字典项（含 inactive），仅管理端用。"""
        return await self._repo.list_by_type(dict_type, status=None)

    async def list_dict_types(self) -> list[str]:
        """列出所有字典类型。"""
        return await self._repo.list_dict_types()

    async def get_item(self, dict_type: str, code: str) -> SystemDict:
        """获取单个字典项。"""
        item = await self._repo.get_item(dict_type, code)
        if item is None:
            raise NotFoundError(f"字典项不存在: {dict_type}/{code}")
        return item

    async def create_item(self, dict_type: str, data: DictItemCreate) -> SystemDict:
        """新增字典项。

        编码唯一性以「含软删除」全量判定：软删除行仍占用唯一索引
        （uk_dict_type_code），若命中软删除行则恢复并更新字段，避免
        IntegrityError → 500；命中 active 行则抛 ConflictError。
        ``data.code`` 未传时按显示名自动生成英文编码（冲突自动追加序号）。
        """
        code = data.code
        if not code:
            code = await self._generate_unique_code(dict_type, data.label)
        existing = await self._repo.get_item_including_deleted(dict_type, code)
        if existing is not None and existing.deleted_at is None:
            raise ConflictError(
                f"字典项已存在: {dict_type}/{code}",
                error_code="DUPLICATE_DICT_CODE",
            )
        if existing is not None:
            # 软删除行 → 恢复重建（去删除标记 + 回 active + 覆盖字段）
            existing.deleted_at = None
            existing.status = "active"
            existing.label = data.label
            existing.sort_order = data.sort_order
            existing.description = data.description
            existing.extra = data.extra
            item = await self._repo.update(existing)
            logger.info(
                "dict_item_restored",
                dict_type=dict_type,
                code=code,
                auto_generated=not data.code,
            )
            return item

        item = SystemDict(
            dict_type=dict_type,
            code=code,
            label=data.label,
            sort_order=data.sort_order,
            status="active",
            description=data.description,
            extra=data.extra,
        )
        item = await self._repo.create(item)
        logger.info(
            "dict_item_created",
            dict_type=dict_type,
            code=code,
            auto_generated=not data.code,
        )
        return item

    async def _generate_unique_code(self, dict_type: str, label: str) -> str:
        """按显示名自动生成唯一字典项编码。

        规则：中文经术语字典翻译（贪心最长匹配）、未覆盖字拼音兜底、
        ASCII 保留生成 slug（对齐 ``app.core.codegen.slugify_code``）；
        纯标点/空白等无可提取字符时回退 ``item``；冲突时追加 ``_2/_3/...``
        后缀（上限 100 次）。与主题域/数据源编码自动生成约定保持一致。
        """
        from app.core.codegen import (
            MAX_CODE_ATTEMPTS,
            MAX_CODE_LEN,
            generate_unique_code,
            slugify_code,
        )

        base = slugify_code(label) or "item"
        try:
            return await generate_unique_code(
                base,
                lambda cand: self._repo.code_exists_in_type(dict_type, cand),
                max_len=MAX_CODE_LEN,
                max_attempts=MAX_CODE_ATTEMPTS,
            )
        except RuntimeError as exc:
            raise BusinessError(
                f"无法为 {label} 生成唯一字典项编码，请手动指定",
                error_code="DICT_CODE_EXHAUSTED",
            ) from exc

    async def update_item(
        self,
        dict_type: str,
        code: str,
        data: DictItemUpdate,
    ) -> SystemDict:
        """更新字典项（code/label/sort_order/description/extra）。

        ``data.code`` 传入且与当前编码不同时执行**改码**（rename-with-sync）：
        校验格式/唯一性 → ENUM 值域校验 → 同步全部引用（指标/逻辑度量/挂载/
        模板/域默认值）→ 更新字典项编码；任一步失败整体回滚（API 层 rollback），
        不出现「引用已改、字典未改」或反向的半程状态。
        """
        item = await self.get_item(dict_type, code)
        new_code = (data.code or "").strip()
        # 仅大小写不同视为归一化（不触发引用同步——lower 比较下新旧语义一致，直接改码）
        if new_code and new_code.lower() != (code or "").lower():
            await self._rename_item_code(dict_type, item, code, new_code)
        if data.label is not None:
            item.label = data.label
        if data.sort_order is not None:
            item.sort_order = data.sort_order
        if data.description is not None:
            item.description = data.description
        if data.extra is not None:
            item.extra = data.extra
        item = await self._repo.update(item)
        logger.info("dict_item_updated", dict_type=dict_type, code=item.code)
        return item

    async def _rename_item_code(
        self,
        dict_type: str,
        item: SystemDict,
        old: str,
        new: str,
    ) -> dict[str, int]:
        """改码并同步全部引用（在同一事务内完成）。

        返回各引用列的同步行数（含 ``subject_domain.defaults_json`` 等内嵌值）。
        """
        targets = _reference_targets().get(dict_type, [])
        # ENUM 列值域校验：新编码必须是 DB 枚举的合法值，否则拒绝改码
        #（提示可选值 + 建议「新增新条目 + 停用旧条目」的替代路径）
        for model, column, is_enum in targets:
            if not is_enum:
                continue
            allowed = list(getattr(model.__table__.c, column).type.enums)
            if new not in allowed:
                raise BusinessError(
                    f"编码 {new} 不在该类型的数据列允许值内（{', '.join(allowed)}），"
                    "无法同步引用。如需其他编码，请新增正确编码的条目并停用本条目",
                    error_code="DICT_CODE_ENUM_CONSTRAINT",
                    ctx={"dict_type": dict_type, "allowed": allowed},
                )
        # 唯一性（含软删行——软删行仍占唯一索引，排除自身）
        dup = await self._repo.get_item_including_deleted(dict_type, new)
        if dup is not None and dup.id != item.id:
            raise ConflictError(
                f"字典项已存在: {dict_type}/{new}",
                error_code="DUPLICATE_DICT_CODE",
            )
        synced: dict[str, int] = {}
        from sqlalchemy import func, update

        for model, column, _is_enum in targets:
            # 引用同步大小写不敏感：列内旧值（可能大写）与字典旧码（可能小写）经
            # func.lower 归一比较后统一改写为新码——防「字典改码后旧值残留」。
            stmt = (
                update(model)
                .where(func.lower(getattr(model, column)) == (old or "").lower())
                .values({column: new})
            )
            result = await self._db.execute(stmt)
            rowcount = result.rowcount or 0
            if rowcount:
                synced[f"{model.__tablename__}.{column}"] = rowcount
        json_rows = await _sync_json_references(self._db, dict_type, old, new)
        if json_rows:
            synced["defaults_json"] = json_rows
        item.code = new
        await self._repo.update(item)
        logger.info(
            "dict_item_code_renamed",
            dict_type=dict_type,
            old_code=old,
            new_code=new,
            synced=synced,
        )
        return synced

    async def deactivate_item(self, dict_type: str, code: str) -> SystemDict:
        """停用字典项。"""
        item = await self.get_item(dict_type, code)
        item.status = "inactive"
        item = await self._repo.update(item)
        logger.info("dict_item_deactivated", dict_type=dict_type, code=code)
        return item

    async def activate_item(self, dict_type: str, code: str) -> SystemDict:
        """启用字典项。"""
        item = await self.get_item(dict_type, code)
        item.status = "active"
        item = await self._repo.update(item)
        logger.info("dict_item_activated", dict_type=dict_type, code=code)
        return item

    async def delete_item(self, dict_type: str, code: str) -> None:
        """删除字典项（需无引用）。"""
        item = await self.get_item(dict_type, code)
        ref_count = await self.get_ref_count(dict_type, code)
        if ref_count > 0:
            raise BusinessError(
                f"该字典项被 {ref_count} 处业务数据引用，不可删除，请先停用",
                error_code="HAS_REFERENCES",
            )
        await self._repo.soft_delete(item)
        logger.info("dict_item_deleted", dict_type=dict_type, code=code)

    async def batch_create_items(
        self,
        dict_type: str,
        items: list[DictItemCreate],
    ) -> DictBatchResult:
        """批量新增字典项（207 语义：单条失败逐项标注，不影响其余）。

        逐条复用 ``create_item`` 的编码自动生成 / 软删恢复 / 冲突判定；
        DB 层错误（并发唯一索引冲突等）经 savepoint 隔离回滚，不污染整批事务。
        业务错误（编码重复等）在写入前抛出，失败项记 ``error_code`` 不阻断其余。
        """
        succeeded: list[DictBatchItem] = []
        failed: list[DictBatchItem] = []
        for data in items:
            try:
                async with self._db.begin_nested():
                    item = await self.create_item(dict_type, data)
                succeeded.append(DictBatchItem(code=item.code, label=item.label, ok=True))
            except BusinessError as exc:
                failed.append(
                    DictBatchItem(
                        code=data.code or "",
                        label=data.label,
                        ok=False,
                        error_code=exc.error_code,
                        message=exc.message,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 批量单条失败不阻断其余（207 语义）
                failed.append(
                    DictBatchItem(
                        code=data.code or "",
                        label=data.label,
                        ok=False,
                        error_code="INTERNAL",
                        message=str(exc),
                    )
                )
        return DictBatchResult(succeeded=succeeded, failed=failed)

    async def batch_toggle_items(
        self,
        dict_type: str,
        codes: list[str],
        action: str,
    ) -> DictBatchResult:
        """批量启用/停用字典项（207 语义）。

        ``action`` 为 ``activate`` 或 ``deactivate``；不存在的编码记为
        ``NOT_FOUND`` 失败项，其余逐条切换状态。
        """
        succeeded: list[DictBatchItem] = []
        failed: list[DictBatchItem] = []
        for code in codes:
            try:
                async with self._db.begin_nested():
                    if action == "activate":
                        item = await self.activate_item(dict_type, code)
                    else:
                        item = await self.deactivate_item(dict_type, code)
                succeeded.append(DictBatchItem(code=item.code, label=item.label, ok=True))
            except NotFoundError as exc:
                failed.append(
                    DictBatchItem(
                        code=code,
                        ok=False,
                        error_code="NOT_FOUND",
                        message=exc.message,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 批量单条失败不阻断其余（207 语义）
                failed.append(
                    DictBatchItem(
                        code=code,
                        ok=False,
                        error_code="INTERNAL",
                        message=str(exc),
                    )
                )
        return DictBatchResult(succeeded=succeeded, failed=failed)

    async def batch_delete_items(
        self,
        dict_type: str,
        codes: list[str],
    ) -> DictBatchResult:
        """批量删除字典项（207 语义）。

        逐条复用 ``delete_item`` 的引用保护：被指标引用的项记为
        ``HAS_REFERENCES`` 失败项（提示先停用），其余软删除。
        """
        succeeded: list[DictBatchItem] = []
        failed: list[DictBatchItem] = []
        for code in codes:
            try:
                async with self._db.begin_nested():
                    await self.delete_item(dict_type, code)
                succeeded.append(DictBatchItem(code=code, ok=True))
            except BusinessError as exc:
                failed.append(
                    DictBatchItem(
                        code=code,
                        ok=False,
                        error_code=exc.error_code,
                        message=exc.message,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 批量单条失败不阻断其余（207 语义）
                failed.append(
                    DictBatchItem(
                        code=code,
                        ok=False,
                        error_code="INTERNAL",
                        message=str(exc),
                    )
                )
        return DictBatchResult(succeeded=succeeded, failed=failed)

    async def get_ref_count(self, dict_type: str, code: str) -> int:
        """获取字典项引用计数（被多少处业务数据引用）。

        复用 ``_reference_targets`` 注册表逐表计数（与改码同步的引用面严格
        一致）：指标 11 个字典字段 + 逻辑度量 category/measure_format +
        挂载/模板粒度。删除保护据此判定——引用面内任何一处引用都阻止删除。
        """
        from sqlalchemy import func, select

        targets = _reference_targets().get(dict_type)
        if not targets:
            return 0

        total = 0
        for model, column, _is_enum in targets:
            # 引用计数大小写不敏感（func.lower）：字典 code 与引用列存量值可能大小写
            # 不一致（如字典补录小写、metric 存量大写），精确匹配会漏检致误停用/误删。
            stmt = (
                select(func.count())
                .select_from(model)
                .where(
                    func.lower(getattr(model, column)) == (code or "").lower()
                )
            )
            # 指标表按软删过滤（软删指标不计引用）；其余表无 deleted_at 或行数极少
            if hasattr(model, "deleted_at"):
                stmt = stmt.where(model.deleted_at.is_(None))
            result = await self._db.execute(stmt)
            total += result.scalar() or 0
        return total

    async def validate_dict_value(self, dict_type: str, code: str) -> SystemDict:
        """校验字典值存在且 active（供指标注册时调用）。"""
        item = await self._repo.get_item(dict_type, code)
        if item is None:
            raise NotFoundError(
                f"字典值不存在: {dict_type}/{code}",
                error_code="DICT_VALUE_NOT_FOUND",
            )
        if item.status != "active":
            raise BusinessError(
                f"字典值已停用: {dict_type}/{code}",
                error_code="DICT_VALUE_INACTIVE",
            )
        return item

    async def verify_values(self, values: list[dict[str, str]]) -> list[dict[str, str]]:
        """批量检测字典值是否未收录（供指标保存前权威校验）。

        ``values`` 形如 ``[{"dict_type": "currency", "value": "XXX"}]``；返回其中
        **确实未收录** 的去重列表（DB 实时判定，避免前端字典快照过期导致误报）。
        """
        seen: set[tuple[str, str]] = set()
        unknown: list[dict[str, str]] = []
        for item in values:
            dict_type = str(item.get("dict_type") or "").strip()
            value = str(item.get("value") or "").strip()
            if not dict_type or not value:
                continue
            key = (dict_type, value)
            if key in seen:
                continue
            seen.add(key)
            if not await self._repo.item_exists(dict_type, value):
                unknown.append({"dict_type": dict_type, "value": value})
        return unknown

    async def notify_unknown_values(
        self,
        *,
        metric_code: str | None,
        values: list[dict[str, str]],
        actor_id: int,
        actor_name: str | None,
        note: str | None = None,
    ) -> dict[str, int]:
        """无收录权限用户保存未收录字典值时，通知全部平台管理员「收录或打回」。

        - 服务端复核 ``values`` 确实未收录（防伪造 / 字典刚被收录后的过期提交）
        - 逐值定向通知（``dict.unknown_pending``），payload 落 ``value_key`` 指纹，
          窗口内同一未收录值不重复打扰（防风暴）
        - 返回 ``{"notified": 通知条数, "unknown": 复核后未收录值数}``
        """
        unknown = await self.verify_values(values)
        if not unknown:
            return {"notified": 0, "unknown": 0}
        admin_ids = await self._repo.list_admin_ids()
        if not admin_ids:
            logger.info("dict_unknown_notify_no_admin", metric_code=metric_code)
            return {"notified": 0, "unknown": len(unknown)}
        notify_svc = NotifyService(self._db)
        notified = 0
        for item in unknown:
            dict_type = item["dict_type"]
            value = item["value"]
            value_key = f"{dict_type}:{value}"
            body_lines = [
                f"指标编码：{metric_code or '—'}",
                f"字典类型：{dict_type}",
                f"未收录值：{value}",
            ]
            if note:
                body_lines.append(f"提交说明：{note}")
            body_lines.append("请收录到系统字典，或打回让提交人改用字典内值。")
            for admin_id in admin_ids:
                recent = await notify_svc._repo.find_recent_notification_by_value_key(
                    admin_id,
                    "dict.unknown_pending",
                    value_key,
                    _NOTIFY_DEDUP_WINDOW_SECONDS,
                )
                if recent is not None:
                    logger.info(
                        "dict_unknown_notify_dedup_skipped",
                        admin_id=admin_id,
                        value_key=value_key,
                    )
                    continue
                await notify_svc.notify_user(
                    admin_id,
                    "dict.unknown_pending",
                    "字典未收录值待收录",
                    body="\n".join(body_lines),
                    payload={
                        "metric_code": metric_code,
                        "dict_type": dict_type,
                        "value": value,
                        "value_key": value_key,
                        "note": note,
                        "actor_id": actor_id,
                        "actor_name": actor_name,
                    },
                )
                notified += 1
        return {"notified": notified, "unknown": len(unknown)}

    async def reject_unknown_value(
        self,
        *,
        notification_id: int,
        reason: str | None,
        actor_id: int,
        actor_name: str | None,
    ) -> Any:
        """管理员打回字典收录申请：通知提交人改用字典内值，并办结原待办通知。

        仅 platform_admin 可调用（API 层 ``require_roles`` 强制）；通知提交人
        从原通知 payload 的 ``actor_id`` 反查（即提交时的操作人）。
        """
        notify_svc = NotifyService(self._db)
        notif = await notify_svc.get_notification(notification_id)
        if notif.template_code != "dict.unknown_pending":
            raise BusinessError(
                "该通知不是字典收录待办，无法打回",
                error_code="INVALID_NOTIFY_TYPE",
            )
        payload = notif.payload or {}
        metric_code = payload.get("metric_code")
        dict_type = payload.get("dict_type")
        value = payload.get("value")
        try:
            submitter_id = int(payload.get("actor_id") or 0) or None
        except (TypeError, ValueError):
            submitter_id = None
        if submitter_id:
            body_lines = [
                f"指标编码：{metric_code or '—'}",
                f"字典类型：{dict_type or '—'}",
                f"未收录值：{value or '—'}",
                "请改用系统字典内已有的取值后重新保存。",
            ]
            if reason:
                body_lines.append(f"打回原因：{reason}")
            await notify_svc.notify_user(
                submitter_id,
                "dict.unknown_rejected",
                "字典收录申请已被打回",
                body="\n".join(body_lines),
                payload={
                    "metric_code": metric_code,
                    "dict_type": dict_type,
                    "value": value,
                    "reason": reason,
                    "actor_id": actor_id,
                    "actor_name": actor_name,
                },
            )
        # 办结原待办通知（不再出现在「仅待处理」；已处理可追溯）
        await notify_svc.mark_handled(notification_id, actor_id, "platform_admin")
        return notif
