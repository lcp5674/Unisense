# DBA 操作手册（Database Administrator）

## 1. 角色定位
对平台**存储层**负总责：MySQL 8 / Neo4j 5 / Elasticsearch 8.15 / Redis 7 的表结构、迁移可逆性、备份恢复、连接池与性能容量。对应 `alembic` 迁移链、`db/mysql`、`db/redis` 与 14 个领域服务的模型。

## 2. 核心职责
- **迁移管理**：所有 Alembic 迁移必须可逆（`downgrade` 顺序 DROP 索引 → DROP 表，规避 MySQL 外键 1553）。
- **备份与恢复**：MySQL 逻辑备份 + binlog；定期恢复演练。
- **性能与容量**：慢查询、连接数、复制延迟、JSON 列与索引策略。
- **韧性**：连接池用 `NullPool` 消除跨事件循环复用；依赖（Redis/Neo4j）不可达须 best-effort 降级。

## 3. 迁移操作
```bash
# 升级到最新（head=0014_benchmark_reconciliation）
poetry run alembic upgrade head
# 迁移可逆复验：up -> down -> up
poetry run alembic downgrade -1 && poetry run alembic upgrade head
```
> 本环境使用 `mysql+aiomysql`（同步 DSN 在建引擎前规整）。离线 SQL 生成须 `exit 0`。

## 4. 当前表与迁移快照（截至 2026-08-11）
| 迁移 | 内容 |
|------|------|
| 0001_initial | organization/user/term/data_source/metric/metric_version/db_catalog/audit_log |
| 0004_governance | role/grants/classification（user.role 扩容 reviewer/compliance_officer + 保留 analyst） |
| 0007_quality_trace | quality_event 留痕列（ack_by/resolved_by/closed_by…） |
| 0009_dimension | dimension / mapping / reconciliation（口径对账） |
| 0013_erasure | erasure_request（被遗忘权台账） |
| 0014_benchmark_reconciliation | external_benchmark / reconciliation_record（外部基准对账，D11） |

## 5. 关键设计与坑位（诚实提示）
- **JSON 列无法建唯一索引**：`grants`、基准幂等键 `dims` 等改用应用层幂等（`find_*`），DB 唯一约束仅覆盖可索引前缀。
- **WORM 约束**：审计日志、指标值快照不可物理删除，仅逻辑标记；被遗忘权用覆写去标识化而非删行。
- **user.role 枚举**：普通 enum（非 StrEnum），权限判定已归一化 `_role_to_str`；扩容枚举须同步迁移与 `RoleName`。
- **连接泄漏**：外部源连接（如 collector SqlalchemyConnector）须 `dispose()` + `finally` 释放。

## 6. 应急响应
1. 迁移失败：回滚 `alembic downgrade` 至上一可逆点；核对迁移链无断点。
2. 主从切换 / 复制延迟：切换读流量、暂停写密集任务、观察延迟恢复。
3. 连接池耗尽：检查 `NullPool` 配置与慢事务；必要时重启连接池。
4. 数据误删：从最近逻辑备份 + binlog 定点恢复（WORM 表优先保留）。

## 7. 性能基线
- 各服务已建 k6 基线（P95<600ms、P99<2000ms、错误率<5%），详见各模块 runbook 与 `backend/tests/perf/`。
