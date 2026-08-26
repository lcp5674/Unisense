"""ES 索引管理与数据同步（TD §1.3 ES 检索加速层落地）。

背景：指标/术语两类检索此前为 MySQL LIKE；ES 8.15 服务已部署但未接线
（仅就绪探针）。本服务负责：
- ``ensure_indexes``：创建 metric_idx / term_idx 索引映射（幂等；analyzer 版本
  变更自动检测并删除重建，同义词扩展实时生效）；
- ``sync_metrics`` / ``sync_terms`` / ``sync_all``：从 MySQL 全量灌入 ES
  （按业务编码 upsert，支持重复执行）；
- ES 未配置/不可用时静默跳过（检索路径由 ``global_search`` 自动降级 MySQL LIKE，
  对齐 TD §1.3 降级边界 "ES✗→退 MySQL"）。

中英同义词过滤器（``search/synonyms.py`` 数据源）：
- mapping 配置 ``search_analyzer``（standard 分词 + lowercase + ``cn_en_synonym``
  token filter），对 name/description/definition/synonyms 文本字段生效；
- 查询"订单"在分词层等价为 "order/sales_order"，命中英文名称/描述中的 token
  （与 MySQL LIKE 路径的中英扩展共用同一份词对，行为一致）。
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
from app.services.search.synonyms import es_synonym_lines

logger = get_logger(__name__)

_METRIC_INDEX = "metric_idx"
_TERM_INDEX = "term_idx"
#: 自定义 analyzer 名（检测 mapping 版本用：字段 analyzer 指向它即认为已含同义词扩展）
_SYSTEM_ANALYZER = "search_analyzer"


def _mapping_settings() -> dict[str, Any]:
    """索引 settings：基础分片 + 中英同义词 analyzer（与 MySQL LIKE 扩展共用词对）。"""
    return {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "analysis": {
            "filter": {
                # ES 8.15 内联 synonyms：逗号分隔等价组（首词为规范形式）
                "cn_en_synonym": {"type": "synonym", "synonyms": es_synonym_lines()},
            },
            "analyzer": {
                _SYSTEM_ANALYZER: {
                    "type": "custom",
                    "tokenizer": "standard",
                    "filter": ["lowercase", "cn_en_synonym"],
                },
            },
        },
    }


_METRIC_MAPPING: dict[str, Any] = {
    "settings": _mapping_settings(),
    "mappings": {
        "properties": {
            "id": {"type": "long"},
            "metric_code": {"type": "keyword"},
            "name": {"type": "text", "analyzer": _SYSTEM_ANALYZER},
            "description": {"type": "text", "analyzer": _SYSTEM_ANALYZER},
            "domain": {"type": "keyword"},
            "status": {"type": "keyword"},
            "pii_flag": {"type": "boolean"},
            # 可见性过滤（D-1）：非管理角色经 bool.filter 按 owner/backup_owner
            # 收敛"本人负责的未发布资产"，与 MySQL 路径同一语义
            "owner_id": {"type": "long"},
            "backup_owner_id": {"type": "long"},
            # 关联逻辑度量同义词（业务别名，如"支付金额"→"pay"），参与 multi_match
            "synonyms": {"type": "text", "analyzer": _SYSTEM_ANALYZER},
        }
    },
}

_TERM_MAPPING: dict[str, Any] = {
    "settings": _mapping_settings(),
    "mappings": {
        "properties": {
            "id": {"type": "long"},
            "term_code": {"type": "keyword"},
            "name": {"type": "text", "analyzer": _SYSTEM_ANALYZER},
            "definition": {"type": "text", "analyzer": _SYSTEM_ANALYZER},
            "domain": {"type": "keyword"},
            "status": {"type": "keyword"},
            "synonyms": {"type": "text", "analyzer": _SYSTEM_ANALYZER},
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

    async def ensure_indexes(self, *, force_recreate: bool = False) -> dict[str, bool]:
        """幂等创建索引映射；analyzer 版本变更自动删除重建。

        同义词过滤器定义在 index settings（analyzer），无法在已存在索引上原地更新。
        检测规则：mapping 的文本字段已指向 ``search_analyzer`` → 幂等（False）；
        索引不存在或 analyzer 缺失 → 删除（若存在）后重建（True），调用方随后需
        ``sync_all`` 全量重灌。``force_recreate=True`` 强制删除重建（同义词词表变更后）。

        Args:
            force_recreate: 是否强制重建（忽略版本检测）。

        Returns:
            ``{index: created_or_recreated}``；ES 不可用抛 SearchUnavailableError。
        """
        return {
            _METRIC_INDEX: await self._ensure_index(
                _METRIC_INDEX, _METRIC_MAPPING, force_recreate
            ),
            _TERM_INDEX: await self._ensure_index(_TERM_INDEX, _TERM_MAPPING, force_recreate),
        }

    async def _ensure_index(self, index: str, mapping: dict[str, Any], force: bool) -> bool:
        """单索引确保存在且 analyzer 为当前版本（返回 True=本次创建/重建）。"""
        if force or not await self._has_current_analyzer(index):
            await self._es.delete_index(index)  # 不存在时静默成功（返回 False）
            return await self._es.create_index(index, mapping)
        return False

    async def _has_current_analyzer(self, index: str) -> bool:
        """索引 mapping 是否为当前版本（false=不存在或旧版需重建）。

        版本检测 = analyzer 已指向 ``search_analyzer`` 且（metric_idx）含可见性
        过滤所需的 ``owner_id`` 字段（D-1 新增列；缺失即旧版，重建后 /sync 重灌）。
        """
        mapping = await self._es.get_mapping(index)
        if not mapping:
            return False
        props = ((mapping.get(index) or {}).get("mappings") or {}).get("properties") or {}
        has_analyzer = any(
            isinstance(field, dict) and field.get("analyzer") == _SYSTEM_ANALYZER
            for field in props.values()
        )
        if not has_analyzer:
            return False
        if index == _METRIC_INDEX and "owner_id" not in props:
            return False
        return True

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
                    "owner_id": metric.owner_id,
                    "backup_owner_id": metric.backup_owner_id,
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
