"""术语库服务（TD §12.14 / FR-08）。

职责：
1. 术语 CRUD + 状态机（DRAFT→PUBLISHED→DEPRECATED）。
2. 每次变更留存 `TermVersion` 快照（版本留痕）。
3. 同义词/别名重合率 > 80% 自动生成 `GlossaryConflict(OPEN)`，由 domain_admin 裁决。
4. 术语关系维护（SYNONYM_OF / BROADER_THAN / NARROWER_THAN / RELATED_TO）。
"""

from __future__ import annotations

import contextlib
import unicodedata
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.config import settings
from app.core.exceptions import (
    AuthError,
    BusinessError,
    ConflictError,
    NotFoundError,
    UnisenseError,
    ValidationError,
)
from app.core.logging import get_logger
from app.models.glossary import (
    GlossaryConflict,
    GlossaryConflictStatus,
    GlossaryConflictType,
    TermRelation,
    TermRelationType,
    TermSourceType,
    TermVersion,
)
from app.models.term import Term
from app.services.glossary.repository import GlossaryRepository
from app.services.glossary.schemas import (
    TermCreate,
    TermRelationCreate,
    TermRelationResponse,
    TermResponse,
    TermStatus,
)
from app.services.master_data_review.service import MasterDataReviewMixin

logger = get_logger("unisense.glossary.service")


def _normalize(token: str) -> str:
    return unicodedata.normalize("NFKC", token.strip().lower())
#: 合法关系类型 / 来源类型取值（DB Enum 列，非法值须在服务层转 4xx，而非 DB 500）。
_VALID_RELATION_TYPES = {e.value for e in TermRelationType}
_VALID_SOURCE_TYPES = {e.value for e in TermSourceType}

#: 术语推断的 LLM 响应格式（对齐指标/采集描述推断：json_schema 强约束优先 + json_object 降级）。
_TERM_INFER_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "term_infer",
        "schema": {
            "type": "object",
            "properties": {
                "definition": {"type": "string"},
                "synonyms": {"type": "array", "items": {"type": "string"}},
                "boundary": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["definition", "confidence"],
        },
    },
}
_TERM_JSON_OBJECT_FORMAT: dict[str, Any] = {"type": "json_object"}
_TERM_STRICT_JSON_HINT = "请严格只输出符合 JSON Schema 的 JSON，不要任何额外文字。"


def _overlap_ratio(a: list[str], b: list[str]) -> float:
    """两组词的归一化重叠率（Jaccard），用于同义词冲突判定。"""
    set_a = {_normalize(x) for x in a if x}
    set_b = {_normalize(x) for x in b if x}
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _get_synonym_threshold() -> float:
    """获取同义词冲突阈值（可配置，默认 0.8）。

    通过 settings.glossary_synonym_threshold 热更新，对齐 OPS-03 配置热更新。
    """
    return getattr(settings, "glossary_synonym_threshold", 0.8)


class GlossaryService(BaseService, MasterDataReviewMixin):
    """术语库服务：复用 ``MasterDataReviewMixin`` 审核流（DRAFT→REVIEW→PUBLISHED→DEPRECATED）。"""

    _review_entity_name = "术语"
    _review_event_prefix = "term"
    _review_code_attr = "term_code"
    _review_status_enum = TermStatus

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self._session = session
        self._repo = GlossaryRepository(session)

    async def _generate_term_code(self, data: TermCreate) -> str:
        """自动生成唯一术语编码。

        规则：``{domain_slug}_{name_slug}``；纯中文等无 ASCII 名回退 ``term``；
        冲突追加 ``_2/_3/...`` 后缀（上限 100 次）。
        """
        from app.core.codegen import generate_unique_code, slugify_code

        domain_slug = slugify_code(data.domain)
        name_slug = slugify_code(data.name)
        if domain_slug and name_slug:
            base = f"{domain_slug}_{name_slug}"
        elif name_slug:
            base = f"term_{name_slug}"
        elif domain_slug:
            base = f"term_{domain_slug}"
        else:
            base = "term"

        async def _exists(code: str) -> bool:
            return await self._repo.get_term(code) is not None

        return await generate_unique_code(base, _exists)

    async def create_term(
        self,
        data: TermCreate,
        actor_id: int | None = None,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> TermResponse:
        # 编码自动生成（FR-010：缺省时由系统生成，非人为创造）
        if not data.term_code:
            data.term_code = await self._generate_term_code(data)
        # 越权守卫：domain_admin/metric_owner 仅可创建本域术语（platform_admin 不限）。
        if (
            role
            and role != "platform_admin"
            and user_domain
            and data.domain
            and user_domain != data.domain
        ):
            raise AuthError(
                    f"无权创建他域术语（当前域: {user_domain}，术语域: {data.domain}）",
                    error_code="FORBIDDEN",
                    ctx={"user_domain": user_domain, "term_domain": data.domain},
                )
        existing = await self._repo.get_term(data.term_code)
        if existing is not None:
            raise ConflictError(f"术语编码已存在: {data.term_code}", error_code="TERM_EXISTS")
        term = Term(
            term_code=data.term_code,
            name=data.name,
            definition=data.definition,
            domain=data.domain,
            synonyms=list(data.synonyms),
            boundary=data.boundary,
            status=TermStatus.DRAFT.value,
            # PLAT-2: 认证身份优先，client 传入的 owner_id 仅作降级
            owner_id=actor_id if actor_id is not None else data.owner_id,
        )
        term = await self._repo.save_term(term)
        await self._snapshot(term, actor_id or 0, "create")
        await self._detect_conflicts(term)
        await self._repo.commit()
        return TermResponse.from_model(term)

    async def get_term(self, term_code: str) -> TermResponse:
        term = await self._repo.get_term(term_code)
        if term is None:
            raise NotFoundError(f"术语不存在: {term_code}")
        return TermResponse.from_model(term)

    async def list_terms(
        self,
        domain: str | None,
        status: str | None,
        search: str | None,
        limit: int,
        offset: int,
        owner_id: int | None = None,
        deleted: bool = False,
    ) -> tuple[list[TermResponse], int]:
        rows, total = await self._repo.list_terms(
            domain, status, search, limit, offset, owner_id, deleted
        )
        return [TermResponse.from_model(t) for t in rows], total

    async def submit_term(
        self,
        term_code: str,
        request: Any,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> TermResponse:
        """提交术语审核（DRAFT → REVIEW，复用主数据审核流 TD §13）。

        业务用户发布术语须走审核流（submit → approve）；admin 直发走 ``publish_term``。
        """
        term = await self._require_term(term_code)
        await self._submit_review(
            term, request, actor_id, role, user_domain, code=term_code
        )
        return TermResponse.from_model(term)

    async def publish_term(self, term_code: str, actor_id: int) -> TermResponse:
        """直接发布术语（平台管理员直发通道，含"再次发布"能力）。

        业务用户发布须走审核流（submit_term → approve_term）；
        本方法保留为系统/种子/管理员兜底直发（API 层收紧为 platform_admin），
        避免造数与批量导入场景被迫走审核流程。已发布幂等返回；已废弃可再次发布。
        """
        term = await self._require_term(term_code)
        if term.status == TermStatus.PUBLISHED.value:
            return TermResponse.from_model(term)
        if term.status not in (TermStatus.DRAFT.value, TermStatus.DEPRECATED.value):
            raise BusinessError(
                f"当前状态不可发布: {term.status}", error_code="INVALID_STATE"
            )
        term.status = TermStatus.PUBLISHED.value
        await self._snapshot(term, actor_id, "publish")
        await self._repo.commit()
        return TermResponse.from_model(term)

    async def approve_term(
        self,
        term_code: str,
        request: Any,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> TermResponse:
        """审核通过术语（REVIEW → PUBLISHED，复用主数据审核流 FR-004）。"""
        term = await self._require_term(term_code)
        await self._approve_review(
            term, request, actor_id, role, user_domain, code=term_code
        )
        await self._snapshot(term, actor_id, "approve")
        return TermResponse.from_model(term)

    async def reject_term(
        self,
        term_code: str,
        request: Any,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> TermResponse:
        """审核驳回术语（REVIEW → DRAFT，复用主数据审核流 FR-005）。"""
        term = await self._require_term(term_code)
        await self._reject_review(
            term, request, actor_id, role, user_domain, code=term_code
        )
        return TermResponse.from_model(term)

    async def update_term(
        self,
        term_code: str,
        data: Any,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> TermResponse:
        term = await self._require_term(term_code)
        self._assert_term_scope(term, actor_id, role, user_domain)
        # P11 C-2：跨请求乐观锁——前端编辑弹窗回传 row_version，不一致说明他人已改 → 409
        expected = getattr(data, "row_version", None)
        if expected is not None and expected != term.row_version:
            raise ConflictError(
                "术语已被他人修改，请刷新后重试",
                error_code="OPTIMISTIC_LOCK_CONFLICT",
                ctx={
                    "term_code": term_code,
                    "current_row_version": term.row_version,
                    "expected_row_version": expected,
                },
            )
        # 审核中锁定（REVIEW）：评审人基于当前定义审核，审核中改定义会造成评审失真；
        # 驳回回 DRAFT 后即可修改重提（对齐指标 REVIEW 编辑即撤回的语义）。
        if term.status == TermStatus.REVIEW.value:
            raise BusinessError(
                f"审核中的术语不可编辑（{term_code}），请等待审核结果或驳回后修改",
                error_code="INVALID_STATE",
            )
        if data.term_code is not None and data.term_code != term.term_code:
            # 编码编辑唯一性校验（防与其他术语冲突）
            existing = await self._repo.get_term(data.term_code)
            if existing is not None:
                raise ConflictError(f"术语编码已存在: {data.term_code}", error_code="TERM_EXISTS")
            term.term_code = data.term_code
        if data.name is not None:
            term.name = data.name
        if data.definition is not None:
            term.definition = data.definition
        if data.domain is not None:
            term.domain = data.domain
        if data.synonyms is not None:
            term.synonyms = list(data.synonyms)
        if data.boundary is not None:
            term.boundary = data.boundary
        # 防御式递增（测试构造的简易对象可能无 row_version 属性）
        term.row_version = (getattr(term, "row_version", None) or 1) + 1
        await self._snapshot(term, actor_id, "update")
        await self._detect_conflicts(term)
        await self._repo.commit()
        return TermResponse.from_model(term)

    async def deprecate_term(
        self,
        term_code: str,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> TermResponse:
        term = await self._require_term(term_code)
        self._assert_term_scope(term, actor_id, role, user_domain)
        if term.status == TermStatus.DEPRECATED.value:
            raise BusinessError("术语已废弃", error_code="INVALID_STATE")
        term.status = TermStatus.DEPRECATED.value
        await self._snapshot(term, actor_id, "deprecate")
        await self._repo.commit()
        return TermResponse.from_model(term)

    async def reactivate_term(
        self,
        term_code: str,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> TermResponse:
        """重新启用已废弃术语（DEPRECATED → DRAFT）。

        已废弃术语为终态，重新启用后回到草稿态，可编辑后**重新走审核**（与
        DRAFT→REVIEW→PUBLISHED 审核流一致，避免绕过审核直接复活）。仅平台
        管理员/本域域管理员或原 Owner 可执行（API 层写角色 + service 层
        ``_assert_term_scope`` 域/Owner 校验）。
        """
        term = await self._require_term(term_code)
        if term.status != TermStatus.DEPRECATED.value:
            raise UnisenseError(
                f"仅 DEPRECATED 状态可重新启用，当前 {term.status}",
                error_code="INVALID_STATE",
            )
        self._assert_term_scope(term, actor_id, role, user_domain)
        term.status = TermStatus.DRAFT.value
        await self._snapshot(term, actor_id, "reactivate")
        await self._repo.commit()
        logger.info("term_reactivated", term_code=term_code, actor_id=actor_id)
        return TermResponse.from_model(term)

    async def delete_term(
        self,
        term_code: str,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> TermResponse:
        """软删除术语（仅 DRAFT/DEPRECATED 未对外投入状态；REVIEW/PUBLISHED 禁止）。

        删除语义（用户决策）：草稿/废弃这种未对外投入的可交由管理员或生产者
        （原 Owner）软删；审核中/启用中的资源不可删。软删后进入回收站，可经
        ``restore_term`` 恢复（对齐维度/度量生命周期）。
        """
        term = await self._require_term(term_code)
        if term.status not in (
            TermStatus.DRAFT.value,
            TermStatus.DEPRECATED.value,
        ):
            raise UnisenseError(
                f"仅 DRAFT/DEPRECATED 状态的术语可删除（当前 {term.status}）；"
                "审核中/启用中的资源不可删除",
                error_code="INVALID_STATE",
            )
        # 权限：平台/本域域管理员或原 Owner（生产者，``_assert_term_scope`` 域/Owner 双校验）
        self._assert_term_scope(term, actor_id, role, user_domain)
        await self._repo.soft_delete_term(term.id)
        await self._snapshot(term, actor_id, "delete")
        await self._repo.commit()
        logger.info("term_deleted", term_code=term_code, actor_id=actor_id, role=role)
        return TermResponse.from_model(term)

    async def restore_term(
        self,
        term_code: str,
        actor_id: int,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> TermResponse:
        """恢复已软删术语（回收站恢复；仅 DRAFT/DEPRECATED 且 deleted_at 置位）。

        仅平台/本域域管理员或原 Owner 可恢复（对齐删除语义）。清除 deleted_at 使
        术语重新进入正常列表，重新走审核流。
        """
        term = await self._repo.get_term_including_deleted(term_code)
        if term is None:
            raise NotFoundError(f"术语不存在: {term_code}")
        if term.deleted_at is None:
            raise UnisenseError(
                f"术语 {term_code} 未处于已删除状态，无需恢复",
                error_code="INVALID_STATE",
            )
        if term.status not in (
            TermStatus.DRAFT.value,
            TermStatus.DEPRECATED.value,
        ):
            raise UnisenseError(
                f"仅 DRAFT/DEPRECATED 状态的已删术语可恢复，当前 {term.status}",
                error_code="INVALID_STATE",
            )
        self._assert_term_scope(term, actor_id, role, user_domain)
        await self._repo.restore_term(term.id)
        await self._snapshot(term, actor_id, "restore")
        await self._repo.commit()
        logger.info("term_restored", term_code=term_code, actor_id=actor_id)
        return TermResponse.from_model(term)

    async def list_conflicts(self, status: str | None) -> list[Any]:
        rows = await self._repo.list_conflicts(status)
        return [_conflict_to_resp(r) for r in rows]

    async def resolve_conflict(self, conflict_id: int, decision: str, resolver_id: int) -> Any:
        conflict = await self._repo.get_conflict(conflict_id)
        if conflict is None:
            raise NotFoundError(f"术语冲突不存在: {conflict_id}")
        if decision not in (
            GlossaryConflictStatus.RESOLVED.value,
            GlossaryConflictStatus.IGNORED.value,
        ):
            raise BusinessError(f"未知裁决: {decision}", error_code="INVALID_DECISION")
        conflict.status = GlossaryConflictStatus(decision)
        conflict.resolver = resolver_id
        await self._repo.commit()
        return _conflict_to_resp(conflict)

    async def create_term_relation(
        self,
        term_code: str,
        data: TermRelationCreate,
        actor_id: int | None = None,
        role: str | None = None,
        user_domain: str | None = None,
    ) -> TermRelationResponse:
        term = await self._require_term(term_code)
        # 越权守卫：仅本人/本域可给源术语建立关系（防跨域污染术语关系网）。
        if actor_id is not None:
            self._assert_term_scope(term, actor_id, role, user_domain)
        # 目标术语存在性校验（防孤儿关系落到库）
        target = await self._repo.get_term_by_id(data.target_term_id)
        if target is None:
            raise NotFoundError(
                f"目标术语不存在: {data.target_term_id}",
                error_code="TERM_TARGET_NOT_FOUND",
                ctx={"target_term_id": data.target_term_id},
            )
        # enum 显式校验：非法值须转 4xx，而非触达 DB Enum 抛 500
        if data.relation_type not in _VALID_RELATION_TYPES:
            raise ValidationError(
                f"未知术语关系类型: {data.relation_type}",
                error_code="INVALID_RELATION_TYPE",
                ctx={"relation_type": data.relation_type},
            )
        if data.source_type not in _VALID_SOURCE_TYPES:
            raise ValidationError(
                f"未知术语来源类型: {data.source_type}",
                error_code="INVALID_SOURCE_TYPE",
                ctx={"source_type": data.source_type},
            )
        # 自引用关系防护（防自环）
        if target.id == term.id:
            raise ConflictError(
                "术语不能与自身建立关系",
                error_code="SELF_RELATION",
                ctx={"term_code": term_code, "term_id": term.id},
            )
        # 重复关系预检：同对（源/目标/类型）已存在时 409，而非触达 uk_term_pair 抛 500
        existing = await self._repo.get_term_relation(
            term.id, target.id, TermRelationType(data.relation_type).value
        )
        if existing is not None:
            raise ConflictError(
                "该术语关系已存在",
                error_code="DUPLICATE_TERM_RELATION",
                ctx={
                    "term_code": term_code,
                    "target_term_id": target.id,
                    "relation_type": data.relation_type,
                },
            )
        relation = TermRelation(
            source_term_id=term.id,
            target_term_id=target.id,
            relation_type=TermRelationType(data.relation_type).value,
            declared_by=data.declared_by,
            source_type=data.source_type,
        )
        relation = await self._repo.save_term_relation(relation)
        await self._repo.commit()
        return TermRelationResponse.from_model(relation)

    async def list_term_relations(self, term_code: str) -> list[dict[str, Any]]:
        """查某术语的全部关系（作为源或目标），供前端关系图谱/详情展示。

        Returns:
            每个元素 ``{relation_type, direction, peer: {id, term_code, name, domain, status}}``
            ——``direction`` 为 ``outgoing``（本术语→对端）/ ``incoming``（对端→本术语），
            前端据此区分箭头方向并渲染关系标签。
        """
        term = await self._require_term(term_code)
        rows = await self._repo.list_term_relations(term.id)
        out: list[dict[str, Any]] = []
        for row in rows:
            relation = row["relation"]
            peer = row["peer"]
            if peer is None:
                continue
            is_source = relation.source_term_id == term.id
            out.append(
                {
                    "relation_type": row["relation_type"],
                    "direction": "outgoing" if is_source else "incoming",
                    "peer": {
                        "id": peer.id,
                        "term_code": peer.term_code,
                        "name": peer.name,
                        "domain": peer.domain,
                        "status": peer.status,
                    },
                }
            )
        return out

    async def infer_term_suggestion(self, name: str) -> dict[str, Any]:
        """基于术语名称用 LLM 推断定义/同义词/边界说明（返回结构化建议）。

        复用 ``LlmConfigService.build_client``（DB 配置优先 + 路由/熔断）；
        LLM 不可用/超时/解析失败时抛 ``UnisenseError``（前端据此提示而非静默）。
        推断结果仅作建议，不落库（由用户在弹窗确认后经 create/update 提交）。
        """
        client = None
        try:
            client = await self._build_llm_client()
            if not getattr(client, "enabled", False):
                raise BusinessError(
                    "LLM 未配置，无法推断术语建议",
                    error_code="LLM_INFER_UNAVAILABLE",
                )
            from app.services.llm.parse import parse_term_infer_result

            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是企业数据术语治理专家。根据术语名称，推断该业务术语的定义、同义词与边界说明。\n"
                        "返回 JSON 格式：{\n"
                        '  "definition": "术语的中文定义，20-100字",\n'
                        '  "synonyms": ["英文/别名同义词", ...],\n'
                        '  "boundary": "边界说明（排除范围，如 不含xxx，可空）",\n'
                        '  "confidence": 0.0-1.0\n'
                        "}\n"
                        "要求：定义准确、口径明确；同义词不超过 5 个；confidence < 0.5 表示不确定"
                    ),
                },
                {"role": "user", "content": f"术语名称: {name}"},
            ]
            for attempt in (0, 1):
                aug = messages
                if attempt:
                    aug = [*messages, {"role": "user", "content": _TERM_STRICT_JSON_HINT}]
                for fmt in (_TERM_INFER_FORMAT, _TERM_JSON_OBJECT_FORMAT):
                    try:
                        result = await client.chat(
                            aug, temperature=0.0, max_tokens=400, response_format=fmt
                        )
                    except Exception:  # noqa: BLE001 - LLM 网关错误按格式失败降级重试
                        continue
                    parsed = parse_term_infer_result(result.get("content", ""))
                    if parsed is not None:
                        return parsed
            logger.warning("llm_infer_term_all_formats_failed", name=name)
            raise BusinessError(
                "LLM 推断失败，请稍后重试或手动填写", error_code="LLM_INFER_UNAVAILABLE"
            )
        except (TimeoutError, ConnectionError, OSError) as exc:
            logger.warning("llm_infer_term_timeout_error: %s", exc)
            raise BusinessError(
                "LLM 服务不可用，请稍后重试或手动填写", error_code="LLM_INFER_UNAVAILABLE"
            ) from exc
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("llm_infer_term_format_error: %s", exc)
            raise BusinessError(
                "LLM 返回格式异常，请手动填写", error_code="LLM_INFER_UNAVAILABLE"
            ) from exc
        except RuntimeError as exc:
            logger.warning("llm_infer_term_runtime_error: %s", exc)
            raise BusinessError(
                "LLM 运行时错误，请手动填写", error_code="LLM_INFER_UNAVAILABLE"
            ) from exc
        finally:
            if client is not None:
                with contextlib.suppress(Exception):  # noqa: BLE001 - 关闭失败不阻断
                    await client.close()

    # ---- 内部辅助 ----
    def _assert_term_scope(
        self,
        term: Term,
        actor_id: int,
        role: str | None,
        user_domain: str | None,
    ) -> None:
        """术语写操作越权守卫（对齐指标/维度/度量的域作用域 + 生产者 Owner 语义）。

        - ``platform_admin``：放行（全局治理兜底）。
        - ``domain_admin``：术语域与操作人域一致才可操作（防跨域治理失控）。
        - ``metric_owner``：仅本人创建的术语（owner 匹配）且域一致（生产者语义）。
        - 其余角色：一律拒绝。

        术语是业务概念标准层（domain 语义），domain_admin 跨域改删会破坏
        他域口径治理；owner 校验防 metric_owner 篡改他人术语。
        """
        if not role or role == "platform_admin":
            return
        term_domain = str(getattr(term, "domain", "") or "")
        if role == "domain_admin":
            if user_domain and term_domain and user_domain != term_domain:
                raise AuthError(
                    f"无权操作他域术语（当前域: {user_domain}，术语域: {term_domain}）",
                    error_code="FORBIDDEN",
                    ctx={
                        "term_code": term.term_code,
                        "user_domain": user_domain,
                        "term_domain": term_domain,
                    },
                )
            return
        if role == "metric_owner":
            if term.owner_id != actor_id:
                raise AuthError(
                    "无权操作他人术语",
                    error_code="FORBIDDEN",
                    ctx={
                        "term_code": term.term_code,
                        "actor_id": actor_id,
                        "owner_id": term.owner_id,
                    },
                )
            if user_domain and term_domain and user_domain != term_domain:
                raise AuthError(
                    f"无权操作他域术语（当前域: {user_domain}，术语域: {term_domain}）",
                    error_code="FORBIDDEN",
                    ctx={
                        "term_code": term.term_code,
                        "user_domain": user_domain,
                        "term_domain": term_domain,
                    },
                )
            return
        raise AuthError(
            "无权操作该术语",
            error_code="FORBIDDEN",
            ctx={"term_code": term.term_code, "role": role},
        )

    async def _require_term(self, term_code: str) -> Term:
        """加载术语并校验可操作：不存在或已软删（回收站）均拒绝。

        已软删记录除「恢复」外不可变——防止回收站中的术语被更新/提交/通过/
        发布/废弃/重新启用等操作复活成矛盾态。恢复用
        ``_repo.get_term_including_deleted`` 直取，不走本守卫。
        """
        term = await self._repo.get_term(term_code)
        if term is None:
            raise NotFoundError(f"术语不存在: {term_code}")
        if getattr(term, "deleted_at", None) is not None:
            raise UnisenseError(
                f"已删除的术语不可执行该操作（{term_code}），请先在回收站恢复",
                error_code="INVALID_STATE",
            )
        return term

    async def _build_llm_client(self) -> Any:
        """构建 LLM 客户端（DB 配置优先 + 路由/熔断；未配置返回禁用客户端）。"""
        from app.services.llm.config_service import LlmConfigService

        return await LlmConfigService(self._session).build_client()

    async def _snapshot(self, term: Term, actor_id: int, note: str) -> None:
        existing_count = await self._repo.count_term_versions(term.id)
        next_version = existing_count + 1
        snapshot = TermVersion(
            term_id=term.id,
            version=next_version,
            snapshot={
                "term_code": term.term_code,
                "name": term.name,
                "definition": term.definition,
                "domain": term.domain,
                "synonyms": list(getattr(term, "synonyms", []) or []),
                "boundary": getattr(term, "boundary", None),
                "status": term.status,
            },
            changed_by=actor_id,
            change_note=note,
        )
        await self._repo.save_term_version(snapshot)

    async def _detect_conflicts(self, term: Term) -> None:
        others = await self._repo.all_terms()
        term_tokens = {_normalize(term.name)} | {_normalize(s) for s in (term.synonyms or [])}
        for other in others:
            if other.id == term.id:
                continue
            other_synonyms = list(getattr(other, "synonyms", []) or [])
            # 名称精确冲突
            if _normalize(other.name) in term_tokens:
                await self._add_conflict(term, GlossaryConflictType.NAME_OVERLAP, other.id)
                continue
            # 同义词重叠率超阈值
            ratio = _overlap_ratio(term.synonyms or [], other_synonyms)
            threshold = _get_synonym_threshold()
            if ratio > threshold:
                await self._add_conflict(term, GlossaryConflictType.ALIAS_OVERLAP, other.id)

    async def _add_conflict(
        self, term: Term, ctype: GlossaryConflictType, ref_term_id: int
    ) -> None:
        # P2-11：冲突行关联指标——若该术语已绑定指标（metric.term_id），
        # 填充 ref_metric_id，前端「关联指标」列展示该冲突影响的指标（此前恒空）。
        from sqlalchemy import select

        from app.models.metric import Metric

        ref_metric_id: int | None = None
        row = (
            await self._session.execute(
                select(Metric.id)
                .where(Metric.term_id == term.id, Metric.deleted_at.is_(None))
                .limit(1)
            )
        ).first()
        if row is not None:
            ref_metric_id = int(row[0])

        conflict = GlossaryConflict(
            term_id=term.id,
            conflict_type=ctype.value,
            ref_term_id=ref_term_id,
            ref_metric_id=ref_metric_id,
            status=GlossaryConflictStatus.OPEN.value,
        )
        await self._repo.save_conflict(conflict)


def _conflict_to_resp(r: GlossaryConflict) -> Any:
    from app.services.glossary.schemas import GlossaryConflictResponse

    return GlossaryConflictResponse.from_model(r)
