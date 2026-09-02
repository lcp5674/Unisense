"""查询引擎（OLAP/MySQL 降级）DB 配置模型（单行，方案 A：前端可配置化）。

仿 ``llm_config`` 范式：DB 配置优先、环境变量兜底、密钥经 ``SecretManager``
Fernet 加密落库（``doris_password_enc`` / ``mysql_fallback_url_enc``），避免
明文连接串/密码入库与日志泄露。前端「系统配置」页通过
``/system/query-engine/config`` 端点读写本表；consume 执行器按生效配置指纹
热重建，保存后无需重启即生效（跨 worker 最长 30s）。

配置优先级：query_engine_config 行（enabled=true） > 环境变量（UNISENSE_OLAP_URL
/ UNISENSE_DORIS_* / UNISENSE_MYSQL_FALLBACK_URL） > 未配置降级。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class QueryEngineConfig(Base, BaseModel):
    """查询引擎连接配置（单行：OLAP + MySQL 降级）。

    Attributes:
        olap_url: OLAP 基础 URL（可选；提供且未显式 doris_host 时派生 host/port/database）。
        doris_host: Doris FE 主机（显式直连优先于 olap_url 派生）。
        doris_port: Doris FE HTTP 端口。
        doris_database: Doris 默认库（可空）。
        doris_user: Doris HTTP basic auth 用户名（可空=无认证）。
        doris_password_enc: Doris 密码（Fernet 加密令牌）。
        mysql_fallback_url_enc: MySQL 降级引擎完整 URL（Fernet 加密令牌）。
        enabled: 是否启用该 DB 配置（停用回落环境变量）。
        updated_by: 最后编辑者用户 ID。
    """

    __tablename__ = "query_engine_config"

    olap_url: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", comment="OLAP 基础 URL（可选）"
    )
    doris_host: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", comment="Doris FE 主机"
    )
    doris_port: Mapped[int] = mapped_column(
        Integer, nullable=False, default=8030, comment="Doris FE HTTP 端口"
    )
    doris_database: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", comment="Doris 默认库（可空）"
    )
    doris_user: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="Doris basic auth 用户名"
    )
    doris_password_enc: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="Doris 密码（Fernet 加密令牌）"
    )
    mysql_fallback_url_enc: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="MySQL 降级 URL（Fernet 加密令牌）"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="是否启用该 DB 配置"
    )
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="最后编辑者用户 ID"
    )
