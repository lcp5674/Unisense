# Feature Specification: 主题域管理与指标注册全字段自动化

**Created**: 2026-08-13  
**Status**: Draft  
**Input**: 主题域管理模块缺失，指标注册时字段过度依赖人工输入，需要统一管理+自动化

## Overview

新增主题域管理模块（树形体系），统一管理指标资产体系中的业务域划分；同时将指标注册流程中的全部字段（指标编码、业务域、类型、粒度、单位、时间语义、新鲜度、数仓层、聚合方式等）改为系统字典驱动+自动推断，最大限度减少人工自由输入，杜绝同义不同名、格式混乱等数据质量问题。

核心原则：**系统提供默认值，用户可覆盖但不可自由创造**——所有可选值来自统一字典或自动推断，不允许手工填写不在字典中的值。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 主题域管理员维护域树 (Priority: P1)

主题域管理员在管理界面创建/编辑/删除主题域节点，构建树形业务域体系（如 sales > sales_order/sales_payment），确保全组织使用统一的域划分。管理员可为每个域配置默认值预设（该域指标默认粒度、单位、聚合方式等），让注册指标时自动带入。

**Why this priority**: 主题域字典是全部自动化的前置依赖，没有统一域体系就无法自动生成指标编码、无法推断默认值。

**Independent Test**: 可通过创建/编辑/删除域节点并验证树结构完整性来独立测试。

**Acceptance Scenarios**:

1. **Given** 管理员进入主题域管理页面，**When** 创建根域"sales"并添加子域"sales_order"/"sales_payment"，**Then** 域树显示3层结构，子域正确挂载在父域下
2. **Given** 已有根域"finance"，**When** 管理员为其配置默认值（粒度=day, 单位=CNY, 聚合=SUM, 数仓层=DWD），**Then** 配置保存成功，后续该域下注册指标时自动带入这些默认值
3. **Given** 域"sales"下已有20个指标关联，**When** 管理员尝试删除该域，**Then** 系统拒绝删除并提示"该域下存在关联指标，请先迁移或归档"
4. **Given** 管理员创建域时输入编码"sales"，**When** 提交，**Then** 系统校验编码格式（小写字母开头+小写字母数字）并检查唯一性

---

### User Story 2 - 注册指标时全字段自动推断 (Priority: P1)

指标Owner注册新指标时，选择业务域后，系统自动生成指标编码（域_业务对象_度量_统计周期4段式），并根据所选域的预设配置自动填充粒度/单位/聚合等全部字段默认值。用户可从字典下拉选择覆盖默认值，但不能手工输入不在字典中的值。

**Why this priority**: 这是核心用户价值——从"全手工填15+字段"变为"选域+确认"的极简流程，直接影响数据质量和注册效率。

**Independent Test**: 可通过创建指标并验证各字段默认值/可选值来独立测试。

**Acceptance Scenarios**:

1. **Given** 用户进入注册指标页面，**When** 选择业务域"sales>sales_order"，**Then** 指标编码第1段自动填入"sales"，粒度默认"day"，单位默认"CNY"，聚合默认"SUM"，数仓层默认"DWD"
2. **Given** 用户已选域并输入度量列名"amount"，**When** 系统生成指标编码建议，**Then** 自动拼出"sales_order_amount_day"，用户可在输入框微调
3. **Given** 粒度字段显示默认值"day"，**When** 用户点击下拉，**Then** 下拉选项来自系统字典（day/week/month/quarter/year/hour/minute），不可自由输入
4. **Given** 用户选择业务域后，**When** 该域已配置类型预设为"atomic"，**Then** 类型字段自动选"atomic"，用户可切换为derived/composite
5. **Given** 单位字段，**When** 用户点击下拉，**Then** 显示字典中所有单位（CNY/USD/cnt/ratio/percent/等），不可自由输入

---

### User Story 3 - 系统字典管理 (Priority: P2)

管理员维护粒度、单位、聚合方式、时间语义、新鲜度、数仓层等全部字段的可选值字典。每个字典项含编码+显示名+描述，支持启用/停用。已使用的字典项不可删除。

**Why this priority**: 字典是自动化的基础数据，但可以在主题域管理建立后逐步完善。

**Independent Test**: 可通过增删改字典项并验证下拉选项同步来独立测试。

**Acceptance Scenarios**:

1. **Given** 管理员进入字典管理页面，**When** 在"单位"字典中新增"KWH"（千瓦时），**Then** 注册指标时单位下拉出现"KWH"选项
2. **Given** 字典项"CNY"已被50个指标使用，**When** 管理员尝试删除，**Then** 系统拒绝并提示"已被 N 个指标引用，不可删除，可停用"
3. **Given** 管理员停用粒度"minute"，**When** 用户注册新指标，**Then** 粒度下拉不显示"minute"，但已有使用"minute"的指标不受影响

---

### User Story 4 - 指标编码半自动生成 (Priority: P1)

用户选择业务域+输入源表/度量列后，系统自动拼出4段式指标编码建议（域_业务对象_度量_统计周期），用户可在此基础上手动微调各段内容。系统实时校验编码格式合规性。

**Why this priority**: 指标编码是指标唯一标识，当前纯手工输入导致格式错误频发，半自动生成+可覆盖是最优平衡。

**Independent Test**: 可通过输入不同域/表/列组合并验证生成结果来独立测试。

**Acceptance Scenarios**:

1. **Given** 用户选择域"sales"、源表"dwd.sales_detail"、度量列"amount"，**When** 系统生成编码建议，**Then** 拼出"sales_sales_amount_day"（域=sales, 业务对象=sales, 度量=amount, 统计周期=day）
2. **Given** 用户将编码建议中的"sales"段改为"order"，**When** 提交，**Then** 系统校验4段格式通过，允许注册
3. **Given** 用户手动删除编码中的一段，**When** 仅剩3段，**Then** 系统实时提示"须符合4段格式（域_业务对象_度量_统计周期）"

---

### User Story 5 - 前端注册指标页重构 (Priority: P1)

将注册指标页面从"15+字段全手工输入"重构为"选域→自动填入→确认/覆盖"的极简流程。业务域选择改为级联树选择器，粒度/单位/类型等全部改为字典下拉，去除自由文本输入。

**Why this priority**: 前端是用户直接交互层，必须与后端字典/域体系同步重构才能实现完整的自动化体验。

**Independent Test**: 可通过端到端操作注册指标并验证各字段行为来独立测试。

**Acceptance Scenarios**:

1. **Given** 用户打开注册指标页，**When** 页面加载完成，**Then** 业务域显示为级联树选择器（非文本输入），其余字段均为下拉选择（非自由输入）
2. **Given** 用户选择域"finance>finance_revenue"，**When** 选择完成，**Then** 编码段1自动填"finance_revenue"，粒度/单位/聚合等自动带入域预设默认值
3. **Given** 用户未选域，**When** 查看表单，**Then** 指标编码、粒度、单位等字段为空或不可编辑状态，引导先选域

---

### Edge Cases

- 新系统初始化时无任何主题域和字典数据，注册指标如何处理？→ 需提供系统初始化seed脚本，预置标准域+字典项
- 管理员停用某个域后，已有该域指标如何处理？→ 已关联指标不受影响，但新注册不可选已停用域
- 字典项被停用后，已有使用该值的指标在编辑时如何展示？→ 编辑时保留原值但不可选已停用项，切换后不可切回
- 批量注册（batch_register）如何适配字典校验？→ 批量注册同样走字典校验，自动推断逻辑一致
- 域树层级深度限制？→ 最多3层（根域>子域>孙域），避免过度碎片化

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: 系统必须提供主题域管理模块，支持树形层级结构（最多3层），每个域节点含编码（code）、显示名（name）、父域ID、状态（active/inactive）、域级默认值配置
- **FR-002**: 主题域编码必须全局唯一，格式为小写字母开头+小写字母数字下划线，与指标编码4段式的第1段保持一致
- **FR-003**: 删除主题域时，系统必须校验该域下是否存在关联指标，存在则拒绝删除；停用域不影响已有指标，但新注册不可选
- **FR-004**: 每个主题域节点可配置默认值预设（granularity/unit/aggregation/time_semantics/freshness/dw_layer/type/serving_mode），注册该域指标时自动带入
- **FR-005**: 系统必须提供系统字典管理模块，统一管理粒度、单位、聚合方式、时间语义、新鲜度、数仓层等全部枚举字段的可选值
- **FR-006**: 每个字典项含：所属字典类型（dict_type）、编码（code）、显示名（label）、排序序号、状态（active/inactive）、描述
- **FR-007**: 字典项被指标引用后不可删除，只能停用；停用后新注册不显示，已有指标不受影响
- **FR-008**: 指标注册时，业务域必须从主题域树选择（级联选择器），不可自由输入
- **FR-009**: 指标注册时，粒度/单位/聚合/时间语义/新鲜度/数仓层/类型/可加性/服务模式/分级等字段必须从系统字典下拉选择，不可自由输入
- **FR-010**: 指标编码必须由系统根据"域+源表+度量列+统计周期"半自动生成建议值，用户可覆盖修改，但最终提交必须通过4段格式校验
- **FR-011**: 选择业务域后，系统必须自动将该域的默认值预设填入指标注册表单的对应字段，用户可从字典下拉覆盖
- **FR-012**: 系统必须提供初始化seed脚本，预置标准主题域（sales/finance/user/product/marketing/logistics）和标准字典项（粒度6项/单位10+项/聚合5项/时间语义4项/新鲜度3项/数仓层5项）
- **FR-013**: 主题域管理API必须支持CRUD+树查询+默认值配置+关联指标统计；需RBAC权限控制（仅platform_admin/domain_admin可管理域）
- **FR-014**: 字典管理API必须支持按dict_type查询列表、增删改、启用停用、引用计数；需RBAC权限控制（仅platform_admin可管理字典）
- **FR-015**: 前端侧边栏导航需新增"主题域管理"入口（归入"指标资产"组），"字典管理"入口（归入"治理合规"组）
- **FR-016**: 批量注册（batch_register_metrics）必须适配字典校验，自动推断逻辑与单条注册一致

### Key Entities

- **SubjectDomain（主题域）**: 编码code / 显示名name / 父域parent_id / 层级level / 排序sort_order / 状态status / 默认值预设defaults_json / 创建人/更新时间。与Metric一对多关联（Metric.domain → SubjectDomain.code）
- **SystemDict（系统字典）**: 字典类型dict_type / 编码code / 显示名label / 排序sort_order / 状态status / 描述description / 创建人/更新时间。dict_type枚举：granularity/unit/aggregation/time_semantics/freshness/dw_layer/metric_type/additivity/serving_mode/metric_tier
- **Metric（指标）**: domain字段从自由文本改为关联SubjectDomain.code，受FK约束+字典校验

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 注册指标时，用户从选择业务域到提交表单，手动输入字段数从15+降至2-3个（名称+度量列+确认），其余全部由系统自动填充
- **SC-002**: 新注册指标的业务域100%来自主题域字典，杜绝同义不同名问题（如sales/selling并存）
- **SC-003**: 新注册指标的粒度/单位/聚合等字段100%来自系统字典，杜绝格式不一致（如day/daily/DAY并存）
- **SC-004**: 指标编码4段格式合规率从当前约70%提升至100%（系统生成+校验双保险）
- **SC-005**: 主题域管理操作（增删改停用）≤3步完成，无需开发介入

## Assumptions

- 现有指标的domain字段值为自由文本，迁移时系统自动匹配到主题域code（精确匹配），无法匹配的归入"uncategorized"域
- 系统字典初始数据覆盖常见业务场景，后续由管理员按需扩展
- 粒度字典预置：day/week/month/quarter/year/hour；单位字典预置：CNY/USD/EUR/cnt/ratio/percent/KWH/GB/TB/MB/KB
- 主题域树最多3层，避免过度碎片化；编码规则与metric_code第1段对齐
- 指标注册页面重构后，已存在的批量注册API需同步适配字典校验
- 当前Metric模型的domain字段是String类型，需要兼容存量数据，通过应用层校验（非FK约束）关联SubjectDomain

## Open Questions

- [NEEDS CLARIFICATION: 现有存量指标的domain字段值如"sales"/"finance"等是否已与即将创建的主题域code完全一致？若不一致需要提供迁移映射表]
- [NEEDS CLARIFICATION: 主题域管理员角色——是否复用现有domain_admin角色，还是新增subject_domain_admin角色？]
