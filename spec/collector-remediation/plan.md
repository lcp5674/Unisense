# Implementation Plan: 采集模块工业级修复

**Input**: Feature specification from `spec/collector-remediation/spec.md`

## Summary

修复采集模块35项缺陷，核心改动：(1) 实现6种新数据源采集器(PostgreSQL/Hive/Doris/ClickHouse/Kafka/StarRocks)+插件式CollectorRegistry；(2) 实现Schema Drift检测(内容指纹+变更历史+drift事件)；(3) 实现增量采集+定时调度+采集水位；(4) 容错重构(单表跳过+超时+重试+分布式锁)；(5) 健康检查+边界处理统一。

## Technical Context

**Language/Version**: Python 3.11  
**Primary Dependencies**: FastAPI 0.115, SQLAlchemy 2.0, aiomysql, aioredis, arq, httpx  
**Storage**: MySQL 8.0 (新增schema_drift_log/collection_watermark表), Redis 7 (分布式锁+水位)  
**Testing**: pytest (现有6个测试文件+新增)  
**Target Platform**: Linux server (Docker)  
**Performance Goals**: 增量采集耗时较全量减少≥80%, 单表查询超时≤60s  
**Constraints**: 不破坏现有API契约, 新增采集器通过可选依赖安装  
**Scale/Scope**: 7种数据源, 10000+表大型数据源增量采集

## Project Structure

### Documentation (this feature)

```text
spec/collector-remediation/
├── spec.md
├── plan.md
└── tasks.md
```

### Source Code (repository root)

```text
backend/app/
├── services/collector/
│   ├── spi.py                    # [重构] CollectorRegistry+6种新Collector
│   ├── service.py                # [修改] 容错+Drift+健康+batch事件+水位
│   ├── classifier.py             # [微调] 无重大改动
│   ├── queue.py                  # [修改] Arq重试参数+移除close
│   ├── tasks.py                  # [修改] 幂等+超时+健康更新
│   ├── events.py                 # [修改] batch事件支持
│   ├── repository.py             # [修改] 内容指纹+Drift+水位+coverage修正
│   ├── schemas.py                # [修改] 统一SourceType枚举+校验
│   └── connectors/               # [新增] 独立连接器目录
│       ├── __init__.py
│       ├── mysql.py              # [新增] MySQL连接器(从spi.py拆出)
│       ├── postgres.py           # [新增] PostgreSQL连接器
│       ├── hive.py               # [新增] Hive连接器
│       ├── doris.py              # [新增] Doris连接器
│       ├── clickhouse.py         # [新增] ClickHouse连接器
│       ├── kafka.py              # [新增] Kafka连接器
│       └── starrocks.py          # [新增] StarRocks连接器
├── models/
│   ├── data_source.py            # [修改] 新增NEEDS_REVIEW+schema_drift_log+watermark
│   └── collector_models.py       # [新增] SchemaDriftLog+CollectionWatermark模型
├── api/collector.py              # [修改] 健康探活端点+超时保护+分布式锁
├── core/config.py                # [修改] 新增采集调度配置
└── alembic/versions/
    └── 0018_collector_drift_watermark.py  # [新增] 迁移脚本

backend/tests/
├── unit/
│   ├── test_collector.py         # [修改] 扩展多数据源+Drift+容错测试
│   ├── test_collector_queue.py   # [修改] Arq重试+幂等测试
│   └── test_collector_registry.py  # [新增] CollectorRegistry测试
├── integration/
│   └── test_collector_integration.py  # [修改] 多数据源集成测试
├── security/
│   └── test_collector_security.py  # [修改] 分布式锁安全测试
└── chaos/
    └── test_collector_chaos.py   # [修改] 容错+重试混沌测试
```

**Structure Decision**: 遵循现有项目架构（FastAPI三层），不引入新架构模式。新增connectors/子目录存放各数据源连接器，保持spi.py作为SPI入口+注册中心。

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| connectors/子目录7个文件 | 每种数据源连接器独立文件 | 放在spi.py中会导致文件过长(700+行)，违反单一职责 |
| SchemaDriftLog新模型 | 需要持久化变更历史 | 仅发布事件无法回溯历史，违反GB/T 36073审计要求 |
| CollectionWatermark新模型 | 增量采集需要水位标记 | Redis存储水位不持久，重启丢失 |

## Research & Decisions

### Decision 1: Hive连接方式
- **Decision**: 使用beeline CLI (subprocess调用beeline -u jdbc:hive2://host:10000 -e "SQL")
- **Rationale**: beeline是Hive官方推荐CLI，生产环境普遍部署，无需额外Python驱动依赖；通过subprocess异步调用，输出解析为表格数据
- **Alternatives considered**: PyHive+thrift (需安装pyhive+thrift+sasl依赖链，环境兼容性差); Hive Metastore REST API (轻量但缺少完整schema信息); impyla (异步支持不完善)

### Decision 2: 增量采集策略
- **Decision**: 混合策略——优先使用源库last_altered时间戳(支持MySQL/PostgreSQL/Doris/StarRocks)，不支持时降级为全量采集
- **Rationale**: 大多数OLTP/OLAP数据库的information_schema包含UPDATE_TIME列；Hive/ClickHouse/Kafka无此字段时自动降级
- **Alternatives considered**: CDC binlog (需额外基础设施，成本过高); 本地水位对比全量(不够精确)

### Decision 3: Kafka Schema Registry认证
- **Decision**: 支持Basic Auth（connection_config中可选auth_user/auth_password字段），TLS暂不支持
- **Rationale**: Basic Auth覆盖大多数企业内网场景；TLS需证书管理复杂度高
- **Alternatives considered**: 无认证 (不安全); mTLS (企业级但实现复杂)

### Decision 4: 采集器插件注册机制
- **Decision**: 模块级CollectorRegistry字典 + build_collector从注册表查找
- **Decision**: 使用Python decorator注册模式 `@CollectorRegistry.register("postgres")`
- **Rationale**: 简单直观，无需元类复杂度；新增采集器只需在connectors/目录新建文件+decorator
- **Alternatives considered**: setuptools entry_points (需包发布流程); YAML配置注册 (运行时无法校验)

### Decision 5: 分布式锁实现
- **Decision**: Redis SET NX EX 原子命令，锁key=`collect_lock:{source_id}`，TTL=600秒，owner_id=job_id
- **Rationale**: 最简实现，Redis原生支持；Arq worker已在用Redis
- **Alternatives considered**: Redisson (Python无原生实现); ZooKeeper (过重); 数据库行锁 (无超时)

### Decision 6: 内容指纹算法
- **Decision**: SHA-256(canonical_json(schema_json))，canonical_json为排序key后的JSON序列化
- **Rationale**: 确定性+抗碰撞+与语言无关；排序key保证相同schema不同序列化顺序产生相同指纹
- **Alternatives considered**: MD5 (碰撞风险); BLAKE3 (需额外依赖)

### Decision 7: ClickHouse连接方式
- **Decision**: 使用ClickHouse HTTP API (8123端口)，无需安装clickhouse-driver
- **Rationale**: HTTP API是ClickHouse原生接口，httpx直接调用；无需额外Python驱动
- **Alternatives considered**: clickhouse-driver (需安装+连接池管理); SQLAlchemy clickhouse dialect (不成熟)

## Data Model

### 新增实体

#### SchemaDriftLog (Schema变更日志)
| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | BIGINT | PK AUTO_INCREMENT | |
| source_id | VARCHAR(64) | NOT NULL, FK→data_source.source_id | 数据源 |
| entity_name | VARCHAR(256) | NOT NULL | 实体名 |
| change_type | VARCHAR(32) | NOT NULL | ADD_COLUMN/DROP_COLUMN/TYPE_CHANGE/SCHEMA_CHANGED |
| before_signature | VARCHAR(64) | NULL | 变更前内容指纹 |
| after_signature | VARCHAR(64) | NOT NULL | 变更后内容指纹 |
| before_schema | JSON | NULL | 变更前schema |
| after_schema | JSON | NOT NULL | 变更后schema |
| diff_json | JSON | NULL | 差异详情({added:[], removed:[], changed:[]}) |
| detected_at | DATETIME | NOT NULL | 检测时间 |

Index: (source_id, entity_name), (detected_at)

#### CollectionWatermark (采集水位)
| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | BIGINT | PK AUTO_INCREMENT | |
| source_id | VARCHAR(64) | NOT NULL, UNIQUE, FK→data_source.source_id | 数据源(唯一) |
| last_collected_at | DATETIME | NOT NULL | 最后采集时间 |
| mode | VARCHAR(16) | NOT NULL DEFAULT 'FULL' | FULL/INCREMENTAL |
| scanned_count | INT | NOT NULL DEFAULT 0 | 采集表数 |
| failed_count | INT | NOT NULL DEFAULT 0 | 失败表数 |
| content_fingerprints | JSON | DEFAULT {} | {entity_name: signature} 实体级指纹映射 |

Index: source_id (UNIQUE)

### 修改实体

#### DataSource
- health_status枚举新增: `NEEDS_ATTENTION` (部分失败时)
- sensitivity_level枚举新增: `NEEDS_REVIEW`
- 新增字段: `schedule_cron VARCHAR(100) NULL` — 定时调度cron表达式
- 新增字段: `collection_mode VARCHAR(16) DEFAULT 'FULL'` — 采集模式(FULL/INCREMENTAL)

#### DBCatalog
- 新增字段: `content_signature VARCHAR(64) NULL` — 内容指纹(替代upstream_signature的计算方式)
- 新增字段: `schema_incomplete BOOLEAN DEFAULT FALSE` — 空schema标记
- upstream_signature保留但语义变更: 现在是SHA-256(canonical_schema_json)

#### SourceType / EntityType 统一
- 新建 `backend/app/models/enums.py`: 定义共享枚举 SourceTypeEnum / EntityTypeEnum / SensitivityLevelEnum
- schemas.py 和 data_source.py 均引用此枚举

## Contracts & Interfaces

### CollectorRegistry (采集器注册表)

```
CollectorRegistry.register(collector_type: str, factory: Callable[[dict], BaseCollector]) -> None
CollectorRegistry.build(collector_type: str, encrypted_config: str) -> BaseCollector
CollectorRegistry.list_types() -> list[str]
```
- 内部存储: `_registry: dict[str, Callable]`
- 内置注册: mysql, postgres, hive, doris, clickhouse, kafka, starrocks

### BaseCollector (抽象基类，不变)

```
BaseCollector.collect(source) -> CollectResult
CollectResult: { specs: list[CatalogSpec], failed_specs: list[FailedSpec], source_id: str }
FailedSpec: { entity_name: str, error: str }
```
- **关键变更**: collect()返回CollectResult而非list[CatalogSpec]，包含失败信息

### 各数据源连接器接口

#### PostgresCollector
- 查询 `information_schema.tables` + `information_schema.columns`
- URL: `postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}`
- 增量: 使用 `pg_catalog.pg_stat_all_tables.last_autovacuum` 或 `information_schema.tables` 无原生UPDATE_TIME，降级全量

#### HiveCollector
- 查询 Hive Metastore via impyla (thrift://host:9083)
- 获取 `SHOW TABLES` + `DESCRIBE {table}`
- 无增量支持，始终全量

#### DorisCollector
- MySQL协议兼容，复用InformationSchemaCollector
- URL: `mysql+aiomysql://{user}:{password}@{host}:{port}/{database}`
- 增量: Doris information_schema.tables无UPDATE_TIME，降级全量

#### ClickHouseCollector
- HTTP API: `GET http://{host}:8123/?query=SQL`
- 查询 `system.tables` + `system.columns`
- 增量: 使用 `system.tables.metadata_modification_time`

#### KafkaCollector
- 连接 Kafka Broker + Schema Registry
- 采集: Topic列表 + 每Topic的partition_count/replication_factor
- Schema Registry: `GET http://{registry_host}:{port}/subjects` + `/subjects/{subject}/versions/latest`
- 无增量，始终全量

#### StarRocksCollector
- MySQL协议兼容，复用InformationSchemaCollector + `starrocks` driver前缀
- URL: `mysql+aiomysql://{user}:{password}@{host}:{port}/{database}`

### Schema Drift检测接口

```
DriftDetector.detect(source_id, entity_name, old_signature, new_signature, old_schema, new_schema) -> DriftResult | None
DriftResult: { change_type: str, diff_json: dict, before_schema: dict, after_schema: dict }
```

### 分布式锁接口

```
CollectionLock.acquire(source_id, owner_id, ttl=600) -> bool
CollectionLock.release(source_id, owner_id) -> bool
CollectionLock.is_locked(source_id) -> bool
```
- Redis实现: `SET collect_lock:{source_id} {owner_id} NX EX {ttl}`

### 健康检查接口

```
GET /api/v1/data-sources/{source_id}/health
Response: { source_id, health_status, last_collected_at, last_error, uptime_check: bool }
```

### 增量采集接口

```
POST /api/v1/data-sources/{source_id}/collect
Request: { collector_type, mode: "FULL"|"INCREMENTAL" }
Response: { source_id, mode, scanned, registered, failed_specs, skipped_unchanged, coverage }
```

### 采集水位接口

```
GET /api/v1/data-sources/{source_id}/watermark
Response: { source_id, last_collected_at, mode, scanned_count, failed_count }
```

### 定时调度配置

```
POST /api/v1/data-sources/{source_id}/schedule
Request: { cron: str, mode: "FULL"|"INCREMENTAL" }
Response: { source_id, schedule_cron, schedule_mode, next_run_at }
```
