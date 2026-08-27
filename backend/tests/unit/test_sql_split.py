"""SQL 批量切分与候选推断单元测试（FR-010 批量注册增强，场景A/B）。

覆盖：
- 场景A 三模式切分：semicolon（引号/注释内分号不误切）、statement（CTE/INSERT 单条）、
  custom（delimiters/start_markers 正则 + LLM 语义分段兜底 + 不可用降级单段）
- 场景B 单语句多度量拆分（split_select_measures：共享源表/维度/周期）
- parse_sql_split_result（别名/去重/空段过滤/整体失败 None）
- infer_sql_batch 集成：多语句候选生成、多度量拆分 + 复合合成、skipped、域建议回填
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.llm.parse import parse_period_infer_result, parse_sql_split_result
from app.services.semantic.sql_infer import parse_sql_profile
from app.services.semantic.sql_split import (
    _period_from_profile,
    _period_uncertain,
    _split_semicolon,
    _split_statement,
    infer_sql_batch,
    split_select_measures,
    split_sql_statements,
)


@pytest.fixture(autouse=True)
def _mock_llm_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """单元测试不触发真实 LLM 校验层（方案 A 默认全量校验会经 LlmConfigService
    构建真实 client——LLM 实例 429/超时/重试后返回任意内容会把候选任意改写，
    致依赖候选精确结构的测试 flaky）。

    校验层本身由 ``test_sql_validation.py`` 单独覆盖；本文件只测 infer_sql_batch
    的切分/候选/并发逻辑，统一 mock 掉校验层返回 ``None``（LLM 不可用时上层
    保持规则结果不动的真实语义），测试确定性且不依赖 LLM 实例状态。
    """

    async def _no_validation(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "app.services.semantic.sql_validation.llm_validate_measures", _no_validation
    )

# 多语句 SQL（场景A）：注释 + 两条 SELECT
_MULTI_SQL = """
-- 指标1：订单金额
SELECT dt, SUM(amount) AS gmv FROM dwd_order_di WHERE dt >= '2026-01-01' GROUP BY dt;

-- 指标2：去重用户数
WITH base AS (SELECT user_id, dt FROM dwd_user_di WHERE dt >= '2026-01-01')
SELECT dt, COUNT(DISTINCT user_id) AS uv FROM base GROUP BY dt
"""
# 单语句多度量（场景B）
_MULTI_MEASURE_SQL = (
    "SELECT dt, SUM(amount) AS gmv, COUNT(DISTINCT user_id) AS uv "
    "FROM dwd_order_di GROUP BY dt"
)


# ---------------------------------------------------------------- 场景A：切分


def test_split_semicolon_basic_and_quote_safe() -> None:
    """分号切分：普通多语句 + 引号内分号不切断。"""
    segments = _split_semicolon("SELECT 'a;b' AS x FROM t; SELECT 2")
    assert len(segments) == 2
    assert "'a;b'" in segments[0]
    # 反引号/双引号内分号同样不切断
    assert len(_split_semicolon('SELECT `a;b` FROM t; SELECT `c`')) == 2
    assert len(_split_semicolon('SELECT "a;b" FROM t; SELECT "c"')) == 2


def test_split_semicolon_comment_semicolon() -> None:
    """行注释内分号不切断（引号扫描对 -- 注释天然忽略）。"""
    segments = _split_semicolon("-- a;b\nSELECT 1; SELECT 2")
    assert len(segments) == 2
    assert "-- a;b" in segments[0]


def test_split_semicolon_no_trailing_semicolon() -> None:
    """末段无分号也应返回（尾段）。"""
    segments = _split_semicolon("SELECT 1; SELECT 2")
    assert segments == ["SELECT 1", "SELECT 2"]


def test_split_statement_cte_and_insert() -> None:
    """statement 模式：CTE 单条、INSERT INTO...SELECT 单条、普通多 SELECT。"""
    sql = "WITH a AS (SELECT 1) SELECT * FROM a; INSERT INTO tgt SELECT * FROM src"
    segments = _split_statement(sql)
    assert len(segments) == 2
    assert "WITH a AS" in segments[0]
    assert "INSERT INTO" in segments[1]


def test_split_statement_invalid_sql_returns_empty() -> None:
    """statement 模式非法 SQL → 空列表（上层回退分号切分）。"""
    assert _split_statement("NOT A VALID SQL") == []


async def test_split_statements_statement_mode_fallback() -> None:
    """statement 模式：语义切分失败时回退分号切分（非法 SQL 至少 1 段）。"""
    segments = await split_sql_statements("SELECT 1; SELECT 2", mode="statement")
    assert len(segments) == 2


async def test_split_statements_semicolon_mode() -> None:
    segments = await split_sql_statements("SELECT 1; SELECT 2", mode="semicolon")
    assert len(segments) == 2


async def test_split_statements_custom_delimiters() -> None:
    """custom 模式：delimiters 正则切分。"""
    sql = "SELECT 1 /*---*/ SELECT 2 /*---*/ SELECT 3"
    segments = await split_sql_statements(
        sql, mode="custom", custom_rules={"delimiters": [r"/\*---\*/"]}
    )
    assert len(segments) == 3


async def test_split_statements_custom_start_markers() -> None:
    """custom 模式：start_markers 正则切分（标记命中位置为段起点）。"""
    sql = "-- 指标A\nSELECT 1\n-- 指标B\nSELECT 2"
    segments = await split_sql_statements(
        sql, mode="custom", custom_rules={"start_markers": [r"-- 指标"]}
    )
    assert len(segments) == 2
    assert "指标A" in segments[0]
    assert "指标B" in segments[1]


async def test_split_statements_custom_llm_fallback() -> None:
    """custom 模式规则未生效（≤1 段）→ LLM 语义分段兜底。"""
    llm_segments = ["SELECT SUM(a) FROM t1", "SELECT COUNT(b) FROM t2"]
    with patch(
        "app.services.semantic.sql_split._llm_split",
        new=AsyncMock(return_value=llm_segments),
    ):
        segments = await split_sql_statements(
            "SELECT SUM(a) FROM t1; SELECT COUNT(b) FROM t2",
            mode="custom",
            custom_rules={"delimiters": ["---"]},  # 未命中 → LLM
            db=MagicMock(),
        )
    assert segments == llm_segments


async def test_split_statements_custom_llm_unavailable_degrade() -> None:
    """LLM 兜底不可用（返回 None）→ 降级整段单候选。"""
    with patch(
        "app.services.semantic.sql_split._llm_split",
        new=AsyncMock(return_value=None),
    ):
        segments = await split_sql_statements(
            "SELECT SUM(a) FROM t1; SELECT COUNT(b) FROM t2",
            mode="custom",
            custom_rules={"delimiters": ["---"]},
            db=MagicMock(),
        )
    assert segments == ["SELECT SUM(a) FROM t1; SELECT COUNT(b) FROM t2"]


async def test_split_statements_custom_no_db_skips_llm() -> None:
    """db 缺省（None）时 custom 模式不触发 LLM（纯规则兜底）。"""
    with patch(
        "app.services.semantic.sql_split._llm_split",
        new=AsyncMock(return_value=["x", "y"]),
    ) as m:
        segments = await split_sql_statements(
            "SELECT 1; SELECT 2",
            mode="custom",
            custom_rules={"delimiters": ["---"]},
        )
    m.assert_not_awaited()
    assert segments == ["SELECT 1; SELECT 2"]


def test_split_statement_multi_sql_preserves_statements() -> None:
    """真实多指标脚本：statement 模式切出 2 条独立语句（CTE 单条正确）。"""
    segments = _split_statement(_MULTI_SQL)
    assert len(segments) == 2
    assert "SUM(amount)" in segments[0]
    assert "COUNT(DISTINCT user_id)" in segments[1]


# ---------------------------------------------------------------- 场景B：多度量拆分


def test_split_select_measures_multi() -> None:
    """单语句多度量 → N 个候选（共享源表/维度/周期，group_key 一致）。"""
    profile = parse_sql_profile(_MULTI_MEASURE_SQL)
    measures = split_select_measures(_MULTI_MEASURE_SQL, profile)
    assert len(measures) == 2
    assert {m["measure_column"] for m in measures} == {"amount", "user_id"}
    assert all(m["source_table"] == "dwd_order_di" for m in measures)
    assert all(m["period"] == "day" for m in measures)
    assert measures[0]["group_key"] == measures[1]["group_key"] == "amountuserid"
    assert measures[0]["aggregation"] == "SUM"
    assert measures[1]["aggregation"] == "COUNT_DISTINCT"


def test_split_select_measures_single() -> None:
    """单度量语句 → 1 个候选。"""
    measures = split_select_measures(
        "SELECT dt, SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt"
    )
    assert len(measures) == 1
    assert measures[0]["aggregation"] == "SUM"


# ---------------------------------------------------------------- parse_sql_split_result


def test_parse_sql_split_result_valid() -> None:
    parsed = parse_sql_split_result(
        '{"statements": [{"sql": "SELECT 1", "name": "a"}, {"sql": "SELECT 2", "name": "b"}]}'
    )
    assert parsed == [
        {"sql": "SELECT 1", "name": "a", "reason": None},
        {"sql": "SELECT 2", "name": "b", "reason": None},
    ]


def test_parse_sql_split_result_aliases_and_dedup() -> None:
    """sql 别名（text/segment）+ 重复/空段过滤。"""
    parsed = parse_sql_split_result(
        '{"statements": [{"text": "SELECT 1"}, {"segment": "SELECT 1"}, {"sql": "  "}]}'
    )
    assert parsed == [{"sql": "SELECT 1", "name": None, "reason": None}]


def test_parse_sql_split_result_none_on_invalid() -> None:
    """整体无有效片段 / 非 JSON / 缺 statements → None。"""
    assert parse_sql_split_result("不是 JSON") is None
    assert parse_sql_split_result('{"statements": []}') is None
    assert parse_sql_split_result('{"foo": 1}') is None


# ---------------------------------------------------------------- infer_sql_batch


def _fake_db() -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock())
    return db


async def test_infer_sql_batch_multi_statement_candidates() -> None:
    """场景A 多语句：每条语句各自产出原子候选（CTE 别名不误当源表）。"""
    result = await infer_sql_batch(
        _fake_db(), sql=_MULTI_SQL, split_mode="statement", domain_code="sales"
    )
    assert len(result["statements"]) == 2
    assert len(result["candidates"]) == 2
    codes = {c["metric_code"] for c in result["candidates"]}
    assert "sales_order_amount_day" in codes
    # CTE 语句取物理表 dwd_user_di（非 CTE 别名 base）
    assert "sales_user_userid_day" in codes
    # 原子候选完整字段
    first = result["candidates"][0]
    assert first["type"] == "atomic"
    assert first["aggregation"] in ("SUM", "COUNT_DISTINCT")
    assert first["definition_json"]["expression"]
    assert first["source_table"] == "dwd_order_di"
    assert first["period"] == "day"


async def test_infer_sql_batch_no_arith_no_composite() -> None:
    """B4：无四则运算的多度量并列（SELECT SUM(a), SUM(b)）不合成复合。"""
    result = await infer_sql_batch(
        _fake_db(),
        sql=_MULTI_MEASURE_SQL,
        split_mode="statement",
        domain_code="sales",
        synthesize_composite=True,
    )
    composites = [c for c in result["candidates"] if c["type"] == "composite"]
    atoms = [c for c in result["candidates"] if c["type"] == "atomic"]
    assert len(atoms) == 2
    assert len(composites) == 0


async def test_infer_sql_batch_synthesize_composite_with_arith() -> None:
    """R1/B4：比率列（SUM/SUM 相除）被自动识别为复合候选，不再「派生列+手工合成」。"""
    arith_sql = (
        "SELECT dt, SUM(amount) AS gmv, "
        "SUM(amount)/COUNT(DISTINCT user_id) AS arpu "
        "FROM dwd_order_di GROUP BY dt"
    )
    result = await infer_sql_batch(
        _fake_db(),
        sql=arith_sql,
        split_mode="statement",
        domain_code="sales",
        synthesize_composite=True,
    )
    composites = [c for c in result["candidates"] if c["type"] == "composite"]
    atoms = [c for c in result["candidates"] if c["type"] == "atomic"]
    # R1：arpu（含除法）直接判 composite，原子仅剩 gmv；不再合成 "0:composite"
    # （合成复合只基于纯原子，原子<2 时不合成，避免复合依赖复合）
    assert len(atoms) == 1
    assert len(composites) == 1
    comp = composites[0]
    assert comp["key"] == "0:arpu"
    assert comp["aggregation"] is None  # 派生比率列：聚合占位 None
    assert comp["definition_json"]["expression"]  # 口径由表达式承载


async def test_infer_sql_batch_single_measure_no_composite() -> None:
    """单度量语句即使 synthesize_composite=True 也不合成复合（需 ≥2 原子）。"""
    result = await infer_sql_batch(
        _fake_db(),
        sql="SELECT dt, SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt",
        split_mode="statement",
        domain_code="sales",
        synthesize_composite=True,
    )
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["type"] == "atomic"


async def test_infer_sql_batch_skipped_statement() -> None:
    """无聚合度量列的语句进 skipped（候选不产出），原因分类 no_aggregate。"""
    sql = "SELECT 1; SELECT dt, SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt"
    result = await infer_sql_batch(_fake_db(), sql=sql, split_mode="semicolon", domain_code="sales")
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["reason"] == "no_aggregate"
    assert len(result["candidates"]) == 1


async def test_infer_sql_batch_domain_suggestion_applied() -> None:
    """域缺省时整段建议：unique 命中自动回填候选域。"""
    suggestion = {
        "status": "unique",
        "domain": {"code": "sales", "name": "销售", "confidence": 0.9, "source": "catalog"},
        "candidates": [],
        "matched_tables": ["dwd_order_di"],
    }
    with patch(
        "app.services.semantic.domain_suggest.suggest_domain",
        new=AsyncMock(return_value=suggestion),
    ):
        result = await infer_sql_batch(
            _fake_db(), sql=_MULTI_SQL, split_mode="statement", synthesize_composite=True
        )
    assert result["domain"]["code"] == "sales"
    assert result["domain"]["status"] == "unique"
    # 候选 metric_code 使用建议域
    assert any(c["metric_code"].startswith("sales_") for c in result["candidates"])


async def test_infer_sql_batch_explicit_domain_no_suggest() -> None:
    """显式指定域 → 不触发建议（domain.status=user）。"""
    with patch(
        "app.services.semantic.domain_suggest.suggest_domain",
        new=AsyncMock(side_effect=AssertionError("should not be called")),
    ):
        result = await infer_sql_batch(
            _fake_db(), sql=_MULTI_SQL, split_mode="statement", domain_code="sales"
        )
    assert result["domain"]["status"] == "user"
    assert result["domain"]["code"] == "sales"


async def test_infer_sql_batch_empty_sql() -> None:
    """空 SQL → 空结果不抛异常。"""
    result = await infer_sql_batch(_fake_db(), sql="   ")
    assert result["candidates"] == []
    assert result["statements"] == []


# ---------------------------------------------------------------- ETL 透传 INSERT 下沉


# Hive ETL 脚本：set 参数 + 中文 comment 的建表 DDL + 透传 INSERT（默认方言对 DDL 解析失败）
_HIVE_ETL_SQL = """
set hive.vectorized.execution.enabled=false;
create table if not exists wedw_dws.doctor_active_month_di(
 month_id string comment "统计月,时间格式yyyy-MM",
 hosp_name string comment "医院名称"
)
stored as orc;
insert overwrite table wedw_dws.doctor_active_month_di
select
    a.month_id,
    coalesce(b.org_name, '-99') as hosp_name,
    a.current_month_active_doctor_cnt
from
(
    select
        substr(create_date,1,7) as month_id,
        count(distinct doctor_code) as current_month_active_doctor_cnt
    from wedw_dw.doctor_visit_agent_info_da
    group by substr(create_date,1,7)
)a
left join
(
    select distinct rel_code, org_name
    from wedw_dw.disease_care_sys_org_staff_relation_df
)b
on a.hosp_code = b.rel_code
"""


def test_split_statement_hive_ddl_uses_hive_dialect() -> None:
    """Hive 特有 DDL（stored as orc + 中文 comment）默认方言解析失败 → 回退 hive 方言正常切分。"""
    segments = _split_statement(_HIVE_ETL_SQL)
    assert len(segments) == 3
    assert "SET " in segments[0].upper()
    assert "CREATE TABLE" in segments[1].upper()
    assert "INSERT OVERWRITE" in segments[2].upper()


async def test_infer_sql_batch_etl_insert_passthrough_candidates() -> None:
    """ETL 透传 INSERT：最外层无聚合 → 下沉提取内层聚合候选。

    候选 key 用投影别名（区分同列不同语义），源表取聚合所在子查询表（非字典表）。
    """
    result = await infer_sql_batch(
        _fake_db(), sql=_HIVE_ETL_SQL, split_mode="statement", domain_code="sales"
    )
    # set + create 无聚合进 skipped，insert 下沉出 1 个候选
    assert len(result["skipped"]) == 2
    # OneData 语义：month 周期 = 原子（活跃医生数）+ 时间周期 → 派生指标
    derived = [c for c in result["candidates"] if c["type"] == "derived"]
    assert len(derived) == 1
    cand = derived[0]
    assert cand["key"] == "2:current_month_active_doctor_cnt"
    assert cand["source_table"] == "wedw_dw.doctor_visit_agent_info_da"
    assert cand["measure_column"] == "doctor_code"
    assert cand["aggregation"] == "COUNT_DISTINCT"
    assert "COUNT(DISTINCT" in cand["definition_json"]["expression"].upper()
    # substr(create_date,1,7) 截月 → 周期自动识别为月（不再回落 day）
    assert cand["period"] == "month"


async def test_infer_sql_batch_etl_insert_no_arith_no_composite() -> None:
    """B4：透传 INSERT 多度量（COUNT DISTINCT / 条件 COUNT）无四则运算 → 不合成复合。"""
    sql = """
    insert overwrite table wedw_dws.t
    select a.month_id, a.current_month_active_doctor_cnt, a.last_month_active_doctor_cnt
    from (
        select substr(create_date,1,7) as month_id,
               count(distinct doctor_code) as current_month_active_doctor_cnt,
               count(distinct case when last_visit_date is not null then doctor_code end)
                   as last_month_active_doctor_cnt
        from wedw_dw.doctor_visit_agent_info_da
        group by substr(create_date,1,7)
    ) a
    """
    result = await infer_sql_batch(
        _fake_db(), sql=sql, split_mode="statement", domain_code="sales", synthesize_composite=True
    )
    # OneData 语义：month 周期 → 派生候选（原子 + 时间周期）
    derived = [c for c in result["candidates"] if c["type"] == "derived"]
    composites = [c for c in result["candidates"] if c["type"] == "composite"]
    assert len(derived) == 2
    # B4：两个独立聚合列无四则运算，不合成复合
    assert len(composites) == 0
    # key 用别名区分同列（doctor_code）不同语义的度量
    keys = {a["key"] for a in derived}
    assert keys == {
        "0:current_month_active_doctor_cnt",
        "0:last_month_active_doctor_cnt",
    }


# ---------------------------------------------------------------- Doris CTAS（场景A 扩展）


# Doris 落宽表脚本：DROP + CREATE TABLE ... DISTRIBUTED BY ... PROPERTIES ... AS SELECT
# （默认方言对 DISTRIBUTED BY/PROPERTIES 降级 Command，须走 starrocks 方言/剥离兜底）
_DORIS_CTAS_SQL = """
DROP TABLE IF EXISTS wedw_dws.doctor_func_index_df;
CREATE TABLE IF NOT EXISTS wedw_dws.doctor_func_index_df
DUPLICATE KEY(create_date, doctor_code, hosp_code)
COMMENT '家医智能体-功能使用分析'
DISTRIBUTED BY HASH(create_date, doctor_code, hosp_code) BUCKETS 5
PROPERTIES ("replication_allocation" = "tag.location.default: 1")
AS
SELECT
    a.create_date,
    a.quality_control_qc_report_cnt,
    a.remote_clinic_cnt
FROM (
    SELECT
        to_date(t1.event_time) AS create_date,
        SUM(CASE WHEN get_json_string(t1.biz_data,'$.skillId')='quality-control-qc-report'
            THEN 1 ELSE 0 END) AS quality_control_qc_report_cnt,
        SUM(CASE WHEN get_json_string(t1.biz_data,'$.skillId')='remote-clinic'
            THEN 1 ELSE 0 END) AS remote_clinic_cnt
    FROM footprint_service_ctl.footprint_service.ods_track_event t1
    WHERE t1.click_event='skill-call'
    GROUP BY to_date(t1.event_time)
) a
"""


def test_split_statement_doris_ctas_uses_starrocks_dialect() -> None:
    """Doris CTAS（DISTRIBUTED BY/PROPERTIES）默认方言降级 Command → starrocks 方言语义切分。"""
    segments = _split_statement(_DORIS_CTAS_SQL)
    assert len(segments) == 2
    assert "DROP TABLE" in segments[0].upper()
    assert "CREATE TABLE" in segments[1].upper()
    # create 段保留 SELECT 口径（未丢失）
    assert "quality_control_qc_report_cnt" in segments[1]


async def test_infer_sql_batch_doris_ctas_candidates_with_alias_anchor() -> None:
    """Doris CTAS 批量解析：产出原子候选；同列多语义（SUM(CASE) 分支）用 alias 锚点区分编码。

    回归：默认方言不支持 DISTRIBUTED BY/PROPERTIES 曾致整句解析失败 → 0 候选
    （前端提示「未解析到可注册的指标候选」）；修复后须产出候选且编码可区分。
    """
    result = await infer_sql_batch(
        _fake_db(), sql=_DORIS_CTAS_SQL, split_mode="statement", domain_code="wedw"
    )
    # DROP 无聚合进 skipped（ddl_only 分类），CTAS 下沉出 2 个原子候选
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["reason"] == "ddl_only"
    atoms = [c for c in result["candidates"] if c["type"] == "atomic"]
    assert len(atoms) == 2
    # alias 锚点：同落 biz_data 列的 2 个 case 聚合 → 编码/名称可区分
    codes = {c["metric_code"] for c in atoms}
    assert len(codes) == 2
    assert any("qualitycontrol" in c for c in codes)
    assert any("remoteclinic" in c for c in codes)
    # 口径表达式保留完整 CASE（非裸 SUM(biz_data)）
    exprs = {c["definition_json"]["expression"] for c in atoms}
    assert all("CASE WHEN" in e.upper() for e in exprs)
    # 源表取聚合所在子查询的物理表
    src = "footprint_service_ctl.footprint_service.ods_track_event"
    assert all(c["source_table"] == src for c in atoms)


# ---------------------------------------------------------------- 周期推断 + LLM 兜底


def test_period_from_profile_substr_month() -> None:
    """substr(create_date,1,7) 截月 → 周期 month（粒度信号优先于列名 token）。"""
    profile = parse_sql_profile(
        "SELECT substr(create_date,1,7) AS month_id, SUM(amt) AS amt "
        "FROM t GROUP BY substr(create_date,1,7)"
    )
    assert _period_from_profile(profile) == "month"


def test_period_from_profile_plain_dt() -> None:
    """dt 日分区 → 周期 day。"""
    profile = parse_sql_profile(
        "SELECT dt, SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt"
    )
    assert _period_from_profile(profile) == "day"


def test_period_from_profile_month_column() -> None:
    """group by month_id 裸列 → 周期 month。"""
    profile = parse_sql_profile(
        "SELECT month_id, SUM(amt) AS amt FROM t GROUP BY month_id"
    )
    assert _period_from_profile(profile) == "month"


def test_period_from_profile_no_time_defaults_day() -> None:
    """无时间信号 → 回落 day。"""
    profile = parse_sql_profile("SELECT SUM(amount) AS gmv FROM dwd_order_di")
    assert _period_from_profile(profile) == "day"


def test_period_uncertain_signal() -> None:
    """有明确粒度信号或时间列 → 不触发 LLM（确定）。"""
    month = parse_sql_profile(
        "SELECT substr(create_date,1,7) AS month_id, SUM(amt) AS amt "
        "FROM t GROUP BY substr(create_date,1,7)"
    )
    assert not _period_uncertain(month)
    dt = parse_sql_profile("SELECT dt, SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt")
    assert not _period_uncertain(dt)


def test_period_uncertain_no_time_signal() -> None:
    """无时间列且无粒度信号 → 不确定（触发 LLM 兜底）。"""
    profile = parse_sql_profile("SELECT SUM(amount) AS gmv FROM dwd_order_di")
    assert _period_uncertain(profile)


async def test_infer_sql_batch_llm_period_fallback() -> None:
    """规则层无时间信号 → LLM 兜底推断周期；LLM 不可用/失败降级 day。"""
    sql = "SELECT SUM(amount) AS gmv FROM dwd_order_di"
    db = _fake_db()

    # LLM 可用且返回 month → 候选周期 month
    async def _fake_chat(**kwargs):
        content = '{"period": "month", "confidence": 0.9, "reason": "月度汇总"}'
        return {"content": content, "role": "assistant"}

    fake_client = MagicMock()
    fake_client.enabled = True
    fake_client.chat = AsyncMock(side_effect=_fake_chat)
    with patch(
        "app.services.llm.config_service.LlmConfigService"
    ) as mock_svc:
        mock_svc.return_value.build_client = AsyncMock(return_value=fake_client)
        result = await infer_sql_batch(db, sql=sql, split_mode="statement", domain_code="sales")
    # OneData 语义：LLM 推断 month 周期 → 派生候选（原子 + 时间周期）
    cands = [c for c in result["candidates"] if c["type"] == "derived"]
    assert cands and all(c["period"] == "month" for c in cands)


async def test_infer_sql_batch_llm_period_unavailable_degrades_day() -> None:
    """LLM 不可用（enabled=False）→ 降级规则层默认 day，不阻断候选。"""
    db = _fake_db()
    fake_client = MagicMock()
    fake_client.enabled = False
    with patch(
        "app.services.llm.config_service.LlmConfigService"
    ) as mock_svc:
        mock_svc.return_value.build_client = AsyncMock(return_value=fake_client)
        result = await infer_sql_batch(
            db, sql="SELECT SUM(amount) AS gmv FROM dwd_order_di",
            split_mode="statement", domain_code="sales",
        )
    cands = [c for c in result["candidates"] if c["type"] == "atomic"]
    assert cands and all(c["period"] == "day" for c in cands)


# ---------------------------------------------------------------- parse_period_infer_result


def test_parse_period_infer_result_valid() -> None:
    parsed = parse_period_infer_result(
        '{"period": "month", "confidence": 0.85, "reason": "substr截月"}'
    )
    assert parsed == {"period": "month", "confidence": 0.85, "reason": "substr截月"}


def test_parse_period_infer_result_alias_and_chinese() -> None:
    """granularity 别名 + 中文「月」→ 归一化 month。"""
    parsed = parse_period_infer_result('{"granularity": "月", "score": 0.9}')
    assert parsed is not None
    assert parsed["period"] == "month"
    assert parsed["confidence"] == 0.9


def test_parse_period_infer_result_invalid() -> None:
    """period 越界 / 缺 confidence / 非 JSON → None（上层降级）。"""
    assert parse_period_infer_result('{"period": "decade", "confidence": 0.8}') is None
    assert parse_period_infer_result('{"period": "month"}') is None
    assert parse_period_infer_result('{"period": "month", "confidence": 1.5}') is None
    assert parse_period_infer_result("不是 JSON") is None


# ---------------------------------------------------------------- 无度量分类 + LLM 兜底


def test_classify_no_measure_categories() -> None:
    """跳过原因四分类：纯 DDL / 含聚合但解析失败 / 确实无聚合 / LLM 已尝试。"""
    from app.services.semantic.sql_infer import SqlProfile
    from app.services.semantic.sql_split import _classify_no_measure

    empty = SqlProfile(sql="x")
    # 无 SELECT 的纯 DDL → ddl_only
    assert (
        _classify_no_measure("DROP TABLE IF EXISTS dwd_tmp", empty, llm_tried=False)
        == "ddl_only"
    )
    assert (
        _classify_no_measure("CREATE TABLE t (a int)", empty, llm_tried=False)
        == "ddl_only"
    )
    # 含 SUM 但规则层解析失败 → parse_failed（值得 LLM 兜底）
    assert (
        _classify_no_measure(
            "SELECT dt, SUM(x) AS v FROM t GROUP BY dt", empty, llm_tried=False
        )
        == "parse_failed"
    )
    # 方言聚合变体（ClickHouse sumMerge/sumIf）未解析出度量 → 也走 parse_failed（LLM 兜底）
    assert (
        _classify_no_measure(
            "SELECT dt, sumMerge(amount_state) AS amount FROM t GROUP BY dt",
            empty,
            llm_tried=False,
        )
        == "parse_failed"
    )
    assert (
        _classify_no_measure("SELECT countIf(x > 0) AS c FROM t", empty, llm_tried=False)
        == "parse_failed"
    )
    # X-6/X-7/X-11：被规则层诚实跳过的聚合（min_by/any_value/mode）也触发 LLM 兜底，
    # 避免静默 no_aggregate
    assert (
        _classify_no_measure(
            "SELECT d, any_value(name) AS n FROM t GROUP BY d", empty, llm_tried=False
        )
        == "parse_failed"
    )
    assert (
        _classify_no_measure(
            "SELECT d, mode() WITHIN GROUP (ORDER BY amt) AS m FROM t GROUP BY d",
            empty,
            llm_tried=False,
        )
        == "parse_failed"
    )
    # 含 SELECT 但确实无聚合 → no_aggregate
    assert (
        _classify_no_measure("SELECT 1", empty, llm_tried=False) == "no_aggregate"
    )
    assert (
        _classify_no_measure("SELECT * FROM dwd_order_di", empty, llm_tried=False)
        == "no_aggregate"
    )
    # LLM 已尝试仍失败 → llm_infer_failed（最高优先级）
    assert (
        _classify_no_measure("SELECT 1", empty, llm_tried=True) == "llm_infer_failed"
    )


async def test_infer_sql_batch_llm_measure_fallback() -> None:
    """规则层解析不出度量的语句 → LLM 兜底提取度量 → 产出候选（不进 skipped）。

    回归：方言/结构异常致 parse_sql_profile 空画像曾直接 skipped → 0 候选
    （前端提示「未解析到候选」）；LLM 兜底后即使规则层失败也能产出候选。
    """
    from app.services.semantic.sql_infer import SqlProfile

    real_parse = parse_sql_profile

    def _fake_parse(sql: str) -> SqlProfile:
        if "SUM(unparsable_col)" in sql:
            return SqlProfile(sql=sql)  # 规则层解析失败（空画像）
        return real_parse(sql)

    llm_measures = [
        {
            "column": "unparsable_col",
            "agg": "SUM",
            "alias": "gmv2",
            "table": "dwd_order_di",
            "period": "day",
            "name": "日成交额",
        }
    ]
    with (
        patch(
            "app.services.semantic.sql_split.parse_sql_profile",
            side_effect=_fake_parse,
        ),
        patch(
            "app.services.semantic.sql_split._llm_infer_measures",
            new=AsyncMock(return_value=llm_measures),
        ),
    ):
        result = await infer_sql_batch(
            _fake_db(),
            sql="SELECT dt, SUM(unparsable_col) AS gmv2 FROM dwd_order_di GROUP BY dt",
            split_mode="statement",
            domain_code="sales",
        )
    assert len(result["skipped"]) == 0
    assert len(result["candidates"]) == 1
    cand = result["candidates"][0]
    assert cand["measure_column"] == "unparsable_col"
    assert cand["aggregation"] == "SUM"
    assert cand["name"] == "日成交额"
    assert cand["source_table"] == "dwd_order_di"
    # P2-2：LLM 兜底提取的候选带 source=llm（前端「AI 推断」复核标识）
    assert cand["source"] == "llm"


async def test_infer_sql_batch_llm_fallback_failure_skips() -> None:
    """LLM 兜底不可用/失败 → 语句进 skipped（reason=llm_infer_failed），不阻断整批。"""
    from app.services.semantic.sql_infer import SqlProfile

    real_parse = parse_sql_profile

    def _fake_parse(sql: str) -> SqlProfile:
        if "SUM(unparsable_col)" in sql:
            return SqlProfile(sql=sql)
        return real_parse(sql)

    with (
        patch(
            "app.services.semantic.sql_split.parse_sql_profile",
            side_effect=_fake_parse,
        ),
        patch(
            "app.services.semantic.sql_split._llm_infer_measures",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await infer_sql_batch(
            _fake_db(),
            sql="SELECT dt, SUM(unparsable_col) AS gmv2 FROM dwd_order_di GROUP BY dt",
            split_mode="statement",
            domain_code="sales",
        )
    assert len(result["candidates"]) == 0
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["reason"] == "llm_infer_failed"


# ----------------------------------------------------------------
# 工业方言：原文保留 + LLM 完整 SQL 传参
# ----------------------------------------------------------------


def test_split_statement_preserves_original_dialect_syntax() -> None:
    """ClickHouse 多语句切分保留原文（sumMerge/countIf 小写不被序列化改写）。

    sqlglot 方言 ``ast.sql()`` 序列化会把 ``sumMerge`` → ``SUMMERGE``、
    ``countIf`` → ``COUNT_IF``（大写），后续方言识别失效丢失度量。切分应返回
    原文切片，完整保留方言写法。
    """
    sql = (
        "CREATE TABLE IF NOT EXISTS dwd.agg_doctor_daily "
        "ENGINE = MergeTree() ORDER BY (stat_date) "
        "AS SELECT toDate(ts) AS d, sumIf(amount, is_valid=1) AS v "
        "FROM ods.e GROUP BY toDate(ts);\n"
        "SELECT toDate(ts) AS d, sumMerge(amount_state) AS a "
        "FROM dwd.agg_doctor_daily GROUP BY toDate(ts);"
    )
    segs = _split_statement(sql)
    assert len(segs) == 2
    # 原文保留：方言函数名保持原样（未被序列化改写为 SUMMERGE/COUNT_IF）
    assert "sumMerge" in segs[1]
    assert "sumIf" in segs[0]
    assert "COUNT_IF" not in "".join(segs).upper().replace("COUNT_IF(", "")


def test_split_statement_cte_merged_semantics() -> None:
    """合法 CTE 多语句按语义切分（CTE 整体一段，不被分号误切）。"""
    sql = (
        "WITH base AS (SELECT user_id, dt FROM dwd_user_di WHERE dt >= '2026-01-01') "
        "SELECT dt, COUNT(DISTINCT user_id) AS uv FROM base GROUP BY dt;\n"
        "SELECT dt, SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt;"
    )
    segs = _split_statement(sql)
    assert len(segs) == 2
    assert "WITH base AS" in segs[0]
    assert "SELECT dt, SUM(amount)" in segs[1]


async def test_infer_sql_batch_llm_measure_passes_full_sql() -> None:
    """LLM 度量兜底传完整脚本 + 焦点语句（上下文不因切分丢失）。"""
    from app.services.semantic.sql_infer import SqlProfile

    real_parse = parse_sql_profile

    def _fake_parse(sql: str) -> SqlProfile:
        if "SUM(unparsable_col)" in sql:
            return SqlProfile(sql=sql)
        return real_parse(sql)

    llm_measures = [{"column": "unparsable_col", "agg": "SUM"}]
    full = (
        "-- 前置说明\n"
        "SELECT dt, SUM(unparsable_col) AS gmv2 FROM dwd_order_di GROUP BY dt;\n"
        "SELECT dt, COUNT(DISTINCT user_id) AS uv FROM dwd_user_di GROUP BY dt;"
    )
    with (
        patch(
            "app.services.semantic.sql_split.parse_sql_profile",
            side_effect=_fake_parse,
        ),
        patch(
            "app.services.semantic.sql_split._llm_infer_measures",
            new=AsyncMock(return_value=llm_measures),
        ) as mock_llm,
    ):
        await infer_sql_batch(
            _fake_db(),
            sql=full,
            split_mode="statement",
            domain_code="sales",
        )
    assert mock_llm.await_count == 1
    kwargs = mock_llm.await_args.kwargs
    # 完整脚本作为上下文传入（含前置注释），焦点语句精确到含聚合的段
    assert kwargs["full_sql"] == full
    assert "SUM(unparsable_col)" in kwargs["focus_sql"]
    assert "COUNT(DISTINCT user_id)" not in kwargs["focus_sql"]


async def test_infer_sql_batch_llm_period_passes_full_sql() -> None:
    """LLM 周期兜底传完整脚本 + 焦点语句。"""
    from app.services.semantic.sql_infer import SqlProfile

    real_parse = parse_sql_profile

    def _fake_parse(sql: str) -> SqlProfile:
        if "dwd_order_di" in sql and "SUM(amount)" in sql:
            return SqlProfile(measures=[{"column": "amount", "agg": "SUM"}])
        return real_parse(sql)

    full = (
        "WITH meta AS (SELECT 1 AS x) SELECT * FROM meta;\n"
        "SELECT SUM(amount) AS gmv FROM dwd_order_di;"
    )
    with (
        patch(
            "app.services.semantic.sql_split.parse_sql_profile",
            side_effect=_fake_parse,
        ),
        patch(
            "app.services.semantic.sql_split._llm_infer_period",
            new=AsyncMock(return_value="month"),
        ) as mock_llm,
    ):
        await infer_sql_batch(
            _fake_db(),
            sql=full,
            split_mode="statement",
            domain_code="sales",
        )
    assert mock_llm.await_count == 1
    kwargs = mock_llm.await_args.kwargs
    assert kwargs["full_sql"] == full
    assert "SUM(amount)" in kwargs["focus_sql"]


# ---------------------------------------------------------------- P0-1/P1-3/P2-10 修复回归


async def test_infer_sql_batch_multiple_domain_no_illegal_code() -> None:
    """P0-1+P2-10：整段域建议为多域时，候选不 bake-in 首段为空的非法编码，
    且携带逐语句建议域（suggested_domain_code）。"""
    multiple = {
        "status": "multiple",
        "domain": None,
        "candidates": [
            {
                "code": "sales",
                "name": "销售",
                "confidence": 0.9,
                "source": "catalog",
                "reason": "表A",
            },
            {
                "code": "health",
                "name": "医疗",
                "confidence": 0.8,
                "source": "catalog",
                "reason": "表B",
            },
        ],
        "matched_tables": ["dwd_order_di", "dwd_patient_di"],
    }

    async def _per_stmt(db, **kwargs):
        # 整段（多语句含分号）→ multiple；单段 → 按内容返回 unique（跨域）
        sql = kwargs.get("sql", "")
        if ";" in sql:
            return multiple
        if "user_id" in sql:
            return {
                "status": "unique",
                "domain": {"code": "health"},
                "candidates": [],
                "matched_tables": [],
            }
        return {
            "status": "unique",
            "domain": {"code": "sales"},
            "candidates": [],
            "matched_tables": [],
        }

    with patch(
        "app.services.semantic.domain_suggest.suggest_domain",
        new=AsyncMock(side_effect=_per_stmt),
    ):
        result = await infer_sql_batch(
            _fake_db(), sql=_MULTI_SQL, split_mode="statement"
        )
    # 整段 multiple → 域未生效 → 原子候选 metric_code 为 None（无 _xxx_day 非法编码）
    assert result["domain"]["status"] == "multiple"
    atoms = [c for c in result["candidates"] if c["type"] == "atomic"]
    assert atoms, "应有原子候选"
    assert all(c["metric_code"] is None for c in atoms), "多域态不得 bake-in 非法编码"
    # P2-10：候选携带逐语句建议域（第二句 health、第一句 sales）
    codes = {c["suggested_domain_code"] for c in atoms}
    assert "sales" in codes and "health" in codes


async def test_infer_sql_batch_domain_suggest_runs_concurrently() -> None:
    """两阶段并发：整段域建议为多域时，逐语句域建议并入阶段 2 gather 并发执行。

    修复前逐语句 ``await suggest_domain`` 串行（N 条语句 = N×单次 DB 反查/LLM 兜底
    墙钟）；修复后阶段 2 并入 gather + 信号量并发。用 active/max_active 追踪验证
    同时活跃数 ≥2（并发而非串行），且建议域按 idx 回填到候选（suggested_domain_code）
    与语句摘要（suggested_domain）。LLM 校验层（方案 A）mock 掉避免真实 LLM 依赖。
    """
    multiple = {
        "status": "multiple",
        "domain": None,
        "candidates": [],
        "matched_tables": [],
    }
    active = 0
    max_active = 0

    async def fake_suggest(db_arg, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        sql = kwargs.get("sql", "")
        if ";" in sql:
            return multiple
        if "user_id" in sql:
            return {
                "status": "unique",
                "domain": {"code": "health"},
                "candidates": [],
                "matched_tables": [],
            }
        return {
            "status": "unique",
            "domain": {"code": "sales"},
            "candidates": [],
            "matched_tables": [],
        }

    with patch(
        "app.services.semantic.domain_suggest.suggest_domain",
        new=AsyncMock(side_effect=fake_suggest),
    ):
        result = await infer_sql_batch(_fake_db(), sql=_MULTI_SQL, split_mode="statement")
    assert max_active >= 2  # 逐语句域建议并发而非串行
    atoms = [c for c in result["candidates"] if c["type"] == "atomic"]
    assert atoms, "应有原子候选"
    codes = {c["suggested_domain_code"] for c in atoms}
    assert "sales" in codes and "health" in codes
    # 语句摘要回填逐语句建议域
    stmt_domains = [s["suggested_domain"] for s in result["statements"]]
    assert "sales" in stmt_domains and "health" in stmt_domains


async def test_infer_sql_batch_composite_uses_real_period() -> None:
    """P1-3：月粒度语句的复合候选编码/粒度用实际周期（不再硬编码 _day/day）。

    B4：需含四则运算才合成复合——此处加入 SUM/COUNT 比率列满足条件。
    """
    month_sql = (
        "SELECT substr(create_date,1,7) AS month_id, "
        "SUM(amount) AS gmv, COUNT(DISTINCT user_id) AS uv, "
        "SUM(amount)/COUNT(DISTINCT user_id) AS arpu "
        "FROM dwd_order_di GROUP BY substr(create_date,1,7)"
    )
    result = await infer_sql_batch(
        _fake_db(), sql=month_sql, split_mode="statement", domain_code="sales",
        synthesize_composite=True,
    )
    comp = next(c for c in result["candidates"] if c["type"] == "composite")
    assert comp["metric_code"].endswith("_month"), comp["metric_code"]
    assert comp["granularity"] == "month"
    assert "_day" not in comp["metric_code"]
    # 原子候选（month 周期 → 派生标签）周期也是 month
    derived = [c for c in result["candidates"] if c["type"] == "derived"]
    assert len(derived) >= 2
    assert all(c["period"] == "month" for c in derived)


def test_build_atomic_candidate_empty_domain_code_none() -> None:
    """P0-1 单测：域为空时原子候选编码为 None（不生成 _xxx_day 非法编码）。"""
    from app.services.semantic.sql_split import _build_atomic_candidate

    cand = _build_atomic_candidate(
        idx=0,
        measure={"column": "amount", "agg": "SUM"},
        table="dwd_order_di",
        period="day",
        domain_code=None,
        domain_defaults={},
        time_column="dt",
    )
    assert cand["metric_code"] is None
    # 有域时正常生成
    cand2 = _build_atomic_candidate(
        idx=0,
        measure={"column": "amount", "agg": "SUM"},
        table="dwd_order_di",
        period="day",
        domain_code="sales",
        domain_defaults={},
        time_column="dt",
    )
    assert cand2["metric_code"] == "sales_order_amount_day"


def test_apply_candidate_period_recomputes_type() -> None:
    """B2：LLM 覆盖周期后类型与周期同步（非日→派生、日→原子；复合保持复合）。"""
    from app.services.semantic.sql_split import _apply_candidate_period

    # day → month：类型 atomic → derived
    cand = {
        "type": "atomic",
        "period": "day",
        "granularity": "day",
        "metric_code": "sales_order_amount_day",
    }
    _apply_candidate_period(cand, "month")
    assert cand["period"] == "month"
    assert cand["type"] == "derived"
    assert cand["metric_code"] == "sales_order_amount_month"

    # month → day：类型 derived → atomic
    cand2 = {
        "type": "derived",
        "period": "month",
        "granularity": "month",
        "metric_code": "sales_order_amount_month",
    }
    _apply_candidate_period(cand2, "day")
    assert cand2["period"] == "day"
    assert cand2["type"] == "atomic"
    assert cand2["metric_code"] == "sales_order_amount_day"

    # 复合保持复合（周期是其属性，不因覆盖降级）
    cand3 = {
        "type": "composite",
        "period": "day",
        "granularity": "day",
        "metric_code": "sales_order_amount_day",
    }
    _apply_candidate_period(cand3, "month")
    assert cand3["type"] == "composite"
    assert cand3["period"] == "month"


async def test_infer_sql_batch_llm_batch_limit() -> None:
    """P1-2：批级 LLM 兜底限额——超过 _LLM_BATCH_LIMIT 的语句降级 skipped(llm_limit)。

    修复前多语句脚本逐条失败语句都调 LLM（20 条失败语句 = 20 次调用），可能打满
    LLM 配额/拖慢解析。修复后 _llm_infer_measures 调用数封顶 _LLM_BATCH_LIMIT，
    超限语句不调 LLM 直接 skipped 并标注 llm_limit（前端提示「已达 AI 兜底上限」）。
    """
    from app.services.semantic.sql_infer import SqlProfile
    from app.services.semantic.sql_split import _LLM_BATCH_LIMIT

    real_parse = parse_sql_profile

    def _fake_parse(sql: str) -> SqlProfile:
        if "unparsable_col" in sql:
            return SqlProfile(sql=sql)  # 规则层解析失败（空画像）
        return real_parse(sql)

    # 构造 _LLM_BATCH_LIMIT + 1 条失败语句（每条独立 SUM）
    parts = [
        f"SELECT dt, SUM(unparsable_col{i}) AS m{i} FROM dwd_order_di GROUP BY dt"
        for i in range(_LLM_BATCH_LIMIT + 1)
    ]
    llm_mock = AsyncMock(
        side_effect=lambda db, full_sql, focus_sql: [
            {
                "column": "unparsable_col0",
                "agg": "SUM",
                "alias": "gmv",
                "table": "dwd_order_di",
                "period": "day",
                "name": "日成交额",
            }
        ]
    )
    with (
        patch(
            "app.services.semantic.sql_split.parse_sql_profile",
            side_effect=_fake_parse,
        ),
        patch(
            "app.services.semantic.sql_split._llm_infer_measures",
            new=llm_mock,
        ),
    ):
        result = await infer_sql_batch(
            _fake_db(), sql=";".join(parts), split_mode="semicolon", domain_code="sales"
        )
    # 前 _LLM_BATCH_LIMIT 条走 LLM 产出候选；超限第 N+1 条降级 skipped(llm_limit)
    assert llm_mock.call_count == _LLM_BATCH_LIMIT
    assert len(result["candidates"]) == _LLM_BATCH_LIMIT
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["reason"] == "llm_limit"


async def test_infer_sql_batch_period_fallback_runs_concurrently() -> None:
    """两阶段并发：多语句同时触发周期兜底 → asyncio.gather 并发执行。

    修复前逐语句 ``await _llm_infer_period`` 串行（N 条语句 = N×单次调用墙钟）；
    修复后阶段 2 用 gather + 信号量并发执行。用 active/max_active 追踪验证
    同时活跃数 ≥2（并发而非串行），且各语句候选周期正确回填。
    """
    parts = [
        f"SELECT SUM(amount{i}) AS m{i} FROM dwd_order_di"
        for i in range(3)
    ]
    db = _fake_db()
    active = 0
    max_active = 0

    async def fake_infer_period(db_arg, full_sql, focus_sql):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return "month"

    with patch(
        "app.services.semantic.sql_split._llm_infer_period",
        new=AsyncMock(side_effect=fake_infer_period),
    ):
        result = await infer_sql_batch(
            db, sql=";".join(parts), split_mode="semicolon", domain_code="sales"
        )
    # 并发执行：同时活跃 ≥2（串行则恒为 1）
    assert max_active >= 2
    # OneData 语义：LLM 推断出 month 周期 → 派生候选（原子 + 时间周期）
    cands = [c for c in result["candidates"] if c["type"] == "derived"]
    assert len(cands) == 3
    assert all(c["period"] == "month" for c in cands)
    # 候选按语句 index 顺序回填（与串行一致）
    assert [int(str(c["key"]).split(":")[0]) for c in cands] == [0, 1, 2]


async def test_infer_sql_batch_measures_fallback_runs_concurrently() -> None:
    """两阶段并发：多条 parse_failed 语句同时触发度量兜底 → gather 并发执行。

    修复前逐语句 ``await _llm_infer_measures`` 串行；修复后阶段 2 并发。用
    active/max_active 追踪验证同时活跃数 ≥2，且各语句候选按 idx 顺序回填。
    """
    from app.services.semantic.sql_infer import SqlProfile

    parts = [
        f"SELECT dt, SUM(unparsable_col{i}) AS m{i} FROM dwd_order_di GROUP BY dt"
        for i in range(3)
    ]
    real_parse = parse_sql_profile

    def _fake_parse(statement: str) -> SqlProfile:
        if "unparsable_col" in statement:
            return SqlProfile(sql=statement)  # 规则层解析失败（空画像）
        return real_parse(statement)

    db = _fake_db()
    active = 0
    max_active = 0

    async def fake_infer_measures(db_arg, full_sql, focus_sql):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        col = focus_sql.split("SUM(")[1].split(")")[0].strip()
        return [
            {
                "column": col,
                "agg": "SUM",
                "alias": f"m_{col}",
                "table": "dwd_order_di",
                "period": "day",
                "name": f"{col} 合计",
            }
        ]

    with (
        patch(
            "app.services.semantic.sql_split.parse_sql_profile",
            side_effect=_fake_parse,
        ),
        patch(
            "app.services.semantic.sql_split._llm_infer_measures",
            new=AsyncMock(side_effect=fake_infer_measures),
        ),
    ):
        result = await infer_sql_batch(
            db, sql=";".join(parts), split_mode="semicolon", domain_code="sales"
        )
    assert max_active >= 2  # 并发而非串行
    cands = [c for c in result["candidates"] if c["type"] == "atomic"]
    assert len(cands) == 3
    # 候选按语句 index 顺序回填（与串行一致）
    assert [int(str(c["key"]).split(":")[0]) for c in cands] == [0, 1, 2]


async def test_infer_sql_batch_mixed_fallback_keeps_statement_order() -> None:
    """两阶段并发：混合场景（有度量 + LLM 兜底 + 有度量）候选顺序与串行一致。

    按语句 index 合并回填——语句 0 的 gmv、语句 1 的 LLM 兜底、语句 2 的 uv
    顺序保持 [0,1,2]，而非「先有度量后 LLM」错位。
    """
    from app.services.semantic.sql_infer import SqlProfile

    sql = ";".join(
        [
            "SELECT dt, SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt",
            "SELECT dt, SUM(unparsable_col) AS bad FROM dwd_order_di GROUP BY dt",
            "SELECT dt, COUNT(DISTINCT user_id) AS uv FROM dwd_user_di GROUP BY dt",
        ]
    )
    real_parse = parse_sql_profile

    def _fake_parse(statement: str) -> SqlProfile:
        if "unparsable_col" in statement:
            return SqlProfile(sql=statement)  # 该语句规则解析失败
        return real_parse(statement)

    db = _fake_db()

    async def fake_infer_measures(db_arg, full_sql, focus_sql):
        return [
            {
                "column": "unparsable_col",
                "agg": "SUM",
                "alias": "bad",
                "table": "dwd_order_di",
                "period": "day",
                "name": "坏列合计",
            }
        ]

    with (
        patch(
            "app.services.semantic.sql_split.parse_sql_profile",
            side_effect=_fake_parse,
        ),
        patch(
            "app.services.semantic.sql_split._llm_infer_measures",
            new=AsyncMock(side_effect=fake_infer_measures),
        ),
    ):
        result = await infer_sql_batch(
            db, sql=sql, split_mode="semicolon", domain_code="sales"
        )
    cands = [c for c in result["candidates"] if c["type"] == "atomic"]
    assert [int(str(c["key"]).split(":")[0]) for c in cands] == [0, 1, 2]
    assert [c["measure_column"] for c in cands] == ["amount", "unparsable_col", "user_id"]
    assert len(result["skipped"]) == 0


async def test_infer_sql_batch_single_statement_profile_error_degrades() -> None:
    """P0-A 兜底：单语句画像解析异常绝不炸整批——降级 skipped(parse_failed) 继续后续。
    方言聚合/极端嵌套等使 ``parse_sql_profile`` 抛异常时，``infer_sql_batch`` 应跳过
    该语句而非让整个批量解析 500。
    """
    parts = ["SELECT 1", "SELECT SUM(amount) AS gmv FROM t"]
    with patch(
        "app.services.semantic.sql_split.parse_sql_profile",
        side_effect=RuntimeError("boom"),
    ):
        result = await infer_sql_batch(
            _fake_db(), sql=";".join(parts), split_mode="semicolon", domain_code="sales"
        )
    # 异常语句 → skipped(parse_failed)；两条语句均被跳过（第一条 DDL、第二条画像异常）
    assert len(result["candidates"]) == 0
    assert all(s["reason"] == "parse_failed" for s in result["skipped"])
    assert len(result["skipped"]) == 2


async def test_infer_sql_batch_statement_limit_exceeded() -> None:
    """P1-2：语句数超上限（生产护栏）→ 抛 SQL_BATCH_TOO_MANY_STATEMENTS。

    超大脚本（数百条语句）会触发逐语句 LLM 兜底/域建议拖慢解析，超限直接拒绝，
    提示用户分批解析，而非让用户等待超时。
    """
    from app.core.exceptions import BusinessError

    # 构造 101 条简单语句（每条约 20 字符，总量远小于 64KB sql 上限）
    parts = [f"SELECT {i} AS x" for i in range(101)]
    with pytest.raises(BusinessError) as exc:
        await infer_sql_batch(
            _fake_db(), sql=";".join(parts), split_mode="semicolon", domain_code="sales"
        )
    assert exc.value.error_code == "SQL_BATCH_TOO_MANY_STATEMENTS"
    assert exc.value.ctx["statement_count"] == 101


def test_custom_regex_redos_safe() -> None:
    """P2-3：custom 切分正则安全护栏——危险/非法/超长正则被跳过。

    灾难性回溯（如 (a+)+、(a|a)+、(.*.*)+）与非法正则直接经 _safe_custom_regex
    拦截，不进入 re.split/re.finditer，避免 ReDoS 拖垮 worker。
    """
    from app.services.semantic.sql_split import _safe_custom_regex

    # 危险嵌套量词（ReDoS）→ None
    assert _safe_custom_regex(r"(a+)+") is None
    assert _safe_custom_regex(r"(a|a)+") is None
    assert _safe_custom_regex(r"(.*.*)+") is None
    assert _safe_custom_regex(r"(a{1,3}){2,}") is None
    # 非法正则 → None
    assert _safe_custom_regex("([unclosed") is None
    # 非字符串 / 空 / 超长 → None
    assert _safe_custom_regex(123) is None
    assert _safe_custom_regex("") is None
    assert _safe_custom_regex("a" * 201) is None
    # 合法正则 → 编译成功
    assert _safe_custom_regex(r"^CREATE\s+TABLE") is not None


def test_split_custom_skips_redos_delimiters() -> None:
    """P2-3：_split_custom 对危险 delimiters 跳过（不因单个 ReDoS 规则失败）。"""
    from app.services.semantic.sql_split import _split_custom

    # 危险分隔符被跳过，安全分隔符仍生效
    segments = _split_custom(
        "A;--\nB",
        {"delimiters": [r"(a+)+", ";"]},  # 前者 ReDoS 风险被跳过，后者生效
    )
    assert len(segments) == 2
    assert segments[0] == "A"
    assert segments[1] == "--\nB"


async def test_infer_sql_batch_candidates_carry_raw_sql() -> None:
    """P1-1/P2-5：infer_sql_batch 产出的候选直接携带所属语句原始 SQL（raw_sql）——
    API 消费者/集成链路提交时无需再从语句 meta 反查（口径溯源闭合）。"""
    sql = "SELECT SUM(amount) AS gmv FROM ods.orders GROUP BY dt"
    result = await infer_sql_batch(_fake_db(), sql=sql, split_mode="statement", domain_code="sales")
    assert result["candidates"], "应产出候选"
    for c in result["candidates"]:
        assert c.get("raw_sql") == sql, "候选应携带完整语句原文"
        # Q2：数仓详细口径（dw_definition）= 所属语句完整 SQL——创建后 MetricDetail/
        # 目录展开「数仓详细口径」区块直接可见，无需用户再手填（此前推断链路未写）
        assert c["definition_json"].get("dw_definition") == sql, (
            "候选 definition_json 应含数仓详细口径（dw_definition）"
        )


async def test_infer_sql_batch_derived_ratio_candidate() -> None:
    """P0-3d：派生比率列（ROUND(SUM/NULLIF(COUNT))）经 infer_sql_batch 产出
    derived 候选——aggregation=None（前端展示派生而非伪聚合）+ needs_review +
    完整 expression 口径。"""
    sql = (
        "SELECT SUM(amount) AS gmv, COUNT(DISTINCT user_id) AS uv, "
        "ROUND(SUM(amount)/NULLIF(COUNT(DISTINCT user_id),0),2) AS avg_price "
        "FROM ods.orders GROUP BY dt"
    )
    result = await infer_sql_batch(
        _fake_db(), sql=sql, split_mode="statement", domain_code="sales"
    )
    derived = [c for c in result["candidates"] if c.get("derived")]
    assert len(derived) == 1, "应产出 1 个派生比率候选"
    d = derived[0]
    assert d["aggregation"] is None, "派生候选聚合应为 None（占位由 Phase1 处理）"
    assert d["needs_review"] is True, "派生候选应标记口径需核对"
    assert "SUM" in (d["definition_json"].get("expression") or "").upper()
    assert d["raw_sql"] == sql, "派生候选也应携带原始 SQL"


# ---------------------------------------------------------------- use_llm 批量补全


async def test_infer_sql_batch_use_llm_annotates_candidates() -> None:
    """use_llm 显式模式：对规则候选做一次 LLM 批量补全——名称润色 + 周期校正，
    4 段式编码末段同步为周期、粒度一致，候选标记 source=llm + 置信度。"""
    sql = "SELECT dt, SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt"
    ann_mock = AsyncMock(
        return_value=[
            {
                "key": "0:gmv",
                "is_measure": True,
                "name": "日订单成交额",
                "period": "month",
                "confidence": 0.9,
                "reason": "GROUP BY 月粒度",
            }
        ]
    )
    with patch(
        "app.services.semantic.sql_split._llm_annotate_candidates",
        new=ann_mock,
    ):
        result = await infer_sql_batch(
            _fake_db(), sql=sql, split_mode="statement", domain_code="sales", use_llm=True
        )
    ann_mock.assert_awaited_once()
    cand = result["candidates"][0]
    assert cand["source"] == "llm"
    assert cand["name"] == "日订单成交额"
    assert cand["period"] == "month"
    assert cand["granularity"] == "month"
    # 4 段式编码末段同步为周期（业务段不受 LLM 影响）
    assert cand["metric_code"] == "sales_order_amount_month"
    assert cand["llm_confidence"] == 0.9


async def test_infer_sql_batch_use_llm_filters_not_measure() -> None:
    """use_llm 规范收敛：LLM 高置信度判非度量 → 候选移入 skipped(llm_not_measure)；
    低置信度保守保留（规则说有就保留，source=llm）。"""
    sql = "SELECT dt, SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt"
    # 高置信度（≥0.7）→ 过滤
    with patch(
        "app.services.semantic.sql_split._llm_annotate_candidates",
        new=AsyncMock(
            return_value=[
                {
                    "key": "0:gmv",
                    "is_measure": False,
                    "confidence": 0.9,
                    "reason": "这是金额投影不是独立度量",
                }
            ]
        ),
    ):
        result = await infer_sql_batch(
            _fake_db(), sql=sql, split_mode="statement", domain_code="sales", use_llm=True
        )
    assert len(result["candidates"]) == 0
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["reason"] == "llm_not_measure"
    # 低置信度（<0.7）→ 保守保留
    with patch(
        "app.services.semantic.sql_split._llm_annotate_candidates",
        new=AsyncMock(
            return_value=[
                {
                    "key": "0:gmv",
                    "is_measure": False,
                    "confidence": 0.5,
                    "reason": "不确定",
                }
            ]
        ),
    ):
        result = await infer_sql_batch(
            _fake_db(), sql=sql, split_mode="statement", domain_code="sales", use_llm=True
        )
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["source"] == "llm"


async def test_infer_sql_batch_use_llm_keeps_composite_candidate() -> None:
    """S1（三轮审查）：use_llm 模式复合候选豁免 is_measure 判定——LLM 高置信度把
    自动复合候选（arpu，measure_column/aggregation 为占位）判为「非度量」时不得剔除
    （与默认路径 B4.1 豁免对齐，防复合功能在 LLM 批量模式下端到端不可见）。"""
    arith_sql = (
        "SELECT dt, SUM(amount) AS gmv, "
        "SUM(amount)/COUNT(DISTINCT user_id) AS arpu "
        "FROM dwd_order_di GROUP BY dt"
    )
    with patch(
        "app.services.semantic.sql_split._llm_annotate_candidates",
        new=AsyncMock(
            return_value=[
                {
                    "key": "0:arpu",
                    "is_measure": False,
                    "confidence": 0.9,
                    "reason": "复合聚合体不是单度量",
                }
            ]
        ),
    ):
        result = await infer_sql_batch(
            _fake_db(), sql=arith_sql, split_mode="statement", domain_code="sales", use_llm=True
        )
    # 复合候选不被 is_measure=false 剔除（保持候选，进入 LLM 收敛）
    comps = [c for c in result["candidates"] if c["type"] == "composite"]
    assert len(comps) == 1
    assert comps[0]["key"] == "0:arpu"
    assert len(result["skipped"]) == 0


async def test_infer_sql_batch_colname_ratio_is_composite() -> None:
    """S3（三轮审查）：批量路径补列名比率规则（rate/ratio/pct→复合）——对齐单条
    _infer_type，两路径对 SELECT SUM(refund_rate)（日粒度）判定一致（复合）。"""
    result = await infer_sql_batch(
        _fake_db(),
        sql="SELECT dt, SUM(refund_rate) AS refund_rate FROM dwd_order_di GROUP BY dt",
        split_mode="statement",
        domain_code="sales",
    )
    comps = [c for c in result["candidates"] if c["type"] == "composite"]
    assert len(comps) == 1
    assert comps[0]["key"] == "0:refund_rate"
    """use_llm 兜底：LLM 补全不可用/失败（返回 None）→ 保持规则候选不动（source=rule）。"""
    sql = "SELECT dt, SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt"
    with patch(
        "app.services.semantic.sql_split._llm_annotate_candidates",
        new=AsyncMock(return_value=None),
    ):
        result = await infer_sql_batch(
            _fake_db(), sql=sql, split_mode="statement", domain_code="sales", use_llm=True
        )
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["source"] == "rule"
    assert result["candidates"][0]["metric_code"] == "sales_order_amount_day"


async def test_infer_sql_batch_use_llm_raises_budget() -> None:
    """use_llm 显式模式批级 LLM 预算放宽到 _LLM_BATCH_LIMIT_LLM（规则模式 5 次的
    4 倍）——逐语句兜底可超过规则模式上限而不降级 llm_limit。"""
    from app.services.semantic.sql_infer import SqlProfile
    from app.services.semantic.sql_split import _LLM_BATCH_LIMIT, _LLM_BATCH_LIMIT_LLM

    real_parse = parse_sql_profile

    def _fake_parse(sql: str) -> SqlProfile:
        if "unparsable_col" in sql:
            return SqlProfile(sql=sql)  # 规则层解析失败（空画像）
        return real_parse(sql)

    # 构造 _LLM_BATCH_LIMIT + 1 条失败语句（规则模式会封顶 _LLM_BATCH_LIMIT 次）
    parts = [
        f"SELECT dt, SUM(unparsable_col{i}) AS m{i} FROM dwd_order_di GROUP BY dt"
        for i in range(_LLM_BATCH_LIMIT + 1)
    ]
    import re as _re

    def _fake_llm_measures(db, full_sql: str, focus_sql: str) -> list:
        m = _re.search(r"unparsable_col(\d+)", focus_sql)
        num = m.group(1) if m else "0"
        return [
            {
                "column": f"unparsable_col{num}",
                "agg": "SUM",
                "alias": f"m{num}",
                "table": "dwd_order_di",
                "period": "day",
            }
        ]

    llm_mock = AsyncMock(side_effect=_fake_llm_measures)
    with (
        patch(
            "app.services.semantic.sql_split.parse_sql_profile",
            side_effect=_fake_parse,
        ),
        patch(
            "app.services.semantic.sql_split._llm_infer_measures",
            new=llm_mock,
        ),
        patch(
            "app.services.semantic.sql_split._llm_annotate_candidates",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await infer_sql_batch(
            _fake_db(),
            sql=";".join(parts),
            split_mode="semicolon",
            domain_code="sales",
            use_llm=True,
        )
    # 预算放宽：6 条失败语句全部走 LLM（规则模式会封顶 _LLM_BATCH_LIMIT=5 条）
    assert _LLM_BATCH_LIMIT_LLM > _LLM_BATCH_LIMIT
    assert llm_mock.call_count == _LLM_BATCH_LIMIT + 1
    assert len(result["candidates"]) == _LLM_BATCH_LIMIT + 1
    assert all(s["reason"] != "llm_limit" for s in result["skipped"])


# ---------------------------------------------------------------- 建表列注释提取（A-5）


def test_extract_column_comments_hive_ddl() -> None:
    """sqlglot 主路径：从 Hive 建表 DDL 提取列注释映射（表名去库前缀、列名小写）。"""
    from app.services.semantic.sql_split import _extract_column_comments

    sql = """
    create table if not exists wedw_dws.doctor_active_month_di(
      month_id string comment '统计月,时间格式yyyy-MM',
      hosp_code string comment '医院编码',
      current_month_active_doctor_cnt int comment '月活',
      last_month_active_doctor_cnt int comment '当月访问的用户在下个月仍活跃，用于统计留存'
    )
    stored as orc;
    """
    comments = _extract_column_comments(sql)
    assert comments["doctor_active_month_di"]["month_id"] == "统计月,时间格式yyyy-MM"
    assert comments["doctor_active_month_di"]["hosp_code"] == "医院编码"
    assert comments["doctor_active_month_di"]["current_month_active_doctor_cnt"] == "月活"
    assert (
        comments["doctor_active_month_di"]["last_month_active_doctor_cnt"]
        == "当月访问的用户在下个月仍活跃，用于统计留存"
    )


def test_extract_column_comments_regex_fallback() -> None:
    """正则兜底路径：sqlglot 无法解析时仍能粗提取建表列注释（不抛异常）。"""
    from app.services.semantic.sql_split import _extract_column_comments

    # 含 sqlglot 不认识的方言片段 → 触发正则兜底
    sql = """
    CREATE TABLE IF NOT EXISTS ods_x.t (col_a STRING COMMENT '金额', col_b INT COMMENT '次数')
    USING PARQUET
    PARTITIONED BY (dt STRING);
    """
    comments = _extract_column_comments(sql)
    # 表名去库前缀
    assert "t" in comments
    assert comments["t"].get("col_a") == "金额"
    assert comments["t"].get("col_b") == "次数"


def test_insert_target_table_extracts_target() -> None:
    """INSERT 目标表提取（下沉场景注释反查的目标表）。"""
    from app.services.semantic.sql_split import _insert_target_table

    sql = (
        "insert overwrite table wedw_dws.doctor_active_month_di "
        "select a.month_id, a.current_month_active_doctor_cnt "
        "from (select substr(create_date,1,7) as month_id, "
        "count(distinct doctor_code) as current_month_active_doctor_cnt "
        "from wedw_dw.doctor_visit_agent_info_da group by substr(create_date,1,7)) a"
    )
    assert _insert_target_table(sql) == "doctor_active_month_di"
    # 无 INSERT 语句返回 None
    assert _insert_target_table("select 1") is None


async def test_infer_sql_batch_uses_create_table_comments_for_name() -> None:
    """A-5：建表 DDL 列注释驱动候选名称——「月活」不再落成「月doctor次数」；
    注释已含周期词（月）时不重复加周期前缀。"""
    from app.services.semantic.sql_split import (
        _extract_column_comments,
        _insert_target_table,
    )

    sql = """
    create table if not exists wedw_dws.doctor_active_month_di(
      month_id string comment '统计月,时间格式yyyy-MM',
      hosp_code string comment '医院编码',
      current_month_active_doctor_cnt int comment '月活',
      last_month_active_doctor_cnt int comment '当月访问的用户在下个月仍活跃，用于统计留存'
    )
    stored as orc;
    insert overwrite table wedw_dws.doctor_active_month_di
    select a.month_id, a.hosp_code,
           a.current_month_active_doctor_cnt,
           a.last_month_active_doctor_cnt
    from (
        select substr(create_date,1,7) as month_id, hosp_code,
               count(distinct doctor_code) as current_month_active_doctor_cnt,
               count(distinct case when last_visit_date is not null then doctor_code end)
                   as last_month_active_doctor_cnt
        from wedw_dw.doctor_visit_agent_info_da
        group by substr(create_date,1,7), hosp_code
    ) a
    """
    result = await infer_sql_batch(
        _fake_db(), sql=sql, split_mode="statement", domain_code="sales"
    )
    by_key = {c["key"]: c for c in result["candidates"]}
    # 下沉度量 alias 作注释反查锚点（sunk 场景 code_col=alias）
    cand = by_key["1:current_month_active_doctor_cnt"]
    # 建表注释「月活」驱动名称，且注释已含「月」不再重复加周期前缀
    assert cand["name"] == "月活"
    assert cand["type"] == "derived"
    # 无注释列：回退词表（医生数），不因注释缺失而空
    cand2 = by_key["1:last_month_active_doctor_cnt"]
    assert cand2["name"] == "当月访问的用户在下个月仍活跃，用于统计留存"
    # INSERT 目标表提取正常（注释反查所依赖）——传含 insert overwrite 前缀的完整语句
    assert (
        _insert_target_table("insert overwrite" + sql.split("insert overwrite")[1])
        == "doctor_active_month_di"
    )
    assert (
        _extract_column_comments(sql)["doctor_active_month_di"][
            "current_month_active_doctor_cnt"
        ]
        == "月活"
    )



async def test_infer_sql_batch_derived_arithmetic_deps_and_auto_composite() -> None:
    """A7/B/C：外层宽表 ETL——算术派生列（a-b-c）产出复合候选且 dependencies
    解析到 3 个原子编码；同列多 count 名称附加 alias 区分；语句含运算但无命名
    派生时 synthesize_composite=False 也自动合成整语句复合。"""
    sql = (
        "select all_order_cnt, session_side_order_cnt, region_org_order_cnt, "
        "all_order_cnt - session_side_order_cnt - region_org_order_cnt as "
            "old_page_transfer_order_cnt "
        "from (select count(1) as all_order_cnt, "
        "count(case when ds='a' then id end) as session_side_order_cnt, "
        "count(case when ds='b' then id end) as region_org_order_cnt "
        "from wedw_dwd.telemedicine_local_bidirectional_referral_record_df "
        "where date_id='2026-08-18' group by hosp_code) result"
    )
    result = await infer_sql_batch(
        _fake_db(), sql=sql, split_mode="statement", domain_code="hosp",
        synthesize_composite=False,  # 验证 B：不依赖开关也能识别
    )
    by_key = {c["key"]: c for c in result["candidates"]}
    # C：同列多 count 名称附加 alias 区分（不再全部「日订单量」）
    names = [
        by_key[k]["name"]
        for k in ("0:all_order_cnt", "0:session_side_order_cnt", "0:region_org_order_cnt")
    ]
    assert len(set(names)) == 3, f"名称应可区分：{names}"
    # A：算术派生列 → 复合候选 + dependencies 解析到原子编码
    derived = by_key["0:old_page_transfer_order_cnt"]
    assert derived["type"] == "composite"
    deps = derived.get("dependencies") or []
    assert len(deps) == 3, f"依赖应 3 个原子，实际 {deps}"
    assert by_key["0:all_order_cnt"]["metric_code"] in deps
    assert by_key["0:session_side_order_cnt"]["metric_code"] in deps
    assert by_key["0:region_org_order_cnt"]["metric_code"] in deps
    # B：含运算无命名派生列 → 自动合成整语句复合（即使开关 False）
    result2 = await infer_sql_batch(
        _fake_db(),
        sql=(
            "SELECT SUM(amount) AS a, COUNT(DISTINCT uid) AS b, "
            "SUM(amount)/COUNT(DISTINCT uid) AS ratio FROM ods.t"
        ),
        split_mode="statement",
        domain_code="sales",
        synthesize_composite=False,
    )
    assert any(c["type"] == "composite" for c in result2["candidates"]), "含运算应自动合成复合"


async def test_infer_sql_batch_union_and_set_agg_needs_review() -> None:
    """U-1/U-2 完整链路：顶层 UNION 合并候选 + 集合聚合候选带 needs_review（不
    再静默降级 COUNT、不再只取首分支）。"""
    # U-1 顶层 UNION ALL → 两分支候选都产出
    r1 = await infer_sql_batch(
        _fake_db(),
        sql=(
            "select d, sum(amt) as amt from ods.a group by d "
            "union all "
            "select d, count(distinct uid) as uv from ods.b group by d"
        ),
        split_mode="statement",
        domain_code="sales",
    )
    keys = [c["key"] for c in r1["candidates"]]
    assert any("amt" in k for k in keys), f"UNION 首分支度量应产出：{keys}"
    assert any("uv" in k for k in keys), f"UNION 次分支度量应产出：{keys}"
    # U-2 集合聚合 → COUNT_DISTINCT + needs_review
    r2 = await infer_sql_batch(
        _fake_db(),
        sql="select collect_set(product) as ps, count(1) as c from ods.a",
        split_mode="statement",
        domain_code="sales",
    )
    by_key = {c["key"]: c for c in r2["candidates"]}
    ps = next(c for k, c in by_key.items() if "ps" in k)
    assert ps["aggregation"] == "COUNT_DISTINCT", f"集合聚合应 COUNT_DISTINCT：{ps['aggregation']}"
    assert ps.get("needs_review"), "集合聚合候选应带口径需核对标识"
