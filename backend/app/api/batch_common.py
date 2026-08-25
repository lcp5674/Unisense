"""共享批量操作 helper（统一「批量治理」复用模式）。

背景：指标/逻辑度量/维度/术语四类资产的批量审核端点此前各自实现/缺失，
形成重复代码。本模块集中批量执行语义（逐条容错 + DB 级异常回滚中止 + 审计
区分 full/partial/failed），各模块 API 复用，避免四套重复代码。

对齐 TD §13 批量治理契约：
- 幂等语义：单条业务异常（UnisenseError 等）只记该条失败，其余继续；
- SQLAlchemy DB 级异常（IntegrityError/OperationalError）会**污染会话**：
  flush 失败后会话处于 rolled-back 态，后续操作与最终 commit 都会抛
  InvalidRequestError，导致本可成功的项也全部失败、最终 500 整体回滚。
  因此对 SQLAlchemyError 单独处理：回滚清理会话，把剩余未执行项统一标记
  失败（返回部分成功语义，不再 500），并记日志供排查；
- 审计 action 区分 full（全成功）/ partial（部分失败）/ failed（全失败）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import public_error_message
from app.core.logging import get_logger

logger = get_logger(__name__)


class BatchItemResult(BaseModel):
    """批量操作的单条结果（逐条收集，不因单条失败整体回滚）。"""

    code: str
    ok: bool
    message: str = ""


class BatchResponse(BaseModel):
    """批量操作响应。"""

    results: list[BatchItemResult]
    ok_count: int
    fail_count: int


class BatchSubmitItem(BaseModel):
    """批量提交审核的单条项（DRAFT → REVIEW，可带评审指派 TD §13）。"""

    code: str = Field(..., max_length=64, description="实体编码")
    change_reason: str = Field(..., min_length=4, description="提交审核说明（为什么发布）")
    reviewer_id: int | None = Field(
        None, description="指定评审用户 ID（reviewer_type=user 时生效）"
    )
    reviewer_type: Literal["user", "domain"] | None = Field(
        None, description="评审指派类型: user(指定用户)/domain(域评审组)"
    )
    reviewer_domain: str | None = Field(
        None, max_length=64, description="域评审组所在域（缺省用实体自身域）"
    )

    @field_validator("reviewer_id", "reviewer_domain", mode="after")
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        """空字符串/0 归一为 None，前端未选择时传空串/0 不致校验失败。"""
        if v is None:
            return v
        if isinstance(v, str) and not v.strip():
            return None
        if isinstance(v, int) and v <= 0:
            return None
        return v


class BatchSubmitRequest(BaseModel):
    """批量提交审核请求（逐条带评审指派，指标/度量/维度/术语共用）。"""

    items: list[BatchSubmitItem] = Field(..., min_length=1, max_length=100)


class BatchCodesRequest(BaseModel):
    """批量操作（通过/废弃/发布等仅需编码）请求。"""

    codes: list[str] = Field(..., min_length=1, max_length=100)


class BatchRejectRequest(BaseModel):
    """批量审核驳回请求（REVIEW → DRAFT，原因统一作用于所有项）。"""

    codes: list[str] = Field(..., min_length=1, max_length=100)
    reason: str = Field(..., min_length=4, description="驳回原因（通知提交人引导修改）")


def batch_response(results: list[BatchItemResult]) -> BatchResponse:
    """汇总批量结果为统一响应结构（成功/失败计数）。"""
    return BatchResponse(
        results=results,
        ok_count=sum(1 for r in results if r.ok),
        fail_count=sum(1 for r in results if not r.ok),
    )


def batch_failed_codes(results: list[BatchItemResult], limit: int = 20) -> list[str]:
    """批量操作的失败明细（编码+原因），供审计逐条追溯；截断 20 条防审计膨胀。"""
    return [f"{r.code}: {r.message}" for r in results if not r.ok][:limit]


async def run_batch(
    db: AsyncSession,
    *,
    units: Sequence[Any],
    code_of: Callable[[Any], str],
    run: Callable[[Any], Awaitable[None]],
    abort_message: str,
) -> list[BatchItemResult]:
    """批量逐条执行：业务失败逐条收集（不整体回滚）；DB 级异常回滚会话并中止后续。

    幂等语义：单条业务异常（UnisenseError 等）只记该条失败，其余继续——这是
    批量治理端点的既定契约（TD §13）。但 SQLAlchemy 的 DB 级异常（如
    IntegrityError/OperationalError）会**污染会话**：flush 失败后会话处于
    rolled-back 态，后续任何操作与最终 commit 都会抛 InvalidRequestError，
    导致本可成功的项也全部失败、最终 500 整体回滚（C5 健壮性修复）。

    因此对 SQLAlchemyError 单独处理：回滚清理会话，把剩余未执行项统一标记
    失败（返回部分成功语义，不再 500），并把中止原因记日志供排查。
    """
    from sqlalchemy.exc import SQLAlchemyError

    results: list[BatchItemResult] = []
    for unit in units:
        code = code_of(unit)
        try:
            await run(unit)
            results.append(BatchItemResult(code=code, ok=True))
        except SQLAlchemyError:
            # DB 级异常：会话污染，后续操作/commit 必失败 → 回滚 + 剩余项标记失败。
            # **结果失真修复**：整会话回滚会把此前已 flush 未 commit 的成功项一并丢弃——
            # 若不改标失败，响应/审计会宣称「N 项成功」而库中实际未落（提交/通过类批量
            # 的严重失真）。故重建已执行项为失败（pydantic 模型不可变，需重建列表）。
            await db.rollback()
            rolled_back = [
                BatchItemResult(code=r.code, ok=False, message=abort_message)
                for r in results
            ]
            for rest in units[len(results):]:
                rolled_back.append(
                    BatchItemResult(
                        code=code_of(rest),
                        ok=False,
                        message=abort_message,
                    )
                )
            results = rolled_back
            logger.warning(
                "batch_aborted_on_db_error",
                action=abort_message,
                processed=len(results),
                total=len(units),
                exc_info=True,
            )
            break
        except Exception as exc:  # noqa: BLE001 - 批量逐条容错，业务失败不整体回滚
            results.append(
                BatchItemResult(code=code, ok=False, message=public_error_message(exc))
            )
    return results


def batch_audit_action(base: str, results: list[BatchItemResult]) -> str:
    """根据批量结果返回审计动作名：全成功/部分失败/全失败。

    生产合规场景下审计 action 须区分部分失败（此前部分失败仍记成功动作，误导审计）。
    """
    ok = sum(1 for r in results if r.ok)
    if ok == len(results):
        return base
    if ok == 0:
        return f"{base}_failed"
    return f"{base}_partial"
