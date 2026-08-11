# Semantic Runbook

> 服务：semantic（语义查询与指标口径，TD §12.3 / FR-05, FR-06, FR-07）
> 状态门槛：released（verified + runbook + migration 可逆，§1.5 人工 ratify 待补）

## 1. 服务概述
- **职责**：指标语义查询（下推）、口径版本管理、缓存（cache-aside）、PII 访问审计、RED 指标采集、熔断与可选依赖探活。
- **依赖**：
  - MySQL（指标定义主存储）
  - Redis（可选缓存；不可达时经 `CircuitBreaker` 降级到 MySQL，舱壁隔离）
  - ES（可选，搜索降级）
- **关键指标**：
  - 语义查询下推 P95 < 3s（小表）
  - 版本缓存失效延迟 < 1s
  - 列表批量 PII 暴露 → 写一条汇总审计（`action=LIST`, `pii_access=True`）

## 2. 部署步骤
- **前置条件**：MySQL 可达；Redis 可选；`alembic upgrade head`；`/metrics` 端点暴露给 Prometheus。
- **部署命令**：
  ```bash
  UNISENSE_DB_URL=... UNISENSE_JWT_SECRET=... alembic upgrade head
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
  ```
- **验证步骤**：
  - `GET /health`、`GET /ready` → 200（`/ready` 可选依赖降级返回 `degraded` 仍可服务）
  - `GET /metrics` → 200 返回非零 RED 指标
  - `GET /api/v1/metric-definitions` 命中 PII → 审计落库 `data_classification=PII`

## 3. 监控指标
- **Prometheus 指标**：请求延迟（RED）、缓存命中率、缓存回源次数、熔断打开次数、PII 审计计数。
- **告警规则**：
  - `semantic_query_p95 > 3s` → 查询下推变慢
  - `semantic_cache_hit_rate < 0.5` → 缓存失效/Redis 异常
  - `semantic_circuit_open > 0` 持续 → Redis 不可用（已降级，关注恢复）

## 4. 告警处置
| 告警 | 原因 | 处置步骤 |
|------|------|----------|
| 查询慢 | 缓存未命中回源 MySQL | 确认熔断未误开；大查询走 OLAP（可选依赖） |
| 缓存命中率低 | Redis 宕机/预热不足 | 已熔断降级 MySQL；恢复后自动回源并重新缓存 |
| PII 审计缺失 | 列表未触发批量审计 | 确认命中任一 PII 指标即写汇总审计（已实现） |
| 注入告警 | SQL 注入守卫命中 | 返回 400 `INJECTION_DETECTED`；查日志 `trace_id` 溯源 |

## 5. 回滚步骤
- **migration down**：`alembic downgrade -1`
- **K8s 回滚**：`kubectl rollout undo deploy/unisense-api`
- **口径版本回退**：指标支持版本化，发布/废弃为状态机变更，回退即置旧版本 active

## 6. 联系人
- **Owner / Backup**：见 `docs/module-status.yaml` semantic.owner（空缺，platform_admin 指派）
- **升级路径**：On-call → platform_admin → 架构委员会
- **相关文档**：TD §12.3、CHANGELOG_MODULES（semantic 双视角审查，0 High；CircuitBreaker 已接入实时读路径）
