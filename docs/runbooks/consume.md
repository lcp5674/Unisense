# Consume Runbook

> 服务：consume（消费层：语义查询 / 结果快照 WORM / 用户偏好，TD §12.6 / FR-12, FR-13）
> 状态门槛：released（verified + runbook + migration 可逆，§1.5 人工 ratify 待补）

## 1. 服务概述
- **职责**：对外提供语义查询（dry-run 口径校验 + 执行计划 + 元信息标注 / 真实下推执行）、结果快照（WORM 一次写不可改）、用户偏好与收藏（user_preference CRUD + 版本确认/驳回回调）。**2026-08-12 补强**：OLAP 执行器（`services/consume/olap_executor.py` 统一方言/直连 Doris/StarRocks、结果封装、不可用降级 503）+ 分布式限流（`services/consume/rate_limiter.py` Redis 令牌桶 + InMemory 降级，多副本一致）。
- **依赖**：
  - MySQL（主存储：api_client / metric_value_snapshot / user_preference）
  - OLAP（可选，查询执行依赖；不可用时查询端点降级 503 非阻塞）
  - Redis（可选，分布式限流计数；不可用降级 InMemory）
- **关键指标**：
  - Semantic API P95 < 300ms
  - 限流 429 带 `retry_after`
  - 含 PII 的访问审计 `data_classification=PII`
- **安全控制**（双视角审查 0 High）：域隔离（`scope_domain` vs `metric.domain` → `FORBIDDEN_DOMAIN`）、PII 强制（域内全量授权不能隐式访问 `pii_flag=1` → `FORBIDDEN_PII`）、注入 fail-closed（400 `INJECTION_DETECTED`）、配额熔断（429 + `retry_after`）、审计分级（`data_classification=PII`）。限流参数：`UNISENSE_CONSUME_QPS`（默认 20/s）/ `UNISENSE_CONSUME_DAILY_QUOTA`（默认 100000/日）。

## 2. 部署步骤
- **前置条件**：MySQL 可达；`alembic upgrade head`；可选 OLAP / Redis（`/metrics` 暴露给 Prometheus）。
- **部署命令**：
  ```bash
  UNISENSE_DB_URL=... UNISENSE_JWT_SECRET=... alembic upgrade head
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
  ```
- **验证步骤**：
  - `GET /health` → 200
  - `POST /consume/query/dry-run` → 返回执行计划（不执行/不写/不计费/不缓存）
  - PII 指标查询 `POST /consume/query` → 审计落库 `data_classification=PII`
  - `POST /consume/api-clients`（platform_admin）创建接入方，secret 仅此一次明文返回

## 3. 监控指标
- **Prometheus 指标**：查询延迟（RED）、dry-run 延迟、429 计数、PII 审计计数、熔断打开次数、配额耗尽次数。
- **告警规则**：
  - `consume_query_p95 > 300ms` → 查询下推变慢（关注 OLAP 是否降级）
  - `consume_rate_limited > 0` 持续 → 配额被打满（调高 qps/daily_quota 或排查滥用）
  - `consume_pii_audit == 0` 但 PII 指标被访问 → 审计链路异常

## 4. 告警处置
| 告警 | 原因 | 处置步骤 |
|------|------|----------|
| 查询慢 | OLAP 回源 MySQL 同步往返 | 确认熔断未误开；大查询走 OLAP（可选依赖） |
| 429 频发 | 配额不足 / 客户端滥用 | 调高 `qps`/`daily_quota`；排查 Fan-out 调用 |
| PII 审计缺失 | 隐式访问未触发审计 | 域内全量授权仍须显式 PII 授权；查 `FORBIDDEN_PII` 日志 |
| 注入告警 | SQL 注入守卫命中 | 返回 400 `INJECTION_DETECTED`；查日志 `trace_id` 溯源 |
| 503 降级 | OLAP 不可用 | 已降级非阻塞；恢复后自动回源 |

## 5. 回滚步骤
- **migration down**：`alembic downgrade -1`（0006_consume：按序 `drop user_preference` / `drop metric_value_snapshot` / `drop_index ix_api_client_status` / `drop api_client`，可逆、数据无损）
- **K8s 回滚**：`kubectl rollout undo deploy/unisense-api`
- **快照 WORM 不可改**：误写须业务补偿（新快照覆盖 + 审计说明）

## 6. 联系人
- **Owner / Backup**：见 `docs/module-status.yaml` consume.owner（空缺，platform_admin 指派）
- **升级路径**：On-call → platform_admin → 架构委员会
- **相关文档**：TD §12.6、CHANGELOG_MODULES（consume 双视角审查 0 High；迁移 0006 可逆复验通过）
