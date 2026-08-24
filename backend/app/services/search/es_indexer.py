"""ES 索引管理与数据同步（TD §1.3 ES 检索加速层落地）。

背景：指标/术语两类检索此前为 MySQL LIKE；ES 8.15 服务已部署但未接线
（仅就绪探针）。本服务负责：
- ``ensure_indexes``：创建 metric_idx / term_idx 索引映射（幂等）；
- ``sync_metrics`` / ``sync_terms`` / ``sync_all``：从 MySQL 全量灌入 ES
  （按业务编码 upsert，支持重复执行）；
- ES 未配置/不可用时静默跳过（检索路径由 ``global_search`` 自动降级 MySQL LIKE，
  对齐 TD §1.3 降级边界 "ES✗→退 MySQL"）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.es_client import EsClient, get_es_client
from app.core.logging import get_logger
from app.models.measure_catalog import MeasureCatalog
from app.models.metric import Metric
from app.models.term import Term

logger = get_logger(__name__)

_METRIC_INDEX = "metric_idx"
_TERM_INDEX = "term_idx"

_METRIC_MAPPING: dict[str, Any] = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "properties": {
            "id": {"type": "long"},
            "metric_code": {"type": "keyword"},
            "name": {"type": "text"},
            "description": {"type": "text"},
            "domain": {"type": "keyword"},
            "status": {"type": "keyword"},
            "pii_flag": {"type": "boolean"},
            # 关联逻辑度量同义词（业务别名，如"支付金额"→"pay"），参与 multi_match
            "synonyms": {"type": "text"},
        }
    },
}

_TERM_MAPPING: dict[str, Any] = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "properties": {
            "id": {"type": "long"},
            "term_code": {"type": "keyword"},
            "name": {"type": "text"},
            "definition": {"type": "text"},
            "domain": {"type": "keyword"},
            "status": {"type": "keyword"},
            "synonyms": {"type": "text"},
        }
    },
}


class EsIndexer:
    """ES 索引管理与 MySQL → ES 数据同步。"""

    def __init__(self, session: AsyncSession, es_client: EsClient | None = None) -> None:
        self._session = session
        self._es = es_client or get_es_client()

    @property
    def enabled(self) -> bool:
        """ES 是否可用（未配置/未安装/熔断均视为不可用，调用方降级 MySQL）。"""
        return self._es.enabled

    async def ensure_indexes(self) -> dict[str, bool]:
        """幂等创建索引映射；已存在返回 False（不报错）。ES 不可用抛 SearchUnavailableError。"""
        return {
            _METRIC_INDEX: await self._es.create_index(_METRIC_INDEX, _METRIC_MAPPING),
            _TERM_INDEX: await self._es.create_index(_TERM_INDEX, _TERM_MAPPING),
        }

    async def sync_metrics(self) -> int:
        """全量灌入指标（含关联逻辑度量同义词）；按 metric_code 作为 doc_id upsert。"""
        rows = (
            await self._session.execute(
                select(Metric, MeasureCatalog.synonyms)
                .outerjoin(MeasureCatalog, Metric.measure_id == MeasureCatalog.id)
                .where(Metric.deleted_at.is_(None))
            )
        ).all()
        count = 0
        for metric, synonyms in rows:
            await self._es.index(
                _METRIC_INDEX,
                {
                    "id": metric.id,
                    "metric_code": metric.metric_code,
                    "name": metric.name,
                    "description": metric.description or "",
                    "domain": metric.domain,
                    "status": metric.status,
                    "pii_flag": bool(metric.pii_flag),
                    "synonyms": _join_synonyms(synonyms),
                },
                doc_id=str(metric.id),
            )
            count += 1
        return count

    async def sync_terms(self) -> int:
        """全量灌入术语；按 term_code 作为 doc_id upsert。"""
        rows = (
            await self._session.execute(
                select(Term).where(Term.deleted_at.is_(None))
            )
        ).scalars().all()
        count = 0
        for term in rows:
            await self._es.index(
                _TERM_INDEX,
                {
                    "id": term.id,
                    "term_code": term.term_code,
                    "name": term.name,
                    "definition": term.definition or "",
                    "domain": term.domain,
                    "status": term.status,
                    "synonyms": _join_synonyms(term.synonyms),
                },
                doc_id=str(term.id),
            )
            count += 1
        return count

    async def sync_all(self) -> dict[str, int]:
        """全量同步指标 + 术语索引，返回各索引写入文档数。"""
        metric_count = await self.sync_metrics()
        term_count = await self.sync_terms()
        return {"metric_idx": metric_count, "term_idx": term_count}


def _join_synonyms(value: Any) -> str:
    """同义词（JSON 数组或字符串）归一为空格分隔文本，供 ES text 字段分词匹配。"""
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value)
