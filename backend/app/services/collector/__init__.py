"""采集领域服务包（对齐 TD §12.1）。

提供数据源注册、元数据（db_catalog）注册/清点、敏感分级、SPI 采集器与
批量废弃能力。详见 ``service.py``。
"""

from app.services.collector.service import CollectorService

__all__ = ["CollectorService"]
