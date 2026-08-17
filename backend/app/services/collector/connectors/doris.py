"""Doris 连接器（对齐 TD §12.1 / spec FR-001）。

Doris 使用 MySQL 协议兼容驱动（mysql+aiomysql），
复用 InformationSchemaCollector 查询内部 information_schema。

- MySQL 协议兼容
- 单表 try/catch 跳过容错
- @registry.register("doris") 注册
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import URL

from app.services.collector.connectors.collector_registry import registry
from app.services.collector.connectors.mysql import InformationSchemaCollector, SqlalchemyConnector


def _build_doris_url(cfg: dict[str, Any]) -> URL:
    """使用 SQLAlchemy URL.create() 构建 Doris 连接 URL（MySQL 协议兼容）。"""
    return URL.create(
        drivername="mysql+aiomysql",
        username=cfg.get("user", ""),
        password=cfg.get("password", ""),
        host=cfg.get("host", "127.0.0.1"),
        port=cfg.get("port", 9030),
        database=cfg.get("database", ""),
    )


@registry.register("doris")
def create_doris_collector(cfg: dict[str, Any]) -> InformationSchemaCollector:
    """Doris 采集器工厂函数（MySQL 协议兼容，复用 InformationSchemaCollector）。

    SSRF 防护：URL 一律由受控字段构建，禁止任意 ``db_url`` 覆盖。
    """
    db_url = _build_doris_url(cfg)
    connector = SqlalchemyConnector(db_url, connect_timeout=10, query_timeout=60)
    return InformationSchemaCollector(connector, database=cfg.get("database"))
