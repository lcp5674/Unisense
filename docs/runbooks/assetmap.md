# Runbook：资产地图服务（assetmap）

> 模块状态：`implemented`（门禁 11/13 绿；runbook 已建；§6.3 双视角审查进行中；待 perf 压测 + §1.5 人工 ratify）
> 关联文档：TD §12.11、docs/CHANGELOG_MODULES.md

## 1. 职责边界

资产地图服务（**只读聚合**）负责：

- 资产目录总览（`catalog_summary`）
- 指标 / 表 / 数据源清单与领域覆盖、敏感级分布
- 孤儿资产识别（`orphan_assets`：无血缘上游/下游的指标）

**依赖**：MySQL（只读聚合 `metric` / `source` / `lineage_edge` / `dimension` 等上游表）、audit 服务（仅读，无写审计）。
**一期明确不做**：资产影响分析、血缘可视化图计算、资产变更订阅。

## 2. 关键端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/assetmap/summary` | 目录总览（guard） |
| GET | `/api/v1/assetmap/metrics` | 指标清单（guard） |
| GET | `/api/v1/assetmap/tables` | 表清单（limit 分页，guard） |
| GET | `/api/v1/assetmap/sources` | 数据源清单（guard） |
| GET | `/api/v1/assetmap/domain-coverage` | 领域覆盖（guard） |
| GET | `/api/v1/assetmap/sensitivity` | 敏感级分布（guard） |
| GET | `/api/v1/assetmap/orphans` | 孤儿资产（guard） |

## 3. 状态机与留痕

只读服务，无状态机、无写审计。全部端点经 `guard_against_injection` + read 角色校验。

## 4. 业务语义

- 孤儿识别基于 `lineage_edge`：既无上游也无下游的指标判定为孤儿。
- `list_tables` 带 `limit` 分页；`orphan_assets` 返回全量列表（无分页，见 §8）。

## 5. 依赖与故障

| 依赖 | 故障表现 | 处置 |
|------|----------|------|
| MySQL（上游表只读） | 5xx | 确认上游（metric/source/lineage）迁移 `upgrade head`；连接池 |
| 上游数据缺失 | 清单/覆盖为空 | 确认上游服务正常写入；非本服务故障 |
| RBAC（read 角色） | 403 | 确认调用方具备读权限 |

## 6. 迁移与回滚

- 本服务**无自有迁移**（仅聚合上游表），无 `downgrade` 影响。
- 回滚：若上游 schema 变更导致聚合异常，回退上游迁移即可；本服务代码回滚用 K8s 版本回退。

## 7. 可观测性

- 只读服务，无写审计；依赖 API 层 RED 指标（`/metrics`）。
- 数据新鲜度取决于上游写入频率，仪表盘应结合上游健康判断。

## 8. 已知限制（一期）

> 以下 High / Medium 项经独立第三方 §6.3 双视角审查确认（详见 CHANGELOG_MODULES + module-status `review`），目前**退回 implemented 待修复**，修复后重跑 §6.3。

- **【High】RBAC 闸门缺失**：`_READ_ROLES` 已定义，但 5 个端点均未挂 `Depends(require_roles(*_READ_ROLES))`，任意 active 用户（含最低权限）可读全量资产地图（安全测试仅测正例，门禁假绿）。
- **【High】敏感字段无差别外泄**：端点直接返回 `DBCatalog` ORM 实体且无 `response_model`，FastAPI 全量序列化泄漏 `etl_sql`（源端 ETL SQL）与 `schema_json`（全字段/注释）；`sensitivity_level=PII/CONFIDENTIAL` 行无任何脱敏 / 门禁。
- **【Medium】软删未过滤**：全部查询未过滤 `deleted_at IS NULL`，软删资产仍计入总数 / 热力，统计数字错误（其余 9 个服务均正确过滤，本服务为唯一例外）。
- **【Medium】`orphan_assets` 无分页** + `total=len(items)` 内存放大，违背「热力聚合 < 3s」契约。
- **【Medium】`limit` 无 `ge/le` 约束**，可传极大值或负数。
- 数据新鲜度完全依赖上游表，上游延迟会静默反映为陈旧资产地图（非故障，需监控上游）；无写入能力，异常修复需回到上游服务。
- 性能基线未单独压测。
