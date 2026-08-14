# Implementation Plan: 采集目录 Schema 表格化展示 + LLM 推断字段描述 + 人工编辑

**Input**: Feature specification from `spec/catalog-schema-view-edit/spec.md`

## Summary

将资产地图 Schema 摘要从嵌套 ObjectView 文本改为 Ant Design Table 展示；在采集目录(Catalogs)页增加字段详情抽屉查看 schema_def.columns；新增后端 LLM 推断字段描述端点 + 人工编辑端点，描述独立存储到 column_descriptions 表（manual>llm>schema_json 优先级链），采集不覆盖。

## Technical Context

**Language/Version**: Python 3.11 (FastAPI) + React 18 (Vite) + TypeScript  
**Primary Dependencies**: FastAPI, SQLAlchemy 2.0, Pydantic v2, Ant Design 5, httpx (LLM client)  
**State Management**: React hooks (useState/useEffect)，无全局状态库  
**Storage**: MySQL 8 (新建 column_descriptions 表)  
**Testing**: pytest (后端), tsc --noEmit (前端类型检查)  
**Target Platform**: Web (浏览器)  
**Performance Goals**: LLM 推断单字段 < 5s P95，字段表格渲染 < 500ms (100 列以内)  
**Constraints**: LLM 推断不阻断主流程，降级友好提示；column_descriptions 不被采集覆盖  
**Scale/Scope**: 14 领域服务已 delivered，本特性涉及 collector + assetmap + ai 三个服务

## Project Structure

### Documentation (this feature)

```text
spec/catalog-schema-view-edit/
├── spec.md              # 需求规格
├── plan.md              # 本文件
└── tasks.md             # 任务分解（Phase 3 产出）
```

### Source Code (repository root)

```text
backend/
├── app/
│   ├── models/
│   │   └── data_source.py          # 新增 ColumnDescription ORM 模型
│   ├── services/
│   │   ├── collector/
│   │   │   ├── schemas.py          # 新增 ColumnDescription 相关 Pydantic schema
│   │   │   ├── repository.py       # 扩展：column_descriptions CRUD
│   │   │   └── service.py          # 扩展：LLM 推断描述 + 保存描述方法
│   │   └── assetmap/
│   │       └── repository.py       # 扩展：get_entity_detail 返回增强 schema_summary 含 description 来源
│   ├── api/
│   │   └── collector.py            # 新增 3 个端点：推断描述/批量推断/编辑描述
│   └── db/
│       └── mysql.py                # 无变更（自动建表 via create_all）
├── alembic/
│   └── versions/                   # 新增 migration: column_descriptions 表
└── tests/
    ├── unit/
    │   ├── test_collector.py       # 扩展：推断描述 + 编辑描述单测
    │   └── test_llm_description.py # 新增：LLM 推断逻辑单测
    └── integration/
        └── test_catalog_description.py  # 新增：端到端推断+编辑集成测试

frontend/
├── src/
│   ├── pages/
│   │   ├── AssetMap.tsx            # 修改：renderSchemaSummary → SchemaTable 组件
│   │   └── Catalogs.tsx            # 修改：增加字段详情抽屉 + 编辑描述 + LLM 推断
│   ├── components/
│   │   └── SchemaTable.tsx         # 新增：通用 Schema 表格组件（字段名/类型/描述/操作）
│   ├── api.ts                      # 扩展：3 个新 API 函数
│   └── types.ts                    # 扩展：ColumnDescription 类型 + schema_summary 类型收窄
└── ...
```

**Structure Decision**: 遵循现有项目架构。后端 FastAPI 分层（models/services/api），前端 React 页面级组件 + 提取通用 SchemaTable 组件。无架构迁移。

## Complexity Tracking

无违规需记录。

## Research & Decisions

### Decision 1: 描述存储方式
- **Decision**: 新建独立 `column_descriptions` 表，不修改 `schema_json`
- **Rationale**: schema_json 是采集器全量写入的领域，人工编辑混入会被下次采集覆盖。独立表可追溯来源(manual/llm/schema)和编辑者，支持优先级链展示
- **Alternatives considered**: (1) 双写 schema_json + 审计日志（采集时合并 manual 优先——复杂且侵入采集器）(2) 仅改 schema_json（简单但必丢数据）

### Decision 2: LLM 推断描述的实现位置
- **Decision**: 在 collector/service.py 新增 `_llm_infer_column_description` 方法，复用已有 `build_llm_client()` + 熔断器
- **Rationale**: collector 服务已持有 `_llm_classify_sensitivity` 方法模式，推断描述与采集目录强相关，放同一服务最自然。复用 LlmClient 基础设施，无需新建 AI 端点
- **Alternatives considered**: (1) 放 ai/service.py（语义上可归属，但 ai 服务当前只做 NL2SQL，引入描述推断职责越界）(2) 新建独立 description 服务（过度设计）

### Decision 3: 前端 Schema 表格组件
- **Decision**: 提取 `SchemaTable.tsx` 通用组件，被 AssetMap 和 Catalogs 两处复用
- **Rationale**: 两处都需要展示字段表格（名称/类型/描述），且 Catalogs 页额外需要编辑和推断操作列。通用组件通过 props 控制是否显示操作列
- **Alternatives considered**: (1) 各页面内联实现（代码重复）(2) 仅改 AssetMap，Catalogs 单独实现（不一致）

### Decision 4: 资产地图 schema_summary 增强策略
- **Decision**: 后端 `get_entity_detail` 返回增强型 schema_summary，每条字段增加 `description` 和 `description_source` 字段（合并 column_descriptions 表数据），前端无需二次请求
- **Rationale**: 前端一次请求获取完整字段信息（含编辑后的描述），减少网络往返。后端在 repository 层 JOIN 查询 column_descriptions，按优先级合并
- **Alternatives considered**: (1) 前端先获取 schema_summary 再请求 column_descriptions（两次请求，体验差）(2) 仅返回 schema_json 原始数据，前端自行请求描述（前端逻辑复杂）

## Data Model

### 新增表: column_descriptions

| 列名 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | BIGINT | PK AUTO_INCREMENT | 主键 |
| catalog_id | BIGINT | FK→db_catalog.id, NOT NULL | 关联目录实体 |
| column_name | VARCHAR(256) | NOT NULL | 字段名 |
| description | TEXT | NOT NULL | 描述文本 |
| source | ENUM('manual','llm','schema') | NOT NULL, DEFAULT 'schema' | 描述来源 |
| updated_by | BIGINT | NULL | 编辑者用户 ID（LLM 推断时为 NULL） |
| created_at | DATETIME(tz) | NOT NULL | 创建时间 |
| updated_at | DATETIME(tz) | NOT NULL | 更新时间 |
| deleted_at | DATETIME(tz) | NULL | 软删除时间 |

**唯一约束**: `uk_column_desc_catalog_col` ON (catalog_id, column_name)  
**索引**: `idx_column_desc_source` ON (source)

### 增强型 schema_summary 字段结构

后端 `get_entity_detail` 返回的 `schema_summary` 每条字段增加：
- `description`: 最终展示描述（优先级合并后）
- `description_source`: 来源标记 ("manual"/"llm"/"schema"/null)

原始字段保持不变：`name`, `type`, `comment`

## Contracts & Interfaces

### 后端 API 新增端点

#### 1. 推断单字段描述
```
POST /api/v1/catalogs/{catalog_id}/columns/{column_name}/infer-description
Auth: WRITE_DEPS (platform_admin, domain_admin)
Request Body: { "entity_name": "dwd_order", "column_type": "bigint" }
Response: { "data": { "column_name": "user_id", "description": "用户唯一标识ID", "source": "llm", "confidence": 0.85 } }
```

#### 2. 批量推断缺失描述
```
POST /api/v1/catalogs/{catalog_id}/infer-descriptions
Auth: WRITE_DEPS (platform_admin, domain_admin)
Request Body: {}
Response: { "data": { "inferred": [{"column_name": "user_id", "description": "用户唯一标识ID", "source": "llm"}], "skipped": ["id"], "failed": [] } }
```

#### 3. 编辑字段描述
```
PUT /api/v1/catalogs/{catalog_id}/columns/{column_name}/description
Auth: WRITE_DEPS (platform_admin, domain_admin)
Request Body: { "description": "订单创建时间戳" }
Response: { "data": { "catalog_id": 42, "column_name": "created_at", "description": "订单创建时间戳", "source": "manual", "updated_by": 1, "updated_at": "2026-08-14T10:00:00Z" } }
```

#### 4. 增强 GET /api/v1/assetmap/entities/{entity_id}
Response 中 schema_summary 每条字段增加 `description` 和 `description_source` 字段。

### 前端新增 API 函数 (api.ts)

- `inferColumnDescription(catalogId, columnName, params)` → POST 端点 1
- `inferDescriptions(catalogId)` → POST 端点 2
- `updateColumnDescription(catalogId, columnName, description)` → PUT 端点 3

### 前端新增/修改类型 (types.ts)

```typescript
// 新增
interface ColumnDescription {
  catalog_id: number;
  column_name: string;
  description: string;
  source: "manual" | "llm" | "schema";
  updated_by: number | null;
  updated_at: string;
}

// schema_summary 类型收窄
interface SchemaColumn {
  name: string;
  type?: string;
  comment?: string;
  description?: string;         // 优先级合并后最终展示
  description_source?: string;  // "manual" | "llm" | "schema" | null
  nullable?: boolean;
  default?: string;
}
```

### 前端组件合约

#### SchemaTable 组件
- **Props**: `columns: SchemaColumn[]`, `loading: boolean`, `editable: boolean`, `onEdit: (col, desc) => void`, `onInfer: (col) => void`, `onBatchInfer: () => void`
- **渲染**: Ant Design `<Table>`，列：字段名/类型/描述(含来源Tag)/操作(编辑/推断)
- **编辑态**: 描述列可点击变为 Input，确认后调 onEdit
- **推断态**: 空 comment 字段行显示"推断"按钮，批量推断按钮在表格上方

### 增强后端 schema_summary 合并逻辑

在 `AssetMapRepository.get_entity_detail` 中：
1. 查询 `column_descriptions` 表获取该 catalog_id 所有记录
2. 构建 `column_descriptions_map: dict[str, ColumnDescription]`（以 column_name 为 key）
3. 遍历 `_summarize_schema` 结果，对每条字段：
   - 如果 column_descriptions_map 有记录：`description=map[name].description`, `description_source=map[name].source`
   - 否则如果 `comment` 非空：`description=comment`, `description_source="schema"`
   - 否则：`description=null`, `description_source=null`
