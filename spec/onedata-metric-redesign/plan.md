# Implementation Plan: OneData 指标模型重构（方案B — 度量目录 + 独立挂载实体）

**Input**: `docs/指标设计以及界限说明.md`（OneData 三层指标 + 三条硬边界）+ `docs/指标语义平台产品需求说明书_v3.0.md`（生产化基线）
**Decisions（用户已拍板）**:
1. 状态枚举保留平台扩展态（DRAFT/REVIEW/PUBLISHED/EXPERIMENTAL/DEPRECATED/DATA_SOURCE_DROPPED），界限文档补"平台扩展态说明"，不改后端状态机
2. 粒度从 `metric` 下沉到挂载实体 `metric_mount`
3. 立即补 P1 字段（度量格式/小数位数/源头系统/同义词）
4. 方案B：新增独立挂载实体 `metric_mount`（源表/列/粒度/周期/域）
5. 原子指标 = 逻辑度量（接入度量目录 `measure_catalog`）+ 聚合方式，**不挂物理表**；派生 = 原子 + 时间 + 业务限定 + 挂载

## Summary

把当前"atomic 自带源表/度量列/粒度"的实现，重构为纯 OneData 三层模型：
- **原子指标**：不绑物理表，由 `measure_id` 引用逻辑度量目录 + 聚合方式 + P1 字段（度量格式/小数位/源头系统/同义词从度量目录继承）
- **挂载实体 `metric_mount`**：承载源表/源列/粒度/默认周期/业务域（granularity 从 metric 下沉到此）
- **派生指标** = 原子（dependencies）+ 统计周期（period）+ 业务限定（business_qualification）+ 挂载（metric_mount）
- **复合指标** = 多个派生指标英文名 + 公式（不变，仅强校验"只引用派生指标、禁裸表字段"）

## Technical Context

**Language/Version**: Python 3.11 (FastAPI/SQLAlchemy 2.0/Pydantic v2) + React 18 (Vite/TS/AntD 5)
**Storage**: MySQL 8（新增 measure_catalog、metric_mount 两表；metric 表加 measure_id、granularity 改可空）
**Testing**: pytest（后端）、vitest + tsc --noEmit（前端）
**Constraints**: 存量 11 条指标（带 source_table/measure_column/granularity）需迁移；血缘/消费/冲突预检读取 `definition_json.source_table` 的路径改为查 `metric_mount`
**Scale**: 涉及 semantic 服务 + 前端 MetricCreate/MetricDetail/MetricCatalog + 新度量目录管理页

## Project Structure

```text
backend/app/
├── models/
│   ├── measure_catalog.py        # 新增：逻辑度量目录 ORM
│   └── metric_mount.py           # 新增：挂载实体 ORM
├── services/
│   ├── measure_catalog/          # 新增：schemas/repository/service（照 dimension 五件式瘦身）
│   │   ├── __init__.py
│   │   ├── schemas.py
│   │   ├── repository.py
│   │   └── service.py
│   ├── metric_mount/             # 新增：挂载实体五件式
│   │   ├── __init__.py
│   │   ├── schemas.py
│   │   ├── repository.py
│   │   └── service.py
│   └── semantic/
│       ├── schemas.py            # 改：MetricCreateRequest/MetricResponse 类型化校验
│       └── service.py            # 改：create/update 按 OneData 语义
├── api/
│   ├── measure_catalog.py        # 新增：/api/v1/measure-catalogs
│   └── metric_mount.py           # 新增：/api/v1/metric-mounts
├── alembic/versions/
│   ├── 0075_measure_catalog.py   # 新增表
│   ├── 0076_metric_mount.py      # 新增表
│   └── 0077_metric_onedata.py    # metric 加 measure_id / granularity 改可空
└── tests/unit/
    ├── test_measure_catalog.py   # 新增
    └── test_metric_mount.py      # 新增

frontend/src/
├── pages/MeasureCatalogs.tsx     # 新增：度量目录管理页
├── pages/MetricCreate.tsx        # 改：原子=选逻辑度量；派生=原子+挂载+周期+限定
├── pages/MetricDetail.tsx        # 改：挂载/粒度/P1 展示
├── pages/MetricCatalog.tsx       # 改：P1/挂载列
├── api.ts                        # 扩展：measure_catalog/metric_mount API
└── types.ts                      # 扩展：MeasureCatalog/MetricMount/MetricResponse P1 字段
```

## Data Model

### 表 1: measure_catalog（逻辑度量目录，照 dimension 状态机 DRAFT/PUBLISHED/DEPRECATED）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGINT | PK AUTO_INCREMENT | 主键 |
| measure_code | VARCHAR(64) | UNIQUE NOT NULL | 逻辑度量编码（英文，如 pay_amt） |
| name | VARCHAR(128) | NOT NULL | 中文名（支付金额） |
| description | TEXT | NULL | 描述 |
| measure_format | ENUM('AMOUNT','RATIO','NUMERIC') | NOT NULL | 度量格式（金额/比率/数值） |
| default_unit | VARCHAR(32) | NOT NULL DEFAULT '元' | 默认单位（金额:元/比率:小数） |
| default_decimal_places | INT | NOT NULL DEFAULT 2 | 默认小数位（金额2/比率4） |
| source_system | JSON | NULL | 源头系统（业务系统术语多值） |
| synonyms | JSON | NULL | 同义词 |
| domain | VARCHAR(64) | NOT NULL | 业务域 |
| owner_id | BIGINT | FK user.id NOT NULL | 负责人 |
| status | ENUM('DRAFT','PUBLISHED','DEPRECATED') | NOT NULL DEFAULT 'DRAFT' | 状态机 |
| created_at / updated_at / deleted_at | DATETIME | 标准 | BaseModel |

**索引**: `idx_measure_domain`(domain)

### 表 2: metric_mount（挂载实体，粒度下沉处）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGINT | PK AUTO_INCREMENT | 主键 |
| metric_id | BIGINT | FK metric.id UNIQUE NOT NULL | 所属指标（派生） |
| source_table | VARCHAR(255) | NOT NULL | 源表 |
| source_column | VARCHAR(255) | NOT NULL | 度量列（映射原子逻辑度量） |
| granularity | VARCHAR(64) | NOT NULL | 粒度（从 metric 下沉） |
| default_period | VARCHAR(32) | NULL | 默认统计周期（day/month…） |
| domain | VARCHAR(64) | NOT NULL | 业务域 |
| created_at / updated_at / deleted_at | DATETIME | 标准 | BaseModel |

**唯一约束**: `uk_mount_metric`(metric_id)

### metric 表变更

- 新增 `measure_id` BIGINT NULL FK→measure_catalog.id（原子指标引用逻辑度量；派生/复合可空）
- `granularity` 改 **nullable**（新值以 metric_mount.granularity 为准；存量保留，派生创建时由挂载冗余回填供列表展示）
- **不改** status/type 枚举（平台保留扩展态）

### definition_json 键调整

- **原子**: 移除对 `source_table`/`measure_column` 的强制；保留 `expression`/`sql`（技术口径）
- **派生**: `dependencies`（原子 code）+ `period`（统计周期）+ `business_qualification`（业务限定/过滤条件）+ `expression`（可选计算逻辑）
- **复合**: 不变（公式仅引用派生英文名）

## Contracts & Interfaces

### 后端新增 API

```
POST/GET /api/v1/measure-catalogs          # 创建/分页列表
GET/PUT   /api/v1/measure-catalogs/{code}  # 详情/更新
POST      /api/v1/measure-catalogs/{code}/publish
POST      /api/v1/measure-catalogs/{code}/deprecate
GET/POST  /api/v1/metric-mounts            # 按 metric 查询/创建挂载
GET/PUT/DELETE /api/v1/metric-mounts/{id}
```

### 指标创建/更新语义（schemas.py 校验）

- **atomic**: 必填 `measure_id`（或内联新建度量）+ 聚合方式；不再要求源表/度量列/粒度
- **derived**: 必填 `dependencies`（≥1 原子）+ `period` + `business_qualification` + 创建 `metric_mount`（源表/列/粒度）
- **composite**: 必填派生依赖 + 公式（发布时校验公式 token 全为已存在派生 code，禁 `table.column` 裸表字段）

## Research & Decisions

### D1: P1 字段归属
- **Decision**: P1 字段（度量格式/默认单位/默认小数位/源头系统/同义词）主存 `measure_catalog`，`MetricResponse` 通过 join 度量目录透出；metric 表不冗余
- **Rationale**: OneData"一处定义多处复用"——度量格式/小数位是逻辑度量固有属性，派生/复合继承原子
- **Alternatives**: 每指标冗余一份（列表免 join 但易漂移）

### D2: granularity 处置
- **Decision**: 新增 `metric_mount.granularity` 为规范来源；`metric.granularity` 改可空保留列，派生创建时由挂载回填冗余（列表排序/展示零改动），后期清理列
- **Rationale**: 最小破坏面。直接删列会波及 schemas/响应/compare/迁移的几十处引用，风险高
- **Alternatives**: 一次性删列（彻底但高险）

### D3: 存量数据迁移
- **Decision**: 存量 11 条 atomic 不强制改类型；`measure_id` 置空，`metric_mount` 不自动生成（避免语义猜测），P1 字段为空；迁移只保证"不丢、可读、不 500"
- **Rationale**: 存量 atomic 在旧语义下绑定了物理表，语义上更接近新"派生"，强行归类会产生错误口径；留人工确认（注册页引导重建）
- **Alternatives**: 自动生成挂载+派生（自动化但语义可能错）

### D4: 派生 metrics 计算逻辑
- **Decision**: 派生首期支持"原子 + 周期 + 业务限定"（继承原子聚合，不填自由表达式）；`expression` 保留可选项（高级 SQL 口径）
- **Rationale**: 对齐界限文档"派生不加统计粒度、继承原子属性"，同时不丢现有 SQL 能力

## Implementation Phases

> 进度（2026-08-18）：Phase 1/2/3 已交付并提交（cda1897/9dc1f00/81a26bd），
> 真实 DB 已跑 migration 至 0077、backend 镜像已重建、接口冒烟通过（eb9859f 文档同步）。

### Phase 1 — 后端新实体（✅ 已完成，commit cda1897）
- [x] 1. `models/measure_catalog.py` + `models/metric_mount.py`
- [x] 2. `services/measure_catalog/`（schemas/repository/service）+ `services/metric_mount/`
- [x] 3. `api/measure_catalog.py` + `api/metric_mount.py` + main.py 挂载
- [x] 4. migration 0075/0076/0077 + metric.py 变更（measure_id、granularity nullable）
- [x] 5. 单测（新模块 CRUD/状态机/分页）

### Phase 2 — 指标创建/更新 OneData 语义（✅ 已完成，commit 9dc1f00）
- [x] 6. schemas.py 类型化校验重构（atomic=measure_id；derived=dependencies+period+挂载）
- [x] 7. service.py create/update：派生自动建 metric_mount + 粒度回填；mount 源表并入 definition_json 供血缘等旧读者
- [x] 8. 存量 metric 测试适配 + 新校验测试

### Phase 3 — 前端（✅ 已完成，commit 81a26bd）
- [x] 9. 度量目录管理页 MeasureCatalogs.tsx（/measure-catalogs）
- [x] 10. MetricCreate 重构：原子=选逻辑度量+聚合（P1 继承）；派生=依赖+挂载(表/列/粒度/周期)
- [x] 11. MetricDetail/MetricCatalog 粒度可空适配（挂载/P1 经 measure-catalogs、metric-mounts 接口取）
- [x] 12. api.ts/types.ts 扩展 + 测试适配 + tsc + vitest 全量（789/789）

### Phase 4 — 收尾（✅ 已完成，commit eb9859f + 真实 DB）
- [x] 13. 真实 DB 跑 migration（alembic upgrade head → 0077）+ 接口冒烟（measure-catalogs/metric-mounts 200、指标列表恢复）
- [x] 14. 界限文档补"平台扩展态/粒度下沉"说明 + TD §4.1 新表
- [x] 15. 提交（每阶段一 commit）

### 后续待办（✅ 已完成，commit 58d3004/8d73a43/28c0b93/2eddf76）
- [x] 复合指标发布时强校验公式 token 全为已发布派生 code、禁裸表字段（界限文档 §4.2）——58d3004
- [x] 派生挂载改粒度在 PUBLISHED 状态接入 PENDING_VERSION 确认联动——8d73a43
- [x] 血缘/消费/冲突预检改读 metric_mount 为权威源（当前以 definition_json 冗余兜底）——28c0b93
- [x] 存量 atomic 指标 OneData 化引导（重建为逻辑度量 + 派生挂载）——2eddf76

### 存量 mock 订正 + 指标目录按用户群体差异化（✅ 已完成，commit 3ad9262，2026-08-19）
- [x] seed_e2e_data.py：新增 `ensure_measure`（幂等创建 4 个逻辑度量 + publish），8 条指标订正——7 条原子经 `measure_id_code` 关联度量（移除顶层/内嵌 source_table/measure_column）、1 条派生携 `mount`（service 自动落 metric_mount 并回填粒度）；`ensure_metric` POST 幂等兜底（409 已存在/归档跳过）
- [x] 测试 fixture OneData 化（约 15 文件）：conftest `make_metric` 加 `measure_id=1`；semantic/subject_domain 集成 `_create_payload` 加 measure_id；consume/governance 集成插 `MeasureCatalog` 行（InnoDB FK）+ ORM 关联；各单测构造器/ORM 对齐；`test_semantic_schemas` 旧式兼容用例有意保留
- [x] 前端 mock：MetricCatalog/MetricDetail fixture 加 `measure_id`（存量引导用例显式 `measure_id:undefined` 保留）
- [x] 指标目录按用户群体差异化（MetricCatalog.tsx）：7 角色聚合 4 群体（消费者/生产者/治理审核/平台管理）——群体默认列 + 角色默认筛选（reviewer=REVIEW、compliance_officer=piiOnly、metric_owner=myMetricsOnly、domain_admin=本域）+ 列设置 Dropdown（visibleCols + localStorage 按群体隔离 + 恢复角色默认）+ 新增提交人列 + URL 参数优先
- [x] 验证：后端 418 单测全绿、前端 tsc + vitest 802/802（含 6 个角色差异化新用例）、seed 冒烟 20/20（真实后端 + 真实 DB）
