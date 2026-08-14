# Feature Specification: 采集目录 Schema 表格化展示 + LLM 推断字段描述 + 人工编辑

**Created**: 2026-08-14  
**Status**: Draft  
**Input**: 资产地图数据表Schema摘要以表格展示；采集后查看库/表/字段/描述；缺失描述LLM推断+人工编辑

## Overview

当前采集目录(Catalogs)页仅展示表级元数据，无法查看字段级详情；资产地图(AssetMap)详情抽屉中Schema摘要以嵌套 ObjectView 文本渲染，视觉混乱。本功能将：1) 资产地图Schema摘要改为规范表格展示；2) 采集目录页增加字段详情展开能力；3) 新增 LLM 字段描述推断（手动触发）+ 人工编辑，描述独立存储避免采集覆盖。

## User Scenarios & Testing

### User Story 1 - Schema 摘要表格化展示 (Priority: P1)

数据治理人员在资产地图点击数据表详情，看到 Schema 摘要以结构化表格展示（字段名、类型、描述列），而非当前嵌套文本。

**Why this priority**: 最直接的视觉体验改善，用户明确反馈"视觉太混乱"，改动局部且风险低。

**Independent Test**: 在资产地图→数据表→点详情→Schema摘要区域呈现为 `<Table>` 组件，含 Name/Type/Comment 三列，空 comment 显示灰色占位符。

**Acceptance Scenarios**:

1. **Given** 资产地图数据表详情抽屉打开，**When** Schema 摘要数据为 `[{name:"id",type:"bigint",comment:"主键"}]` 数组，**Then** 渲染为 Ant Design `<Table>` 组件，三列分别显示字段名、类型、描述
2. **Given** Schema 摘要数据为字符串或空，**When** 详情抽屉打开，**Then** 兼容展示：字符串原样显示，空值显示占位符 "-"
3. **Given** 表有 50+ 字段，**When** Schema 表格渲染，**Then** 表格分页展示（每页 20 行）

---

### User Story 2 - 采集目录页查看字段详情 (Priority: P1)

数据治理人员配置数据源并完成采集后，在采集目录页点击某张表，展开查看该表所有字段的名称、类型、描述信息。

**Why this priority**: 核心诉求——"在哪里可以看到库、表、字典、描述信息等这些元数据"，这是采集后最自然的查看入口。

**Independent Test**: 在 Catalogs 页点击表行的"字段详情"按钮，弹出抽屉展示该表 schema_def.columns 的完整字段表格。

**Acceptance Scenarios**:

1. **Given** Catalogs 页已加载目录列表，**When** 用户点击某表行的"字段详情"按钮，**Then** 打开抽屉/弹窗，调用 `GET /catalogs?keyword={entity_name}` 获取 schema_def，展示字段表格（名称/类型/描述/可空/默认值）
2. **Given** 某表 schema_incomplete=true，**When** 打开字段详情，**Then** 表格顶部显示警告标签"Schema 不完整，部分字段信息缺失"
3. **Given** 表无 schema 数据（schema_def={}），**When** 打开字段详情，**Then** 显示空状态提示"暂无字段信息，请先执行采集"

---

### User Story 3 - LLM 推断缺失字段描述 (Priority: P2)

数据治理人员发现多张表的字段描述(comment)为空，点击"推断描述"按钮，系统调用 LLM 根据表名+字段名+类型推断字段描述并回填。

**Why this priority**: 解决描述缺失问题，提升元数据质量。P2 因为依赖 LLM 服务且需要新后端端点。

**Independent Test**: 在字段详情表格中，对 comment 为空的字段行点击"推断描述"按钮，LLM 返回描述并填充到表格。

**Acceptance Scenarios**:

1. **Given** 字段详情表格打开，存在 comment 为空的字段，**When** 用户点击单个字段行的"推断"按钮，**Then** 调用后端 `POST /catalogs/{catalog_id}/columns/{column_name}/infer-description`，返回推断结果并实时更新表格
2. **Given** 表有多条字段缺失描述，**When** 用户点击表格上方"批量推断缺失描述"按钮，**Then** 后端一次性推断所有空 comment 字段，逐条返回结果，前端逐行更新
3. **Given** LLM 服务不可用或超时，**When** 用户触发推断，**Then** 显示友好错误提示"LLM 推断暂时不可用，请稍后重试"
4. **Given** LLM 推断成功，**When** 结果返回，**Then** 描述标记来源为 `llm`（Tag 显示"LLM 推断"），用户可在此基础上人工修正

---

### User Story 4 - 人工编辑字段描述 (Priority: P2)

数据治理人员在字段详情表格中直接双击/点击编辑按钮修改字段描述，修改持久化到独立描述表，不被后续采集覆盖。

**Why this priority**: P2 与 LLM 推断配套，允许用户修正 LLM 结果或手动填写描述。需新建后端存储。

**Independent Test**: 在字段详情表格中点击描述列的编辑图标，输入新描述保存，刷新后描述仍在。

**Acceptance Scenarios**:

1. **Given** 字段详情表格打开，**When** 用户点击某字段描述列的编辑图标，**Then** 描述单元格变为可编辑 Input，可输入/修改描述文本
2. **Given** 用户修改描述并点击确认，**When** 调用 `PUT /catalogs/{catalog_id}/columns/{column_name}/description`，**Then** 描述保存到独立 column_descriptions 表，source 标记为 `manual`，表格即时更新
3. **Given** 字段已有 LLM 推断的描述(source=llm)，**When** 用户手动编辑并保存，**Then** source 更新为 `manual`，Tag 显示从"LLM 推断"变为"人工编辑"
4. **Given** 后续采集更新了 schema_json.columns[].comment，**When** 字段详情加载，**Then** 优先展示 column_descriptions 表中 manual > llm > schema_json 原始 comment 的优先级链

---

### Edge Cases

- 字段名含特殊字符（中文/空格）时 API 路径编码问题
- 表字段数极大（1000+）时的表格性能与分页
- LLM 推断并发限流（多用户同时触发）
- 采集目录 entity_type 非 TABLE（如 VIEW/FIELD）时的字段展示兼容
- column_descriptions 表中手动编辑记录与 schema_json 原始 comment 冲突时的合并展示

## Requirements

### Functional Requirements

- **FR-001**: 资产地图实体详情抽屉的 Schema 摘要区域必须以 `<Table>` 组件渲染，列包含：字段名(Name)、类型(Type)、描述(Comment)，替代原 ObjectView 嵌套文本
- **FR-002**: 采集目录(Catalogs)页必须支持点击表行查看字段详情（抽屉/弹窗），展示 schema_def.columns 的完整信息（名称/类型/描述/可空/默认值）
- **FR-003**: 字段详情表格中，comment 为空的字段行必须显示"推断描述"操作入口
- **FR-004**: 系统必须提供 `POST /catalogs/{catalog_id}/columns/{column_name}/infer-description` 端点，调用 LLM 根据表名+字段名+类型推断字段描述
- **FR-005**: 系统必须支持批量推断：`POST /catalogs/{catalog_id}/infer-descriptions` 对该 catalog 所有空 comment 字段一次性推断
- **FR-006**: 系统必须提供 `PUT /catalogs/{catalog_id}/columns/{column_name}/description` 端点保存人工编辑的字段描述
- **FR-007**: 字段描述必须独立存储到 column_descriptions 表（catalog_id, column_name, description, source, updated_by, updated_at），采集不覆盖
- **FR-008**: 描述展示优先级：manual > llm > schema_json 原始 comment
- **FR-009**: LLM 推断结果必须标记 source=llm，人工编辑标记 source=manual，表格中以 Tag 区分来源
- **FR-010**: LLM 推断失败时必须返回友好错误信息，不阻断页面操作

### Key Entities

- **ColumnDescription**: 独立字段描述记录。属性：catalog_id(关联目录)、column_name(字段名)、description(描述文本)、source(来源: manual/llm/schema)、updated_by(编辑者)、updated_at(更新时间)。唯一约束：(catalog_id, column_name)
- **SchemaColumn**: schema_json.columns 中单条字段信息。属性：name、type/data_type、comment、nullable、default。非独立实体，从 catalog.schema_def 解析

## Success Criteria

### Measurable Outcomes

- **SC-001**: 资产地图 Schema 摘要区域以表格呈现，字段数 ≤ 20 时无需滚动即可阅读完整信息
- **SC-002**: 采集目录页可从表行直达字段详情，操作路径 ≤ 2 次点击
- **SC-003**: LLM 推断单个字段描述响应时间 < 5 秒（P95）
- **SC-004**: 人工编辑描述保存后刷新页面，描述不丢失
- **SC-005**: 后续采集更新 schema_json 后，人工/LLM 编辑的描述仍正确显示（不被覆盖）

## Assumptions

- LLM 客户端基础设施已有（LlmClient + 多 provider 支持），推断描述复用现有配置
- 采集器(MySQL/Postgres)已采集 column_comment 存入 schema_json.columns[].comment
- 字段描述推断仅基于表名+字段名+类型，不涉及字段数据抽样
- 描述推断使用项目已配置的 LLM provider（DeepSeek/Qwen/OpenAI 等），无需新增模型
- column_descriptions 表新建于 MySQL，与 db_catalog 一一对应
- 资产地图详情抽屉宽度可适当加宽（560→720）以适配字段表格

## Open Questions

- LLM 推断并发限流阈值（建议：同一 catalog 最多 1 次并发推断，全局 5 QPS）
- column_descriptions 是否需要 soft-delete（建议：是，与 db_catalog 一致）
