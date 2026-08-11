# 运营操作手册（Operations / SRE）

## 1. 角色定位
对平台**可用性、可观测性、韧性、发布**负总责。对应 `observability`（RED 指标 / /metrics / /ready）、各服务的混沌（chaos）与可观测（observability）门禁、CI 门禁（`CI/.gateways.yml`）。

## 2. 核心职责
- **健康检查与就绪**：`GET /health`、`GET /ready`（可选依赖不可达时 `/ready` 返回 `degraded` 而非失败）。
- **RED 指标**：请求率 / 错误率 / 时延，经 `core/metrics.py` 采集、Prometheus 文本导出。
- **依赖降级**：Redis / Neo4j 经 `CircuitBreaker` 熔断，宕机时优雅降级到 MySQL（舱壁隔离）。
- **混沌韧性**：定期跑 `tests/chaos/`，验证缓存降级、熔断、连接释放。
- **发布与回滚**：遵循 `DEV_GUIDE §11` 合并策略；异常一键回滚。

## 3. 日常操作清单
| 操作 | 动作 | 说明 |
|------|------|------|
| 探活 | 打 `/health` + `/ready` | 接入存活/就绪探针 |
| 指标拉取 | 拉 `/metrics` | RED + 业务指标，接入 Prometheus/Grafana |
| 告警处置 | 按模块 runbook（`docs/runbooks/*`） | 每个服务有独立 runbook |
| 混沌演练 | `poetry run pytest tests/chaos` | 验证熔断/降级/释放 |
| 门禁巡检 | CI `gateways.yml` | 13 道门禁（lint/unit/type/migration/integration/security_reverse/chaos/perf_baseline/contract/doc_sync/secret/supply_chain/runbook/§6.3 双视角） |

## 4. 性能基线（k6）
- 阈值：P95 < 600ms、P99 < 2000ms、错误率 < 5%。
- 基线脚本：`backend/tests/perf/baseline.js` 及各服务专属脚本；强制鉴权、压真实端点、失败率 0.5% 内。

## 5. 关键设计（诚实提示）
- `/ready` 在 Redis/Neo4j 等可选依赖不可达时返回 `degraded`，不阻断存活。
- 写缓存失效（版本缓存失效延迟 < 1s）；缓存未命中回源并跳过 DB。
- 审计日志（含 PII 访问）写入是同步落库，高吞吐下关注写入延迟。

## 6. 应急响应
1. **依赖故障**：Redis/Neo4j 不可达 → 熔断触发 → 流量降级到 MySQL；检查依赖健康与熔断半开恢复。
2. **错误率飙升**：核对 `/metrics` 的 5xx 与熔断打开计数；必要时回滚上一版本。
3. **性能劣化**：查慢查询（DBA 协同）、连接池、N+1；按 runbook 限流/扩容。
4. **发布事故**：按 `DEV_GUIDE §11` 回滚；混沌演练结论用于快速定位薄弱依赖。

## 7. 待补（诚实提示）
- integration / chaos / perf_baseline 部分门禁在本机缺 Docker/DB 凭据时跳过，须于 CI/Docker 环境复测。
- §1.5 人工 ratify 待补（agent 不可代签）。
