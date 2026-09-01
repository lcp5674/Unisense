"""ES 索引管理与数据同步单测（TD §1.3 ES 检索加速层）。

ES 以 MagicMock 隔离（不连真实 ES）；聚焦：
- ensure_indexes 幂等创建 / analyzer 版本检测自动重建 / 强制重建；
- mapping 含中英同义词 analyzer（search_analyzer + cn_en_synonym filter）；
- sync_metrics / sync_terms 从 MySQL 灌入，按业务编码 upsert；
- _join_synonyms 归一（JSON 数组/字符串 → 空格分隔）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.models.metric import Metric
from app.models.term import Term
from app.services.search.es_indexer import (
    _METRIC_INDEX,
    _SYSTEM_ANALYZER,
    _TERM_INDEX,
    EsIndexer,
    _join_synonyms,
    _mapping_settings,
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
    m.owner_id = 100
    m.backup_owner_id = 200
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


def _current_mapping(index: str) -> dict:
    """含当前 search_analyzer 版本的 mapping（供幂等检测）。

    metric_idx 需含 D-1 可见性过滤新增的 ``owner_id`` 字段（缺失即旧版需重建）。
    """
    props: dict = {"name": {"type": "text", "analyzer": _SYSTEM_ANALYZER}}
    if index == _METRIC_INDEX:
        props["owner_id"] = {"type": "long"}
    return {index: {"mappings": {"properties": props}}}


def _es_client() -> MagicMock:
    es = MagicMock()
    es.enabled = True
    es.create_index = AsyncMock(return_value=True)
    es.delete_index = AsyncMock(return_value=True)
    es.get_mapping = AsyncMock(return_value=None)  # 默认索引不存在 → 需创建
    es.index = AsyncMock()
    es.bulk = AsyncMock(return_value=1)  # P1：批量写入返回成功数
    return es


async def test_ensure_indexes_creates_both() -> None:
    """ensure_indexes 幂等创建 metric_idx/term_idx 映射。"""
    es = _es_client()
    indexer = EsIndexer(MagicMock(), es_client=es)
    result = await indexer.ensure_indexes()
    assert result == {_METRIC_INDEX: True, _TERM_INDEX: True}
    assert es.create_index.await_count == 2


async def test_ensure_indexes_existing_is_idempotent() -> None:
    """索引已含当前 analyzer → 幂等返回 False，不重建。"""
    es = _es_client()
    es.get_mapping = AsyncMock(
        side_effect=lambda idx: _current_mapping(idx)
    )
    indexer = EsIndexer(MagicMock(), es_client=es)
    result = await indexer.ensure_indexes()
    assert result == {_METRIC_INDEX: False, _TERM_INDEX: False}
    es.delete_index.assert_not_awaited()
    es.create_index.assert_not_awaited()


async def test_ensure_indexes_recreates_when_analyzer_missing() -> None:
    """索引存在但 analyzer 缺失（旧版 mapping）→ 删除重建返回 True。"""
    es = _es_client()
    # 旧版 mapping：name 为 text 但无 analyzer
    es.get_mapping = AsyncMock(
        return_value={
            _METRIC_INDEX: {"mappings": {"properties": {"name": {"type": "text"}}}}
        }
    )
    indexer = EsIndexer(MagicMock(), es_client=es)
    result = await indexer.ensure_indexes()
    assert result[_METRIC_INDEX] is True
    es.delete_index.assert_any_await(_METRIC_INDEX)


async def test_ensure_indexes_force_recreate() -> None:
    """force_recreate=True 强制删除重建（同义词词表变更后）。"""
    es = _es_client()
    es.get_mapping = AsyncMock(side_effect=lambda idx: _current_mapping(idx))
    indexer = EsIndexer(MagicMock(), es_client=es)
    result = await indexer.ensure_indexes(force_recreate=True)
    assert result == {_METRIC_INDEX: True, _TERM_INDEX: True}
    assert es.delete_index.await_count == 2
    assert es.create_index.await_count == 2


def test_mapping_settings_contain_synonym_analyzer() -> None:
    """settings 含中英同义词 analyzer：search_analyzer + cn_en_synonym filter。"""
    settings = _mapping_settings()
    analysis = settings["analysis"]
    assert analysis["filter"]["cn_en_synonym"]["type"] == "synonym"
    assert analysis["analyzer"][_SYSTEM_ANALYZER]["filter"] == ["lowercase", "cn_en_synonym"]
    # 同义词等价组含业务词对（如订单 → order/sales_order）
    synonyms = analysis["filter"]["cn_en_synonym"]["synonyms"]
    assert any(line.startswith("订单,") for line in synonyms)


async def test_sync_metrics_indexes_with_synonyms() -> None:
    """指标灌入：包含关联逻辑度量同义词，按 id 作为 doc_id upsert。"""
    session = MagicMock()
    session.execute = AsyncMock(return_value=_rows_result([(_metric(), "支付金额 pay")]))
    es = _es_client()
    indexer = EsIndexer(session, es_client=es)
    count = await indexer.sync_metrics()
    assert count == 1
    es.bulk.assert_awaited_once()
    index, docs = es.bulk.call_args.args
    assert index == _METRIC_INDEX
    assert docs[0]["metric_code"] == "sales_gmv_day"
    assert docs[0]["synonyms"] == "支付金额 pay"
    assert es.bulk.call_args.kwargs["doc_id"] == "id"


async def test_sync_terms_indexes() -> None:
    """术语灌入：同义词数组归一为空格分隔文本。"""
    session = MagicMock()
    session.execute = AsyncMock(return_value=_rows_result([_term()]))
    es = _es_client()
    indexer = EsIndexer(session, es_client=es)
    count = await indexer.sync_terms()
    assert count == 1
    es.bulk.assert_awaited_once()
    index, docs = es.bulk.call_args.args
    assert index == _TERM_INDEX
    assert docs[0]["term_code"] == "gmv"
    assert docs[0]["synonyms"] == "成交额 GMV"


async def test_sync_all_reports_counts() -> None:
    """sync_all 返回各索引写入数。"""
    session = MagicMock()
    # sync_metrics 分批：第一轮 1 行 → 第二轮空 break；sync_terms 首轮即空
    session.execute = AsyncMock(
        side_effect=[_rows_result([(_metric(), None)]), _rows_result([]), _rows_result([])]
    )
    es = _es_client()
    indexer = EsIndexer(session, es_client=es)
    counts = await indexer.sync_all()
    assert counts == {_METRIC_INDEX: 1, _TERM_INDEX: 0}


def test_join_synonyms_normalizes() -> None:
    """同义词归一：None→空串、数组→空格分隔、字符串→原样。"""
    assert _join_synonyms(None) == ""
    assert _join_synonyms(["a", "b"]) == "a b"
    assert _join_synonyms("成交额 GMV") == "成交额 GMV"
    assert _join_synonyms([1, "x"]) == "1 x"
