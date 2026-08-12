# Feature Specification: 采集模块工业级修复

**Created**: 2026-08-12  
**Status**: Draft  
**Input**: 基于深度审查报告的35项缺陷（C-01~C-38），覆盖多数据源适配、Schema Drift检测、增量采集、容错重试、边界处理、健康检查6大维度

## Overview

元数据采集模块经工业级深度审查，真实达标率40.5/100。核心差距：多数据源仅实现MySQL（1/7种）、Schema Drift检测完全缺失、增量采集未实现、容错机制不足。本次修复目标：解决全部35项缺陷，将达标率从40.5提升至≥88，满足GB/T 36073 §6.4数据标准管理工业级要求。

## User Scenarios & Testing

### User Story 1 - 数据开发注册多种数据源并成功采集 (Priority: P0)

数据开发人员注册MySQL/PostgreSQL/Hive/Doris/ClickHouse/Kafka数据源后，系统能够自动识别类型并执行采集，无需修改源码。

**Why this priority**: 多数据源适配是采集模块的基础能力，当前仅MySQL可用，其余6种类型声明了但无法采集，阻断生产使用。

**Independent Test**: 注册一个PostgreSQL数据源，触发采集，验证返回的catalog包含表和字段信息。

**Acceptance Scenarios**:

1. **Given** 数据开发注册PostgreSQL数据源(connection_config含host/port/user/password/database), **When** 触发采集, **Then** 系统使用PostgresCollector采集information_schema并返回catalog
2. **Given** 数据开发注册Hive数据源, **When** 触发采集, **Then** 系统使用HiveCollector采集Hive Metastore并返回catalog
3. **Given** 数据开发注册Doris数据源, **When** 触发采集, **Then** 系统使用DorisCollector采集Doris内部information_schema并返回catalog
4. **Given** 数据开发注册ClickHouse数据源, **When** 触发采集, **Then** 系统使用ClickHouseCollector采集system.tables/system.columns并返回catalog
5. **Given** 数据开发注册Kafka数据源, **When** 触发采集, **Then** 系统使用KafkaCollector采集Topic列表+Schema Registry元数据并返回catalog
6. **Given** 数据开发注册StarRocks数据源, **When** 触发采集, **Then** 系统使用StarRocksCollector(MySQL协议兼容)采集并返回catalog

---

### User Story 2 - 治理人员感知Schema变更 (Priority: P0)

治理人员注册数据源后，当源库表结构发生变更（新增列/删除列/类型变更）时，系统自动检测变更、记录历史、通知下游指标Owner。

**Why this priority**: Schema Drift检测完全缺失，采集静默覆盖，下游指标定义可能基于过期schema，违反GB/T 36073 §6.4。

**Independent Test**: 采集一次后修改源库表结构（新增列），再次采集，验证Drift事件发布+历史记录+通知。

**Acceptance Scenarios**:

1. **Given** 已注册数据源且完成首次采集, **When** 源库表新增一列后再次采集, **Then** 系统检测到Schema变更，发布catalog_schema_drifted事件，记录变更历史
2. **Given** 已注册数据源且完成首次采集, **When** 源库表删除一列后再次采集, **Then** 系统检测到Schema变更，标记受影响的下游指标
3. **Given** 两次采集间schema未变化, **When** 再次采集, **Then** 不发布drift事件，upstream_signature不变

---

### User Story 3 - 大数据源增量采集 (Priority: P1)

数据开发人员对含10000+表的大型数据源配置增量采集模式（仅采集上次后有变更的表），系统按调度周期自动执行。

**Why this priority**: 全量采集在大型数据源上耗时过长、消耗源库资源，无法满足SLA。

**Independent Test**: 注册大数据源，首次全量采集后修改2张表，触发增量采集，验证仅采集变更的表。

**Acceptance Scenarios**:

1. **Given** 大数据源首次全量采集完成, **When** 触发增量采集, **Then** 系统仅查询last_altered时间戳晚于采集水位的表
2. **Given** 数据源配置cron调度(每天2:00), **When** 调度时间到达, **Then** 系统自动触发增量采集并记录水位
3. **Given** 增量采集任务提交, **When** 任务失败, **Then** Arq自动重试最多3次，每次超时600秒

---

### User Story 4 - 采集容错不中断 (Priority: P0)

数据开发人员采集含1000+表的数据源时，即使部分表查询失败，剩余表仍正常采集，最终汇总成功/失败数量。

**Why this priority**: 当前单表失败中断全批（spi.py:94-97），一个表异常导致1000+表全部失败，生产不可接受。

**Independent Test**: 采集含1000表的数据源，模拟第500表查询超时，验证499表成功采集+1表失败记录。

**Acceptance Scenarios**:

1. **Given** 采集1000表的数据源, **When** 第500表查询超时, **Then** 系统跳过该表继续采集，最终返回scanned=1000, registered=999, failed_specs=[table_500]
2. **Given** 采集任务执行中, **When** 源库连接超时(10秒), **Then** 单表查询在10秒内超时返回，不影响其他表
3. **Given** 采集同一数据源的两次并发请求, **When** 第二次请求到达, **Then** 被分布式锁拒绝，返回"采集进行中"

---

### User Story 5 - 运维人员查看数据源健康状态 (Priority: P1)

运维人员在数据源列表中看到每个数据源的真实健康状态（healthy/unhealthy/unknown），而非永远UNKNOWN。

**Why this priority**: health_status字段存在但无填入逻辑，运维无法判断源库是否可用。

**Independent Test**: 采集成功后验证health_status=healthy，采集失败后验证health_status=unhealthy。

**Acceptance Scenarios**:

1. **Given** 采集任务成功完成, **When** 采集结束, **Then** DataSource.health_status更新为healthy
2. **Given** 采集任务因源库不可用失败, **When** 采集结束, **Then** DataSource.health_status更新为unhealthy
3. **Given** 数据源注册后未采集, **When** 查看数据源, **Then** health_status显示unknown

---

### User Story 6 - 边界处理与数据一致性 (Priority: P1)

系统正确处理各种边界场景：EntityType枚举统一、空schema告警、connection_config格式校验、coverage计算修正、并发保护、LLM异常可观测等。

**Why this priority**: 多项边界缺陷导致数据不一致或安全隐患，需系统化修复。

**Independent Test**: 提交缺必填字段的connection_config，验证校验拒绝。

**Acceptance Scenarios**:

1. **Given** 提交connection_config缺少host字段, **When** 注册数据源, **Then** 返回422校验错误
2. **Given** 采集到空schema的表(无列信息), **When** 注册catalog, **Then** 记录warning日志，catalog标记schema_incomplete=True
3. **Given** 数据源quota未配置(expected=0), **When** 采集完成, **Then** coverage=1.0而非0.0
4. **Given** sensitivity_level枚举包含NEEDS_REVIEW, **When** LLM confidence<0.7, **Then** catalog.sensitivity_level="NEEDS_REVIEW"合法存储
5. **Given** 采集1000表, **When** 采集完成, **Then** 发布1次batch事件而非1000次
6. **Given** LLM分类调用超时, **When** 异常捕获, **Then** 记录llm_classify_error_total metric而非静默吞没

---

### Edge Cases

- 源库返回0张表时如何处理？→ 返回scanned=0, registered=0, 不报错
- 源库连接凭据过期时如何处理？→ ExternalDependencyError(503)，health_status=unhealthy
- 采集任务Arq重试3次仍失败时？→ job_status="FAILED"，记录最终错误详情
- Kafka无Schema Registry时如何处理？→ 仅采集Topic列表，schema_json含topic/partition_count/replication_factor
- Hive Metastore连接失败时如何处理？→ ExternalDependencyError(503)，不静默吞没
- 分布式锁TTL=600s但采集超过10分钟？→ 锁过期后可被新请求获取，需在任务完成时主动释放锁
- 两次采集间schema完全相同但upstream_signature因内容指纹变化？→ 不可能，内容指纹相同即无drift
- 增量采集水位表被清空？→ 降级为全量采集

## Requirements

### Functional Requirements

**P0级（阻断生产）**:

- **FR-001**: 系统MUST支持MySQL/PostgreSQL/Hive/Doris/ClickHouse/Kafka/StarRocks 7种数据源类型的采集器实现
- **FR-002**: 采集器工厂MUST支持插件式注册（CollectorRegistry），新增数据源类型无需修改spi.py源码
- **FR-003**: SourceType枚举MUST在schemas.py和data_source.py间统一为同一枚举定义
- **FR-004**: 采集过程中单表查询失败MUST跳过继续，记录失败表到failed_specs，不中断全批采集
- **FR-005**: SqlalchemyConnector MUST配置connect_timeout=10秒和query_timeout=60秒
- **FR-006**: Arq采集任务MUST配置max_tries=3和timeout=600秒
- **FR-007**: EntityType MUST统一为TABLE/VIEW/FIELD三值枚举，Schema与Model一致
- **FR-008**: sensitivity_level枚举MUST新增NEEDS_REVIEW值
- **FR-009**: coverage计算MUST修正：expected<=0时coverage=1.0（表示无配额限制下全量完成）
- **FR-010**: 系统MUST实现Schema Drift检测：采集后计算内容指纹(SHA-256 of canonical schema_json)，与旧指纹比对，变更时发布catalog_schema_drifted事件
- **FR-011**: 系统MUST记录Schema变更历史（新增列/删除列/类型变更），包含变更时间、变更类型、变更前后值

**P1级（影响SLA）**:

- **FR-012**: 系统MUST支持增量采集模式：基于源库last_altered时间戳或采集水位，仅采集有变更的表
- **FR-013**: 系统MUST支持定时调度采集：通过Arq cron配置自动采集周期（cron表达式可配置）
- **FR-014**: 系统MUST记录采集水位（collection_watermark）：每次采集完成后记录source_id+最后采集时间+采集模式(全量/增量)
- **FR-015**: 采集成功MUST更新DataSource.health_status为healthy，采集失败MUST更新为unhealthy
- **FR-016**: 系统MUST实现数据源健康探活端点GET /data-sources/{source_id}/health
- **FR-017**: 同步采集API MUST添加asyncio.timeout(300秒)保护
- **FR-018**: 系统MUST实现采集并发保护：Redis分布式锁collect_lock:{source_id}，TTL=600秒
- **FR-019**: ArqCollectionQueue.enqueue MUST复用Redis连接池，不再每次enqueue后close
- **FR-020**: DataSourceCreateRequest MUST校验connection_config包含host字段（必填）

**P2级（边界与可维护性）**:

- **FR-021**: _build_url MUST为每种数据库类型提供对应的URL构建器（postgresql+asyncpg/hive+pyhive等）
- **FR-022**: 空schema（columns为空）MUST记录warning日志并标记catalog.schema_incomplete=True
- **FR-023**: LLM分类异常MUST替换BLE001为具体异常类型+metric计数(llm_classify_error_total)
- **FR-024**: collect_and_register MUST在所有spec处理完成后发布1次batch事件而非逐条publish
- **FR-025**: build_collector MUST使用SQLAlchemy URL.create()构建连接URL，避免密码出现在字符串中
- **FR-026**: Arq任务MUST实现幂等保障：job_id作为幂等key，重复执行检测
- **FR-027**: upstream_signature MUST改为内容指纹SHA-256(canonical_schema_json)而非复合键哈希
- **FR-028**: 系统MUST支持StarRocks采集器（基于MySQL协议兼容，使用InformationSchemaCollector+starrocks driver）

### Key Entities

- **CollectorRegistry**: 采集器注册表，维护collector_type → factory函数映射，支持运行时注册新类型
- **SchemaDriftLog**: Schema变更记录，含source_id/entity_name/change_type(ADD_COLUMN/DROP_COLUMN/TYPE_CHANGE)/before_json/after_json/detected_at
- **CollectionWatermark**: 采集水位记录，含source_id/last_collected_at/mode(FULL/INCREMENTAL)/scanned_count
- **ContentSignature**: 内容指纹 = SHA-256(canonical_json(schema_json))，替代当前的复合键哈希
- **DistributedLock**: Redis分布式锁 collect_lock:{source_id}，含owner_id/TTL/acquired_at

## Success Criteria

### Measurable Outcomes

- **SC-001**: 7种数据源类型均可注册+采集+返回catalog（当前仅1/7种）
- **SC-002**: 采集1000表时1表失败，其余999表正常采集成功，不中断全批
- **SC-003**: Schema变更（新增/删除列）自动检测并发布drift事件，检测延迟≤1次采集周期
- **SC-004**: 增量采集模式仅扫描有变更的表，采集耗时较全量模式减少≥80%
- **SC-005**: 数据源健康状态真实反映源库可用性（非永远UNKNOWN）
- **SC-006**: coverage计算正确：无配额限制时coverage=1.0（非0.0）
- **SC-007**: 综合达标率从40.5提升至≥88（GB/T 36073 §6.4工业级）

## Assumptions

- PostgreSQL使用asyncpg驱动（需安装asyncpg包）
- Hive使用PyHive/impyla驱动或Hive Metastore REST API
- Doris/StarRocks使用MySQL协议兼容驱动（mysql+aiomysql）
- ClickHouse使用clickhouse-driver或HTTP API
- Kafka使用confluent-kafka或kafka-python，Schema Registry使用REST API
- Arq worker已部署并配置Redis连接
- SQLAlchemy 2.0的URL.create()方法可用
- 异步驱动包(pyhive/asyncpg等)通过poetry可选依赖安装

## Open Questions

- [Hive连接方式]：Hive采集使用beeline方式（通过subprocess调用beeline CLI执行查询），需要HiveServer2已部署且beeline命令可用。
- [增量采集策略]：增量采集是基于源库的last_altered时间戳（需源库支持）还是基于本地采集水位对比？前者更精确但非所有源库支持。
- [Kafka Schema Registry认证]：Kafka Schema Registry是否需要认证（Basic Auth / TLS）？影响KafkaCollector实现复杂度。
