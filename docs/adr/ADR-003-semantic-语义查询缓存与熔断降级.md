# ADR-003: 语义查询层采用 cache-aside + 可选依赖熔断降级，MySQL 为唯一核心依赖

## 状态：Accepted
## 日期：2026-08-08
## 背景
语义查询需对指标定义/口径提供稳定低延迟响应（TD §12.3）。核心挑战：
1. 读多写少，存在热点查询压力；
2. Redis/Neo4j/ES/OLAP 任一依赖宕机不应拖垮核心语义查询链路。

对应模块 `semantic`（状态 released，13/13 门禁）。

## 决策
1. **cache-aside 缓存**：`MetricCache` 以 Redis 存热点指标定义（TTL 300s）；写操作（创建/更新/发布/废弃/合规复核）触发 `invalidate`，满足 module-status 中 semantic 的 perf_contract「版本缓存失效延迟 < 1s」。
2. **韧性分层 + 熔断舱壁**：MySQL 为**唯一核心依赖**；Redis / Neo4j / ES / OLAP 均为**可选依赖**。`MetricCache` 与 `optional_dependency_status()` 均受 `CircuitBreaker` 保护（连续 5 次失败熔断，30s 后半开探活）：Redis 抖动/宕机 → 熔断打开 → 读取自动降级到 MySQL，核心链路不受影响。
3. **降级可见**：缓存禁用 / 熔断打开 / Redis 异常时读取返回 `None`，由调用方显式降级到 MySQL，避免静默不一致。

## 备选方案
- **无缓存直打 MySQL**：读压力大时性能瓶颈（P95 超 3s） → 否决。
- **Redis 强依赖**：Redis 故障拖垮语义查询 → 否决。
- **本地进程内缓存**：多实例不一致、失效难保证 <1s → 否决。

## 后果
- **正面**：Redis 故障不影响核心查询；失效延迟可控（TTL + 显式失效双保险）；可选依赖均受熔断隔离。
- **负面**：引入缓存一致性窗口（TTL + invalidate 双保险缓解）；多实例下 Redis 仍为共享层，依赖 Redis 高可用；`CircuitBreaker` 为进程内最小实现，多副本状态不共享（水平扩展下按实例独立熔断，可接受）。
