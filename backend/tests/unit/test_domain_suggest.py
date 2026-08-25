"""业务域建议（FR-010 域建议增强）单元测试。

覆盖：
- 采集目录唯一命中 → ``unique``（source=catalog，置信度 0.9，名称回填）
- 挂载实体命中 → source=mount（置信度 0.85）
- 多域候选 → ``multiple`` + candidates 按置信度降序
- 目录/挂载均未命中 → LLM 兜底（source=llm，置信度封顶 0.7）
- LLM 不可用 / 返回非法域编码 / 非法 JSON → ``none``（降级不炸）
- SQL 解析出多源表去重；源表与 SQL 并集
- ``parse_domain_infer_result``：合法 / 缺字段 / 置信度越界
- API：空 payload 422 / 非字符串 sql 422 / 合法请求 200 返回四态
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from app.services.llm.parse import parse_domain_infer_result
from app.services.semantic.domain_suggest import (
    DomainCandidate,
    _aggregate,
    _candidate_tables,
    suggest_domain,
)

# ---------------------------------------------------------------- service 层


@pytest.fixture
def domain_map() -> dict[str, str]:
    """固定域清单（code → name）。"""
    return {"sales": "销售", "sales_order": "订单", "finance": "财务", "medical_fee": "医疗收费"}


async def _run_suggest(
    domain_map: dict[str, str],
    *,
    matches: list[tuple[str, DomainCandidate]] | None = None,
    llm: DomainCandidate | None = None,
    sql: str | None = None,
    source_table: str | None = None,
) -> dict:
    """以固定 seams（_domain_map/_lookup_tables/_llm_suggest）跑 suggest_domain。"""
    db = MagicMock()
    with (
        patch(
            "app.services.semantic.domain_suggest._domain_map",
            new=AsyncMock(return_value=domain_map),
        ),
        patch(
            "app.services.semantic.domain_suggest._lookup_tables",
            new=AsyncMock(return_value=matches or []),
        ),
        patch(
            "app.services.semantic.domain_suggest._llm_suggest",
            new=AsyncMock(return_value=llm),
        ),
    ):
        return await suggest_domain(db, sql=sql, source_table=source_table)


async def test_suggest_unique_from_catalog(domain_map: dict[str, str]) -> None:
    """采集目录唯一命中 → unique，source=catalog，名称回填。"""
    result = await _run_suggest(
        domain_map,
        matches=[
            (
                "dwd.sales_detail",
                DomainCandidate(
                    code="sales", name="", confidence=0.9, source="catalog", reason="采集目录命中"
                ),
            )
        ],
        source_table="dwd.sales_detail",
    )
    assert result["status"] == "unique"
    assert result["domain"]["code"] == "sales"
    assert result["domain"]["name"] == "销售"
    assert result["domain"]["source"] == "catalog"
    assert result["domain"]["confidence"] == 0.9
    assert result["matched_tables"] == ["dwd.sales_detail"]


async def test_suggest_unique_from_mount(domain_map: dict[str, str]) -> None:
    """挂载实体命中 → source=mount。"""
    result = await _run_suggest(
        domain_map,
        matches=[
            (
                "dwd.sales_detail",
                DomainCandidate(
                    code="finance", name="", confidence=0.85, source="mount", reason="挂载实体命中"
                ),
            )
        ],
        source_table="dwd.sales_detail",
    )
    assert result["status"] == "unique"
    assert result["domain"]["source"] == "mount"
    assert result["domain"]["confidence"] == 0.85


async def test_suggest_multiple_candidates_sorted(domain_map: dict[str, str]) -> None:
    """多域候选 → multiple，candidates 按置信度降序（跨域共用 DWD 层表）。"""
    result = await _run_suggest(
        domain_map,
        matches=[
            (
                "dwd.sales_detail",
                DomainCandidate(
                    code="sales", name="", confidence=0.9, source="catalog", reason="a"
                ),
            ),
            (
                "dwd.sales_detail",
                DomainCandidate(
                    code="finance", name="", confidence=0.85, source="mount", reason="b"
                ),
            ),
        ],
        source_table="dwd.sales_detail",
    )
    assert result["status"] == "multiple"
    assert result["domain"] is None
    assert [c["code"] for c in result["candidates"]] == ["sales", "finance"]
    assert result["candidates"][0]["name"] == "销售"


async def test_suggest_aggregate_same_domain_takes_max_confidence(
    domain_map: dict[str, str],
) -> None:
    """同一域多表命中 → 取最高置信度（聚合去重）。"""
    matches = [
        (
            "t1",
            DomainCandidate(code="sales", name="", confidence=0.9, source="catalog", reason="a"),
        ),
        (
            "t2",
            DomainCandidate(code="sales", name="", confidence=0.85, source="mount", reason="b"),
        ),
    ]
    agg = _aggregate(matches, domain_map)
    assert len(agg) == 1
    assert agg[0]["code"] == "sales"
    assert agg[0]["confidence"] == 0.9
    assert agg[0]["name"] == "销售"


async def test_suggest_llm_fallback(domain_map: dict[str, str]) -> None:
    """目录/挂载均未命中（表未被采集）→ LLM 兜底，置信度封顶 0.7。"""
    result = await _run_suggest(
        domain_map,
        llm=DomainCandidate(
            code="medical_fee",
            name="医疗收费",
            confidence=0.7,
            source="llm",
            reason="AI 依据 SQL/表名推断的业务域",
        ),
        sql="SELECT SUM(amount) FROM dwd.fee_bill_di GROUP BY dt",
    )
    assert result["status"] == "llm"
    assert result["domain"]["code"] == "medical_fee"
    assert result["domain"]["source"] == "llm"
    assert result["domain"]["confidence"] == 0.7
    assert result["matched_tables"] == []


async def test_suggest_llm_unavailable_falls_back_none(domain_map: dict[str, str]) -> None:
    """LLM 不可用（返回 None）→ none，不抛异常。"""
    result = await _run_suggest(
        domain_map,
        llm=None,
        sql="SELECT SUM(amount) FROM some_uncollected_table GROUP BY dt",
    )
    assert result["status"] == "none"
    assert result["domain"] is None


async def test_suggest_no_input_returns_none() -> None:
    """无有效输入（sql/source_table 皆空）→ none。"""
    db = MagicMock()
    result = await suggest_domain(db, sql="   ", source_table=None)
    assert result["status"] == "none"


def test_candidate_tables_sql_and_source_dedup() -> None:
    """SQL 解析源表 + 显式源表并集去重。"""
    tables = _candidate_tables(
        source_table="dwd.sales_detail",
        sql="SELECT SUM(gmv) FROM dwd.sales_detail GROUP BY dt",
    )
    assert tables == ["dwd.sales_detail"]


def test_candidate_tables_sql_parse_failure_keeps_source() -> None:
    """SQL 解析失败（非法 SQL）不阻断，保留显式源表。"""
    tables = _candidate_tables(source_table="dwd.sales_detail", sql="NOT A VALID SQL")
    assert tables == ["dwd.sales_detail"]


# ---------------------------------------------------------------- _lookup_tables（真实查询路径）


async def test_lookup_tables_catalog_and_mount() -> None:
    """真实查询路径：DBCatalog→DataSource.domain + MetricMount.domain 反查。"""
    db = MagicMock()
    # DBCatalog join DataSource → (entity_name, domain)
    catalog_rows = [("dwd.sales_detail", "sales")]
    # MetricMount → (source_table, domain)
    mount_rows = [("dwd.sales_detail", "finance")]
    db.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(all=lambda: catalog_rows),
            SimpleNamespace(all=lambda: mount_rows),
        ]
    )
    with patch(
        "app.services.semantic.domain_suggest._domain_map",
        new=AsyncMock(return_value={"sales": "销售", "finance": "财务"}),
    ):
        matches = await suggest_domain(db, source_table="dwd.sales_detail")
    # 目录 0.9 + 挂载 0.85 → multiple（跨域共用表）
    assert matches["status"] == "multiple"
    assert {c["code"] for c in matches["candidates"]} == {"sales", "finance"}
    assert {c["source"] for c in matches["candidates"]} == {"catalog", "mount"}


# ---------------------------------------------------------------- parse_domain_infer_result


def test_parse_domain_infer_result_valid() -> None:
    parsed = parse_domain_infer_result(
        '{"domain_code": "medical_fee", "confidence": 0.82, "reason": "涉及门诊收费账单"}'
    )
    assert parsed == {
        "domain_code": "medical_fee",
        "confidence": 0.82,
        "reason": "涉及门诊收费账单",
    }


def test_parse_domain_infer_result_missing_confidence() -> None:
    assert parse_domain_infer_result('{"domain_code": "sales"}') is None


def test_parse_domain_infer_result_out_of_range() -> None:
    assert parse_domain_infer_result('{"domain_code": "sales", "confidence": 1.5}') is None


def test_parse_domain_infer_result_not_json() -> None:
    assert parse_domain_infer_result("销售域") is None


# ---------------------------------------------------------------- API 契约


@pytest.fixture
async def metrics_client() -> AsyncIterator[httpx.AsyncClient]:
    """覆盖 DB 会话与当前用户依赖（platform_admin，写端点放行）。"""

    async def fake_db() -> AsyncIterator[MagicMock]:
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock())
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        domain=None,
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_suggest_domain_empty_payload_rejected_422(
    metrics_client: httpx.AsyncClient,
) -> None:
    """sql 与 source_table 均缺省 → 422（至少提供一个）。"""
    resp = await metrics_client.post(
        "/api/v1/metric-definitions/suggest-domain",
        json={},
    )
    assert resp.status_code == 422


async def test_suggest_domain_non_string_sql_rejected_422(
    metrics_client: httpx.AsyncClient,
) -> None:
    """非字符串 sql（数字 payload）→ 422 而非 500。"""
    resp = await metrics_client.post(
        "/api/v1/metric-definitions/suggest-domain",
        json={"sql": 123},
    )
    assert resp.status_code == 422


async def test_suggest_domain_valid_request_returns_suggestion(
    metrics_client: httpx.AsyncClient,
) -> None:
    """合法请求（SQL）→ 200，返回 unique 建议。"""
    suggestion = {
        "status": "unique",
        "domain": {
            "code": "sales",
            "name": "销售",
            "confidence": 0.9,
            "source": "catalog",
            "reason": "采集目录中表 dwd.sales_detail 归属数据源绑定域（销售）",
        },
        "candidates": [],
        "matched_tables": ["dwd.sales_detail"],
    }
    with patch(
        "app.services.semantic.domain_suggest.suggest_domain",
        new=AsyncMock(return_value=suggestion),
    ):
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/suggest-domain",
            json={"sql": "SELECT SUM(gmv) FROM dwd.sales_detail GROUP BY dt"},
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "unique"
    assert data["domain"]["code"] == "sales"
    assert data["domain"]["source"] == "catalog"
