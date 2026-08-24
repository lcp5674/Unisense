"""E2E 种子数据脚本：通过真实 HTTP API 造出覆盖全部业务模块的端到端测试数据。

目的：
    让 Playwright E2E 在真实浏览器中验证"每个按钮都能看到真实业务数据"，而不是
    空态兜底。数据全部通过真实 REST API 产生（个别基础用户因无公开创建端点而
    直插 DB，与 scripts/seed_admin.py 一致），并对每类数据做 GET 冒烟断言。

用法（backend 目录，venv 激活，后端已在 http://localhost:8100 运行）：
    python scripts/seed_e2e_data.py [--base http://localhost:8100] \
        [--admin-user admin] [--admin-pass changeme123]

幂等：可重复运行；已存在实体跳过，不产生重复数据。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import httpx  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.db.mysql import async_session_factory, engine  # noqa: E402
from app.models.metric import Metric  # noqa: E402
from app.models.user import User  # noqa: E402

API_PREFIX = "/api/v1"

# 造数账号（与前端 E2E 登录账号一致）
ADMIN = {"username": "admin", "password": "changeme123"}

# 直插的基础用户：用于验证权限闭环（PII 复核职责分离、仲裁角色、只读消费）
E2E_USERS = [
    {
        "username": "e2e_owner",
        "email": "e2e_owner@unisense.local",
        "display_name": "E2E指标Owner",
        "role": "metric_owner",
        "domain": "outpatient",
    },
    {
        "username": "e2e_compliance",
        "email": "e2e_compliance@unisense.local",
        "display_name": "E2E合规官",
        "role": "compliance_officer",
        "domain": None,
    },
    {
        "username": "e2e_analyst",
        "email": "e2e_analyst@unisense.local",
        "display_name": "E2E分析师",
        "role": "analyst",
        "domain": None,
    },
]

# OneData 原子层：逻辑度量目录（原子指标 = 逻辑度量 + 聚合方式，不绑物理表）。
# 度量格式联动默认单位/小数位（AMOUNT→元/2 位；RATIO→小数/4 位；NUMERIC→显式单位/0 位）。
# 度量目录从电商演示改为医疗实际场景：口径依托 HIS 门诊真实数据（dp元数据.csv：
# tj_cf_drug_prescription / tj_cf_diagnosis / tj_pharmacy_feebill_master 等）。
MEASURES: list[dict[str, Any]] = [
    {
        "code": "outp_register_cnt",
        "name": "门诊挂号人次",
        "description": "门诊挂号记录数（按挂号记录去重）",
        "measure_format": "NUMERIC",
        "default_unit": "人次",
        "default_decimal_places": 0,
        "domain": "outpatient",
        "category": "FLOW",
        "stat_caliber": "挂号表记录数，按挂号单号去重后计数",
    },
    {
        "code": "outp_visit_cnt",
        "name": "门诊就诊人次",
        "description": "门诊实际就诊人次（收费记录按就诊去重）",
        "measure_format": "NUMERIC",
        "default_unit": "人次",
        "default_decimal_places": 0,
        "domain": "outpatient",
        "category": "FLOW",
        "stat_caliber": "收费主表按就诊号去重计数",
    },
    {
        "code": "outp_fee_amount",
        "name": "门诊收费金额",
        "description": "门诊收费总额（实收合计）",
        "measure_format": "AMOUNT",
        "default_unit": "CNY",
        "default_decimal_places": 2,
        "domain": "medical_fee",
        "category": "FEE",
        "stat_caliber": "收费明细实收金额求和",
    },
    {
        "code": "outp_drug_fee_amount",
        "name": "门诊药品费用",
        "description": "门诊收费中药品费用合计",
        "measure_format": "AMOUNT",
        "default_unit": "CNY",
        "default_decimal_places": 2,
        "domain": "medication",
        "category": "DRUG",
        "stat_caliber": "收费明细中药品类费用求和",
    },
    {
        "code": "outp_prescription_cnt",
        "name": "门诊处方数",
        "description": "门诊处方数量",
        "measure_format": "NUMERIC",
        "default_unit": "笔",
        "default_decimal_places": 0,
        "domain": "medication",
        "category": "DRUG",
        "stat_caliber": "处方表记录数，按处方单号去重后计数",
    },
    {
        "code": "yb_settle_amount",
        "name": "门诊医保结算金额",
        "description": "门诊医保结算总金额",
        "measure_format": "AMOUNT",
        "default_unit": "CNY",
        "default_decimal_places": 2,
        "domain": "medical_insurance",
        "category": "MEDICAL_INSURANCE",
        "stat_caliber": "医保结算明细结算金额求和",
    },
    {
        "code": "yb_reimburse_ratio",
        "name": "医保报销比例",
        "description": "医保支付金额占结算金额的比例",
        "measure_format": "RATIO",
        "default_unit": "小数",
        "default_decimal_places": 4,
        "domain": "medical_insurance",
        "category": "MEDICAL_INSURANCE",
        "stat_caliber": "医保支付金额 ÷ 结算总金额",
    },
    {
        "code": "outp_avg_fee",
        "name": "次均门诊费用",
        "description": "门诊收费总额 ÷ 就诊人次",
        "measure_format": "AMOUNT",
        "default_unit": "CNY",
        "default_decimal_places": 2,
        "domain": "medical_fee",
        "category": "EFFICIENCY",
        "stat_caliber": "门诊收费总额 ÷ 门诊就诊人次",
    },
    {
        "code": "outp_drug_ratio",
        "name": "门诊药占比",
        "description": "药品费用占门诊收费总额的比例",
        "measure_format": "RATIO",
        "default_unit": "小数",
        "default_decimal_places": 4,
        "domain": "medication",
        "category": "QUALITY",
        "stat_caliber": "门诊药品费用 ÷ 门诊收费总额",
    },
]

# 标准指标定义（definition_json 里 sql 会被 sqlglot 校验，必须是合法 SQL）
METRICS: list[dict[str, Any]] = [
    {
        "code": "outp_e2e_register_day",
        "name": "门诊挂号人次",
        "domain": "outpatient",
        "type": "atomic",
        "unit": "TIMES",
        "aggregation": "COUNT",
        "time_semantics": "PERIOD",
        "freshness": "T1",
        "dw_layer": "DWS",
        "metric_tier": "T1",
        # OneData 原子层：引用逻辑度量（门诊挂号人次），不绑物理表
        "measure_id_code": "outp_register_cnt",
        "period": "day",
        "definition_json": {
            "sql": (
                "SELECT COUNT(register_id) AS register_cnt, department_id, dt "
                "FROM ods_his_register GROUP BY department_id, dt"
            ),
            "source_tables": ["ods_his_register"],
            "dimensions": ["department_id", "dt"],
            "measures": [{"name": "register_cnt", "aggregation": "COUNT"}],
            "period": "day",
        },
    },
    {
        "code": "outp_e2e_visit_day",
        "name": "门诊就诊人次",
        "domain": "outpatient",
        "type": "atomic",
        "unit": "TIMES",
        "aggregation": "COUNT_DISTINCT",
        "time_semantics": "PERIOD",
        "freshness": "T1",
        "dw_layer": "DWS",
        "metric_tier": "T1",
        # OneData 原子层：引用逻辑度量（门诊就诊人次），不绑物理表
        "measure_id_code": "outp_visit_cnt",
        "period": "day",
        "definition_json": {
            "sql": (
                "SELECT COUNT(DISTINCT visit_id) AS visit_cnt, dept_id, dt "
                "FROM ods_his_receipt WHERE delete_flag = 0 GROUP BY dept_id, dt"
            ),
            "source_tables": ["ods_his_receipt"],
            "dimensions": ["dept_id", "dt"],
            "measures": [{"name": "visit_cnt", "aggregation": "COUNT_DISTINCT"}],
            "period": "day",
        },
    },
    {
        "code": "outp_e2e_fee_day",
        "name": "门诊收费金额",
        "domain": "medical_fee",
        "type": "atomic",
        "unit": "CNY",
        "aggregation": "SUM",
        "time_semantics": "PERIOD",
        "freshness": "T1",
        "dw_layer": "DWS",
        "metric_tier": "T1",
        # OneData 原子层：引用逻辑度量（门诊收费金额），不绑物理表
        "measure_id_code": "outp_fee_amount",
        "period": "day",
        "definition_json": {
            "sql": (
                "SELECT SUM(total_fee) AS fee, dept_id, dt FROM ods_his_receipt "
                "WHERE delete_flag = 0 GROUP BY dept_id, dt"
            ),
            "source_tables": ["ods_his_receipt"],
            "dimensions": ["dept_id", "dt"],
            "measures": [{"name": "fee", "aggregation": "SUM"}],
            "period": "day",
        },
    },
    {
        "code": "outp_e2e_drugfee_day",
        "name": "门诊药品费用",
        "domain": "medication",
        "type": "atomic",
        "unit": "CNY",
        "aggregation": "SUM",
        "time_semantics": "PERIOD",
        "freshness": "T1",
        "dw_layer": "DWS",
        "metric_tier": "T2",
        # OneData 原子层：引用逻辑度量（门诊药品费用），不绑物理表
        "measure_id_code": "outp_drug_fee_amount",
        "period": "day",
        "definition_json": {
            "sql": (
                "SELECT SUM(drug_fee) AS drug_fee, dt FROM ods_his_receipt "
                "WHERE delete_flag = 0 GROUP BY dt"
            ),
            "source_tables": ["ods_his_receipt"],
            "dimensions": ["dt"],
            "measures": [{"name": "drug_fee", "aggregation": "SUM"}],
            "period": "day",
        },
    },
    {
        "code": "outp_e2e_prescription_day",
        "name": "门诊处方数",
        "domain": "medication",
        "type": "atomic",
        "unit": "ORDER",
        "aggregation": "COUNT",
        "time_semantics": "PERIOD",
        "freshness": "T1",
        "dw_layer": "DWS",
        "metric_tier": "T1",
        # OneData 原子层：引用逻辑度量（门诊处方数），不绑物理表
        "measure_id_code": "outp_prescription_cnt",
        "period": "day",
        "definition_json": {
            "sql": (
                "SELECT COUNT(DISTINCT prescription_id) AS prescription_cnt, doctor_id, dt "
                "FROM ods_his_prescription GROUP BY doctor_id, dt"
            ),
            "source_tables": ["ods_his_prescription"],
            "dimensions": ["doctor_id", "dt"],
            "measures": [{"name": "prescription_cnt", "aggregation": "COUNT"}],
            "period": "day",
        },
    },
    {
        "code": "outp_e2e_piipatient_day",
        "name": "门诊患者电话留存指标",
        "domain": "patient",
        "type": "atomic",
        "unit": "PERSON",
        "aggregation": "COUNT_DISTINCT",
        "time_semantics": "PERIOD",
        "freshness": "T1",
        "dw_layer": "DWS",
        "metric_tier": "T3",
        # OneData 原子层：引用逻辑度量（门诊挂号人次），不绑物理表
        "measure_id_code": "outp_register_cnt",
        "period": "day",
        "definition_json": {
            "sql": (
                "SELECT COUNT(DISTINCT patient_id) AS piipatient, dt "
                "FROM ods_his_register GROUP BY dt"
            ),
            "source_tables": ["ods_his_register"],
            "dimensions": ["dt"],
            "measures": [{"name": "piipatient", "aggregation": "COUNT_DISTINCT"}],
            "source_fields": [{"name": "patient_phone", "pii": True}],
            "period": "day",
        },
    },
    {
        "code": "outp_e2e_deprecated_day",
        "name": "门诊待废弃指标",
        "domain": "medical_fee",
        "type": "atomic",
        "unit": "CNY",
        "aggregation": "SUM",
        "time_semantics": "PERIOD",
        "freshness": "T1",
        "dw_layer": "DWS",
        "metric_tier": "T3",
        # OneData 原子层：引用逻辑度量（门诊收费金额），不绑物理表
        "measure_id_code": "outp_fee_amount",
        "period": "day",
        "definition_json": {
            "sql": "SELECT total_fee, dt FROM ods_his_receipt",
            "source_tables": ["ods_his_receipt"],
            "dimensions": ["dt"],
            "measures": [{"name": "total_fee", "aggregation": "SUM"}],
            "period": "day",
        },
    },
    {
        "code": "yb_e2e_settle_day",
        "name": "门诊医保结算金额",
        "domain": "medical_insurance",
        "type": "atomic",
        "unit": "CNY",
        "aggregation": "SUM",
        "time_semantics": "PERIOD",
        "freshness": "T1",
        "dw_layer": "DWS",
        "metric_tier": "T1",
        # OneData 原子层：引用逻辑度量（门诊医保结算金额），不绑物理表
        "measure_id_code": "yb_settle_amount",
        "period": "day",
        "definition_json": {
            "sql": (
                "SELECT SUM(settle_amount) AS settle_amount, settle_time, dt "
                "FROM ods_his_yb_settle WHERE delete_flag = 0 GROUP BY settle_time, dt"
            ),
            "source_tables": ["ods_his_yb_settle"],
            "dimensions": ["settle_time", "dt"],
            "measures": [{"name": "settle_amount", "aggregation": "SUM"}],
            "period": "day",
        },
    },
    {
        "code": "outp_e2e_avgfee_day",
        "name": "次均门诊费用（派生）",
        "domain": "medical_fee",
        "type": "derived",
        "unit": "CNY",
        "aggregation": "AVG",
        "time_semantics": "PERIOD",
        "freshness": "T1",
        "dw_layer": "ADS",
        "metric_tier": "T1",
        # OneData 挂载层：派生指标挂载数据集（源表/列/粒度/周期/域），granularity 由 service 从 mount 回填
        "mount": {
            "source_table": "ads_outp_e2e_fee_day",
            "source_column": "avg_fee",
            "granularity": "day",
            "default_period": "day",
            "domain": "medical_fee",
        },
        "period": "day",
        "definition_json": {
            "sql": (
                "SELECT fee / NULLIF(visit_cnt, 0) AS avg_fee, dt "
                "FROM ads_outp_e2e_fee_day"
            ),
            "source_tables": ["ads_outp_e2e_fee_day"],
            "dimensions": ["dt"],
            "measures": [{"name": "avg_fee", "aggregation": "AVG"}],
            "dependencies": ["outp_e2e_fee_day", "outp_e2e_visit_day"],
            "period": "day",
        },
    },
]

# 口径相同但编码不同的一对指标 → 稳定触发 same_def_diff_name 软冲突
CONFLICT_DEF = "SELECT SUM(total_fee) AS fee, dt FROM ods_his_receipt GROUP BY dt"
CONFLICT_PAIR = [
    {
        "code": "outp_e2e_conflicta_day",
        "name": "门诊冲突口径A",
        "domain": "medical_fee",
        "type": "atomic",
        "unit": "CNY",
        "aggregation": "SUM",
        "time_semantics": "PERIOD",
        "freshness": "T1",
        "dw_layer": "DWS",
        "metric_tier": "T3",
        # OneData 原子层：引用逻辑度量（门诊收费金额），不绑物理表
        "measure_id_code": "outp_fee_amount",
        "period": "day",
        "definition_json": {
            "sql": CONFLICT_DEF,
            "source_tables": ["ods_his_receipt"],
            "dimensions": ["dt"],
            "measures": [{"name": "fee", "aggregation": "SUM"}],
            "period": "day",
        },
    },
    {
        "code": "outp_e2e_conflictb_day",
        "name": "门诊冲突口径B",
        "domain": "medical_fee",
        "type": "atomic",
        "unit": "CNY",
        "aggregation": "SUM",
        "time_semantics": "PERIOD",
        "freshness": "T1",
        "dw_layer": "DWS",
        "metric_tier": "T3",
        # OneData 原子层：引用逻辑度量（门诊收费金额），不绑物理表
        "measure_id_code": "outp_fee_amount",
        "period": "day",
        "definition_json": {
            "sql": CONFLICT_DEF,
            "source_tables": ["ods_his_receipt"],
            "dimensions": ["dt"],
            "measures": [{"name": "fee", "aggregation": "SUM"}],
            "period": "day",
        },
    },
]

ALL_METRICS = METRICS + CONFLICT_PAIR


class SeedError(RuntimeError):
    """造数脚本错误。"""


class Api:
    """真实 HTTP API 客户端（统一信封 {code,message,data,...}）。"""

    def __init__(self, base: str, token: str = ""):
        self.base = base.rstrip("/")
        self.token = token
        self._client = httpx.Client(timeout=30.0)

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        token: str | None = None,
        ok_status: tuple[int, ...] = (200, 201),
        quiet: bool = False,
    ) -> dict[str, Any]:
        url = f"{self.base}{API_PREFIX}{path}"
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        elif self.token:
            headers.update(self.auth_headers())
        resp = self._client.request(method, url, json=body, headers=headers)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text}
        if resp.status_code not in ok_status:
            msg = payload.get("message") or payload.get("detail") or resp.text
            raise SeedError(f"{method} {path} -> {resp.status_code}: {msg}")
        return payload

    def ok(
        self, method: str, path: str, body: dict[str, Any] | None = None, **kw
    ) -> dict[str, Any]:
        """请求并取出统一信封的 data。"""
        return self.request(method, path, body, **kw).get("data", {})

    def get(self, path: str, **kw) -> dict[str, Any]:
        return self.ok("GET", path, **kw)

    def post(self, path: str, body: dict[str, Any] | None = None, **kw) -> dict[str, Any]:
        return self.ok("POST", path, body, **kw)

    def put(self, path: str, body: dict[str, Any] | None = None, **kw) -> dict[str, Any]:
        return self.ok("PUT", path, body, **kw)


def _fmt(obj: Any) -> str:
    """简短描述实体，便于日志。"""
    if isinstance(obj, dict):
        return str(
            obj.get("metric_code")
            or obj.get("code")
            or obj.get("name")
            or obj.get("id")
            or obj
        )
    return str(obj)


# ---------------------------------------------------------------------------
# 1. 基础用户（直插 DB，幂等；无公开创建端点）
# ---------------------------------------------------------------------------
async def _ensure_users_async() -> dict[str, int]:
    """插入 E2E 基础用户，返回 {username: id}。"""
    async with async_session_factory() as db:
        ids: dict[str, int] = {}
        for spec in E2E_USERS:
            from sqlalchemy import select

            result = await db.execute(
                select(User).where(User.username == spec["username"])
            )
            user = result.scalar_one_or_none()
            if user is None:
                user = User(
                    org_id=1,
                    username=spec["username"],
                    email=spec["email"],
                    password_hash=await hash_password("changeme123"),
                    display_name=spec["display_name"],
                    role=spec["role"],
                    domain=spec["domain"],
                    status="active",
                )
                db.add(user)
                await db.flush()
                print(f"[users] 创建 {spec['username']} (role={spec['role']}) id={user.id}")
            ids[spec["username"]] = user.id
        await db.commit()
    await engine.dispose()
    return ids


def ensure_users() -> dict[str, int]:
    return asyncio.run(_ensure_users_async())


def _fetch_user_ids() -> dict[str, int]:
    """跳过创建时，仍从 DB 取回已存在 E2E 用户的 {username: id}。"""

    async def _run() -> dict[str, int]:
        async with async_session_factory() as db:
            from sqlalchemy import select

            ids: dict[str, int] = {}
            for spec in E2E_USERS:
                result = await db.execute(
                    select(User).where(User.username == spec["username"])
                )
                user = result.scalar_one_or_none()
                if user is not None:
                    ids[spec["username"]] = user.id
        await engine.dispose()
        return ids

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# 2. OneData 原子层：逻辑度量目录 + 指标生命周期
# ---------------------------------------------------------------------------
def ensure_measure(api: Api, spec: dict[str, Any]) -> dict[str, Any]:
    """幂等创建逻辑度量并发布，返回响应 dict。

    OneData（界限文档 §2.1）：原子指标 = 逻辑度量 + 聚合方式。度量必须 PUBLISHED
    才能被原子指标引用（create_metric 校验 measure 状态）。
    """
    code = spec["code"]
    # 已存在则直接查（可能处于 DRAFT，确保发布后返回）
    try:
        existing = api.get(f"/measure-catalogs/{code}")
        if existing.get("status") != "PUBLISHED":
            published = api.post(f"/measure-catalogs/{code}/publish")
            print(f"[measure] 已存在但未发布，补发布 {code} -> {published.get('status')}")
            return published
        print(f"[measure] 已存在 {code} status={existing.get('status')}")
        return existing
    except SeedError:
        pass
    body = {
        "measure_code": code,
        "name": spec["name"],
        "description": spec.get("description"),
        "measure_format": spec["measure_format"],
        "domain": spec["domain"],
    }
    if spec.get("default_unit") is not None:
        body["default_unit"] = spec["default_unit"]
    if spec.get("default_decimal_places") is not None:
        body["default_decimal_places"] = spec["default_decimal_places"]
    if spec.get("category") is not None:
        body["category"] = spec["category"]
    if spec.get("stat_caliber") is not None:
        body["stat_caliber"] = spec["stat_caliber"]
    created = api.post("/measure-catalogs", body)
    print(f"[measure] 创建 {code} id={created.get('id')} status={created.get('status')}")
    # 发布逻辑度量（DRAFT→PUBLISHED）
    published = api.post(f"/measure-catalogs/{code}/publish")
    print(f"[measure] 发布 {code} -> {published.get('status')}")
    return published


def ensure_metric(
    api: Api, spec: dict[str, Any], measure_ids: dict[str, int] | None = None
) -> dict[str, Any]:
    """幂等创建指标，返回响应 dict。

    OneData（界限文档 §2.3）：原子指标经 measure_id 引用逻辑度量（不绑物理表）；
    派生指标携带 mount（挂载数据集，粒度由 service 从 mount 回填）。
    """
    code = spec["code"]
    # 已存在则直接查
    try:
        existing = api.get(f"/metric-definitions/{code}")
        print(f"[metric] 已存在 {code} status={existing.get('status')}")
        return existing
    except SeedError:
        pass
    body = {
        "metric_code": code,
        "name": spec["name"],
        "domain": spec["domain"],
        "type": spec["type"],
        "unit": spec["unit"],
        "aggregation": spec["aggregation"],
        "time_semantics": spec["time_semantics"],
        "freshness": spec["freshness"],
        "dw_layer": spec["dw_layer"],
        "metric_tier": spec.get("metric_tier", "T3"),
        # 显式使用 active 字典值（ADDITIVE 在种子环境可能被停用）
        "additivity": "SEMI_ADDITIVE",
        "serving_mode": "BATCH_ONLY",
        "definition_json": spec["definition_json"],
    }
    # 原子指标：引用逻辑度量（measure_id_code → measure_ids 数值 id）
    measure_code = spec.get("measure_id_code")
    if measure_code and measure_ids and measure_ids.get(measure_code):
        body["measure_id"] = measure_ids[measure_code]
    # 派生指标：挂载数据集（source_table/source_column/granularity/period/domain）
    if spec.get("mount"):
        body["mount"] = spec["mount"]
    # 粒度：原子可显式声明（兼容展示）；派生缺省由 service 从 mount 回填
    if spec.get("granularity"):
        body["granularity"] = spec["granularity"]
    if spec.get("period"):
        body["period"] = spec["period"]
    # POST 幂等兜底：已存在的编码（含被口径裁决作废/归档的，其单条 GET 返回 404
    # 导致上面的存在性检测 miss）会返回 409——此时视为已存在跳过，不抛错。
    created = api.post("/metric-definitions", body, ok_status=(200, 201, 409))
    if created.get("id"):
        print(f"[metric] 创建 {code} id={created.get('id')} status={created.get('status')}")
    else:
        print(f"[metric] {code} 已存在（编码冲突/归档），跳过")
    return created


def _backfill_legacy_metric_measures(measure_ids: dict[str, int]) -> None:
    """OneData 存量订正：旧式原子指标（measure_id 为空）按度量编码补关联逻辑度量。

    仅填 measure_id（数据订正，绕过业务版本流——避免已发布指标触发 PENDING_VERSION）；
    派生指标不在此列（其 OneData 化 = 挂载，需重建，D3 决策留人工引导）。
    幂等：仅命中 measure_id 为空的存量行，不重复覆盖。
    """

    async def _run() -> None:
        from sqlalchemy import select, update

        async with async_session_factory() as db:
            for code, measure_code in legacy_metric_measure_map().items():
                mid = measure_ids.get(measure_code)
                if not mid:
                    continue
                result = await db.execute(
                    select(Metric.id).where(
                        Metric.metric_code == code,
                        Metric.measure_id.is_(None),
                    )
                )
                metric_id = result.scalar_one_or_none()
                if metric_id is None:
                    continue
                await db.execute(update(Metric).where(Metric.id == metric_id).values(measure_id=mid))
                print(f"[metric] 存量订正 {code} measure_id={mid}")
            await db.commit()
        await engine.dispose()

    asyncio.run(_run())


def legacy_metric_measure_map() -> dict[str, str]:
    """存量 E2E 原子指标 → 逻辑度量编码（与 METRICS spec 的 measure_id_code 一致）。"""
    return {
        "outp_e2e_register_day": "outp_register_cnt",
        "outp_e2e_visit_day": "outp_visit_cnt",
        "outp_e2e_fee_day": "outp_fee_amount",
        "outp_e2e_drugfee_day": "outp_drug_fee_amount",
        "outp_e2e_prescription_day": "outp_prescription_cnt",
        "outp_e2e_piipatient_day": "outp_register_cnt",
        "outp_e2e_deprecated_day": "outp_fee_amount",
        "yb_e2e_settle_day": "yb_settle_amount",
        "outp_e2e_conflicta_day": "outp_fee_amount",
        "outp_e2e_conflictb_day": "outp_fee_amount",
    }


def publish_metric(api: Api, code: str, *, pii_columns: list[str] | None = None) -> dict[str, Any]:
    """提交审批 + 审批通过（admin 自审豁免），返回审批后响应。"""
    metric = api.get(f"/metric-definitions/{code}")
    status = metric.get("status")
    if status in ("PUBLISHED", "DEPRECATED"):
        print(f"[metric] {code} 已 {status}，跳过")
        return metric
    if status == "DRAFT":
        api.post(f"/metric-definitions/{code}/submit", {"change_reason": "e2e 种子数据提交审批"})
        print(f"[metric] {code} 提交审批")
    # 若 PII 指标，先由合规官做 PII 复核（职责分离：复核人 != owner）
    metric = api.get(f"/metric-definitions/{code}")
    if metric.get("pii_flag") and not metric.get("compliance_reviewed"):
        _pii_review(api, code, columns=pii_columns or [])
    approved = api.post(f"/metric-definitions/{code}/approve", {"mode": "standard"})
    print(f"[metric] {code} 审批通过 -> {approved.get('status')}")
    return approved


def _pii_review(api: Api, code: str, *, columns: list[str]) -> None:
    """用合规官 token 做 PII 复核（复核人必须非指标 owner）。"""
    token = login(api.base, "e2e_compliance", "changeme123")
    api.post(
        "/pii/review",
        {
            "metric_code": code,
            "decision": "APPROVE",
            "sensitivity_level": "PII",
            "pii_columns": columns,
            "masking_policy": "hash",
            "comment": "e2e 种子数据 PII 复核通过",
        },
        token=token,
    )
    print(f"[metric] {code} PII 复核通过（e2e_compliance）")


# ---------------------------------------------------------------------------
# 3. 数据源 + 目录（手动注册元数据，不连真实源库）
# ---------------------------------------------------------------------------
DATA_SOURCE = {
    "name": "HIS 门诊业务库",
    "source_type": "mysql",
    "connection_config": {
        "host": "127.0.0.1",
        "port": 3306,
        "user": "e2e",
        "password": "e2e",
        "database": "his_outpatient",
    },
    "domain": "outpatient",
}

CATALOGS: list[dict[str, Any]] = [
    {
        "entity_name": "ods_his_register",
        "entity_type": "TABLE",
        "schema_json": {
            "columns": [
                {"name": "register_id", "type": "bigint", "nullable": False, "comment": "挂号单号"},
                {"name": "patient_id", "type": "bigint", "nullable": False, "comment": "患者ID"},
                {"name": "patient_phone", "type": "string", "nullable": False, "comment": "患者手机号"},
                {"name": "doctor_id", "type": "bigint", "nullable": False, "comment": "医生ID"},
                {"name": "department_id", "type": "bigint", "nullable": False, "comment": "科室ID"},
                {"name": "register_time", "type": "datetime", "nullable": False, "comment": "挂号时间"},
                {"name": "register_fee", "type": "decimal", "nullable": False, "comment": "挂号费"},
                {"name": "delete_flag", "type": "tinyint", "nullable": False, "comment": "删除标记"},
                {"name": "dt", "type": "date", "nullable": False, "comment": "日期"},
            ]
        },
        "etl_sql": "SELECT * FROM source.ods_register",
    },
    {
        "entity_name": "ods_his_receipt",
        "entity_type": "TABLE",
        "schema_json": {
            "columns": [
                {"name": "receipt_id", "type": "bigint", "nullable": False, "comment": "收费单号"},
                {"name": "visit_id", "type": "bigint", "nullable": False, "comment": "就诊号"},
                {"name": "patient_id", "type": "bigint", "nullable": False, "comment": "患者ID"},
                {"name": "dept_id", "type": "bigint", "nullable": False, "comment": "科室ID"},
                {"name": "receipt_time", "type": "datetime", "nullable": False, "comment": "收费时间"},
                {"name": "total_fee", "type": "decimal", "nullable": False, "comment": "收费总额"},
                {"name": "drug_fee", "type": "decimal", "nullable": False, "comment": "药品费用"},
                {"name": "check_fee", "type": "decimal", "nullable": False, "comment": "检查费用"},
                {"name": "lab_fee", "type": "decimal", "nullable": False, "comment": "检验费用"},
                {"name": "delete_flag", "type": "tinyint", "nullable": False, "comment": "删除标记"},
                {"name": "dt", "type": "date", "nullable": False, "comment": "日期"},
            ]
        },
        "etl_sql": "SELECT * FROM source.ods_receipt",
    },
    {
        "entity_name": "ods_his_prescription",
        "entity_type": "TABLE",
        "schema_json": {
            "columns": [
                {"name": "prescription_id", "type": "bigint", "nullable": False, "comment": "处方单号"},
                {"name": "patient_id", "type": "bigint", "nullable": False, "comment": "患者ID"},
                {"name": "doctor_id", "type": "bigint", "nullable": False, "comment": "开方医生ID"},
                {"name": "prescription_time", "type": "datetime", "nullable": False, "comment": "开方时间"},
                {"name": "drug_id", "type": "bigint", "nullable": False, "comment": "药品ID"},
                {"name": "drug_name", "type": "string", "nullable": False, "comment": "药品名称"},
                {"name": "drug_fee", "type": "decimal", "nullable": False, "comment": "药品金额"},
                {"name": "is_antibiotic", "type": "tinyint", "nullable": False, "comment": "是否抗菌药物"},
                {"name": "dt", "type": "date", "nullable": False, "comment": "日期"},
            ]
        },
        "etl_sql": "SELECT * FROM source.ods_prescription",
    },
    {
        "entity_name": "ods_his_drug",
        "entity_type": "TABLE",
        "schema_json": {
            "columns": [
                {"name": "drug_id", "type": "bigint", "nullable": False, "comment": "药品ID"},
                {"name": "drug_name", "type": "string", "nullable": False, "comment": "药品名称"},
                {"name": "drug_code", "type": "string", "nullable": False, "comment": "药品编码"},
                {"name": "drug_type", "type": "string", "nullable": False, "comment": "药品分类"},
                {"name": "antibiotic_flag", "type": "tinyint", "nullable": False, "comment": "是否抗菌药"},
            ]
        },
    },
    {
        "entity_name": "ods_his_yb_settle",
        "entity_type": "TABLE",
        "schema_json": {
            "columns": [
                {"name": "settle_id", "type": "bigint", "nullable": False, "comment": "结算单号"},
                {"name": "receipt_id", "type": "bigint", "nullable": False, "comment": "收费单号"},
                {"name": "patient_id", "type": "bigint", "nullable": False, "comment": "患者ID"},
                {"name": "settle_time", "type": "datetime", "nullable": False, "comment": "结算时间"},
                {"name": "settle_amount", "type": "decimal", "nullable": False, "comment": "结算金额"},
                {"name": "yb_pay", "type": "decimal", "nullable": False, "comment": "医保支付"},
                {"name": "self_pay", "type": "decimal", "nullable": False, "comment": "个人自付"},
                {"name": "delete_flag", "type": "tinyint", "nullable": False, "comment": "删除标记"},
                {"name": "dt", "type": "date", "nullable": False, "comment": "日期"},
            ]
        },
    },
    {
        "entity_name": "dwd_his_receipt",
        "entity_type": "TABLE",
        "schema_json": {
            "columns": [
                {"name": "receipt_id", "type": "bigint", "nullable": False, "comment": "收费单号"},
                {"name": "visit_id", "type": "bigint", "nullable": False, "comment": "就诊号"},
                {"name": "total_fee", "type": "decimal", "nullable": False, "comment": "收费总额"},
                {"name": "drug_fee", "type": "decimal", "nullable": False, "comment": "药品费用"},
                {"name": "dept_id", "type": "bigint", "nullable": False, "comment": "科室ID"},
                {"name": "dt", "type": "date", "nullable": False, "comment": "日期"},
            ]
        },
    },
    {
        "entity_name": "ads_outp_e2e_fee_day",
        "entity_type": "TABLE",
        "schema_json": {
            "columns": [
                {"name": "avg_fee", "type": "decimal", "nullable": False, "comment": "次均门诊费用"},
                {"name": "dt", "type": "date", "nullable": False, "comment": "日期"},
            ]
        },
        # owner 留空 → 资产地图"孤儿资产"
    },
]


def ensure_datasource(api: Api) -> dict[str, Any]:
    """幂等注册数据源并手动注册目录元数据。"""
    # 找已存在的 HIS 门诊数据源（keyword 与数据源名匹配，防重复创建）
    sources = api.get("/data-sources?keyword=HIS&page_size=50")
    for s in (sources.get("items") or []):
        if s.get("name") == DATA_SOURCE["name"]:
            print(f"[datasource] 已存在 {s.get('source_id')}")
            return s
    created = api.post("/data-sources", DATA_SOURCE)
    sid = created.get("source_id")
    print(f"[datasource] 创建 {sid}")
    # 手动注册目录
    for cat in CATALOGS:
        try:
            body = dict(cat)
            body["source_id"] = sid
            resp = api.post(f"/data-sources/{sid}/catalogs", body)
            print(f"[catalog] 注册 {cat['entity_name']} id={resp.get('id')}")
        except SeedError as e:
            if "exists" in str(e).lower():
                print(f"[catalog] {cat['entity_name']} 已存在")
            else:
                raise
    return created


# ---------------------------------------------------------------------------
# 4. 血缘
# ---------------------------------------------------------------------------
LINEAGE_SQLS = [
    (
        "INSERT INTO dwd_his_receipt "
        "SELECT r.receipt_id, r.visit_id, r.total_fee, r.drug_fee, r.dept_id, r.dt "
        "FROM ods_his_receipt r WHERE r.delete_flag = 0"
    ),
    (
        "INSERT INTO ads_outp_e2e_fee_day "
        "SELECT fee / NULLIF(visit_cnt, 0) AS avg_fee, dt "
        "FROM (SELECT dt, SUM(total_fee) AS fee, COUNT(DISTINCT visit_id) AS visit_cnt "
        "      FROM dwd_his_receipt GROUP BY dt) t"
    ),
]


def seed_lineage(api: Api) -> None:
    edges = api.get("/lineage/edges?node=table%3Aods_his_receipt&page_size=50")
    if (edges.get("items") or []):
        print("[lineage] 血缘边已存在，跳过")
        return
    total = 0
    for sql in LINEAGE_SQLS:
        resp = api.post("/lineage/parse", {"sql": sql, "dialect": "mysql", "provenance": "sqlglot"})
        # 响应为计数：{table_edges: int, field_edges: int, graph_written: bool}
        table_n = int(resp.get("table_edges") or 0)
        field_n = int(resp.get("field_edges") or 0)
        total += table_n + field_n
        print(f"[lineage] parse 产出 {table_n} 表级边 + {field_n} 字段级边")
    print(f"[lineage] 共写入 {total} 条边")


# ---------------------------------------------------------------------------
# 5. 冲突
# ---------------------------------------------------------------------------
def seed_conflicts(api: Api, metric_ids: dict[str, int]) -> None:
    # 已存在 OPEN 冲突则跳过（幂等）
    existing = api.get("/conflicts?status=OPEN&page_size=50")
    if (existing.get("items") or []):
        print(f"[conflict] 已有 {len(existing['items'])} 条 OPEN 冲突，跳过")
        return
    # 软冲突：不同编码 + 相同 definition → same_def_diff_name（200 正常返回）
    cand_b = {
        "metric_code": "outp_e2e_conflictb_day",
        "domain": "medical_fee",
        "definition": CONFLICT_DEF,
        "source_tables": ["ods_his_receipt"],
        "has_pii": False,
        "pii_authorized": False,
        "metric_id": metric_ids.get("outp_e2e_conflictb_day"),
    }
    exist_a = {
        "metric_code": "outp_e2e_conflicta_day",
        "domain": "medical_fee",
        "definition": CONFLICT_DEF,
        "source_tables": ["ods_his_receipt"],
        "has_pii": False,
        "pii_authorized": False,
        "metric_id": metric_ids.get("outp_e2e_conflicta_day"),
    }
    resp = api.post("/conflicts/check", {"candidate": cand_b, "existing": [exist_a]})
    for d in resp.get("detections") or []:
        print(f"[conflict] 软冲突 {d.get('existing_code')} type={d.get('conflict_type')} "
              f"score={d.get('score'):.2f} block={d.get('block_publish')}")
    # 硬冲突：同编码 + 不同 definition → same_name_diff_def（409 但落库）
    try:
        api.post(
            "/conflicts/check",
            {
                "candidate": {
                    "metric_code": "outp_e2e_conflicta_day",
                    "domain": "medical_fee",
                    "definition": CONFLICT_DEF,
                    "source_tables": ["ods_his_receipt"],
                    "has_pii": False,
                    "pii_authorized": False,
                    "metric_id": metric_ids.get("outp_e2e_conflicta_day"),
                },
                "existing": [
                    {
                        "metric_code": "outp_e2e_conflicta_day",
                        "domain": "medical_insurance",
                        "definition": (
                            "SELECT SUM(settle_amount) FROM ods_his_yb_settle "
                            "GROUP BY settle_time"
                        ),
                        "source_tables": ["ods_his_yb_settle"],
                        "has_pii": False,
                        "pii_authorized": False,
                    }
                ],
            },
            ok_status=(200, 409),
        )
        print("[conflict] 硬冲突 check 返回（200/409 均已落库）")
    except SeedError as e:
        print(f"[conflict] 硬冲突：{e}")


# ---------------------------------------------------------------------------
# 6. 质量中心
# ---------------------------------------------------------------------------
def seed_quality(api: Api, metric_ids: dict[str, int]) -> None:
    fee_id = metric_ids["outp_e2e_fee_day"]
    rules = api.get(f"/quality/rules?metric_id={fee_id}&page_size=50")
    rule = None
    for r in (rules.get("items") or []):
        if r.get("rule_type") == "COMPLETENESS":
            rule = r
            break
    if rule is None:
        rule = api.post(
            "/quality/rules",
            {
                "metric_id": fee_id,
                "rule_type": "COMPLETENESS",
                "threshold": {"op": ">=", "value": 90},
                "rule_mode": "static",
                "severity": "P1",
                "enabled": True,
            },
        )
        print(f"[quality] 创建规则 id={rule.get('id')}")
    else:
        print(f"[quality] 规则已存在 id={rule.get('id')}")

    # 观测样本（供动态基线/统计用）
    api.post(
        "/quality/observe",
        {
            "metric_id": fee_id,
            "metric_code": "outp_e2e_fee_day",
            "value": 95.5,
            "obs_time": datetime.now().isoformat(),
        },
    )
    # 触发事件（obs_value 越界 → OPEN 事件，幂等：同键 OPEN 不重复）
    try:
        detect = api.post(
            "/quality/events/detect",
            {"metric_id": fee_id, "rule_type": "COMPLETENESS", "obs_value": 50.0},
        )
        if detect:
            print(
                f"[quality] 触发事件 {detect.get('id') or detect.get('event_id')} "
                f"status={detect.get('status')}"
            )
        else:
            print("[quality] detect 未命中（已有 OPEN 事件）")
    except SeedError as e:
        print(f"[quality] detect: {e}")

    # 事件闭环：OPEN -> ACK -> RESOLVED -> CLOSED（若有 OPEN 事件）
    events = api.get("/quality/events?status=OPEN&page_size=50")
    ev = (events.get("items") or [None])[0]
    if ev:
        eid = ev.get("id")
        api.post(f"/quality/events/{eid}/ack", {"note": "e2e 已确认"})
        api.post(f"/quality/events/{eid}/resolve")
        api.post(f"/quality/events/{eid}/close")
        print(f"[quality] 事件 {eid} 闭环 ACK->RESOLVED->CLOSED")

    # 基准 + 对账
    benchs = api.get("/quality/benchmarks?metric_code=outp_e2e_fee_day&page_size=50")
    bench = (benchs.get("items") or [None])[0]
    if bench is None:
        bench = api.post(
            "/quality/benchmarks/import",
            {
                "source_id": "his_bench_2026",
                "metric_code": "outp_e2e_fee_day",
                "bench_date": date.today().isoformat(),
                "bench_value": 1000.0,
                "provider": "e2e_seed",
                "tolerance_pct": 5,
            },
        )
        print(f"[quality] 导入基准 id={bench.get('id')}")
    else:
        print(f"[quality] 基准已存在 id={bench.get('id')}")

    records = api.get("/quality/reconciliation-records?page_size=50")
    rec = (records.get("items") or [None])[0]
    if rec is None and bench:
        rec = api.post(
            "/quality/reconciliation/run",
            {"benchmark_id": bench.get("id"), "metric_value": 950.0},
        )
        print(f"[quality] 对账 {rec.get('id')} status={rec.get('status')}")
        api.post(
            f"/quality/reconciliation-records/{rec.get('id')}/confirm",
            {"decision": "reasonable", "owner_note": "e2e 对账合理"},
        )
        print(f"[quality] 对账确认 {rec.get('id')}")


# ---------------------------------------------------------------------------
# 7. 术语表
# ---------------------------------------------------------------------------
def seed_glossary(api: Api) -> None:
    terms = api.get("/terms?page_size=50")
    existing = {t.get("name") for t in (terms.get("items") or [])}
    specs = [
        {"name": "E2E术语-门诊挂号人次", "definition": "门诊挂号记录数，衡量门诊接诊规模。",
         "domain": "outpatient", "synonyms": ["挂号量", "门诊量"]},
        {"name": "E2E术语-门诊挂号人次", "definition": "与上一条口径不同但名称相同，用于触发同名冲突。",
         "domain": "outpatient", "synonyms": ["挂号量", "门诊量"]},
        {"name": "E2E术语-门诊收费金额", "definition": "一定周期内门诊收费总额，衡量门诊收入规模。",
         "domain": "medical_fee", "synonyms": ["门诊收入", "门诊费用"]},
    ]
    for s in specs:
        if s["name"] in existing:
            print(f"[glossary] 术语已存在 {s['name']}")
            continue
        t = api.post("/terms", s)
        print(
            f"[glossary] 创建术语 {t.get('name')} code={t.get('term_code')} "
            f"status={t.get('status')}"
        )
        # 发布一个正常术语（供推荐/搜索）——admin 直发通道（submit 现走审核流）
        if s["name"].endswith("收费金额"):
            api.post(f"/terms/{t.get('term_code')}/publish")
            print(f"[glossary] 发布术语 {t.get('name')}")


# ---------------------------------------------------------------------------
# 8. 维度
# ---------------------------------------------------------------------------
def seed_dimensions(api: Api, metric_ids: dict[str, int]) -> None:
    # 按名称查找（code 为自动生成的域_名称_slug，不硬编码）
    dims = api.get("/dimensions?keyword=%E7%A7%91%E5%AE%A4&page_size=50")
    dim = None
    for d in (dims.get("items") or []):
        if d.get("name") == "E2E科室维度":
            dim = d
            break
    if dim is None:
        dim = api.post("/dimensions", {"name": "E2E科室维度", "domain": "outpatient", "type": "SCD1"})
        print(f"[dimension] 创建维度 {dim.get('dim_code')}")
    else:
        print(f"[dimension] 维度已存在 {dim.get('dim_code')}")
    dim_code = dim.get("dim_code")
    # 确保 3 个成员（缺则补）
    members = api.get(f"/dimensions/{dim_code}/members")
    existing_members = {m.get("member_name") for m in (members.get("items") or [])}
    for name in ["内科", "外科", "儿科"]:
        if name in existing_members:
            continue
        api.post(
            f"/dimensions/{dim_code}/members",
            {"dim_code": dim_code, "member_name": name},
        )
    print(f"[dimension] 确保成员 内科/外科/儿科 (已有 {len(existing_members)})")
    # 绑定指标（role 枚举: PARTITION/SPLICE/FILTER）——幂等：先查已绑定
    metric_id = metric_ids["outp_e2e_fee_day"]
    bound = api.get(f"/dimensions/{metric_id}/metric-dimensions") or {}
    already = any(b.get("dim_code") == dim_code for b in (bound.get("items") or []))
    if already:
        print("[dimension] 已绑定指标 outp_e2e_fee_day，跳过")
    else:
        api.post(
            f"/dimensions/{dim_code}/metrics",
            {"metric_id": metric_id, "dim_code": dim_code, "role": "FILTER"},
        )
        print("[dimension] 绑定指标 outp_e2e_fee_day")


# ---------------------------------------------------------------------------
# 9. 治理：授权 / PII 复核 / 被遗忘权
# ---------------------------------------------------------------------------
def seed_governance(api: Api, user_ids: dict[str, int], metric_ids: dict[str, int]) -> None:
    # 授权：给 e2e_analyst 授 outpatient 域读权限 + outp_e2e_fee_day 白名单
    grants = api.get("/grants?user_id={}&page_size=50".format(user_ids["e2e_analyst"]))
    if not (grants.get("items") or []):
        api.post(
            "/grants",
            {
                "user_id": user_ids["e2e_analyst"],
                "domain": "outpatient",
                "metric_whitelist": ["outp_e2e_fee_day", "outp_e2e_visit_day"],
                "grant_type": "READ",
                "reason": "e2e 种子数据授权",
            },
        )
        print("[governance] 授予 e2e_analyst outpatient 域 READ")
    else:
        print("[governance] 授权已存在")

    # 权限校验（PDP）响应字段为 allow
    check = api.post(
        "/permissions/check",
        {
            "user_id": user_ids["e2e_analyst"],
            "action": "read",
            "domain": "outpatient",
            "metric_code": "outp_e2e_fee_day",
        },
    )
    print(f"[governance] PDP check e2e_analyst read outp_e2e_fee_day -> {check.get('allow')}")

    # 被遗忘权：先让 subject 用户产生审计行，再匿名化
    analyst_token = login(api.base, "e2e_analyst", "changeme123")
    with contextlib.suppress(SeedError):
        api.get("/terms", token=analyst_token)  # 产生审计读操作
    compliance_token = login(api.base, "e2e_compliance", "changeme123")
    erasure = api.post(
        "/erasure",
        {"subject_user_id": user_ids["e2e_analyst"], "reason": "e2e 数据主体删除请求"},
        token=compliance_token,
    )
    print(f"[governance] erasure affected_rows={erasure.get('affected_rows', 0)}")


# ---------------------------------------------------------------------------
# 10. 通知
# ---------------------------------------------------------------------------
def seed_notify(api: Api) -> None:
    # 建立订阅（幂等 upsert）
    subs = api.get("/notify/subscriptions")
    if not (subs.get("items") or []):
        for event_type in [
            "quality.anomaly",
            "conflict_open",
            "quality.alert",
            "reconciliation.alert",
        ]:
            api.put(
                "/notify/subscriptions",
                {"channel": "IN_APP", "event_type": event_type, "enabled": True},
            )
        print("[notify] 建立 4 类订阅")
    # 若尚无 notification（订阅在事件发布后建立时需补发），则手动发布事件扇出
    notifs = api.get("/notify/notifications?page_size=5")
    if not (notifs.get("items") or []):
        for event_type, payload in [
            ("quality.anomaly", {"metric_code": "outp_e2e_fee_day", "message": "完整率低于阈值"}),
            (
                "conflict_open",
                {"metric_code": "outp_e2e_conflicta_day", "message": "检测到口径冲突"},
            ),
        ]:
            resp = api.post(
                "/notify/events",
                {
                    "event_type": event_type,
                    "source": "quality",
                    "payload": payload,
                    "level": "WARN",
                },
            )
            print(
                f"[notify] 发布 {event_type} -> "
                f"notifications={len(resp.get('notifications') or [])}"
            )
    else:
        print("[notify] notification 已存在，跳过")


# ---------------------------------------------------------------------------
# 11. 可观测
# ---------------------------------------------------------------------------
def seed_observability(api: Api) -> None:
    feedbacks = api.get("/observability/feedback?limit=50")
    fb = None
    for f in (feedbacks.get("items") or []):
        if f.get("target_id") == "outp_e2e_fee_day":
            fb = f
            break
    if fb is None:
        fb = api.post(
            "/observability/feedback",
            {
                "target_type": "metric",
                "target_id": "outp_e2e_fee_day",
                "rating": 5,
                "comment": "e2e 反馈：门诊收费金额口径清晰",
            },
        )
        print(f"[observability] 提交反馈 id={fb.get('id') or fb.get('feedback_id')}")
    nps = api.post("/observability/nps", {"score": 9, "comment": "e2e NPS 反馈"})
    print(f"[observability] NPS id={nps.get('id') or nps.get('nps_id')}")


# ---------------------------------------------------------------------------
# 12. 消费：API 客户端 / 收藏 / dry-run
# ---------------------------------------------------------------------------
def seed_consume(api: Api) -> None:
    # GET 返回裸 list[ClientResponse]
    try:
        clients = api.get("/consume/api-clients")
    except SeedError:
        clients = []
    if not clients:
        created = api.post(
            "/consume/api-clients",
            {
                "client_id": "e2e_app",
                "secret": "e2e_secret_2026",
                "scope_domain": "outpatient",
                "metric_whitelist": ["outp_e2e_fee_day", "outp_e2e_visit_day"],
            },
        )
        print(f"[consume] 创建 API 客户端 {created.get('client_id')}")
    else:
        print("[consume] API 客户端已存在")
    # 收藏
    try:
        api.post("/consume/me/favorites", {"metric_code": "outp_e2e_fee_day"})
        print("[consume] 收藏 outp_e2e_fee_day")
    except SeedError as e:
        print(f"[consume] 收藏跳过：{e}")


def _path_exists(api: Api, path: str) -> bool:
    try:
        api.get(path)
        return True
    except SeedError:
        return False


# ---------------------------------------------------------------------------
# 13. 行为埋点（推荐协同过滤底座）
# ---------------------------------------------------------------------------
def seed_tracking(api: Api, metric_ids: dict[str, int]) -> None:
    for event_type in ["favorite", "browse", "search"]:
        try:
            api.post(
                "/tracking/event",
                {
                    "event_type": event_type,
                    "target_type": "metric",
                    "target_id": "outp_e2e_fee_day",
                    "context": {"source": "e2e_seed"},
                },
            )
        except SeedError as e:
            print(f"[tracking] {event_type} 跳过：{e}")
    print("[tracking] 埋点 favorite/browse/search")


# ---------------------------------------------------------------------------
# 冒烟验证
# ---------------------------------------------------------------------------
SMOKE_CHECKS = [
    ("逻辑度量", lambda a: _count(a.get("/measure-catalogs?page_size=100"), "items") >= 4),
    ("指标目录", lambda a: _count(a.get("/metric-definitions?page_size=100"), "items") >= 8),
    ("已发布指标", lambda a: _count(
        a.get("/metric-definitions?status=PUBLISHED&page_size=100"), "items"
    ) >= 5),
    ("数据源", lambda a: _count(a.get("/data-sources?page_size=50"), "items") >= 1),
    ("目录表", lambda a: _count(a.get("/catalogs?entity_type=TABLE&page_size=100"), "items") >= 4),
    ("血缘边", lambda a: _count(
        a.get("/lineage/edges?node=table%3Aods_his_receipt&page_size=100"), "items"
    ) >= 1),
    ("冲突", lambda a: _count(a.get("/conflicts?page_size=100"), "items") >= 1),
    ("质量规则", lambda a: _count(a.get("/quality/rules?page_size=100"), "items") >= 1),
    ("质量事件", lambda a: _count(a.get("/quality/events?page_size=100"), "items") >= 1),
    ("基准", lambda a: _count(
        a.get("/quality/benchmarks?metric_code=outp_e2e_fee_day&page_size=100"), "items"
    ) >= 1),
    ("对账记录", lambda a: _count(
        a.get("/quality/reconciliation-records?page_size=100"), "items"
    ) >= 1),
    ("术语", lambda a: _count(a.get("/terms?page_size=100"), "items") >= 3),
    ("术语冲突", lambda a: _count(a.get("/terms/conflicts?status=OPEN"), "items") >= 1),
    ("维度", lambda a: 1 if _first_dim_code(a) else 0),
    ("维度成员", lambda a: _count_dim_members(a) >= 3),
    ("授权", lambda a: _count(a.get("/grants?page_size=100"), "items") >= 1),
    ("通知", lambda a: _count(a.get("/notify/notifications?page_size=100"), "items") >= 1),
    ("反馈", lambda a: _count(a.get("/observability/feedback?limit=50"), "items") >= 1),
    ("收藏", lambda a: len(a.get("/consume/me/favorites") or []) >= 1),
    ("搜索命中", lambda a: _nonzero(a.get("/search?q=outp_e2e_fee_day&limit=5"), "total")),
]


def _count(data: dict[str, Any], key: str) -> int:
    return len(data.get(key) or [])


def _first_dim_code(api: Api) -> str | None:
    """按名称定位 E2E 科室维度 code（code 为自动生成的域_名称_slug，不硬编码）。"""
    dims = api.get("/dimensions?keyword=%E7%A7%91%E5%AE%A4&page_size=50")
    for d in (dims.get("items") or []):
        if d.get("name") == "E2E科室维度":
            return d.get("dim_code")
    return None


def _count_dim_members(api: Api) -> int:
    code = _first_dim_code(api)
    if not code:
        return 0
    return _count(api.get(f"/dimensions/{code}/members"), "items")


def _nonzero(data: dict[str, Any], key: str) -> int:
    return 1 if (data.get(key) or 0) else 0


def smoke_verify(api: Api) -> tuple[int, int]:
    print("\n===== 冒烟验证 =====")
    passed = failed = 0
    for name, fn in SMOKE_CHECKS:
        try:
            if fn(api):
                passed += 1
                print(f"  [PASS] {name}")
            else:
                failed += 1
                print(f"  [FAIL] {name}")
        except SeedError as e:
            failed += 1
            print(f"  [FAIL] {name}: {e}")
    return passed, failed


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def login(base: str, username: str, password: str) -> str:
    api = Api(base)
    data = api.post("/auth/login", {"username": username, "password": password})
    return data.get("access_token", "")


def main() -> int:
    parser = argparse.ArgumentParser(description="E2E 种子数据脚本")
    parser.add_argument("--base", default="http://localhost:8100", help="后端地址")
    parser.add_argument("--admin-user", default=ADMIN["username"])
    parser.add_argument("--admin-pass", default=ADMIN["password"])
    parser.add_argument("--skip-users", action="store_true", help="跳过基础用户创建")
    args = parser.parse_args()

    if not args.skip_users:
        print("===== 1. 基础用户 =====")
        user_ids = ensure_users()
        print(f"[users] 就绪: {user_ids}")
    else:
        # 跳过创建但仍取回已存在用户的 ID（供后续授权/埋点/被遗忘权使用）
        print("===== 1. 基础用户（跳过创建，查询已存在 ID）=====")
        user_ids = _fetch_user_ids()
        print(f"[users] 查询: {user_ids}")

    print("\n===== 2. 登录 =====")
    token = login(args.base, args.admin_user, args.admin_pass)
    if not token:
        raise SeedError(f"登录失败 {args.admin_user}")
    print(f"[auth] 登录成功 {args.admin_user}")
    api = Api(args.base, token)

    print("\n===== 3. 逻辑度量（OneData 原子层）=====")
    measure_ids: dict[str, int] = {}
    for spec in MEASURES:
        m = ensure_measure(api, spec)
        measure_ids[spec["code"]] = m.get("id") or 0
    print(f"[measure] 就绪: {measure_ids}")

    print("\n===== 4. 指标生命周期 =====")
    metric_ids: dict[str, int] = {}
    for spec in ALL_METRICS:
        m = ensure_metric(api, spec, measure_ids=measure_ids)
        metric_ids[spec["code"]] = m.get("id") or 0
    # OneData 存量订正：旧式原子指标（measure_id 为空）补关联逻辑度量（幂等）
    _backfill_legacy_metric_measures(measure_ids)
    # 发布：outp_e2e_register_day / outp_e2e_visit_day / outp_e2e_fee_day /
    #        outp_e2e_drugfee_day / outp_e2e_prescription_day / outp_e2e_piipatient_day /
    #        outp_e2e_deprecated_day
    publish_metric(api, "outp_e2e_register_day")
    publish_metric(api, "outp_e2e_visit_day")
    publish_metric(api, "outp_e2e_fee_day")
    publish_metric(api, "outp_e2e_drugfee_day")
    publish_metric(api, "outp_e2e_prescription_day")
    publish_metric(api, "outp_e2e_piipatient_day", pii_columns=["patient_phone"])
    publish_metric(api, "yb_e2e_settle_day")
    publish_metric(api, "outp_e2e_deprecated_day")
    # 废弃 outp_e2e_deprecated_day（successor=outp_e2e_fee_day 已发布）→ 造 DEPRECATED 状态
    try:
        dep = api.post(
            "/metric-definitions/outp_e2e_deprecated_day/deprecate",
            {"successor_code": "outp_e2e_fee_day"},
        )
        print(f"[metric] outp_e2e_deprecated_day 废弃 -> {dep.get('status')}")
    except SeedError as e:
        if "deprecat" in str(e).lower():
            print(f"[metric] outp_e2e_deprecated_day 废弃跳过：{e}")
        else:
            raise
    # 派生指标依赖已发布指标
    publish_metric(api, "outp_e2e_avgfee_day")
    # 冲突对保持 DRAFT（用于冲突列表来源），不发布

    print("\n===== 5. 数据源 + 目录 =====")
    ensure_datasource(api)

    print("\n===== 6. 血缘 =====")
    seed_lineage(api)

    print("\n===== 7. 冲突 =====")
    seed_conflicts(api, metric_ids)

    print("\n===== 8. 质量中心 =====")
    seed_quality(api, metric_ids)

    print("\n===== 9. 术语表 =====")
    seed_glossary(api)

    print("\n===== 10. 维度 =====")
    seed_dimensions(api, metric_ids)

    print("\n===== 11. 治理 =====")
    seed_governance(api, user_ids, metric_ids)

    print("\n===== 12. 通知 =====")
    seed_notify(api)

    print("\n===== 13. 可观测 =====")
    seed_observability(api)

    print("\n===== 14. 消费 =====")
    seed_consume(api)

    print("\n===== 15. 行为埋点 =====")
    seed_tracking(api, metric_ids)

    passed, failed = smoke_verify(api)
    print(f"\n===== 冒烟结果: {passed} passed / {failed} failed =====")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
