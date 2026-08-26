"""SQL 智能推断评测样本模型（自定义可管理，与内置 GOLDEN 基线合并运行）。

内置基线（``sql_infer_eval.dataset.GOLDEN``）是代码级 pytest 依赖、只读不可改；
自定义样本落库（本表），业务用户可通过评测页 CRUD 管理——运行时与内置合并，
让「解析成功率」随样本持续扩充而度量（缺陷样本可入库追踪待修缺口，不阻断 CI）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class SqlInferEvalSample(Base, BaseModel):
    """SQL 智能推断评测样本（自定义，软删可恢复）。

    Attributes:
        case_id: 样本编码（内置固定 id；自定义可为用户可读短码，唯一）。
        dialect: 方言/场景标注（hive/oracle/spark/clickhouse/trino/...）。
        sql: 待解析的完整 SQL 脚本（多语句 ETL / 方言写法）。
        expected_measures: 期望度量 [{column, agg, alias?, table?}]。
        expected_tables: 期望源表集合 [str]。
        expected_period: 期望统计周期（day/week/month/quarter/year/hour）。
        note: 样本说明（缺陷场景/期望行为）。
        enabled: 是否参与评测（停用样本从报告与成功率分母剔除）。
        is_builtin: 内置基线标记（True 时前端只读不可改/删）。
        created_by: 创建人 ID。
    """

    __tablename__ = "sql_infer_eval_sample"

    case_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, comment="样本编码（唯一）"
    )
    dialect: Mapped[str] = mapped_column(
        String(32), nullable=False, default="hive", comment="方言/场景标注"
    )
    sql: Mapped[str] = mapped_column(Text, nullable=False, comment="待解析 SQL 脚本")
    expected_measures: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, comment="期望度量 [{column, agg, alias?, table?}]"
    )
    expected_tables: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, comment="期望源表集合"
    )
    expected_period: Mapped[str] = mapped_column(
        String(16), nullable=False, default="day", comment="期望统计周期"
    )
    note: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", comment="样本说明"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="是否参与评测"
    )
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="内置基线标记（只读）"
    )
    created_by: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="创建人 ID"
    )
