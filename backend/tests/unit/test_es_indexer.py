"""ES 索引管理与数据同步单测（TD §1.3 ES 检索加速层）。

ES 以 MagicMock 隔离（不连真实 ES）；聚焦：
- ensure_indexes 幂等创建（已存在不报错）；
- sync_metrics / sync_terms 从 MySQL 灌入，按业务编码 upsert；
- _join_synonyms 归一（JSON 数组/字符串 → 空格分隔）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.models.metric import Metric
from app.models.term import Term
from app.services.search.es_indexer import (
    _METRIC_INDEX,
    _TERM_INDEX,
    EsIndexer,
    _join_synonyms,
)


def _metric() -> Metric:
    m = Metric()
    m.id = 1
    m.metric_code = "sales_gmv_day"
    m.name = "销售GMV"
    m.description = "销售总额（日）"
    m.domain = "sales"
    m.status = "PUBLISHED"
    m.pii_flag = False
    m.measure_id = 10
    return m


def _term() -> Term:
    t = Term()
    t.id = 2
    t.term_code = "gmv"
    t.name = "成交总额"
    t.definition = "一段时间内成交订单金额合计"
    t.domain = "sales"
    t.status = "ACTIVE"
    t.synonyms = ["成交额", "GMV"]
    return t


def _rows_result(rows):
    res = MagicMock()
    res.all.return_value = rows
    # 兼容 .scalars().all()（sync_terms 走 scalars 路径）
    res.scalars.return_value = res
    return res


async def test_ensure_indexes_creates_both() -> None:
    """ensure_indexes 幂等创建 metric_idx/term_idx 映射。"""
    session = MagicMock()
    es = MagicMock()
    es.enabled = True
    es.create_index = AsyncMock(side_effect=lambda idx, body: True)
    indexer = EsIndexer(session, es_client=es)
    result = await indexer.ensure_indexes()
    assert result == {_METRIC_INDEX: True, _TERM_INDEX: True}
    assert es.create_index.await_count == 2


async def test_ensure_indexes_existing_is_idempotent() -> None:
    """已存在的索引返回 False（幂等不报错）。"""
    session = MagicMock()
    es = MagicMock()
    es.enabled = True
    es.create_index = AsyncMock(return_value=False)
    indexer = EsIndexer(session, es_client=es)
    result = await indexer.ensure_indexes()
    assert result == {_METRIC_INDEX: False, _TERM_INDEX: False}


async def test_sync_metrics_indexes_with_synonyms() -> None:
    """指标灌入：包含关联逻辑度量同义词，按 id 作为 doc_id upsert。"""
    session = MagicMock()
    session.execute = AsyncMock(return_value=_rows_result([(_metric(), "支付金额 pay")]))
    es = MagicMock()
    es.enabled = True
    es.index = AsyncMock()
    indexer = EsIndexer(session, es_client=es)
    count = await indexer.sync_metrics()
    assert count == 1
    index, doc = es.index.call_args.args
    assert index == _METRIC_INDEX
    assert doc["metric_code"] == "sales_gmv_day"
    assert doc["synonyms"] == "支付金额 pay"
    assert es.index.call_args.kwargs["doc_id"] == "1"


async def test_sync_terms_indexes() -> None:
    """术语灌入：同义词数组归一为空格分隔文本。"""
    session = MagicMock()
    session.execute = AsyncMock(return_value=_rows_result([_term()]))
    es = MagicMock()
    es.enabled = True
    es.index = AsyncMock()
    indexer = EsIndexer(session, es_client=es)
    count = await indexer.sync_terms()
    assert count == 1
    index, doc = es.index.call_args.args
    assert index == _TERM_INDEX
    assert doc["term_code"] == "gmv"
    assert doc["synonyms"] == "成交额 GMV"


async def test_sync_all_reports_counts() -> None:
    """sync_all 返回各索引写入数。"""
    session = MagicMock()
    # 第一次调用 = sync_metrics（1 行），第二次 = sync_terms（空）
    session.execute = AsyncMock(
        side_effect=[_rows_result([(_metric(), None)]), _rows_result([])]
    )
    es = MagicMock()
    es.enabled = True
    es.index = AsyncMock()
    indexer = EsIndexer(session, es_client=es)
    counts = await indexer.sync_all()
    assert counts == {_METRIC_INDEX: 1, _TERM_INDEX: 0}


def test_join_synonyms_normalizes() -> None:
    """同义词归一：None→空串、数组→空格分隔、字符串→原样。"""
    assert _join_synonyms(None) == ""
    assert _join_synonyms(["a", "b"]) == "a b"
    assert _join_synonyms("成交额 GMV") == "成交额 GMV"
    assert _join_synonyms([1, "x"]) == "1 x"
