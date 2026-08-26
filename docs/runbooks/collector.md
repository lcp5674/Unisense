# Collector Runbook

> 服务：collector（指标采集与元数据注册，TD §12.1 / FR-02, FR-03）
> 状态门槛：released（verified + runbook + migration 可逆，§1.5 人工 ratify 待补）

## 1. 服务概述
- **职责**：数据源（DataSource）注册与连通性校验；库表目录（DBCatalog）采集、敏感分级（PII/CONFIDENTIAL/INTERNAL）、幂等废弃；批量自动采集编排；采集事件发布。**2026-08-12 补强**：7 种数据源连接器 + `CollectorRegistry` 插件注册（mysql/postgres/clickhouse/doris/starrocks/hive）、Schema Drift 检测（内容指纹 SHA-256 + diff_json + 变更历史，迁移 0018）、增量采集（MySQL/ClickHouse 支持增量，其余降级全量）、定时调度（cron + mode）、容错（单表跳过 failed_specs + `asyncio.timeout(300)` + 分布式锁 + 幂等）、采集健康检查（healthy/unhealthy/unknown + 探活端点）。
- **依赖**：
  - MySQL（unisense 库，主存储）
  - Redis（可选，缓存/异步队列；不可达时经熔断降级，不影响主流程）
  - 外部源连接器（JDBC/HTTP/API，凭据经 Fernet 加密存储）
  - 事件总线（MQ，发布经 `CircuitBreaker` best-effort 降级）
- **关键指标**：
  - 采集增量延迟 P95 < 5min
  - 全量清点 10000 表 < 30min
  - `pii_registered`（本次采集发现的 PII 表计数，写入 COLLECT 审计 `pii_access`）

## 2. 部署步骤
- **前置条件**：MySQL 可达；`UNISENSE_DB_URL`、`UNISENSE_JWT_SECRET` 已通过 Secret Manager 注入；alembic 迁移已 `upgrade head`（含 0018_collector_drift_watermark）。
- **部署命令**：
  ```bash
  # 迁移
  UNISENSE_DB_URL=... UNISENSE_JWT_SECRET=... alembic upgrade head
  # 启动（多 worker 建议置于网关后）
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
  ```
- **验证步骤**：
  - `GET /health` → 200
  - `GET /ready` → 200（依赖缺失时返回 `degraded` 仍可服务）
  - `POST /api/v1/data-sources` 建源 → 200；`POST /api/v1/data-sources/{id}/collect` → 200/503（源不可达降级）
  - 采集健康检查端点 → `healthy/unhealthy/unknown`（含探活）

## 3. 监控指标
- **Prometheus 指标**：采集耗时直方图、PII 注册计数、连接池使用率、事件发布失败计数、外部源失败计数。
- **告警规则**：
  - `collector_collect_latency_p95 > 5min` → 采集延迟超标
  - `collector_event_publish_fail_total > 0` 持续 5min → 事件总线异常
  - `collector_connector_pool_exhausted > 0` → 连接池泄漏（已修复：`collector.dispose()` 释放）

## 4. 告警处置
| 告警 | 原因 | 处置步骤 |
|------|------|----------|
| 采集延迟超标 | 外部源慢/网络抖动 | 检查源连通性；`/collect` 已支持异步队列（InMemory 默认 / Arq 生产），大批量拆小批次或走定时调度 |
| 事件发布失败 | MQ 不可达 | 无需干预，已 best-effort 降级；恢复后补发由下游幂等消费 |
| 连接池耗尽 | 连接器未释放 | 已修复 `finally` 释放；复现时查日志 `trace_id` 定位长事务 |
| PII 审计缺失 | 批量采集未触发审计 | 确认 `pii_registered>0` 时 COLLECT 审计 `pii_access=true` |

## 5. 回滚步骤
- **migration down**：`alembic downgrade -1`（按依赖逆序 DROP，已验证可逆）
- **代码回滚**：镜像 tag 回退——`UNISENSE_IMAGE_TAG=<上一版本> docker compose up -d backend worker frontend`（发布/回滚载体见 `scripts/release.sh`；schema 回滚仍用 migration down）
- **口径/数据源回退**：采集为软删（保留目录历史审计留痕），无需硬删；孤儿资产由后续级联软删补

## 6. 联系人
- **Owner / Backup**：见 `docs/module-status.yaml` collector.owner（当前空缺，由 platform_admin 指派）
- **升级路径**：On-call → platform_admin → 架构委员会
- **相关文档**：TD §12.1、CHANGELOG_MODULES（collector 双视角审查记录，0 High）
