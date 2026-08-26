"""SQL 批量切分与候选推断单元测试（FR-010 批量注册增强，场景A/B）。

覆盖：
- 场景A 三模式切分：semicolon（引号/注释内分号不误切）、statement（CTE/INSERT 单条）、
  custom（delimiters/start_markers 正则 + LLM 语义分段兜底 + 不可用降级单段）
- 场景B 单语句多度量拆分（split_select_measures：共享源表/维度/周期）
- parse_sql_split_result（别名/去重/空段过滤/整体失败 None）
- infer_sql_batch 集成：多语句候选生成、多度量拆分 + 复合合成、skipped、域建议回填
"""

from __future__ import annotations

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


async def test_infer_sql_batch_synthesize_composite() -> None:
    """场景B 多度量 + synthesize_composite → N 原子 + 1 复合（依赖组内原子）。"""
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
    assert len(composites) == 1
    comp = composites[0]
    assert comp["key"] == "0:composite"
    assert comp["metric_code"].startswith("sales_order_")
    assert set(comp["dependencies"]) == {a["metric_code"] for a in atoms}
    assert comp["definition_json"]["sql"] == _MULTI_MEASURE_SQL


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
    atoms = [c for c in result["candidates"] if c["type"] == "atomic"]
    assert len(atoms) == 1
    cand = atoms[0]
    assert cand["key"] == "2:current_month_active_doctor_cnt"
    assert cand["source_table"] == "wedw_dw.doctor_visit_agent_info_da"
    assert cand["measure_column"] == "doctor_code"
    assert cand["aggregation"] == "COUNT_DISTINCT"
    assert "COUNT(DISTINCT" in cand["definition_json"]["expression"].upper()
    # substr(create_date,1,7) 截月 → 周期自动识别为月（不再回落 day）
    assert cand["period"] == "month"


async def test_infer_sql_batch_etl_insert_synthesize_composite() -> None:
    """透传 INSERT 多度量 + synthesize_composite → N 原子 + 1 复合。"""
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
    atoms = [c for c in result["candidates"] if c["type"] == "atomic"]
    composites = [c for c in result["candidates"] if c["type"] == "composite"]
    assert len(atoms) == 2
    assert len(composites) == 1
    # key 用别名区分同列（doctor_code）不同语义的度量
    keys = {a["key"] for a in atoms}
    assert keys == {
        "0:current_month_active_doctor_cnt",
        "0:last_month_active_doctor_cnt",
    }
    comp = composites[0]
    assert comp["key"] == "0:composite"
    assert set(comp["dependencies"]) == {a["metric_code"] for a in atoms}


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
    cands = [c for c in result["candidates"] if c["type"] == "atomic"]
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
        _classify_no_measure(
            "SELECT countIf(x > 0) AS c FROM t", empty, llm_tried=False
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
            {"code": "sales", "name": "销售", "confidence": 0.9, "source": "catalog", "reason": "表A"},
            {"code": "health", "name": "医疗", "confidence": 0.8, "source": "catalog", "reason": "表B"},
        ],
        "matched_tables": ["dwd_order_di", "dwd_patient_di"],
    }

    async def _per_stmt(db, **kwargs):
        # 整段（多语句含分号）→ multiple；单段 → 按内容返回 unique（跨域）
        sql = kwargs.get("sql", "")
        if ";" in sql:
            return multiple
        if "user_id" in sql:
            return {"status": "unique", "domain": {"code": "health"}, "candidates": [], "matched_tables": []}
        return {"status": "unique", "domain": {"code": "sales"}, "candidates": [], "matched_tables": []}

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


async def test_infer_sql_batch_composite_uses_real_period() -> None:
    """P1-3：月粒度语句的复合候选编码/粒度用实际周期（不再硬编码 _day/day）。"""
    month_sql = (
        "SELECT substr(create_date,1,7) AS month_id, "
        "SUM(amount) AS gmv, COUNT(DISTINCT user_id) AS uv "
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
    # 原子候选周期也是 month
    atoms = [c for c in result["candidates"] if c["type"] == "atomic"]
    assert all(c["period"] == "month" for c in atoms)


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
