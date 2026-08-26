"""SQL 智能推断评测运行记录模型。

每次「评测集运行」落一行（成功/失败用例数、各维度精确率/召回率、逐用例明细），
供前端评测页面可视化成功率趋势——「解析成功率」的可度量、可追踪载体。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class SqlInferEvalRun(Base, BaseModel):
    """SQL 智能推断评测运行记录。

    Attributes:
        total: 评测集用例总数。
        exact_count: 完全匹配（度量+表+周期全等）用例数。
        exact_rate: 完全匹配率（0~1）。
        measure_precision: 度量级精确率（宏平均）。
        measure_recall: 度量级召回率（宏平均）。
        table_precision: 表级精确率（宏平均）。
        table_recall: 表级召回率（宏平均）。
        period_match_rate: 周期匹配率（0~1）。
        cases_json: 逐用例明细（[{case_id, dialect, exact, ...}]）。
        elapsed_ms: 本次评测耗时（毫秒）。
        actor_id: 触发人 ID（可空：CLI/CI 运行）。
    """

    __tablename__ = "sql_infer_eval_run"

    total: Mapped[int] = mapped_column(Integer, nullable=False, comment="评测集用例总数")
    exact_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="完全匹配用例数"
    )
    exact_rate: Mapped[float] = mapped_column(
        Float, nullable=False, comment="完全匹配率（0~1）"
    )
    measure_precision: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="度量级精确率（宏平均）"
    )
    measure_recall: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="度量级召回率（宏平均）"
    )
    table_precision: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="表级精确率（宏平均）"
    )
    table_recall: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="表级召回率（宏平均）"
    )
    period_match_rate: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="周期匹配率（0~1）"
    )
    cases_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, comment="逐用例明细"
    )
    elapsed_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="本次评测耗时（毫秒）"
    )
    actor_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="触发人 ID（CLI/CI 为空）"
    )
    ran_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="评测运行时间（UTC）"
    )
