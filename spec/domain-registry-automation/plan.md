# Implementation Plan: 主题域管理与指标注册全字段自动化

**Input**: Feature specification from `spec/domain-registry-automation/spec.md`

## Summary

新增主题域（SubjectDomain）管理模块和系统字典（SystemDict）管理模块，将指标注册流程从"15+字段全手工输入"重构为"选域→自动推断→确认/覆盖"。主题域采用树形3层体系（根域>子域>孙域），每个域可配置默认值预设；系统字典统一管理粒度/单位/聚合等全部枚举字段的可选值，杜绝自由输入。指标编码由系统半自动生成（域+表+列→4段式），用户可覆盖。前端注册页改为级联域选择+字典下拉。

## Technical Context

**Language/Version**: Python 3.11 / TypeScript (React 18)
**Primary Dependencies**: FastAPI + SQLAlchemy 2.0 + Pydantic v2 / React 18 + Ant Design 5 + Zustand
**Storage**: MySQL 8 (新表 subject_domain / system_dict), Alembic 迁移
**Testing**: pytest (unit/integration), Vitest (frontend)
**Target Platform**: Docker (docker-compose)
**Project Type**: Web service (后端 API + 前端 SPA)
**Performance Goals**: 指标注册接口 ≤200ms p95, 域树查询 ≤100ms p95
**Constraints**: 存量指标 domain 字段需兼容迁移；Metric.domain 字段暂不改为 FK（应用层校验）
**Scale/Scope**: 约 6-10 个主题域根节点、30+ 字典项、存量 8+ 指标迁移

## Project Structure

### Documentation (this feature)

```text
spec/domain-registry-automation/
├── spec.md              # 需求规格
├── plan.md              # 本文件
└── tasks.md             # 任务清单（Phase 3 生成）
```

### Source Code (repository root)

```text
backend/
├── alembic/versions/
│   └── 0026_subject_domain_system_dict.py     # 新表迁移
├── app/
│   ├── models/
│   │   ├── subject_domain.py                  # 主题域 ORM 模型
│   │   ├── system_dict.py                     # 系统字典 ORM 模型
│   │   └── enums.py                           # 新增 DomainStatusEnum / DictStatusEnum
│   ├── services/
│   │   ├── subject_domain/                    # 主题域服务层
│   │   │   ├── __init__.py
│   │   │   ├── repository.py                  # 仓储层
│   │   │   ├── schemas.py                     # Pydantic Schema
│   │   │   └── service.py                     # 业务逻辑
│   │   ├── system_dict/                       # 系统字典服务层
│   │   │   ├── __init__.py
│   │   │   ├── repository.py
│   │   │   ├── schemas.py
│   │   │   └── service.py
│   │   └── semantic/                          # 修改：注册流程适配字典校验+自动推断
│   │       ├── service.py                     # create_metric 增加字典校验+自动推断
│   │       ├── schemas.py                     # MetricCreateRequest domain 改为校验域存在
│   │       └── auto_fill.py                   # 新增：自动推断引擎
│   ├── api/
│   │   ├── subject_domain.py                  # 主题域 API 端点
│   │   ├── system_dict.py                     # 系统字典 API 端点
│   │   └── metrics.py                         # 修改：新增 auto-suggest 端点
│   └── core/
│       └── config.py                          # 修改：新增 seed 相关配置
├── scripts/
│   └── seed_domains_dicts.py                  # 初始化 seed 脚本
└── tests/
    ├── unit/
    │   ├── test_subject_domain_service.py
    │   ├── test_system_dict_service.py
    │   └── test_auto_fill.py
    └── integration/
        └── test_subject_domain_integration.py

frontend/
├── src/
│   ├── pages/
│   │   ├── SubjectDomain.tsx                  # 主题域管理页
│   │   ├── SystemDict.tsx                     # 字典管理页
│   │   └── MetricCreate.tsx                   # 重构：级联域选择+字典下拉+自动推断
│   ├── api.ts                                 # 修改：新增域/字典/auto-suggest API
│   ├── types.ts                               # 修改：新增类型定义
│   └── components/
│       └── Layout.tsx                         # 修改：新增导航入口
```

**Structure Decision**: 遵循现有项目架构（FastAPI 服务层模式 + React SPA），新建 subject_domain / system_dict 两个服务模块，修改 semantic 模块适配字典校验。前端新增2个管理页面+重构1个注册页面。

## Complexity Tracking

无宪法冲突需记录。

## Research & Decisions

### D1: Metric.domain 字段关联方式

- **Decision**: 应用层校验（非 FK 约束）
- **Rationale**: 存量 Metric 表 domain 为 String(64) 自由文本，加 FK 需 ALTER + 数据迁移，风险高。应用层在 create_metric / update_metric 时校验 domain 值必须存在于 SubjectDomain 中（active 状态），兼顾安全性与向后兼容。
- **Alternatives considered**: ①加FK约束（需停机迁移，风险高）；②不加校验（无法杜绝自由输入，不符合需求）

### D2: 字典值校验策略

- **Decision**: Schema 层 validator + Service 层双重校验
- **Rationale**: Pydantic Schema 的 field_validator 在请求入参时校验值在字典中存在；Service 层在业务逻辑中再次确认（防御绕过 Schema 的内部调用）。两层校验确保数据质量。
- **Alternatives considered**: ①仅 Schema 层校验（内部调用绕过）；②仅 Service 层校验（错误信息不够友好）

### D3: 自动推断引擎设计

- **Decision**: 独立 auto_fill.py 模块，纯函数式，输入（域code+源表名+度量列+域预设），输出建议值 dict
- **Rationale**: 自动推断逻辑需覆盖：①指标编码4段拼接 ②域默认值带入 ③数据源表结构推断（dw_layer/类型）。纯函数式便于单元测试，不依赖 DB session。
- **Alternatives considered**: ①嵌入 service.py（职责过重）；②前端纯推断（不可信，后端需兜底）

### D4: 主题域层级实现

- **Decision**: 邻接表模型（parent_id 自引用）+ 物化路径（path 字段冗余存储），限制3层
- **Rationale**: 邻接表是通用树形方案，path 字段加速子树查询（如查所有"sales"下节点）。3层硬限制在 Service 层校验。与 DimensionMember 模型一致。
- **Alternatives considered**: ①纯邻接表（递归查询性能差）；②嵌套集（复杂，不适合频繁增删）

### D5: 存量指标域迁移

- **Decision**: seed 脚本自动创建标准域（sales/finance/user/product/marketing/logistics/uncategorized），运行时扫描 Metric 表已有 domain 值，精确匹配则关联，不匹配则归入 uncategorized
- **Rationale**: 最小化人工干预，uncategorized 兜底确保无遗漏。
- **Alternatives considered**: ①手动映射表（需用户参与，体验差）；②不允许不匹配（阻塞性太强）

## Data Model

### SubjectDomain（主题域）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | BigInteger | PK, auto | 主键 |
| code | String(64) | unique, not null | 域编码（小写字母开头+小写字母数字下划线） |
| name | String(128) | not null | 显示名 |
| parent_id | BigInteger | FK→subject_domain.id, nullable | 父域ID（根域为null） |
| level | Integer | not null, default=1 | 层级（1=根/2=子/3=孙） |
| path | String(512) | nullable | 物化路径（如"1.5.12"） |
| sort_order | Integer | not null, default=0 | 同级排序 |
| status | Enum(active/inactive) | not null, default=active | 状态 |
| defaults_json | JSON | not null, default={} | 域级默认值预设 |
| description | Text | nullable | 描述 |
| owner_id | BigInteger | not null | 域管理员ID |
| created_at | DateTime | not null | 创建时间 |
| updated_at | DateTime | not null | 更新时间 |
| deleted_at | DateTime | nullable | 软删除 |

**索引**: idx_domain_code(code), idx_domain_parent(parent_id), idx_domain_path(path), idx_domain_status(status)

**defaults_json 结构**:
```json
{
  "granularity": "day",
  "unit": "CNY",
  "aggregation": "SUM",
  "time_semantics": "PERIOD",
  "freshness": "T1",
  "dw_layer": "DWD",
  "type": "atomic",
  "serving_mode": "BATCH_ONLY",
  "additivity": "ADDITIVE",
  "metric_tier": "T3"
}
```

### SystemDict（系统字典）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | BigInteger | PK, auto | 主键 |
| dict_type | String(64) | not null | 字典类型（granularity/unit/aggregation/time_semantics/freshness/dw_layer/metric_type/additivity/serving_mode/metric_tier） |
| code | String(64) | not null | 字典项编码 |
| label | String(128) | not null | 显示名 |
| sort_order | Integer | not null, default=0 | 排序 |
| status | Enum(active/inactive) | not null, default=active | 状态 |
| description | String(256) | nullable | 描述 |
| created_at | DateTime | not null | 创建时间 |
| updated_at | DateTime | not null | 更新时间 |

**唯一约束**: uk_dict_type_code(dict_type, code)
**索引**: idx_dict_type(dict_type), idx_dict_status(status)

**dict_type 枚举值与预置数据**:

| dict_type | 预置项 (code:label) |
|-----------|---------------------|
| granularity | day:天, week:周, month:月, quarter:季, year:年, hour:小时 |
| unit | CNY:人民币元, USD:美元, EUR:欧元, cnt:个数, ratio:比率, percent:百分比, KWH:千瓦时, GB:吉字节, TB:太字节, MB:兆字节 |
| aggregation | SUM:求和, AVG:平均, COUNT:计数, COUNT_DISTINCT:去重计数, LAST_VALUE:末值 |
| time_semantics | PERIOD:期间, YTD:年初至今, TTM:滚动12月, AVG:均值 |
| freshness | REALTIME:实时, T1:T+1, HOURLY:小时级 |
| dw_layer | ODS:原始层, DWD:明细层, DWS:汇总层, ADS:应用层, DM:域模型层 |
| metric_type | atomic:原子, derived:衍生, composite:复合 |
| additivity | ADDITIVE:可加, SEMI_ADDITIVE:半可加, NON_ADDITIVE:不可加 |
| serving_mode | BATCH_ONLY:仅批, REALTIME_ONLY:仅流, BATCH_REALTIME_DUAL:批流双路 |
| metric_tier | T1:核心, T2:重要, T3:一般 |

## Contracts & Interfaces

### 主题域 API（/api/v1/domains）

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| GET | / | 查询域树（可选 parent_id 过滤） | ALL_ROLES |
| GET | /{code} | 获取域详情+默认值+关联指标数 | ALL_ROLES |
| POST | / | 创建域节点 | platform_admin, domain_admin |
| PUT | /{code} | 更新域（名称/排序/默认值/描述） | platform_admin, domain_admin |
| PATCH | /{code}/status | 启用/停用域 | platform_admin |
| DELETE | /{code} | 删除域（需无关联指标） | platform_admin |
| GET | /{code}/defaults | 获取域默认值预设 | ALL_ROLES |
| PUT | /{code}/defaults | 更新域默认值预设 | platform_admin, domain_admin |
| GET | /{code}/metrics | 获取该域下指标列表 | ALL_ROLES |

### 系统字典 API（/api/v1/dicts）

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| GET | /{dict_type} | 获取某类型字典列表（仅active） | ALL_ROLES |
| GET | /{dict_type}/all | 获取某类型全部字典项（含inactive） | platform_admin |
| POST | /{dict_type} | 新增字典项 | platform_admin |
| PUT | /{dict_type}/{code} | 更新字典项 | platform_admin |
| PATCH | /{dict_type}/{code}/status | 启用/停用字典项 | platform_admin |
| DELETE | /{dict_type}/{code} | 删除字典项（需无引用） | platform_admin |
| GET | /{dict_type}/{code}/ref-count | 获取字典项引用计数 | platform_admin |

### 指标注册自动推断 API（/api/v1/metric-definitions）

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| POST | /auto-suggest | 输入域+源表+度量列→返回编码建议+字段默认值 | ALL_ROLES |

**auto-suggest 请求**:
```json
{
  "domain_code": "sales",
  "source_table": "dwd.sales_detail",
  "measure_column": "amount",
  "period": "day"
}
```

**auto-suggest 响应**:
```json
{
  "metric_code_suggestion": "sales_sales_amount_day",
  "defaults": {
    "granularity": "day",
    "unit": "CNY",
    "aggregation": "SUM",
    "time_semantics": "PERIOD",
    "freshness": "T1",
    "dw_layer": "DWD",
    "type": "atomic",
    "serving_mode": "BATCH_ONLY",
    "additivity": "ADDITIVE",
    "metric_tier": "T3"
  },
  "segments": {
    "domain": "sales",
    "biz_object": "sales",
    "measure": "amount",
    "period": "day"
  }
}
```

### 前端组件契约

**SubjectDomainPage**: 树形域管理页
- 树组件（Ant Design Tree）展示3层域结构
- 右侧详情面板：域编码/名称/父域/默认值/关联指标数
- 操作：新增/编辑/停用/删除/配置默认值

**SystemDictPage**: 字典管理页
- 左侧 Tab 切换字典类型
- 右侧表格：编码/显示名/排序/状态/引用数
- 操作：新增/编辑/停用/删除

**MetricCreatePage（重构）**: 注册指标页
- 业务域：级联树选择器（Cascader），选中后触发 auto-suggest
- 指标编码：半自动输入框（显示建议值，可覆盖，实时校验4段格式）
- 粒度/单位/聚合/时间语义/新鲜度/数仓层：字典下拉（Select），选中域后自动带入默认值
- 类型/可加性/服务模式/分级：字典下拉
- 名称/口径定义：仍为手动输入

### MetricCreateRequest Schema 变更

- `domain`: 增加 field_validator 校验值必须存在于 SubjectDomain（active 状态）
- `granularity/unit`: 增加 field_validator 校验值必须存在于 SystemDict（对应 dict_type）
- 新增可选字段 `source_table` / `measure_column` / `period`（用于 auto-suggest 场景）
