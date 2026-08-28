# Unisense 对外 API 集成指南

> 面向：**外部 Agent 智能体 / 数仓开发 / 集成方**——将企业存量指标批量录入 Unisense 平台。
> 配套：Swagger UI（`http://<host>:8180/docs`，登录后点 Authorize 填 token 即可在线调试全部接口）。

---

## 1. 概述

Unisense 是指标管理平台（OneData 建模体系）。本指南聚焦**存量指标批量录入**这一高频集成场景：
外部 Agent 批量解析 SQL/元数据后，通过平台 API 自动创建指标（DRAFT 态），随后走平台内审批流程发布。

### 1.1 指标生命周期（集成方须知）

```
DRAFT(草稿) → 提交审核(REVIEW) → 通过(PUBLISHED) → 下线(DEPRECATED)
```

- 批量导入创建的指标均为 **DRAFT 草稿**（不直接发布），须经平台审批后生效。
- 支持类型：`atomic`（原子指标）、`derived`（派生指标）、`composite`（复合指标，多指标运算）。

### 1.2 指标编码（4 段式，平台规范）

```
{域}_{业务对象}_{度量}_{周期}
示例：outp_doctor_active_cnt_month
      └域┘ └─业务对象─┘ └─度量─┘ └周期┘
```

- **域**：平台主题域编码（必须已存在，可在系统内「主题域管理」维护）。
- **业务对象**：表名末段（去通用后缀，如 `_da/_di/_df`）。
- **度量**：度量列名。
- **周期**：`day/week/month/quarter/year/hour`。

> 集成方可自行生成编码；**也可缺省**——调用批量导入接口时 `metric_code` 留空，平台自动按上述规则补全。

---

## 2. 鉴权

所有接口（除登录外）需在请求头携带 Bearer Token：

```http
POST /api/v1/auth/login
Content-Type: application/json

{ "username": "<账号>", "password": "<密码>" }
```

响应：

```json
{ "code": "OK", "data": { "access_token": "<token>", ... } }
```

之后每个请求：

```http
Authorization: Bearer <token>
X-Api-Key: <平台网关 Key>
Content-Type: application/json
```

> `X-Api-Key` 缺省为 `dev-semantic-key`（平台统一网关鉴权）。

### 2.1 权限要求

批量录入接口要求登录用户具备以下任一角色：
`platform_admin`（平台管理员）/ `domain_admin`（域管理员）/ `metric_owner`（指标 Owner）。

> 域管理员 / 指标 Owner 仅能录入**本域**指标（跨域返回 403 `FORBIDDEN`）。

---

## 3. 批量录入接口（推荐：`batch-import`）

通用批量导入端点，接受**纯结构化候选清单**（编码/名称可缺省）：

```http
POST /api/v1/metric-definitions/batch-import
```

### 3.1 请求体

```json
{
  "domain": "outp",
  "source": "agent",
  "candidates": [
    {
      "metric_code": "outp_doctor_active_cnt_month",
      "name": "月活医生数",
      "type": "atomic",
      "source_table": "wedw_dws.doctor_active_month_di",
      "measure_column": "current_month_active_doctor_cnt",
      "aggregation": "COUNT_DISTINCT",
      "unit": "人",
      "period": "month",
      "granularity": "month",
      "expression": "COUNT(DISTINCT doctor_code)",
      "raw_sql": "-- 原始 SQL 原文（溯源，可选）"
    },
    {
      "name": "上月活跃医生数",
      "type": "atomic",
      "source_table": "wedw_dws.doctor_active_month_di",
      "measure_column": "last_month_active_doctor_cnt",
      "aggregation": "COUNT_DISTINCT",
      "unit": "人",
      "period": "month",
      "expression": "COALESCE(COUNT(DISTINCT CASE WHEN NOT last_month_last_visit_date IS NULL THEN doctor_code END), 0)"
    }
  ]
}
```

### 3.2 响应

```json
{
  "code": "OK",
  "data": {
    "batch_id": "sqlbatch_ab12cd34ef56",
    "candidates": [
      { "metric_code": "outp_doctor_active_cnt_month", "status": "DRAFT" },
      { "metric_code": "outp_lastmonth_doctor_active_cnt_month", "status": "VALIDATION_ERROR", "validation_errors": ["..."] }
    ]
  }
}
```

- `status=DRAFT`：创建成功（草稿）。
- `status=VALIDATION_ERROR`：该条失败（`validation_errors` 给出原因），**不影响其余候选**（逐条 savepoint 隔离）。
- `batch_id`：本次批次的追溯 ID（列表页可按批次筛选，审计可反查）。

### 3.3 候选字段说明

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `metric_code` | 否* | string(≤64) | 4 段式编码；缺省自动生成（见 §1.2） |
| `name` | 否* | string(≤128) | 中文名称；缺省按度量列自动生成（如 `cnt→数`） |
| `type` | 否 | `atomic`/`derived`/`composite` | 缺省 `atomic` |
| `source_table` | 否 | string(≤256) | 源表名（`库.表`）；复合可空 |
| `measure_column` | 否 | string(≤128) | 度量列；复合为空 |
| `aggregation` | 否 | 枚举 | `SUM/AVG/COUNT/COUNT_DISTINCT/LAST_VALUE/FIRST_VALUE/MAX/MIN/MEDIAN/PERCENTILE` |
| `unit` | 否 | string(≤32) | 单位 |
| `period` | 否 | string(≤16) | 统计周期（`day/week/month/...`），缺省 `day` |
| `granularity` | 否 | string(≤64) | 粒度 |
| `measure_id` | 否 | int | 关联逻辑度量 ID（原子指标口径库）；可选 |
| `expression` | 是* | string | 计算表达式（口径）。**原子/派生必填**；复合可填也可用 `dependencies` 组合 |
| `dependencies` | 条件 | string[] | 依赖指标编码（**复合必填**） |
| `raw_sql` | 否 | string | 原始 SQL 原文（溯源，落 `raw_sql` 字段，批内反查） |

> \* `metric_code`/`name`/`expression` 至少应提供其一 —— 三者全空该条会被创建端校验拒绝。
> `definition_json` 由平台从 `expression` + `dependencies` 自动组装，**无需集成方构造**。

---

## 4. 补充端点

### 4.1 从 SQL 解析候选批量注册（`batch-register-from-sql`）

适合**集成方已完成 SQL 解析**、产出与平台解析候选同构的数据：

```http
POST /api/v1/metric-definitions/batch-register-from-sql
```

请求体为 `{ "domain": "<域>", "candidates": [SqlBatchCreateCandidate...] }`。
`SqlBatchCreateCandidate` 在 `batch-import` 候选字段基础上增加：`key`（稳定标识）、`definition_json`（口径定义，需自行构造 `{"expression": ..., "dependencies": [...]}`）、`mount`（挂载实体）、责任方三字段。

> 大多数场景推荐用 `batch-import`（字段更少、自动补全更多）。`batch-register-from-sql` 用于前端已勾选微调的候选回传。

### 4.2 CSV 文件导入（`imports/csv`）

适合人工填表或 Agent 产出 CSV：

```http
POST /api/v1/metric-definitions/imports/csv
Content-Type: multipart/form-data

file:   <CSV 文件（UTF-8，表头见模板）>
domain: <目标域>
```

- 模板下载：`GET /api/v1/metric-definitions/imports/template`（需登录 + 写权限）。
- 模板列：`metric_code,name,type,source_table,measure_column,aggregation,unit,period,granularity,measure_id,expression,dependencies,raw_sql`
- `dependencies` 列用 `|` 分隔；`expression` 必填；解析失败的行记入响应 `row_errors`（不阻断其余行）。

### 4.3 单条创建

```http
POST /api/v1/metric-definitions
```

对应 `MetricCreateRequest`（字段最全，含 OneData 原子层/挂载层/责任方），适合逐条录入或 Agent 精确控制每条字段。

---

## 5. 集成示例（Agent 批量解析 SQL → 自动录入）

以一段建表注释驱动的 Hive SQL 为例：

```sql
create table if not exists wedw_dws.doctor_active_month_di(
  month_id string comment "统计月,时间格式yyyy-MM",
  hosp_code string comment "医院编码",
  current_month_active_doctor_cnt int comment "月活",
  ...
);
insert overwrite table wedw_dws.doctor_active_month_di
select a.month_id, a.hosp_code, ..., a.current_month_active_doctor_cnt, ...
from (...) a left join (...) b on a.hosp_code = b.rel_code;
```

Agent 解析后应提取（**关键：优先用建表 DDL 的列注释作为指标名称**）：

| 解析项 | 提取来源 | 示例 |
|---|---|---|
| `name` | **建表列注释** `comment "月活"` | `月活` |
| `measure_column` | INSERT SELECT 投影列 | `current_month_active_doctor_cnt` |
| `aggregation` | 聚合函数 | `COUNT_DISTINCT` |
| `source_table` | 内层 FROM | `wedw_dw.doctor_visit_agent_info_da` |
| `expression` | 投影表达式 | `COUNT(DISTINCT doctor_code)` |
| `period` | 时间截断 `substr(create_date,1,7)` | `month` |

> 建表注释是**最准确的名称来源**——平台 SQL 智能推断本身也优先消费它；集成方同样应如此。

---

## 6. 错误码速查

| HTTP | `code` | 含义 | 处理 |
|---|---|---|---|
| 401 | — | 未登录 / token 过期 | 重新登录换 token |
| 403 | `FORBIDDEN` | 无批量录入权限 或 跨域写入 | 检查角色与 `domain` 归属 |
| 400 | `CONFLICT_EXISTS` | 候选与现有指标口径冲突 | 检查 `conflict` 模块仲裁或换编码 |
| 400 | `METRIC_CODE_CONFLICT` | 编码已存在 | 换编码或先处置冲突 |
| 400 | `DICT_VALUE_*` | 字典值非法/停用（如单位、聚合） | 对照枚举/字典修正 |
| 400 | `LLM_INFER_UNAVAILABLE` | LLM 不可用（仅涉及 AI 推断端点） | 重试或稍后 |
| 422 | — | 请求体校验失败（字段缺失/类型错） | 对照 §3.3 字段说明 |

---

## 7. 建议对接顺序

1. 用 `POST /auth/login` 获取 token；
2. 用 `GET /api/v1/metric-definitions/imports/template` 下载模板（或直接构造 JSON）；
3. 小批量（1~3 条）调 `batch-import` 验证字段映射；
4. 全量接入，按响应 `candidates[].status` 逐条对账（DRAFT 即成功）；
5. 平台侧在「指标目录」按批次 `batch_id` 筛选核对，走审批发布。
